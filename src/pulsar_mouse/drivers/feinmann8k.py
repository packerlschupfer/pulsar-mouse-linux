"""
Pulsar Feinmann 8K Dongle — protocol driver.

USB protocol reverse-engineered from a Wireshark/USBPcap capture of Pulsar
Fusion on Windows, then confirmed live against real hardware via pyusb.

Same command framing as the Sonix X2A protocol (see x2a.py) — commands are
sent on interface 3 as a 64-byte HID Feature report. Where this model
differs: the Feature-report GET_REPORT reply is *never* real data, it is
always a dead echo of the write buffer with byte[0] flipped 0x00->0x02
(this is what made earlier direct probing look completely broken). Real
read data instead arrives asynchronously as a 24-byte report on the
interrupt IN endpoint 0x82, which belongs to interface 1 — so both
interface 1 and interface 3 must be claimed.

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

Every SET_REPORT is immediately followed by a GET_REPORT poll on the same
report (this appears to be what makes the device execute the command /
push its async reply — a bare SET_REPORT with no follow-up GET_REPORT was
not tested and may not be sufficient).

Async reply format (interrupt IN, EP 0x82, up to 24 bytes):
  [0]     reply category (mirrors the command's category byte)
  [1]     reply subtype
  [2..]   payload, meaning depends on subtype

Confirmed subtypes:
  05 05 <stage> <dpiLoLE16> <dpiLoLE16>   -- DPI value for `stage` (1-6),
                                              X and Y repeated, uint16 LE
  05 0d <val>                             -- periodic ~1Hz heartbeat,
                                              meaning unconfirmed (battery?
                                              signal? cursor rate? — value
                                              range shifted a lot between
                                              capture sessions)

Status: PARTIALLY VERIFIED. Polling rate, DPI-stage read/write, LED
brightness, angle snap, ripple control, and motion sync are all
live-verified against real hardware. Debounce, LOD, LED effect/breath,
stage colors, and button bindings were observed as read-only queries in
the capture with no decodable response, and are NOT implemented — see
base.PulsarDevice defaults (NotImplementedError) for those. Note:
get_dpi_stages() has a known side effect of also changing the active DPI
stage (see its docstring) - no side-effect-free per-stage DPI read has
been found yet.
"""

import struct
import time
from typing import Optional

import usb.core
import usb.util

from pulsar_mouse.base import PulsarDevice, DeviceCapabilities

# ── Encoding tables ──────────────────────────────────────────────────────────

# Bitmask, not sequential like X2A - extends up to 8000Hz.
POLL_HZ_TO_VAL = {125: 0x01, 250: 0x02, 500: 0x04, 1000: 0x08,
                  2000: 0x10, 4000: 0x20, 8000: 0x40}
