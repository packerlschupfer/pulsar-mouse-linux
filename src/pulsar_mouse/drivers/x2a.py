"""
Pulsar X2A Medium Wired — protocol driver.

USB protocol reverse-engineered from Wireshark captures of Pulsar Fusion on Windows.
Interface 3, Feature report (wValue=0x0300), 64-byte packets.

Packet format:
  [0]     direction: 0x00=CMD (host→device), 0x01=RSP (device→host)
  [1]     command category
  [2]     register (bit7=0: write, bit7=1: read)
  [3]     sub-register
  [4-5]   always 0x00
  [6]     profile: 0x00=global, else 1 to capabilities.num_profiles
          (5 on the X2A, 6 on the X3)
  [7-61]  payload
  [62-63] checksum: little-endian uint16 of sum(bytes[0:62])

Global settings (profile=0): polling rate, debounce, angle snap, ripple, motion sync
Per-profile settings (profile=1-N): DPI stages, LOD, brightness, LED effect, button bindings
"""

import struct
import os
import time
from typing import Optional

import usb.core
import usb.util

from pulsar_mouse.base import PulsarDevice, DeviceCapabilities
from pulsar_mouse.hid import BTN_TYPE_MOUSE, BTN_TYPE_DPI

# ── Encoding tables ──────────────────────────────────────────────────────────

POLL_HZ_TO_VAL = {125: 1, 250: 2, 500: 4, 1000: 8}
POLL_VAL_TO_HZ = {v: k for k, v in POLL_HZ_TO_VAL.items()}

LOD_MM_TO_VAL  = {1: 0, 2: 1}
LOD_VAL_TO_MM  = {v: k for k, v in LOD_MM_TO_VAL.items()}

LED_NAME_TO_VAL = {'off': 0, 'steady': 1, 'breathe': 2}
LED_VAL_TO_NAME = {v: k for k, v in LED_NAME_TO_VAL.items()}

# Factory default button bindings (from Pulsar Fusion reset capture)
BUTTON_DEFAULTS = {
    0x01: (BTN_TYPE_MOUSE, 0x01, 0x00),  # left   → left click
    0x02: (BTN_TYPE_MOUSE, 0x02, 0x00),  # right  → right click
    0x03: (BTN_TYPE_MOUSE, 0x03, 0x00),  # wheel  → wheel click
    0x04: (BTN_TYPE_MOUSE, 0x04, 0x00),  # thumb1 → backward
    0x05: (BTN_TYPE_MOUSE, 0x05, 0x00),  # thumb2 → forward
    0x06: (BTN_TYPE_MOUSE, 0x04, 0x00),  # thumb3 → backward
    0x07: (BTN_TYPE_MOUSE, 0x05, 0x00),  # thumb4 → forward
    0x0b: (BTN_TYPE_DPI,   0x03, 0x00),  # dpi    → dpiloop
}


# Cabled PIDs of mice that keep 07/02/03 as a mode and a distance rather
# than as the X2A's single index - see Fusion2LodTenths.  The 1K dongle asks
# which mouse it is carrying and looks it up here.
LOD_TENTHS_PIDS = {
    0x3409,     # Pulsar X3, x3.py's cabled PID
    0x3410,     # Pulsar X3 LHD, x3.py's other cabled PID
    0x3401,     # Pulsar Xlite v4, xlite_v4.py's
}

# Byte 7 of 07/02/03 when byte 8 is a distance in tenths of a millimetre.
# Module-level rather than a class attribute: the dongle calls the methods
# below by name, not through its own MRO (#24).
_LOD_MODE_TENTHS = 0x02


