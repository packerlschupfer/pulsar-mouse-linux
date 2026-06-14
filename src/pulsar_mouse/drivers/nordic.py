"""
Pulsar Nordic chipset — protocol driver.

Covers wireless and wired modes of Pulsar mice using the Nordic MCU
(VID 0x3554).  Known compatible: X2A Wireless, X2 V2 Mini.

Protocol based on python-pulsar-mouse-tool by andrewrabert:
https://github.com/andrewrabert/python-pulsar-mouse-tool

USB protocol:
  Interface 1, Endpoint 0x82 (IN), 17-byte packets.
  Write: control transfer  bmRequestType=0x21 bRequest=0x09 wValue=0x0208
  Read:  interrupt transfer EP 0x82, 17 bytes

Packet format (17 bytes):
  [0]     report ID: always 0x08
  [1]     command
  [2-15]  data
  [16]    checksum: uint8(0x55 - sum(bytes[0:16]))

Settings are stored in a flat memory map (addresses 0x00–0xB8).
MEM_GET reads 10 bytes at a time; MEM_SET writes up to 10 bytes.
Each value has a per-byte checksum at the next address: uint8(0x55 - value).

The memory map represents the currently active profile.  Switching profile
via ACTIVE_PROFILE_SET reloads the map from the device.

Status: UNTESTED — protocol assumed compatible with X2A Wireless based on
        shared Nordic chipset.  Needs validation with real hardware.
"""

import ctypes
import struct
from typing import Optional

import usb.core
import usb.util

from pulsar_mouse.base import PulsarDevice, DeviceCapabilities

# ── Commands ─────────────────────────────────────────────────────────────────

CMD_STATUS            = 0x03
CMD_POWER             = 0x04
CMD_MEM_SET           = 0x07
CMD_MEM_GET           = 0x08
CMD_RESTORE           = 0x09
CMD_ACTIVE_PROFILE_GET = 0x0E
CMD_ACTIVE_PROFILE_SET = 0x0F

# ── Memory addresses ────────────────────────────────────────────────────────

ADDR_POLLING_RATE     = 0x00
ADDR_DPI_STAGE_COUNT  = 0x02
ADDR_ACTIVE_DPI_STAGE = 0x04
ADDR_LOD_MM           = 0x0A

# DPI stages: 4 bytes each (dpi1, dpi2, dpi3, checksum) × 4 stages
ADDR_DPI_BASE         = 0x0C
DPI_STAGE_SIZE        = 4

# LED stage colors: 4 bytes each (R, G, B, checksum) × 4 stages
ADDR_LED_COLOR_BASE   = 0x2C
LED_COLOR_SIZE        = 4

ADDR_LED_EFFECT       = 0x4C
ADDR_LED_BRIGHTNESS   = 0x4E
ADDR_LED_BREATH_SPEED = 0x50
ADDR_LED_ENABLED      = 0x52

# Buttons: 4 bytes each (mode, arg1, arg2, checksum)
ADDR_BUTTON_BASE      = 0x60
BUTTON_SIZE           = 4
BUTTON_ADDRS = {
    'left':    0x60,
    'right':   0x64,
    'wheel':   0x68,
    'back':    0x6C,
    'forward': 0x70,
}

ADDR_DEBOUNCE         = 0xA9
ADDR_MOTION_SYNC      = 0xAB
ADDR_ANGLE_SNAP       = 0xAF
ADDR_RIPPLE_CONTROL   = 0xB1
ADDR_AUTOSLEEP        = 0xB7

# ── Encoding tables ─────────────────────────────────────────────────────────

POLL_HZ_TO_VAL = {1000: 0x01, 500: 0x02, 250: 0x04, 125: 0x08}
POLL_VAL_TO_HZ = {v: k for k, v in POLL_HZ_TO_VAL.items()}

LED_EFFECT_STEADY  = 0x01
LED_EFFECT_BREATHE = 0x02

BUTTON_MODE_DISABLED       = 0x00
BUTTON_MODE_MOUSE          = 0x01
BUTTON_MODE_DPI_CHANGE     = 0x02
BUTTON_MODE_PROFILE_CHANGE = 0x09
BUTTON_MODE_DPI_LOCK       = 0x0A


# ── DPI encoding (50 DPI steps, 3 bytes per stage) ──────────────────────────

