"""
Pulsar eS Xlite on its cable (3710:9604) - protocol driver, read-only so far.

A third transport, neither of the other two: Feature report 0x0e on interface
1, with an a5 magic and an opcode byte.  Protocol notes, with how each opcode
was established:
https://github.com/itCFBTS/espulse/blob/main/docs/protocol.md

Packet format (64 bytes, in both directions):
  [0]     report ID: always 0x0e
  [1]     magic: always 0xa5
  [2]     opcode
  [3]     direction: 0x01=read, 0x02=write, 0x03 in a reply=busy
  [4]     data length
  [5-]    data; a selector comes first where the opcode has one

A request is a SET_REPORT, and the reply is polled for with GET_REPORT.  A
reply can be left over from an earlier read, so it only counts if it echoes
the opcode and any selector.

Read-only: _read_packet() is the only thing that builds a packet, and it can
only build the reads in _READS.  Every setter refuses.

Hidraw only, with no libusb fallback.  Interface 1 carries the mouse's motion,
consumer and system reports as well as this one, so detaching the kernel's
driver from it would stop the mouse moving (see PulsarNordic.open()).
"""

import fcntl
import os
import time
from types import SimpleNamespace

from pulsar_mouse.base import PulsarDevice, DeviceCapabilities

REPORT_ID = 0x0e
MAGIC = 0xa5
DIR_READ = 0x01
REPORT_SIZE = 64

# HIDIOCSFEATURE(64) and HIDIOCGFEATURE(64) from <linux/hidraw.h>.
_HIDIOCSFEATURE = 0xC0404806
_HIDIOCGFEATURE = 0xC0404807

OP_FIRMWARE      = 0x01
OP_DPI_SELECTION = 0x02
OP_DPI_LED       = 0x03
OP_DPI_VALUE     = 0x04
OP_DEBOUNCE      = 0x05
OP_REPORT_RATE   = 0x07
OP_SENSOR        = 0x08
OP_LOD           = 0x09
OP_BATTERY       = 0x0b
OP_BUTTON        = 0x0c
OP_LINK          = 0x0f     # dongle only: is a mouse connected to it?
OP_POWER         = 0x10
OP_ANGLE_SNAP    = 0x1c
OP_LED           = 0x1f

# Everything this driver may ask the mouse.  Unknown opcodes are not to be
# tried: 0x17 is the factory reset and 0x1a starts dongle pairing.
_READS = frozenset({
    OP_FIRMWARE, OP_DPI_SELECTION, OP_DPI_LED, OP_DPI_VALUE, OP_DEBOUNCE,
    OP_REPORT_RATE, OP_SENSOR, OP_LOD, OP_BATTERY, OP_BUTTON, OP_LINK,
    OP_POWER, OP_ANGLE_SNAP, OP_LED,
})

POLL_VAL_TO_HZ = {0: 125, 1: 500, 2: 1000, 3: 2000, 4: 4000, 5: 8000}
LOD_VAL_TO_MM = {1: 0.7, 2: 0.8, 4: 1.0, 7: 1.2, 11: 1.4}
DEBOUNCE_VAL_TO_MS = {0: 0, 1: 2, 2: 4}
LED_VAL_TO_NAME = {0: 'off', 1: 'steady'}

# What 0x0c says a button does.  It is one code from this list rather than the
# (type, action, action) the other families store, so get_button() returns the
# code as the type and describe_button() below names it.
BUTTON_FUNCTION_NAMES = {
    1: 'left', 2: 'right', 3: 'wheel', 4: 'backward', 5: 'forward',
    6: 'disabled', 7: 'pagedown', 8: 'full screenshot', 9: 'region capture',
    10: 'cut', 11: 'copy', 12: 'paste',
}

_READ_ONLY = 'the eS driver is read-only so far'


def _read_packet(op: int, length: int, selector=None) -> bytearray:
    """The 64-byte feature report for a read.  Always a read."""
    if op not in _READS:
        raise ValueError(f'opcode 0x{op:02x} is not a known read')
    packet = bytearray(REPORT_SIZE)
    packet[:5] = bytes([REPORT_ID, MAGIC, op, DIR_READ, length])
    if selector is not None:
        packet[5] = selector
    return packet