POLL_VAL_TO_HZ = {v: k for k, v in POLL_HZ_TO_VAL.items()}


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
        buttons={},                # not reverse-engineered yet
        polling_rates=sorted(POLL_HZ_TO_VAL),
        lod_values=[1, 2],         # unconfirmed for this model
    )

    _WVALUE = 0x0300      # HID Feature report, report ID 0
    _RESPONSE_IFACE = 1   # owns EP 0x82, separate from the command interface
    _RESPONSE_EP = 0x82

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
        for iface in (caps.interface_num, self._RESPONSE_IFACE):
            if dev.is_kernel_driver_active(iface):
                dev.detach_kernel_driver(iface)
            usb.util.claim_interface(dev, iface)
        self._dev = dev

    def close(self) -> None:
        if self._dev is None:
            return
        for iface in (self.capabilities.interface_num, self._RESPONSE_IFACE):
            usb.util.release_interface(self._dev, iface)
            try:
                self._dev.attach_kernel_driver(iface)
            except Exception:
                pass
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

        The returned bytes are a dead echo (byte[0] flips 0x00->0x02) -
        not real data - but sending this poll appears necessary for the
        device to act on the command and/or push its async reply.
        """
        return bytes(self._dev.ctrl_transfer(
            0xA1, 0x01, self._WVALUE,
            self.capabilities.interface_num, self.capabilities.report_size))

    def _drain_responses(self, timeout_ms=300) -> list[bytes]:
        out = []
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                out.append(bytes(self._dev.read(
                    self._RESPONSE_EP, 64, timeout=50)))
            except usb.core.USBError:
                pass
        return out

    def _cmd(self, cat, reg, sub, profile=0x01, payload=()) -> None:
        """Fire a write command. No reply is expected/awaited."""
        self._set_report(self._build(cat, reg, sub, profile, payload))
        self._poll_ack()

    def _query(self, cat, reg, sub, profile=0x01, payload=(),
               match=None, timeout_ms=500, retries=5, apply_read_bit=True) -> bytes:
        """Issue a read command and wait for its async reply on EP 0x82.

        `match` is an optional predicate over the raw reply bytes, used to
        pick the right reply out of the response stream (the device also
        emits an unrelated ~1Hz heartbeat on the same endpoint). The reply
        depends on a live RF round-trip to the mouse, which occasionally
        drops a packet or arrives late - retry the whole request rather
        than just waiting longer on a single window.

        `apply_read_bit` ORs `reg` with 0x80 to mark it as a read, matching
        the convention most categories use (e.g. polling rate: 0x09 write /
        0x89 read). Not universal though - the DPI-stage query (cat=0x05,
        reg=0x01) was captured with the bare register and no write
        counterpart, so callers for that one must pass False.
        """
        build = self._build_read if apply_read_bit else self._build
        for attempt in range(retries):
            self._set_report(build(cat, reg, sub, profile, payload))
            self._poll_ack()
            for resp in self._drain_responses(timeout_ms):
                if match is None or match(resp):
                    return resp
        raise IOError(
            f"No matching response from device for cat=0x{cat:02x} "
            f"reg=0x{reg:02x} sub=0x{sub:02x} after {retries} attempts "
            "(mouse may be asleep - try moving it or clicking a button)")

    # ── Global settings ─────────────────────────────────────────────────────

    def set_polling_rate(self, hz: int) -> None:
        val = POLL_HZ_TO_VAL.get(hz)
        if val is None:
            raise ValueError(f"Polling rate must be one of {sorted(POLL_HZ_TO_VAL)}")
        self._cmd(0x01, 0x09, 0x02, 0x01, [val])

    def get_polling_rate(self) -> int:
        # The read variant (cat=0x01, reg=0x89) was captured but its async
        # reply was never observed/decoded - needs another capture pass.
        raise NotImplementedError

    # Captured 2026-08-07: toggling each on Fusion produced a single-byte
    # SET_REPORT, cat=0x07/sub=0x02/profile=0x01, payload=[1 or 0], differing
    # only by reg. Live-verified working (no async reply observed or needed,
    # same as other fire-and-forget global settings).
    def set_angle_snap(self, enabled: bool) -> None:
        self._cmd(0x07, 0x04, 0x02, 0x01, [1 if enabled else 0])

    def set_ripple_control(self, enabled: bool) -> None:
        self._cmd(0x07, 0x03, 0x02, 0x01, [1 if enabled else 0])

    def set_motion_sync(self, enabled: bool) -> None:
        self._cmd(0x07, 0x05, 0x02, 0x01, [1 if enabled else 0])

    # ── Per-profile: DPI stages ─────────────────────────────────────────────

    def get_dpi_stages(self, profile: int) -> dict:
        # WARNING: cat=0x05/reg=0x01/sub=0x02 is the *set active stage*
        # command (see set_active_dpi_stage below) - re-captured traffic
        # confirmed it's the same request Fusion sends when the user clicks
        # to change stage. Calling this therefore leaves the mouse on
        # whatever stage was queried last (stage 6 if the full loop
        # completes), and can strand it on an earlier stage if a reply is
        # missed mid-loop. Left as-is pending a real side-effect-free read;
        # do not call this casually.
        stages = []
        for stage in range(1, self.capabilities.max_dpi_stages + 1):
            resp = self._query(
                0x05, 0x01, 0x02, 0x01, [stage], apply_read_bit=False,
                match=lambda r: r[0] == 0x05 and r[1] == 0x05 and r[2] == stage)
            dpi = struct.unpack_from('<H', resp, 3)[0]
            stages.append((dpi, dpi))
        return {'active': -1, 'count': len(stages), 'stages': stages}

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
        # A "05 08 <stage> <enabled>" reply was seen sporadically in the
        # capture but never reliably correlated to a specific request.
        raise NotImplementedError

    def set_dpi_stages(self, stages: list[int], active: int, profile: int) -> None:
        # Only single-stage reads/active-stage switching were decoded;
        # writing new DPI values for a stage was never captured.
        raise NotImplementedError

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

    def get_brightness(self, profile: int) -> int:
        raise NotImplementedError

    # ── Hidraw support ───────────────────────────────────────────────────────

    def parse_hidraw_event(self, data: bytes) -> Optional[dict]:
        if len(data) >= 5 and data[0] == 0x05 and data[1] == 0x05:
            dpi = struct.unpack_from('<H', data, 3)[0]
            stage = data[2]
            return {'dpi': dpi, 'stage': stage}
        return None