def _dpi_to_raw(dpi: int) -> bytes:
    if not (50 <= dpi <= 26000) or dpi % 50:
        raise ValueError(f"DPI must be 50–26000 in steps of 50, got {dpi}")
    quo = (dpi // 50) - 1
    factor12800, factor50 = divmod(quo, 256)
    index3 = (factor12800 << 2) | (factor12800 << 6)
    return bytes([factor50, factor50, index3])


def _raw_to_dpi(raw: bytes) -> int:
    factor50 = raw[0] + 1
    nib = (raw[2] >> 2) & 0x03
    return (factor50 * 50) + (nib * 12800)


# ── Driver ───────────────────────────────────────────────────────────────────

class PulsarNordic(PulsarDevice):
    """Driver for Pulsar mice with Nordic chipset (wireless/wired).

    Settings are read into a memory cache on open().  Getters read from
    cache; setters write to the device and update the cache.
    """

    capabilities = DeviceCapabilities(
        name='Pulsar X2A Wireless',
        vid_pid_pairs=[(0x3554, 0xF507), (0x3554, 0xF508)],
        interface_num=1,
        report_size=17,
        num_profiles=1,
        max_dpi_stages=4,
        dpi_min=50,
        dpi_max=26000,
        dpi_step=50,
        buttons={
            'left':    0x01,
            'right':   0x02,
            'wheel':   0x03,
            'back':    0x04,
            'forward': 0x05,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        has_breath_speed=True,
        breath_speed_range=(1, 5),
        debounce_range=(0, 30),
        has_stage_colors=True,
        button_labels={
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'back': 'Side Back', 'forward': 'Side Front',
        },
    )

    _ENDPOINT_IN = 0x82

    def __init__(self):
        self._dev = None
        self._mem = {}

    # ── Connection lifecycle ─────────────────────────────────────────────

    def open(self) -> None:
        caps = self.capabilities
        dev = None
        for vid, pid in caps.vid_pid_pairs:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is not None:
                break
        if dev is None:
            pairs = ', '.join(f'0x{v:04x}:0x{p:04x}'
                              for v, p in caps.vid_pid_pairs)
            raise RuntimeError(f"{caps.name} not found ({pairs}). "
                               "Is the mouse plugged in?")
        iface = caps.interface_num
        if dev.is_kernel_driver_active(iface):
            dev.detach_kernel_driver(iface)
        usb.util.claim_interface(dev, iface)
        self._dev = dev
        self._mem_read_all()

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
        self._mem = {}

    # ── Low-level protocol ───────────────────────────────────────────────

    @staticmethod
    def _checksum(*values) -> int:
        return ctypes.c_uint8(0x55 - sum(values)).value

    def _build_packet(self, command, **kwargs):
        pkt = [0] * 16
        pkt[0] = 0x08  # report ID
        pkt[1] = command
        for key, val in kwargs.items():
            idx = int(key.replace('b', ''))
            pkt[idx] = val
        pkt.append(self._checksum(*pkt))
        return bytes(pkt)

    def _send(self, packet):
        iface = self.capabilities.interface_num
        self._dev.ctrl_transfer(0x21, 0x09, 0x0208, iface, packet)

    def _recv(self):
        return bytes(self._dev.read(self._ENDPOINT_IN,
                                    self.capabilities.report_size, timeout=2000))

    def _command(self, cmd, **kwargs):
        pkt = self._build_packet(cmd, **kwargs)
        self._send(pkt)
        return self._recv()

    # ── Memory access ────────────────────────────────────────────────────

    def _mem_read_all(self):
        """Read the full settings memory map (0x00–0xC8) into cache."""
        self._mem = {}
        addr = 0x00
        while addr <= 0xC0:
            length = 10
            resp = self._command(CMD_MEM_GET, b4=addr, b5=length)
            for i in range(length):
                self._mem[addr + i] = resp[6 + i]
            addr += length

    def _mem_write(self, addresses: dict[int, int]):
        """Write a contiguous block of memory addresses to the device."""
        if not addresses:
            return
        start = min(addresses)
        length = len(addresses)
        if length > 10:
            raise ValueError("Cannot write more than 10 bytes at once")

        kwargs = {'b4': start, 'b5': length}
        for i, addr in enumerate(range(start, start + length)):
            kwargs[f'b{6 + i}'] = addresses[addr]

        self._command(CMD_MEM_SET, **kwargs)
        self._mem.update(addresses)

    def _write_value(self, addr: int, value: int):
        """Write a single value with its per-byte checksum."""
        self._mem_write({
            addr: value,
            addr + 1: self._checksum(value),
        })

    def _write_bool(self, addr: int, enabled: bool):
        self._write_value(addr, 1 if enabled else 0)

    # ── Global settings ──────────────────────────────────────────────────

    def get_polling_rate(self) -> int:
        val = self._mem.get(ADDR_POLLING_RATE, 0x01)
        return POLL_VAL_TO_HZ.get(val, 1000)

    def set_polling_rate(self, hz: int) -> None:
        val = POLL_HZ_TO_VAL.get(hz)
        if val is None:
            raise ValueError(f"Polling rate must be one of {sorted(POLL_HZ_TO_VAL)}")
        self._write_value(ADDR_POLLING_RATE, val)

    def get_debounce(self) -> int:
        return self._mem.get(ADDR_DEBOUNCE, 0)

    def set_debounce(self, ms: int) -> None:
        lo, hi = self.capabilities.debounce_range
        if not lo <= ms <= hi:
            raise ValueError(f"Debounce must be {lo}–{hi} ms")
        self._write_value(ADDR_DEBOUNCE, ms)

    def get_angle_snap(self) -> bool:
        return bool(self._mem.get(ADDR_ANGLE_SNAP, 0))

    def set_angle_snap(self, enabled: bool) -> None:
        self._write_bool(ADDR_ANGLE_SNAP, enabled)

    def get_ripple_control(self) -> bool:
        return bool(self._mem.get(ADDR_RIPPLE_CONTROL, 0))

    def set_ripple_control(self, enabled: bool) -> None:
        self._write_bool(ADDR_RIPPLE_CONTROL, enabled)

    def get_motion_sync(self) -> bool:
        return bool(self._mem.get(ADDR_MOTION_SYNC, 0))

    def set_motion_sync(self, enabled: bool) -> None:
        self._write_bool(ADDR_MOTION_SYNC, enabled)

    # ── Per-profile: DPI stages ──────────────────────────────────────────

    def get_dpi_stages(self, profile: int) -> dict:
        count = self._mem.get(ADDR_DPI_STAGE_COUNT, 4)
        active = self._mem.get(ADDR_ACTIVE_DPI_STAGE, 0)
        stages = []
        for i in range(count):
            base = ADDR_DPI_BASE + i * DPI_STAGE_SIZE
            raw = bytes([self._mem.get(base + j, 0) for j in range(3)])
            dpi = _raw_to_dpi(raw)
            stages.append((dpi, dpi))
        return {'active': active, 'count': count, 'stages': stages}

    def set_dpi_stages(self, stages: list[int], active: int, profile: int) -> None:
        caps = self.capabilities
        if not 1 <= len(stages) <= caps.max_dpi_stages:
            raise ValueError(f"Must have 1–{caps.max_dpi_stages} DPI stages")
        if not 0 <= active < len(stages):
            raise ValueError(f"Active stage must be 0–{len(stages) - 1}")

        # Write stage count
        self._write_value(ADDR_DPI_STAGE_COUNT, len(stages))

        # Write active stage
        self._write_value(ADDR_ACTIVE_DPI_STAGE, active)

        # Write each DPI stage
        for i, dpi in enumerate(stages):
            if not caps.dpi_min <= dpi <= caps.dpi_max:
                raise ValueError(f"DPI {dpi} out of range {caps.dpi_min}–{caps.dpi_max}")
            raw = _dpi_to_raw(dpi)
            base = ADDR_DPI_BASE + i * DPI_STAGE_SIZE
            self._mem_write({
                base: raw[0],
                base + 1: raw[1],
                base + 2: raw[2],
                base + 3: self._checksum(*raw),
            })

    def get_active_dpi_stage(self, profile: int) -> int:
        return self._mem.get(ADDR_ACTIVE_DPI_STAGE, 0)

    def set_active_dpi_stage(self, stage: int, profile: int) -> None:
        self._write_value(ADDR_ACTIVE_DPI_STAGE, stage)

    # ── Per-profile: LOD ─────────────────────────────────────────────────

    def get_lod(self, profile: int) -> int:
        return self._mem.get(ADDR_LOD_MM, 1)

    def set_lod(self, mm: int, profile: int) -> None:
        if mm not in self.capabilities.lod_values:
            raise ValueError(f"LOD must be one of {self.capabilities.lod_values}")
        self._write_value(ADDR_LOD_MM, mm)

    # ── Per-profile: LED ─────────────────────────────────────────────────

    def get_brightness(self, profile: int) -> int:
        return self._mem.get(ADDR_LED_BRIGHTNESS, 255)

    def set_brightness(self, value: int, profile: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError("Brightness must be 0–255")
        self._write_value(ADDR_LED_BRIGHTNESS, value)

    def get_led_effect(self, profile: int) -> str:
        enabled = self._mem.get(ADDR_LED_ENABLED, 1)
        if not enabled:
            return 'off'
        effect = self._mem.get(ADDR_LED_EFFECT, LED_EFFECT_STEADY)
        if effect == LED_EFFECT_BREATHE:
            return 'breath'
        return 'steady'

    def set_led_effect(self, effect: str, profile: int) -> None:
        if effect == 'off':
            self._write_bool(ADDR_LED_ENABLED, False)
        elif effect == 'steady':
            self._write_value(ADDR_LED_EFFECT, LED_EFFECT_STEADY)
            self._write_bool(ADDR_LED_ENABLED, True)
        elif effect == 'breath':
            self._write_value(ADDR_LED_EFFECT, LED_EFFECT_BREATHE)
            self._write_bool(ADDR_LED_ENABLED, True)
        else:
            raise ValueError("Effect must be 'off', 'steady', or 'breath'")

    def get_breath_speed(self, profile: int) -> int:
        return self._mem.get(ADDR_LED_BREATH_SPEED, 1)

    def set_breath_speed(self, speed: int, profile: int) -> None:
        lo, hi = self.capabilities.breath_speed_range
        if not lo <= speed <= hi:
            raise ValueError(f"Breath speed must be {lo}–{hi}")
        self._write_value(ADDR_LED_BREATH_SPEED, speed)

    # ── Per-profile: stage colors ────────────────────────────────────────

    def get_stage_color(self, stage: int, profile: int) -> tuple[int, int, int]:
        base = ADDR_LED_COLOR_BASE + stage * LED_COLOR_SIZE
        r = self._mem.get(base, 0)
        g = self._mem.get(base + 1, 0)
        b = self._mem.get(base + 2, 0)
        return (r, g, b)

    def set_stage_color(self, stage: int, r: int, g: int, b: int,
                        profile: int) -> None:
        for val, name in [(r, 'R'), (g, 'G'), (b, 'B')]:
            if not 0 <= val <= 255:
                raise ValueError(f"{name} must be 0–255")
        base = ADDR_LED_COLOR_BASE + stage * LED_COLOR_SIZE
        self._mem_write({
            base: r,
            base + 1: g,
            base + 2: b,
            base + 3: self._checksum(r + g + b),
        })

    # ── Per-profile: button bindings ─────────────────────────────────────

    def get_button(self, btn_id: int, profile: int) -> tuple[int, int, int]:
        btn_names = {v: k for k, v in self.capabilities.buttons.items()}
        name = btn_names.get(btn_id)
        if name is None:
            raise ValueError(f"Unknown button ID 0x{btn_id:02x}")
        addr = BUTTON_ADDRS[name]
        return (self._mem.get(addr, 0),
                self._mem.get(addr + 1, 0),
                self._mem.get(addr + 2, 0))

    def set_button(self, btn_id: int, btn_type: int, a1: int, a2: int,
                   profile: int) -> None:
        btn_names = {v: k for k, v in self.capabilities.buttons.items()}
        name = btn_names.get(btn_id)
        if name is None:
            raise ValueError(f"Unknown button ID 0x{btn_id:02x}")
        addr = BUTTON_ADDRS[name]
        self._mem_write({
            addr: btn_type,
            addr + 1: a1,
            addr + 2: a2,
            addr + 3: self._checksum(btn_type + a1 + a2),
        })

    # ── Factory reset ────────────────────────────────────────────────────

    def reset_to_defaults(self, profile: int) -> None:
        self._command(CMD_RESTORE)
        self._mem_read_all()

    # ── Nordic-specific: battery / power ─────────────────────────────────

    def get_power(self) -> dict:
        """Return battery status (Nordic wireless only)."""
        resp = self._command(CMD_POWER)
        return {
            'battery_percent': resp[6],
            'power_connected': bool(resp[7]),
            'battery_mv': struct.unpack('>H', bytes(resp[8:10]))[0],
        }

    def get_autosleep(self) -> int:
        """Return auto-sleep timeout in seconds."""
        return self._mem.get(ADDR_AUTOSLEEP, 0) * 10