def _reply_data(reply: bytes, op: int, length: int, selector=None):
    """The reply's data if it answers this read, else None.

    None covers the replies to poll past: `0e a5 ff` (not ready, over the
    dongle), `00 00 00 00 88 0f` (not ready, on the cable), our opcode with
    direction 03 (busy), and whatever was left from the read before.
    """
    if (len(reply) < 5 or reply[0] != REPORT_ID or reply[1] != MAGIC
            or reply[2] != op or reply[3] != DIR_READ):
        return None
    data = bytes(reply[5:5 + reply[4]])
    if len(data) < length or (selector is not None and data[0] != selector):
        return None
    return data


class PulsarEs(PulsarDevice):
    """Driver for the Pulsar eS Xlite on its cable."""

    # num_profiles=1 stands for "the profile the mouse is on".  The mouse has
    # four, but no read here takes a profile, so reaching another means
    # writing 0x14 and switching the live mouse.  Getters ignore their profile
    # argument for the same reason.
    capabilities = DeviceCapabilities(
        name='Pulsar eS Xlite Wired',
        vid_pid_pairs=[(0x3710, 0x9604)],
        interface_num=1,
        report_size=REPORT_SIZE,
        num_profiles=1,
        max_dpi_stages=6,
        dpi_min=50,
        dpi_max=42000,
        dpi_step=1,
        buttons={
            'left': 0xb1, 'right': 0xb2, 'wheel': 0xb3,
            'back': 0xb4, 'forward': 0xb5,
        },
        polling_rates=[500, 1000, 2000, 4000, 8000],
        lod_values=[0.7, 0.8, 1.0, 1.2, 1.4],
        led_effects=['off', 'steady'],
        has_breathe_speed=False,
        brightness_range=(1, 10),
        has_ripple_control=False,
        debounce_range=(0, 4),
        has_reset=False,
        wireless=False,
        has_turbo=True,
        power_saving_range=None,
        model_key='pulsar-es-xlite',
        reports_signal_quality=False,
    )

    # Left unset on purpose, so the GUI starts no listener: nothing unprompted
    # has been seen here except motion, and it would be read at up to 8 kHz.
    _HIDRAW_INTERFACE = None

    _SENDS = 3
    _POLLS_PER_SEND = 60
    _REPLY_POLL = 0.01

    def __init__(self):
        self._fd = None

    # ── Connection lifecycle ──────────────────────────────────────────────

    def _config_node(self, root: str = '/sys/class/hidraw'):
        # find_hidraw() does the matching, but it answers for the listener,
        # and this driver wants the node without starting one.
        return PulsarDevice.find_hidraw(
            SimpleNamespace(_HIDRAW_INTERFACE='/input1',
                            capabilities=self.capabilities), root)

    def open(self) -> None:
        if self._fd is not None:
            return
        name = self.capabilities.name
        path = self._config_node()
        if path is None:
            raise RuntimeError(f'{name} not found. Is it plugged in?')
        try:
            self._fd = os.open(path, os.O_RDWR)
        except PermissionError as why:
            raise RuntimeError(
                f'No permission to open {path} ({why}). Install '
                'udev/50-pulsar-mouse.rules and re-plug the device.') from why
        try:
            self._after_open()
        except Exception:
            self.close()
            raise

    def _after_open(self) -> None:
        pass

    def close(self) -> None:
        if self._fd is None:
            return
        os.close(self._fd)
        self._fd = None

    # ── Low-level protocol helpers ────────────────────────────────────────

    def _read(self, op: int, length: int, selector=None) -> bytes:
        """Send one read and poll for its reply, about 10 ms apart."""
        for _send in range(self._SENDS):
            fcntl.ioctl(self._fd, _HIDIOCSFEATURE,
                        _read_packet(op, length, selector))
            for _poll in range(self._POLLS_PER_SEND):
                time.sleep(self._REPLY_POLL)
                reply = bytearray(REPORT_SIZE)
                reply[0] = REPORT_ID
                got = fcntl.ioctl(self._fd, _HIDIOCGFEATURE, reply)
                data = _reply_data(reply[:got], op, length, selector)
                if data is not None:
                    return data
        raise IOError(f'no reply to opcode 0x{op:02x} '
                      '(is the mouse awake, and is Bibimbap closed?)')

    # ── Device information ────────────────────────────────────────────────

    def get_firmware_version(self) -> str:
        # A build number: Pulsar's server maps it to the version Bibimbap
        # shows, so 56 00 is "0056" here and "V 1.0" there.
        low, high = self._read(OP_FIRMWARE, 2)
        return f'{high:02x}{low:02x}'

    def get_power(self) -> dict:
        raw = self._read(OP_BATTERY, 1)[0]
        if raw > 20:    # 0xff is "no reading yet"
            raise IOError(f'no battery reading (0x{raw:02x})')
        return {
            'battery_percent': raw * 5,
            # Bibimbap calls this charging, but it has only been seen set on
            # the cable, so it may just mean the cable is connected.
            'power_connected': self._read(OP_POWER, 1)[0] == 1,
            'battery_mv': None,
        }

    # ── Global settings ───────────────────────────────────────────────────

    def get_polling_rate(self) -> int:
        code = self._read(OP_REPORT_RATE, 1)[0]
        if code not in POLL_VAL_TO_HZ:
            raise IOError(f'unknown polling rate code 0x{code:02x}')
        return POLL_VAL_TO_HZ[code]

    def get_debounce(self) -> int:
        code = self._read(OP_DEBOUNCE, 1)[0]
        if code not in DEBOUNCE_VAL_TO_MS:
            raise IOError(f'unknown debounce code 0x{code:02x}')
        return DEBOUNCE_VAL_TO_MS[code]

    def get_angle_snap(self) -> bool:
        return self._read(OP_ANGLE_SNAP, 1)[0] != 0

    def get_motion_sync(self) -> bool:
        return self._read(OP_SENSOR, 2)[1] != 0

    def get_turbo_mode(self) -> bool:
        return self._read(OP_SENSOR, 2)[0] != 0

    # ── DPI ───────────────────────────────────────────────────────────────

    def get_dpi_stages(self, profile: int = 1) -> dict:
        count, active = self._read(OP_DPI_SELECTION, 2)
        stages = []
        for stage in range(1, min(count, self.capabilities.max_dpi_stages) + 1):
            data = self._read(OP_DPI_VALUE, 6, stage)
            stages.append((int.from_bytes(data[2:4], 'little'),
                           int.from_bytes(data[4:6], 'little')))
        return {'active': active, 'count': count, 'stages': stages}

    def get_active_dpi_stage(self, profile: int = 1) -> int:
        return self._read(OP_DPI_SELECTION, 2)[1]

    def get_stage_color(self, stage: int, profile: int = 1) -> tuple[int, int, int]:
        _stage, r, g, b = self._read(OP_DPI_LED, 4, stage)[:4]
        return (r, g, b)

    # ── Other settings ────────────────────────────────────────────────────

    def get_lod(self, profile: int = 1) -> float:
        code = self._read(OP_LOD, 1)[0]
        if code not in LOD_VAL_TO_MM:
            raise IOError(f'unknown lift-off code 0x{code:02x}')
        return LOD_VAL_TO_MM[code]

    def get_brightness(self, profile: int = 1) -> int:
        return self._read(OP_LED, 2)[1]

    def get_led_effect(self, profile: int = 1) -> str:
        code = self._read(OP_LED, 2)[0]
        if code not in LED_VAL_TO_NAME:
            raise IOError(f'unknown LED effect code 0x{code:02x}')
        return LED_VAL_TO_NAME[code]

    def get_button(self, btn_id: int, profile: int = 1) -> tuple[int, int, int]:
        return (self._read(OP_BUTTON, 2, btn_id)[1], 0, 0)

    def describe_button(self, btn_type: int, a1: int, a2: int) -> str:
        return BUTTON_FUNCTION_NAMES.get(btn_type,
                                         f'function(0x{btn_type:02x})')

    def parse_button_function(self, spec: str) -> tuple[int, int, int]:
        # ValueError, unlike the setters below: it is what the GUI's remap
        # dialog catches and shows, and anything else leaves it silently open.
        raise ValueError(_READ_ONLY)

    # ── Writes ────────────────────────────────────────────────────────────
    # NotImplementedError carrying a message, because the GUI's Apply shows
    # it, and base's empty one would be a blank error banner.

    def set_polling_rate(self, hz: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_debounce(self, ms: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_angle_snap(self, enabled: bool) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_motion_sync(self, enabled: bool) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_turbo_mode(self, enabled: bool) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_dpi_stages(self, stages: list[int], active: int, profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_active_dpi_stage(self, stage: int, profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_stage_color(self, stage: int, r: int, g: int, b: int, profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_lod(self, mm: float, profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_brightness(self, value: int, profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_led_effect(self, effect: str, profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)

    def set_button(self, btn_id: int, btn_type: int, a1: int, a2: int,
                   profile: int) -> None:
        raise NotImplementedError(_READ_ONLY)
