"""
Pulsar Feinmann 8K Dongle — protocol driver.

USB protocol reverse-engineered from a Wireshark/USBPcap capture of Pulsar
Fusion on Windows, then confirmed live against real hardware via pyusb.

Same command framing as the Sonix X2A protocol (see x2a.py) — commands are
sent on interface 3 as a 64-byte HID Feature report. Every SET_REPORT is
immediately followed by a GET_REPORT poll on the same report; this
appears necessary for the device to act on the command, and for
*read*-flavored queries (reg|0x80) a 2026-08-07 Windows/Fusion capture
showed that GET_REPORT completion carrying real reply data rather than a
dead echo (see _poll_ack()'s docstring for the byte layout). **This has
NOT been reproduced live from Linux** despite extensive testing (byte-
identical requests confirmed via a Linux-side usbmon trace, various
delays/retries, draining the interrupt endpoint in parallel, sending the
same SET_IDLE Windows sends first, a software USB reset, and a full
physical replug) — on Linux the same request consistently gets the dead
echo the original version of this file described. All of the get_*
methods below (including get_dpi_stages() as of 2026-08-07 - see its
docstring) are therefore based on decoding the capture and cross-
referencing the values against independently-known device state (exact
hex match to a named GNOME palette color, exact match to factory-default
button bindings, exact match to session values set moments earlier, exact
match to all six known DPI stage values in two independent capture
instances) rather than a live pyusb round-trip. High confidence, but flag
this if one ever behaves oddly. Only interface 3 is claimed - an earlier
version of this file also claimed interface 1 for the interrupt-endpoint
mechanism get_dpi_stages() used to rely on (see its docstring), but
nothing here needs that anymore.

Packet format (64 bytes, sent via SET_REPORT, wValue=0x0300 Feature/ID0):
  [0]     direction: 0x00=CMD (host->device)
  [1]     command category
  [2]     register (bit7=0: write, bit7=1: read)
  [3]     sub-register
  [4-5]   always 0x00
  [6]     profile (only ever observed as 0x01 in captures - device may
          only expose a single onboard profile)
  [7-61]  payload
  [62-63] checksum: little-endian uint16 of sum(bytes[0:62])

GET_REPORT reply format for read-flavored queries (same 64-byte report,
byte[0] flipped from the request's 0x00 to 0x01): byte[1:4] echo the
cat/reg/sub of the query, byte[6] echoes the profile, and byte[7:] holds
the actual reply data in the same layout the write side used for that
command (e.g. get_brightness's byte[8] lines up with set_brightness's
payload=[0x01, value] landing at byte[7],byte[8]).

Async reply format (interrupt IN, EP 0x82, up to 24 bytes) — this driver
no longer reads this endpoint itself (see get_dpi_stages()'s docstring
for why), but it's what parse_hidraw_event() decodes from
/dev/hidrawN for the GUI's live tray-icon DPI notifications (the kernel's
hidraw layer exposes the same endpoint without needing pyusb to claim
interface 1 - see the note above on why only interface 3 is claimed now):
  [0]     reply category (mirrors the command's category byte)
  [1]     reply subtype
  [2..]   payload, meaning depends on subtype

Confirmed subtypes:
  05 05 <stage> <dpiLoLE16> <dpiLoLE16>   -- DPI value for `stage` (1-6),
                                              X and Y repeated, uint16 LE
                                              (this is what's pushed when
                                              the physical DPI button is
                                              pressed, not a query reply)
  05 0d <val>                             -- periodic ~1Hz heartbeat,
                                              meaning unconfirmed (battery?
                                              signal? cursor rate? — value
                                              range shifted a lot between
                                              capture sessions)

Status: All writes, including set_button(), are implemented (see
set_button()'s docstring - it's a same-command-as-x2a.py, byte-layout
match rather than a live-verified changed-value capture). Every write
except set_button() itself has been live-verified against real hardware.
All reads (polling rate, debounce, LOD, angle snap, ripple control,
motion sync, brightness, LED effect, breath speed, stage colors, DPI
stages, active DPI stage, button bindings) are implemented and high-
confidence but NOT live-verified from Linux - see the dead-echo note
above. get_dpi_stages() is, as of 2026-08-07, genuinely side-effect-free
(no longer mutates the active DPI stage - see its docstring for the fix).
"""