class Fusion2LodTenths:
    """07/02/03 as a mode and a distance in tenths of a millimetre.

    The X3 does not use this register the way the X2A does, which is where
    x2a.py's two-value table came from.  Byte 7 says how byte 8 is to be
    read, and in mode 2 byte 8 is the distance in tenths of a millimetre.

    @f-o-o-g-s captured Fusion2 1.0.5.8 driving an X3 through the 1K dongle,
    and its three buttons write exactly that, each reading back unchanged
    (#24):

        0.7 mm -> 02 07        1 mm -> 02 0a        2 mm -> 02 14

    A lift test with coins under the mouse's feet agrees: of those three only
    02 14 still tracked at 1.75 mm, 02 0a stopped around 1.5 mm and 02 07 at
    or below that.  The X3 ships on 02 0a, its 1 mm.

    x2a.py reads byte 7, so it reported that as "2 mm" while the mouse was on
    1.0, and its set_lod() writes the value twice, so this app's "2 mm" wrote
    01 01 - mode 1, which the same lift test puts at about 1.0 mm too.  Apply
    writes the LOD row whether or not it changed, so changing anything else
    on the page replaced the mouse's own 02 0a with that.

    Hence a pair that is not mode 2 reads as 1 mm: it is one this app wrote
    before it knew the encoding, when its two choices were labelled 1 mm and
    2 mm and both landed at or near the mouse's 1.0, and the next write makes
    it exact.  Nothing else writes this register - Fusion2 never wrote a mode
    other than 2 in any capture.

    Of those two, 01 01 (the old "2 mm") was measured behaving like 02 0a.
    00 00 (the old "1 mm") was put through the coins too: it is a working
    setting rather than off or ignored, tracking in all eight windows at 1.35
    and 1.52 mm, but that round's rig sat low enough that 02 07 and 02 0a
    tracked at 1.52 mm as well, so it cannot be separated from them.  All it
    supports is that 00 00 tracks like the others up to 1.5 mm and nothing
    says it reaches further - which is consistent with reading it as 1 mm,
    and is as far as coins go.

    Not the X3 alone: @variasjon read all six profiles of an Xlite v4
    through the same dongle and the five he had never touched hold 02 0a
    (#34), the second of the five mice the Dongle Kit covers to be read and
    the second to keep this shape.  A third would make it worth flipping the
    default - the mice in that kit are one generation on one sensor - and
    until then LOD_TENTHS_PIDS stays a list of what has actually been read.

    Which settings are offered is the driver's, not this mixin's: it writes
    whatever capabilities.lod_values holds.
    """

    def get_lod(self, profile: int) -> float:
        rsp = self._read(0x07, 0x02, 0x03, profile)
        if rsp[7] != _LOD_MODE_TENTHS:
            return 1.0
        return round(rsp[8] / 10, 1)

    def set_lod(self, mm: float, profile: int) -> None:
        values = self.capabilities.lod_values
        if mm not in values:
            raise ValueError("LOD must be one of "
                             + ", ".join(f'{v}' for v in values) + " (mm)")
        self._cmd(0x07, 0x02, 0x03, profile,
                  [_LOD_MODE_TENTHS, round(mm * 10)])

    def _lod_export_is_mode_byte(self) -> bool:
        return True


