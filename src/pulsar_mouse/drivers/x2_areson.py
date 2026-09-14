"""Pulsar X2 Wireless with the Areson USB identifiers.

This model uses the Nordic 17-byte protocol and memory layout shared by the
X2 CrazyLight driver. The wired and dongle modes use different PIDs; the
wired class keeps the link at its 1 kHz limit.

Reads and writes were confirmed on the supplied hardware, primarily through
the dongle. Button remapping and LED effects were also tested through the
dongle; the wired mode was tested for detection and readout.
"""

from dataclasses import replace
import time

import usb.core

from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight
from pulsar_mouse.drivers.x2_crazylight_wired import PulsarX2CrazyLightWired
from pulsar_mouse.drivers.nordic import (
    CMD_MEM_GET,
    CMD_POWER,
    CMD_STATUS,
    DPI_MODES_SINGLE,
)


class _AresonMixin:
    """Shared protocol differences for both Areson USB modes."""

    _DPI_MODES = DPI_MODES_SINGLE

    _BUTTON_ADDRS = {
        'left': 0x60,
        'right': 0x64,
        'wheel': 0x68,
        'thumb1': 0x70,
        'thumb2': 0x6C,
    }

    def _command(self, command, **kwargs):
        retryable = command in (CMD_MEM_GET, CMD_STATUS, CMD_POWER)
        attempts = 3 if retryable else 1
        for attempt in range(attempts):
            try:
                return super()._command(command, **kwargs)
            except usb.core.USBTimeoutError:
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.05)

    def _write_led_block(self, effect=None, brightness=None, speed=None,
                         profile=None):
        self._ensure_profile(profile)
        current_effect = self._mem.get(0x4C, 0)
        current_brightness = self._mem.get(0x4E, 255)
        current_speed = self._mem.get(0x50, 1)
        effect = current_effect if effect is None else effect
        brightness = current_brightness if brightness is None else brightness
        speed = current_speed if speed is None else speed
        data = bytes((
            effect, self._checksum(effect),
            brightness, self._checksum(brightness),
            speed, self._checksum(speed),
        ))
        self._mem_write_bytes(0x4C, data)
        self._command(CMD_STATUS)

    def get_led_effect(self, profile: int) -> str:
        self._ensure_profile(profile)
        effect = self._mem.get(0x4C, 0)
        if effect == 0:
            return 'off'
        return 'breathe' if effect == 0x02 else 'steady'

    def set_led_effect(self, effect: str, profile: int) -> None:
        self._ensure_profile(profile)
        if effect == 'off':
            self._write_value(0x4C, 0)
            self._command(CMD_STATUS)
            return
        if effect == 'steady':
            code = 0x01
        elif effect == 'breathe':
            code = 0x02
        else:
            raise ValueError("Effect must be 'off', 'steady', or 'breathe'")
        self._write_led_block(effect=code, profile=profile)

    def set_brightness(self, value: int, profile: int) -> None:
        if not 0 <= value <= 255:
            raise ValueError("Brightness must be 0–255")
        self._write_led_block(brightness=value, profile=profile)

    def set_breathe_speed(self, speed: int, profile: int) -> None:
        lo, hi = self.capabilities.breathe_speed_range
        if not lo <= speed <= hi:
            raise ValueError(f"Breathe speed must be {lo}–{hi}")
        self._write_led_block(speed=speed, profile=profile)

    def get_firmware_version(self) -> str:
        # Command 0x12 did not provide a usable mouse firmware version in the
        # Areson captures; the USB bcdDevice identifies the receiver instead.
        return 'unknown'

    def get_power(self) -> dict:
        response = self._command(CMD_POWER)
        return {
            'battery_percent': response[6],
            'power_connected': bool(response[7]),
            'battery_mv': None,
        }


class PulsarX2AresonWireless(_AresonMixin, PulsarX2CrazyLight):
    """Driver for the Areson X2 Wireless dongle."""

    capabilities = replace(
        PulsarX2CrazyLight.capabilities,
        name='Pulsar X2 Wireless (Areson dongle)',
        vid_pid_pairs=[(0x25A7, 0xFA7C)],
        dpi_min=50,
        dpi_max=26000,
        dpi_step=50,
        buttons={
            'left': 0x01,
            'right': 0x02,
            'wheel': 0x03,
            'thumb1': 0x04,
            'thumb2': 0x05,
        },
        button_labels={
            'left': 'Left Click',
            'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Forward',
            'thumb2': 'Back',
        },
    )

class PulsarX2AresonWired(_AresonMixin, PulsarX2CrazyLightWired):
    """Driver for the Areson X2 Wireless mouse over USB cable."""

    capabilities = replace(
        PulsarX2CrazyLightWired.capabilities,
        name='Pulsar X2 Wireless (Areson cable)',
        vid_pid_pairs=[(0x25A7, 0xFA7B)],
        dpi_min=50,
        dpi_max=26000,
        dpi_step=50,
        buttons=PulsarX2AresonWireless.capabilities.buttons,
        button_labels=PulsarX2AresonWireless.capabilities.button_labels,
    )