import struct
from typing import Optional

import usb.core
import usb.util

from pulsar_mouse.base import PulsarDevice, DeviceCapabilities

# ── Encoding tables ──────────────────────────────────────────────────────────

# Bitmask, not sequential like X2A - extends up to 8000Hz.
POLL_HZ_TO_VAL = {125: 0x01, 250: 0x02, 500: 0x04, 1000: 0x08,
                  2000: 0x10, 4000: 0x20, 8000: 0x40}
POLL_VAL_TO_HZ = {v: k for k, v in POLL_HZ_TO_VAL.items()}

# Same cat=0x03/reg=0x04/sub=0x0f command and value mapping as x2a.py.
LED_NAME_TO_VAL = {'off': 0, 'steady': 1, 'breath': 2}
LED_VAL_TO_NAME = {v: k for k, v in LED_NAME_TO_VAL.items()}


class PulsarFeinmann8K(PulsarDevice):
    """Driver for the Pulsar Feinmann 8K wireless dongle."""

    capabilities = DeviceCapabilities(
        name='Pulsar Feinmann 8K Dongle',
        vid_pid_pairs=[(0x3710, 0x5404)],
        interface_num=3,          # commands (control transfers)
        report_size=64,
        num_profiles=1,           # only ever saw profile byte == 0x01
        max_dpi_stages=6,
        dpi_min=100,
        dpi_max=26000,
        dpi_step=100,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,   # left thumb front (default: forward)
            'thumb2': 0x05,   # left thumb back  (default: backward)
            'dpi':    0x0b,
        },
        polling_rates=sorted(POLL_HZ_TO_VAL),
        # Confirmed 2026-08-07: on-wire protocol actually supports 0.1mm
        # steps (captured 0.7/1.0/2.0mm all as raw byte = mm*10), but the
        # shared int-only set_lod()/--lod CLI interface only exposes whole
        # mm - listing the two values every other model in this codebase
        # supports until the API is extended to carry finer precision.
        lod_values=[1, 2],
        button_labels={
            'left': 'Left Click', 'right': 'Right Click', 'wheel': 'Wheel Click',
            'thumb1': 'Thumb 1 (forward)', 'thumb2': 'Thumb 2 (back)',
            'dpi': 'DPI Button',
        },
    )

    _WVALUE = 0x0300      # HID Feature report, report ID 0

    def __init__(self):
        self._dev = None

    # ── Connection lifecycle ────────────────────────────────────────────────

    def open(self) -> None:
        caps = self.capabilities
        vid, pid = caps.vid_pid_pairs[0]
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(
                f"{caps.name} not found (VID=0x{vid:04x}, PID=0x{pid:04x}). "
                "Is the dongle plugged in?")
        iface = caps.interface_num
        if dev.is_kernel_driver_active(iface):
            dev.detach_kernel_driver(iface)
        usb.util.claim_interface(dev, iface)
        self._dev = dev

    def close(self) -> None:
        if self._dev is None:
            return
        iface = self.capabilities.interface_num
        usb.util.release_interface(self._dev, iface)
        try:
            self._dev.attach_kernel_driver(iface)
        except Exception:
            pass
        # release_interface()/attach_kernel_driver() only affect the
        # interface claim - the underlying libusb device handle stays open
        # until pyusb's backend is explicitly disposed (or GC eventually
        # gets to it, which is not deterministic). Without this, the next
        # open() races the leaked handle and fails with "Resource busy".
        usb.util.dispose_resources(self._dev)
        self._dev = None

    # ── Low-level protocol helpers ──────────────────────────────────────────

    @staticmethod
    def _checksum(buf: bytearray) -> bytes:
        return struct.pack('<H', sum(buf[:62]) & 0xFFFF)

    def _build(self, cat, reg, sub, profile=0x01, payload=()):
        buf = bytearray(64)
        buf[1] = cat
        buf[2] = reg
        buf[3] = sub
        buf[6] = profile
        for i, b in enumerate(payload):
            buf[7 + i] = b
        buf[62:64] = self._checksum(buf)
        return bytes(buf)

    def _build_read(self, cat, reg, sub, profile=0x01, payload=()):
        return self._build(cat, reg | 0x80, sub, profile, payload)

    def _set_report(self, data: bytes) -> None:
        self._dev.ctrl_transfer(0x21, 0x09, self._WVALUE,
                                 self.capabilities.interface_num, data)

    def _poll_ack(self) -> bytes:
        """GET_REPORT poll that follows every SET_REPORT in the capture.

        For plain writes this is a dead echo (byte[0] flips 0x00->0x02) and
        sending it just appears necessary for the device to act on the
        command. But for *read*-flavored queries (reg|0x80, see
        _build_read/_query_ctrl) a 2026-08-07 Windows/Fusion capture proved
        this reply is NOT always a dead echo - it carries the real value for
        most fields (debounce, LOD, angle snap/ripple/motion sync,
        brightness, LED effect/breath speed, stage colors, active DPI
        stage, button bindings, polling rate), mirroring the write payload
        layout: byte[6]=profile echo, byte[7:]=actual data. The one
        confirmed exception is get_dpi_stages() (see its docstring), which
        still needs the async interrupt-endpoint reply.
        """
        return bytes(self._dev.ctrl_transfer(
            0xA1, 0x01, self._WVALUE,
            self.capabilities.interface_num, self.capabilities.report_size))

    def _query_ctrl(self, cat, reg, sub, profile=0x01, payload=()) -> bytes:
        """Read query whose reply comes back directly in the GET_REPORT
        completion - see _poll_ack()'s docstring. Much simpler than _query()
        (no interrupt-endpoint draining/retries needed) but only works for
        the fields confirmed there.
        """
        self._set_report(self._build_read(cat, reg, sub, profile, payload))
        return self._poll_ack()

    def _cmd(self, cat, reg, sub, profile=0x01, payload=()) -> None:
        """Fire a write command. No reply is expected/awaited."""
        self._set_report(self._build(cat, reg, sub, profile, payload))
        self._poll_ack()

    # ── Global settings ─────────────────────────────────────────────────────

    def set_polling_rate(self, hz: int) -> None:
        val = POLL_HZ_TO_VAL.get(hz)
        if val is None:
            raise ValueError(f"Polling rate must be one of {sorted(POLL_HZ_TO_VAL)}")
        self._cmd(0x01, 0x09, 0x02, 0x01, [val])

    def get_polling_rate(self) -> int:
        # cat=0x01/reg=0x89(=0x09|0x80)/sub=0x02 - reply byte[7] is the same
        # POLL_HZ_TO_VAL bitmask byte used for writes. Confirmed 2026-08-07
        # via Windows capture: read back 0x08 (1000 Hz), matching the last
        # rate set that session.
        resp = self._query_ctrl(0x01, 0x09, 0x02)
        val = resp[7]
        if val not in POLL_VAL_TO_HZ:
            raise IOError(f"Unknown polling rate byte 0x{val:02x}")
        return POLL_VAL_TO_HZ[val]

    # Captured 2026-08-07: toggling each on Fusion produced a single-byte
    # SET_REPORT, cat=0x07/sub=0x02/profile=0x01, payload=[1 or 0], differing
    # only by reg. Live-verified working (no async reply observed or needed,
    # same as other fire-and-forget global settings).
    def set_angle_snap(self, enabled: bool) -> None:
        self._cmd(0x07, 0x04, 0x02, 0x01, [1 if enabled else 0])

    def get_angle_snap(self) -> bool:
        return bool(self._query_ctrl(0x07, 0x04, 0x02)[7])

    def set_ripple_control(self, enabled: bool) -> None:
        self._cmd(0x07, 0x03, 0x02, 0x01, [1 if enabled else 0])

    def get_ripple_control(self) -> bool:
        return bool(self._query_ctrl(0x07, 0x03, 0x02)[7])

    def set_motion_sync(self, enabled: bool) -> None:
        self._cmd(0x07, 0x05, 0x02, 0x01, [1 if enabled else 0])

    def get_motion_sync(self) -> bool:
        return bool(self._query_ctrl(0x07, 0x05, 0x02)[7])

    def set_debounce(self, ms: int) -> None:
        # Captured 2026-08-07: dragging the debounce slider 0->15 then back
        # to 0 in Fusion produced a linear single-byte SET_REPORT per step,
        # cat=0x04/reg=0x03/sub=0x03/profile=0x01, payload=[ms].
        if not 0 <= ms <= 15:
            raise ValueError("Debounce must be 0-15 ms")
        self._cmd(0x04, 0x03, 0x03, 0x01, [ms])

    def get_debounce(self) -> int:
        return self._query_ctrl(0x04, 0x03, 0x03)[7]

    # ── Per-profile: DPI stages ─────────────────────────────────────────────

    def get_dpi_stages(self, profile: int) -> dict:
        # Genuinely side-effect-free, unlike the previous implementation
        # here (see git history) which reused the *set active stage*
        # command per-stage and mutated the mouse's active stage as a
        # result. Found 2026-08-07 in a capture of opening Fusion's DPI tab
        # without touching any stage: cat=0x05/reg=0x04(bare)/sub=0x15, no
        # stage in the payload (just profile) - a genuinely different
        # command from set_active_dpi_stage's cat=0x05/reg=0x01/sub=0x02.
        # Reply echoes back reg=0x84 as expected but oddly sub=0x21 rather
        # than the queried 0x15 (harmless, ignored - not worth chasing).
        # byte[7]=stage count, then `count` 5-byte blocks starting at
        # byte[9]: [stage_num, dpi_x_lo, dpi_x_hi, dpi_y_lo, dpi_y_hi].
        # Confirmed against all 6 known stage values (400/1200/2000/3200/
        # 6400/12800) byte-exact, in two independent instances in the same
        # capture.
        resp = self._query_ctrl(0x05, 0x04, 0x15, profile)
        count = resp[7]
        stages = []
        off = 9
        for _ in range(count):
            x = struct.unpack_from('<H', resp, off + 1)[0]
            y = struct.unpack_from('<H', resp, off + 3)[0]
            stages.append((x, y))
            off += 5
        active = self.get_active_dpi_stage(profile)
        return {'active': active, 'count': count, 'stages': stages}

    def set_active_dpi_stage(self, stage: int, profile: int) -> None:
        if not 1 <= stage <= self.capabilities.max_dpi_stages:
            raise ValueError(f"DPI stage must be 1-{self.capabilities.max_dpi_stages}")
        # Re-captured traffic (cycling stages 1->2->3->4->5->6->1 in Fusion)
        # showed 7 SET_REPORTs, all cat=0x05/reg=0x01/sub=0x02 with a
        # single-byte payload equal to the target stage - the same command
        # get_dpi_stages() calls per-stage above. The earlier cat=0x04/
        # reg=0x01/sub=0x06 payload=[stage,0x01,stage] guess (inferred from
        # only 5 ambiguous samples) was wrong; live-verified fixed.
        self._cmd(0x05, 0x01, 0x02, 0x01, [stage])

    def get_active_dpi_stage(self, profile: int) -> int:
        # cat=0x05/reg=0x81(=0x01|0x80)/sub=0x02, no stage in the payload -
        # confirmed 2026-08-07 via Windows capture to be a genuinely
        # side-effect-free read (unlike get_dpi_stages() above, which reuses
        # the *write* command cat=0x05/reg=0x01/sub=0x02 with a stage number
        # and mutates the active stage as a result). Reply byte[7] is the
        # active stage index.
        return self._query_ctrl(0x05, 0x01, 0x02, profile)[7]

    def set_dpi_stages(self, stages: list[int], active: int, profile: int) -> None:
        # Only single-stage reads/active-stage switching were decoded;
        # writing new DPI values for a stage was never captured.
        raise NotImplementedError

    def get_lod(self, profile: int) -> int:
        # cat=0x07/reg=0x82(=0x02|0x80)/sub=0x03 - reply byte[8] is raw =
        # mm*10, same encoding as the write (see set_lod below). Confirmed
        # 2026-08-07 via Windows capture: read back 20 (2.0mm). Rounded to
        # whole mm since the public API is int-only (see set_lod).
        resp = self._query_ctrl(0x07, 0x02, 0x03, profile)
        return round(resp[8] / 10)

    def set_lod(self, mm: int, profile: int) -> None:
        # Captured 2026-08-07: dragging the LOD slider 0.7mm -> 1.0mm ->
        # 2.0mm -> 0.7mm in Fusion produced cat=0x07/reg=0x02/sub=0x03,
        # payload=[0x02, raw] where raw = mm*10 (0.7mm->7, 1.0mm->10,
        # 2.0mm->20) - the device clearly supports 0.1mm steps, but the
        # set_lod()/--lod interface here is int-only like every other
        # driver in this codebase, so only whole-mm values are reachable
        # through this method for now.
        if mm not in self.capabilities.lod_values:
            raise ValueError(f"LOD must be one of {self.capabilities.lod_values}")
        self._cmd(0x07, 0x02, 0x03, 0x01, [0x02, mm * 10])

    # ── Per-profile: LED ─────────────────────────────────────────────────────

    def set_brightness(self, value: int, profile: int) -> None:
        lo, hi = self.capabilities.brightness_range
        if not lo <= value <= hi:
            raise ValueError(f"Brightness must be {lo}-{hi}")
        # Re-captured 2026-08-07: dragging the brightness slider in Fusion
        # produced cat=0x03/reg=0x03/sub=0x03, payload=[0x01, value] every
        # time - the old cat=0x07/reg=0x02/sub=0x03 payload=[0x02, value]
        # guess was wrong in every field. Live-verified fixed.
        self._cmd(0x03, 0x03, 0x03, 0x01, [0x01, value])

    def set_led_effect(self, effect: str, profile: int) -> None:
        # Captured 2026-08-07: cat=0x03/reg=0x04/sub=0x0f, payload=[0x01,
        # val] - identical command and value mapping (off=0/steady=1/
        # breath=2) to x2a.py's set_led_effect.
        val = LED_NAME_TO_VAL.get(effect)
        if val is None:
            raise ValueError(f"Effect must be one of {list(LED_NAME_TO_VAL)}")
        self._cmd(0x03, 0x04, 0x0F, 0x01, [0x01, val])

    def get_led_effect(self, profile: int) -> str:
        # Same command as the write (cat=0x03/reg=0x84/sub=0x0f), reply
        # byte[8] is the effect enum - confirmed 2026-08-07 via Windows
        # capture (see get_breath_speed for the rest of this reply).
        val = self._query_ctrl(0x03, 0x04, 0x0F, profile)[8]
        return LED_VAL_TO_NAME.get(val, f'unknown(0x{val:02x})')

    def set_breath_speed(self, speed: int, profile: int) -> None:
        # Same command as set_led_effect, with effect pinned to breath (2)
        # and a raw byte appended - same command shape as x2a.py's
        # set_breath_speed, but live-tested 2026-08-07 and confirmed
        # INVERTED on this model: raw=100 pulsed visibly slower than
        # raw=5. Unlike x2a (which sends `speed` directly), the raw byte
        # sent to the device is `hi - speed` so the public speed=0..100
        # API stays intuitive (0=slowest, 100=fastest) across drivers.
        lo, hi = self.capabilities.breath_speed_range
        if not lo <= speed <= hi:
            raise ValueError(f"Breath speed must be {lo}-{hi}")
        self._cmd(0x03, 0x04, 0x0F, 0x01, [0x01, 0x02, 0x00, 0x00, hi - speed])

    def get_breath_speed(self, profile: int) -> int:
        # Same reply as get_led_effect (cat=0x03/reg=0x84/sub=0x0f) -
        # byte[11] is the raw inverted speed byte, same `hi - speed`
        # encoding as the write. Confirmed 2026-08-07 via Windows capture:
        # read back 100, matching `--breath-speed 0` set earlier that
        # session (hi=100, so raw=hi-0=100).
        hi = self.capabilities.breath_speed_range[1]
        raw = self._query_ctrl(0x03, 0x04, 0x0F, profile)[11]
        return hi - raw

    def get_brightness(self, profile: int) -> int:
        # cat=0x03/reg=0x83(=0x03|0x80)/sub=0x03 - reply byte[8] is the
        # brightness value, mirroring the write's payload=[0x01, value]
        # (byte[7] is that same 0x01 marker). Confirmed 2026-08-07 via
        # Windows capture: read back 0, matching brightness=0 set via the
        # GUI earlier that session.
        return self._query_ctrl(0x03, 0x03, 0x03, profile)[8]

    def set_stage_color(self, stage: int, r: int, g: int, b: int,
                        profile: int) -> None:
        # Captured 2026-08-07: Fusion's LED tab auto-populated a rainbow
        # preset across all 6 stages, giving a clean sample of this command
        # at cat=0x05/reg=0x05/sub=0x05, payload=[stage, r, g, b] - byte-
        # identical to x2a.py's set_stage_color (direct RGB, no inversion
        # needed here unlike breath speed). Note: observing this traffic
        # required cycling through all 6 stages in the UI, which - per
        # get_dpi_stages()'s known side effect - also changed the mouse's
        # active DPI stage each time; not an issue for this write itself.
        for val, name in [(r, 'R'), (g, 'G'), (b, 'B')]:
            if not 0 <= val <= 255:
                raise ValueError(f"{name} must be 0-255")
        if not 1 <= stage <= self.capabilities.max_dpi_stages:
            raise ValueError(f"Stage must be 1-{self.capabilities.max_dpi_stages}")
        self._cmd(0x05, 0x05, 0x05, 0x01, [stage, r, g, b])

    def get_stage_color(self, stage: int, profile: int) -> tuple[int, int, int]:
        # Same command as the write (cat=0x05/reg=0x85/sub=0x05,
        # payload=[stage]) - reply byte[7] echoes the stage, bytes[8:11]
        # are RGB. Confirmed 2026-08-07 via Windows capture: stage 1 read
        # back as (237, 51, 59), an exact match for GNOME/Adwaita's named
        # "Scarlet Red 3" palette swatch (#ED333B) set via the GUI color
        # picker earlier that session - not a coincidence.
        resp = self._query_ctrl(0x05, 0x05, 0x05, profile, [stage])
        return (resp[8], resp[9], resp[10])

    # ── Buttons ──────────────────────────────────────────────────────────────

    def set_button(self, btn_id: int, btn_type: int, a1: int, a2: int,
                    profile: int) -> None:
        # Found 2026-08-07 in the original broad pulsar.pcapng capture (not
        # the later targeted ones): cat=0x04/reg=0x01(bare)/sub=0x06,
        # payload=[btn_id, btn_type, a1, a2] - identical command to
        # x2a.py's set_button. The captured instances were Fusion
        # re-applying the untouched factory-default binding for each
        # button (not an actual remap to something else), but the byte
        # layout matches get_button()'s reply layout exactly (byte[7]=id,
        # [8]=type, [9]=a1, [10]=a2), which is independently confirmed -
        # high confidence despite no *changed*-value sample.
        self._cmd(0x04, 0x01, 0x06, profile, [btn_id, btn_type, a1, a2])

    def get_button(self, btn_id: int, profile: int) -> tuple[int, int, int]:
        # cat=0x04/reg=0x81(=0x01|0x80)/sub=0x06, payload=[btn_id] - reply
        # byte[7] echoes the button ID, byte[8]=type, byte[9]=a1,
        # byte[10]=a2 (see hid.py's describe_button/BTN_TYPE_* for the
        # encoding). Confirmed 2026-08-07 via Windows capture across all
        # known button IDs on a factory-default mouse: left/right/wheel/
        # thumb1/thumb2 decoded as BTN_TYPE_MOUSE with a1=their own ID
        # (i.e. mouse(left)=1,1 mouse(right)=2,2 etc, matching
        # hid.MOUSE_ACTIONS), and the dpi button (0x0b) decoded as
        # BTN_TYPE_DPI/dpiloop (type=9, a1=3) - exactly the expected
        # untouched defaults, which is what confirmed the byte offsets.
        resp = self._query_ctrl(0x04, 0x01, 0x06, profile, [btn_id])
        return (resp[8], resp[9], resp[10])

    # ── Hidraw support ───────────────────────────────────────────────────────

    def parse_hidraw_event(self, data: bytes) -> Optional[dict]:
        if len(data) >= 5 and data[0] == 0x05 and data[1] == 0x05:
            dpi = struct.unpack_from('<H', data, 3)[0]
            stage = data[2]
            return {'dpi': dpi, 'stage': stage}
        return None