class PulsarX2A(PulsarDevice):
    """Driver for the Pulsar X2A Medium Wired mouse."""

    capabilities = DeviceCapabilities(
        name='Pulsar X2A Medium Wired',
        vid_pid_pairs=[(0x3710, 0x1404)],
        interface_num=3,
        report_size=64,
        num_profiles=5,
        max_dpi_stages=6,
        dpi_min=100,
        dpi_max=26000,
        dpi_step=100,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,   # left side, rear   (default: backward)
            'thumb2': 0x05,   # left side, front  (default: forward)
            'thumb3': 0x06,   # right side, rear  (default: backward)
            'thumb4': 0x07,   # right side, front (default: forward)
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        # Both flanks the same way round, rear back and front forward:
        # measured on the X2A by binding 0x04 and 0x05 to scroll up and down,
        # so all four side buttons sent something different, and pressing
        # them one at a time (#24).
        button_labels={
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Left Side Back (backward)',
            'thumb2': 'Left Side Front (forward)',
            'thumb3': 'Right Side Back (backward)',
            'thumb4': 'Right Side Front (forward)',
            'dpi': 'DPI Button',
        },
    )

    _WVALUE = 0x0300  # HID Feature report, report ID 0

    # hid.py's back and forward codes go to the mouse as they are; see
    # PulsarDevice._V1_EXPORTS_SWAP_BACK_FORWARD.
    _V1_EXPORTS_SWAP_BACK_FORWARD = True

    def __init__(self):
        self._dev = None
        self._read_resends_left = self._READ_RESENDS

    # ── Connection lifecycle ──────────────────────────────────────────────

    def open(self) -> None:
        caps = self.capabilities
        if self._dev is not None:
            return      # already open; claiming the interface twice is EBUSY
        for vid, pid in caps.vid_pid_pairs:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is not None:
                break
        if dev is None:
            vid, pid = caps.vid_pid_pairs[0]
            raise RuntimeError(
                f"{caps.name} not found (VID=0x{vid:04x}, PID=0x{pid:04x}). "
                "Is the mouse plugged in?")
        if dev.is_kernel_driver_active(caps.interface_num):
            dev.detach_kernel_driver(caps.interface_num)
        usb.util.claim_interface(dev, caps.interface_num)
        self._dev = dev
        self._read_resends_left = self._READ_RESENDS   # see _READ_RESENDS

    def close(self) -> None:
        if self._dev is None:
            return
        iface = self.capabilities.interface_num
        usb.util.release_interface(self._dev, iface)
        try:
            self._dev.attach_kernel_driver(iface)
        except Exception:
            pass
        self._dev = None

    # ── Low-level protocol helpers ────────────────────────────────────────

    def _checksum(self, data: bytes) -> bytes:
        return struct.pack('<H', sum(data[:62]) & 0xFFFF)

    def _build(self, cat, reg, sub, profile, payload=()):
        buf = bytearray(64)
        buf[0] = 0x00       # direction: CMD
        buf[1] = cat
        buf[2] = reg
        buf[3] = sub
        buf[6] = profile
        for i, b in enumerate(payload):
            buf[7 + i] = b
        cs = self._checksum(buf)
        buf[62] = cs[0]
        buf[63] = cs[1]
        return bytes(buf)

    def _build_read(self, cat, reg, sub, profile, payload=()):
        return self._build(cat, reg | 0x80, sub, profile, payload)

    def _set_report(self, data):
        iface = self.capabilities.interface_num
        self._dev.ctrl_transfer(0x21, 0x09, self._WVALUE, iface, data)

    def _get_report(self) -> bytes:
        iface = self.capabilities.interface_num
        return bytes(self._dev.ctrl_transfer(
            0xA1, 0x01, self._WVALUE, iface, self.capabilities.report_size))

    _ACK_POLLS = 8
    _ACK_DELAY = 0.02

    # A read still on 0x00 after its 8 polls was never taken, and polling
    # longer doesn't fix that: the mouse has nothing in hand to answer.  A
    # cabled X3 does this to commands sent in the first moments after a swap
    # from the 1K dongle to the cable, with the mouse awake (#21).  The
    # polling-rate read, sent 1 ms after the cable showed up:
    #
    #   * not resent: 0x00 for these 8 polls and for 50 more over a full
    #     second, never answered
    #   * resent straight after the polls: 0x00 again on the first resend,
    #     then 0x01 and the right rate (1000 Hz) on the second, about 0.16
    #     and 0.32 s after the first send
    #   * with this code and the constants below: dropped at +1, +260 and
    #     +518 ms, answered (1000 Hz) at +775 ms
    #
    # Not every swap does it (another one answered at +1 ms), and how long it
    # lasts varies: the app's reads have been dropped at 0.23, 0.39, 0.47 and
    # 0.63 s after the cable arrived, and answered at 0.40, 0.78 and 1.17 s.
    # So rather than guess the window, resend: with the polls, the delay
    # below puts sends about 0.26 s apart, the last one about 1.55 s after
    # the first, more than twice the latest drop seen.
    #
    # Only at the start of an open, though.  Once a read has been answered,
    # every later read in that open has answered on its first poll in every
    # measurement so far: after each swap above (up to 34 reads), 1000 in a
    # row on a cabled X3, 67 on an X2A.  So the first answer after open()
    # ends the resends, and until then the _READ_RESENDS are shared by all
    # the reads instead of given to each.  A read that is answered sends
    # once as before.  A mouse that answers nothing costs about 1.6 s on the
    # first read (1.58 s measured on an X2A) and 0.14 s on each one after
    # it, as before the resends, so a 25-read GUI pass, which holds
    # _USB_LOCK throughout, takes about 5 s, where resending every read
    # would take about 40 s.
    #
    # Only 0x00 is resent.  0x02 means the mouse has the command and is still
    # on it, and a second copy then is how the dongle loses replies (#15).
    # Only reads, too: _cmd() accepts 0x02, and resending a write the mouse
    # did take is a different risk, not measured here.  The 1K dongle has its
    # own _read() (xlite_v4_wireless.py) and doesn't come through here.
    _READ_RESENDS = 6
    _READ_RESEND_DELAY = 0.1

    def _poll_ack(self, accept: tuple) -> bytes:
        rsp = self._get_report()
        for _ in range(self._ACK_POLLS - 1):
            if rsp[0] in accept:
                return rsp
            time.sleep(self._ACK_DELAY)
            rsp = self._get_report()
        return rsp

    # 0x02 means the request is pending and 0x01 means the device has
    # answered, which xlite_v4_wireless.py waits for: over the air a command
    # sent while the last is still pending gets dropped, and its late reply
    # is handed to the next read (#15).  Accepting 0x02 here is deliberate,
    # but the margin is thinner than it looks.  Measured on a cabled X2A:
    #
    #   * a write is pending at 18 ms and answered at 28 ms, so this returns
    #     roughly 10 ms before the mouse has finished with it
    #   * every write made through this method landed, including ones
    #     followed straight away by a read — the single poll above is doing
    #     real work, not just costing time
    #   * a write sent with no poll at all and then read back immediately is
    #     lost outright, three times out of three.  Same mouse, same cable:
    #     the only difference is the acknowledgement
    #
    # So the dongle's problem has a cabled cousin; what keeps it away is a
    # poll that happens to take long enough.  Left alone because nothing
    # reaches the device except through here and nothing here misbehaved,
    # not because the cable is immune.  Anything that writes without going
    # through _cmd() should wait for 0x01.
    def _cmd(self, cat, reg, sub, profile, payload=()):
        self._set_report(self._build(cat, reg, sub, profile, payload))
        rsp = self._poll_ack((0x01, 0x02))
        if rsp[0] not in (0x01, 0x02):
            raise IOError(f"Unexpected response byte: 0x{rsp[0]:02x} "
                          f"(after {self._ACK_POLLS} polls)")
        return rsp

    def _read(self, cat, reg, sub, profile, payload=()):
        cmd = self._build_read(cat, reg, sub, profile, payload)
        self._set_report(cmd)
        rsp = self._poll_ack((0x01,))
        sends = 1
        while rsp[0] == 0x00 and self._read_resends_left > 0:
            self._read_resends_left -= 1
            time.sleep(self._READ_RESEND_DELAY)
            self._set_report(cmd)
            rsp = self._poll_ack((0x01,))
            sends += 1
        if rsp[0] != 0x01:
            sent = f" each, sent {sends} times" if sends > 1 else ""
            raise IOError(f"Bad response direction byte: 0x{rsp[0]:02x} "
                          f"(after {self._ACK_POLLS} polls{sent})")
        self._read_resends_left = 0
        return rsp

    # ── Device information ────────────────────────────────────────────────

    def get_firmware_version(self) -> str:
        # cat=0x01/reg=0x87(=0x07|0x80)/sub=0x04, profile 0, no payload.  A
        # reply's byte 3 is its data length (02/82/10, asked with 10, answers
        # 07 and seven bytes), and here it is 04: bytes [6:10] are one
        # four-byte little-endian version.  The X3's two set bytes are its
        # USB bcdDevice, low byte first; the X2A's one set byte, 08 at byte 8,
        # is the high byte of its bcdDevice, one place further up:
        #
        #   X3     16 10 00 00  ->  00.00.10.16    bcdDevice 10.16
        #   X2A    00 00 08 00  ->  00.08.00.00    bcdDevice  8.00
        #
        # So no fixed pair of bytes fits both, and all four are shown,
        # highest first.  Through the 1K dongle the X3 answers the same
        # 10.16, its own version and not the dongle's (bcdDevice 40.16), so
        # on the dongle this is the mouse's firmware.  A sleeping mouse
        # behind the dongle doesn't answer it at all (status 05 on every
        # poll), so this raises rather than make a version up.
        rsp = self._read(0x01, 0x07, 0x04, 0x00)
        return '.'.join(f'{b:02X}' for b in reversed(rsp[6:10]))

    # ── Global settings ───────────────────────────────────────────────────

    def get_polling_rate(self) -> int:
        rsp = self._read(0x01, 0x09, 0x02, 0x00)
        return POLL_VAL_TO_HZ.get(rsp[7], rsp[7] * 125)

    def set_polling_rate(self, hz: int) -> None:
        val = POLL_HZ_TO_VAL.get(hz)
        if val is None:
            raise ValueError(f"Polling rate must be one of {sorted(POLL_HZ_TO_VAL)}")
        self._cmd(0x01, 0x09, 0x02, 0x00, [val])

    def get_debounce(self) -> int:
        rsp = self._read(0x04, 0x03, 0x03, 0x00)
        return rsp[7]

    def set_debounce(self, ms: int) -> None:
        lo, hi = self.capabilities.debounce_range
        if not lo <= ms <= hi:
            raise ValueError(f"Debounce must be {lo}–{hi} ms")
        self._cmd(0x04, 0x03, 0x03, 0x00, [ms])

    def get_angle_snap(self) -> bool:
        rsp = self._read(0x07, 0x04, 0x02, 0x00)
        return bool(rsp[7])

    def set_angle_snap(self, enabled: bool) -> None:
        self._cmd(0x07, 0x04, 0x02, 0x00, [1 if enabled else 0])

    def get_ripple_control(self) -> bool:
        rsp = self._read(0x07, 0x03, 0x02, 0x00)
        return bool(rsp[7])

    def set_ripple_control(self, enabled: bool) -> None:
        self._cmd(0x07, 0x03, 0x02, 0x00, [1 if enabled else 0])

    def get_motion_sync(self) -> bool:
        rsp = self._read(0x07, 0x05, 0x02, 0x00)
        return bool(rsp[7])

    def set_motion_sync(self, enabled: bool) -> None:
        self._cmd(0x07, 0x05, 0x02, 0x00, [1 if enabled else 0])

    # ── Active profile ────────────────────────────────────────────────────

    # cat=0x02/reg=0x06/sub=0x01: the profile the mouse is using.  Read with
    # profile 0, the answer is in the reply's profile byte [6].  The write
    # carries the new profile in that same byte and no payload; put it in
    # the payload at byte 7 instead and the X2A refuses it with 0x03 (#24).
    # It's a live switch, not just a stored number: the active DPI stage
    # read through profile 0 (05/81/02) follows it straight away on the X3
    # and the X2A, and on the X3's cable a write through profile 0 sent
    # right after the switch lands on the new profile.  The X3 also
    # announces a switch on interface 1 as 05 01 <profile> (seen through
    # the 1K dongle), but the X2A doesn't, so nothing here waits for that.
    def get_active_profile(self) -> int:
        return self._read(0x02, 0x06, 0x01, 0x00)[6]

    def set_active_profile(self, profile: int) -> None:
        count = self.capabilities.num_profiles
        if not 1 <= profile <= count:
            raise ValueError(f"Profile must be 1-{count}")
        self._cmd(0x02, 0x06, 0x01, profile)

    # ── Per-profile: LOD ──────────────────────────────────────────────────

    def get_lod(self, profile: int) -> int:
        rsp = self._read(0x07, 0x02, 0x03, profile)
        return LOD_VAL_TO_MM.get(rsp[7], rsp[7])

    def set_lod(self, mm: int, profile: int) -> None:
        val = LOD_MM_TO_VAL.get(mm)
        if val is None:
            raise ValueError("LOD must be 1 or 2 (mm)")
        self._cmd(0x07, 0x02, 0x03, profile, [val, val])

    # ── Per-profile: LED ──────────────────────────────────────────────────

    def get_brightness(self, profile: int) -> int:
        rsp = self._read(0x03, 0x03, 0x03, profile, [0x01])
        return rsp[8]

    def set_brightness(self, value: int, profile: int) -> None:
        lo, hi = self.capabilities.brightness_range
        if not lo <= value <= hi:
            raise ValueError(f"Brightness must be {lo}–{hi}")
        self._cmd(0x03, 0x03, 0x03, profile, [0x01, value])

    def get_led_effect(self, profile: int) -> str:
        rsp = self._read(0x03, 0x04, 0x0F, profile, [0x01])
        return LED_VAL_TO_NAME.get(rsp[8], f"unknown(0x{rsp[8]:02x})")

    def _write_led_block(self, profile: int, effect_val: int, speed: int) -> None:
        self._cmd(0x03, 0x04, 0x0F, profile, [0x01, effect_val, 0x00, 0x00, speed])

    def set_led_effect(self, effect: str, profile: int) -> None:
        val = LED_NAME_TO_VAL.get(effect)
        if val is None:
            raise ValueError(f"Effect must be one of {list(LED_NAME_TO_VAL)}")
        self._write_led_block(profile, val, self.get_breathe_speed(profile))

    def get_breathe_speed(self, profile: int) -> int:
        rsp = self._read(0x03, 0x04, 0x0F, profile, [0x01])
        return rsp[11]

    def set_breathe_speed(self, speed: int, profile: int) -> None:
        lo, hi = self.capabilities.breathe_speed_range
        if not lo <= speed <= hi:
            raise ValueError(f"Breathe speed must be {lo}–{hi}")
        rsp = self._read(0x03, 0x04, 0x0F, profile, [0x01])
        self._write_led_block(profile, rsp[8], speed)

    # ── Per-profile: DPI stages ───────────────────────────────────────────

    def get_dpi_stages(self, profile: int) -> dict:
        rsp = self._read(0x05, 0x04, 0x15, profile)
        active     = rsp[7]
        num_stages = rsp[8]
        stages = []
        for i in range(num_stages):
            base = 9 + i * 5
            dpi_x = struct.unpack_from('<H', rsp, base + 1)[0]
            dpi_y = struct.unpack_from('<H', rsp, base + 3)[0]
            stages.append((dpi_x, dpi_y))
        return {'active': active, 'count': num_stages, 'stages': stages}

    def set_dpi_stages(self, stages: list[int], active: int, profile: int) -> None:
        caps = self.capabilities
        if not 1 <= len(stages) <= caps.max_dpi_stages:
            raise ValueError(f"Must have 1–{caps.max_dpi_stages} DPI stages")
        if not 1 <= active <= len(stages):
            raise ValueError(f"Active stage must be 1–{len(stages)}")
        for dpi in stages:
            if not caps.dpi_min <= dpi <= caps.dpi_max:
                raise ValueError(
                    f"DPI value {dpi} out of range {caps.dpi_min}–{caps.dpi_max}")
        payload = [active, len(stages)]
        for i, dpi in enumerate(stages):
            lo = dpi & 0xFF
            hi = (dpi >> 8) & 0xFF
            payload += [i + 1, lo, hi, lo, hi]
        self._cmd(0x05, 0x04, 0x21, profile, payload)

    def get_active_dpi_stage(self, profile: int) -> int:
        rsp = self._read(0x05, 0x01, 0x02, profile)
        return rsp[7]

    def set_active_dpi_stage(self, stage: int, profile: int) -> None:
        if not 1 <= stage <= self.capabilities.max_dpi_stages:
            raise ValueError(f"DPI stage must be 1–{self.capabilities.max_dpi_stages}")
        self._cmd(0x05, 0x01, 0x02, profile, [stage])

    # ── Per-profile: DPI stage colors ─────────────────────────────────────

    def get_stage_color(self, stage: int, profile: int) -> tuple[int, int, int]:
        rsp = self._read(0x05, 0x05, 0x05, profile, [stage])
        return (rsp[8], rsp[9], rsp[10])

    def set_stage_color(self, stage: int, r: int, g: int, b: int,
                        profile: int) -> None:
        for val, name in [(r, 'R'), (g, 'G'), (b, 'B')]:
            if not 0 <= val <= 255:
                raise ValueError(f"{name} must be 0–255")
        if not 1 <= stage <= self.capabilities.max_dpi_stages:
            raise ValueError(f"Stage must be 1–{self.capabilities.max_dpi_stages}")
        self._cmd(0x05, 0x05, 0x05, profile, [stage, r, g, b])

    # ── Per-profile: Button bindings ──────────────────────────────────────

    def get_button(self, btn_id: int, profile: int) -> tuple[int, int, int]:
        rsp = self._read(0x04, 0x01, 0x06, profile, [btn_id, 0xff])
        return (rsp[8], rsp[9], rsp[10])

    def set_button(self, btn_id: int, btn_type: int, a1: int, a2: int,
                   profile: int) -> None:
        self._cmd(0x04, 0x01, 0x06, profile, [btn_id, btn_type, a1, a2])

    # ── Factory reset ─────────────────────────────────────────────────────

    # Measured on a cabled X2A (2026-10-13): this resets the profile it is
    # given *and* every global — polling rate to 1000 Hz, debounce to 3 ms,
    # angle snap, ripple control and motion sync all off — while the other
    # four profiles keep everything they had.  So despite taking a profile
    # argument it is not a per-profile operation, and 3 ms is this model's
    # factory debounce.  Checked by marking three profiles with distinct
    # DPI stages, moving all five globals off their defaults, resetting
    # profile 5 alone, and reading all of it back.
    def reset_to_defaults(self, profile: int) -> None:
        self._cmd(0x02, 0x05, 0x01, profile)
        # 02/06/01 is the profile switch (set_active_profile()), so a reset
        # also leaves the mouse on `profile`.
        pkt = self._build(0x02, 0x06, 0x01, profile)
        self._set_report(pkt)
        try:
            self._get_report()
        except Exception:
            pass  # device may have already reset

    # ── Hidraw support ────────────────────────────────────────────────────

    # DPI-button reports arrive on interface 1.  The match itself (VID
    # *and* PID, so a dongle sharing the VID can't be picked instead) lives
    # in PulsarDevice.find_hidraw(), which @f-o-o-g-s fixed here in #17
    # before it was hoisted - the Xlite v4, X3 and 1K dongle drivers all
    # inherit this and had the same bug.
    _HIDRAW_INTERFACE = '/input1'

    # What the mouse calls its first DPI stage in that report.  The X2A
    # counts from zero: six presses of its own DPI button walked
    # 400/800/1600/3200/6400/12800 as bytes 0x00 to 0x05 (2026-10, on the
    # maintainer's X2A).  Not every Pulsar does - the X3 counts from one,
    # which is why this is a number and not a hardcoded +1.
    _DPI_EVENT_FIRST_STAGE = 0

    def parse_hidraw_event(self, data: bytes) -> Optional[dict]:
        if len(data) >= 7 and data[0] == 0x05 and data[1] == 0x05:
            dpi = struct.unpack_from('<H', data, 3)[0]
            stage = data[2] - self._DPI_EVENT_FIRST_STAGE + 1
            return {'dpi': dpi, 'stage': stage}
        return None
