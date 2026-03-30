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
  [6]     profile: 0x00=global, 0x01-0x05=profile 1-5
  [7-61]  payload
  [62-63] checksum: little-endian uint16 of sum(bytes[0:62])

Global settings (profile=0): polling rate, debounce, angle snap, ripple, motion sync
Per-profile settings (profile=1-5): DPI stages, LOD, brightness, LED effect, button bindings
"""

import struct
import glob
import os
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

LED_NAME_TO_VAL = {'off': 0, 'steady': 1, 'breath': 2}
LED_VAL_TO_NAME = {v: k for k, v in LED_NAME_TO_VAL.items()}

# Factory default button bindings (from Pulsar Fusion reset capture)
BUTTON_DEFAULTS = {
    0x01: (BTN_TYPE_MOUSE, 0x01, 0x00),  # left   → left click
    0x02: (BTN_TYPE_MOUSE, 0x02, 0x00),  # right  → right click
    0x03: (BTN_TYPE_MOUSE, 0x03, 0x00),  # wheel  → wheel click
    0x04: (BTN_TYPE_MOUSE, 0x04, 0x00),  # thumb1 → forward
    0x05: (BTN_TYPE_MOUSE, 0x05, 0x00),  # thumb2 → backward
    0x06: (BTN_TYPE_MOUSE, 0x04, 0x00),  # thumb3 → forward
    0x07: (BTN_TYPE_MOUSE, 0x05, 0x00),  # thumb4 → backward
    0x0b: (BTN_TYPE_DPI,   0x03, 0x00),  # dpi    → dpiloop
}


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
            'thumb1': 0x04,   # left thumb front (default: forward)
            'thumb2': 0x05,   # left thumb back  (default: backward)
            'thumb3': 0x06,   # right thumb front (default: forward)
            'thumb4': 0x07,   # right thumb back  (default: backward)
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        button_labels={
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Thumb 1 (forward)', 'thumb2': 'Thumb 2 (back)',
            'thumb3': 'Thumb 3 (forward)', 'thumb4': 'Thumb 4 (back)',
            'dpi': 'DPI Button',
        },
    )

    _WVALUE = 0x0300  # HID Feature report, report ID 0

    def __init__(self):
        self._dev = None

    # ── Connection lifecycle ──────────────────────────────────────────────

    def open(self) -> None:
        caps = self.capabilities
        vid, pid = caps.vid_pid_pairs[0]
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            raise RuntimeError(
                f"{caps.name} not found (VID=0x{vid:04x}, PID=0x{pid:04x}). "
                "Is the mouse plugged in?")
        if dev.is_kernel_driver_active(caps.interface_num):
            dev.detach_kernel_driver(caps.interface_num)
        usb.util.claim_interface(dev, caps.interface_num)
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

    def _cmd(self, cat, reg, sub, profile, payload=()):
        self._set_report(self._build(cat, reg, sub, profile, payload))
        rsp = self._get_report()
        if rsp[0] not in (0x01, 0x02):
            raise IOError(f"Unexpected response byte: 0x{rsp[0]:02x}")
        return rsp

    def _read(self, cat, reg, sub, profile, payload=()):
        self._set_report(self._build_read(cat, reg, sub, profile, payload))
        rsp = self._get_report()
        if rsp[0] != 0x01:
            raise IOError(f"Bad response direction byte: 0x{rsp[0]:02x}")
        return rsp

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

    def set_led_effect(self, effect: str, profile: int) -> None:
        val = LED_NAME_TO_VAL.get(effect)
        if val is None:
            raise ValueError(f"Effect must be one of {list(LED_NAME_TO_VAL)}")
        self._cmd(0x03, 0x04, 0x0F, profile, [0x01, val])

    def get_breath_speed(self, profile: int) -> int:
        rsp = self._read(0x03, 0x04, 0x0F, profile, [0x01])
        return rsp[11]

    def set_breath_speed(self, speed: int, profile: int) -> None:
        lo, hi = self.capabilities.breath_speed_range
        if not lo <= speed <= hi:
            raise ValueError(f"Breath speed must be {lo}–{hi}")
        self._cmd(0x03, 0x04, 0x0F, profile, [0x01, 0x02, 0x00, 0x00, speed])

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

    def reset_to_defaults(self, profile: int) -> None:
        self._cmd(0x02, 0x05, 0x01, profile)
        pkt = self._build(0x02, 0x06, 0x01, profile)
        self._set_report(pkt)
        try:
            self._get_report()
        except Exception:
            pass  # device may have already reset

    # ── Hidraw support ────────────────────────────────────────────────────

    def find_hidraw(self) -> Optional[str]:
        vid = f'{self.capabilities.vid_pid_pairs[0][0]:04x}'
        for path in sorted(glob.glob('/sys/class/hidraw/hidraw*/device/uevent')):
            try:
                text = open(path).read()
                if vid not in text:
                    continue
                phys_line = [l for l in text.splitlines() if 'HID_PHYS' in l]
                if phys_line and phys_line[0].endswith('/input1'):
                    return '/dev/' + path.split('/')[4]
            except OSError:
                continue
        return None

    def parse_hidraw_event(self, data: bytes) -> Optional[dict]:
        if len(data) >= 7 and data[0] == 0x05 and data[1] == 0x05:
            dpi = struct.unpack_from('<H', data, 3)[0]
            stage = data[2] + 1
            return {'dpi': dpi, 'stage': stage}
        return None
