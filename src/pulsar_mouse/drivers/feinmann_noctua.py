"""
Pulsar Feinmann F01 Noctua Edition (dongle) — protocol driver.

Despite the name, this mouse has nothing in common with feinmann8k.py.  That
driver speaks the Sonix 64-byte HID Feature Report protocol on interface 3;
this one speaks the 17-byte **Nordic** protocol on interface 1, endpoint 0x82,
SET_REPORT wValue=0x0208 — the same protocol as the X2 CrazyLight, with the
same register map.  Anyone starting from the Feinmann FO1 driver gets silence
from the device, which is exactly what happened to the first attempt (issue
#12).

Decoded from a usbmon capture of Pulsar Fusion taken by @Wyatt-Robinson
(2026-09-27), correlated frame by frame against a screen recording of the same
session.  The wired half reported firmware 5.09.

Every register this mouse writes is one the CrazyLight already has, except two:

    0x00E7   fan mode, 0 = off … 4 = maximum   (this model has a fan)
    0x00D7   low-power-mode threshold, percent

Both sit *above* the 0x00–0xBF per-profile window, so they are treated as
device-wide rather than per-profile.  That placement is the evidence; it has
not been confirmed by switching profiles and reading back.

No LEDs.  Fusion still shows an LED panel and the firmware still accepts
writes to the LED registers — the Noctua edition shares firmware with its
lit siblings — but the hardware has none, confirmed by the owner, so the
capability says so and the CLI and GUI omit those controls.
"""

from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight

# Outside the per-profile window (0x00–0xBF); see the module docstring.
ADDR_FAN_MODE          = 0x00E7
ADDR_LOW_POWER_PERCENT = 0x00D7

# Level 0 reads "OFF" in Fusion; 4 sits at the right-hand end of its slider.
FAN_OFF = 0
FAN_MAX = 4

# Observed: stored 0x04 while Fusion read 1.00 mm, written 0x06 for 1.20 mm.
# Two points, 0.1 mm apart per step, giving mm = (code + 6) / 10.  Only those
# two are confirmed; the rest of the range follows that line and wants a
# hardware check.
_LOD_MM_OFFSET = 0.6
_LOD_STEP_MM = 0.1


class PulsarFeinmannNoctua(PulsarX2CrazyLight):
    """Driver for the Feinmann F01 Noctua Edition over its dongle.

    Inherits the CrazyLight's Nordic protocol wholesale — same packet
    framing, same checksum, same register addresses for everything the two
    mice share.
    """

    capabilities: DeviceCapabilities = replace(
        PulsarX2CrazyLight.capabilities,
        name='Feinmann F01 Noctua Edition (dongle)',
        vid_pid_pairs=[(0x3710, 0x5504)],
        # DPI: the stage records decode with the same layout as the
        # CrazyLight's but at 50 DPI per step, not 10 — 07/0f/1f/3f read back
        # as Fusion's 400/800/1600/3200.  One mode, so the whole range is a
        # single linear scale.  12800 is where Fusion's own stage list
        # stopped; whether the sensor goes higher is untested, and claiming a
        # range the device can't reach would write values it rejects.
        dpi_min=50,
        dpi_max=12800,
        dpi_step=50,
        # No LEDs on this edition — see the module docstring.
        has_led=False,
        has_breathe_speed=False,
        has_stage_colors=False,
        lod_values=[0.7, 2.0],
        lod_step=_LOD_STEP_MM,
        fan_range=(FAN_OFF, FAN_MAX),
        wireless=True,
    )

    # Single mode, (mode, mult, base, limit): index = dpi/50 - 1, which is
    # what the captured stage records hold.
    _DPI_MODES = ((0, 1, 1, None),)

    # mm -> code, from the line described at the top of this module.
    _LOD_CODES = {round(_LOD_MM_OFFSET + _LOD_STEP_MM * code, 1): code
                  for code in range(1, 15)}

    # ── Fan ──────────────────────────────────────────────────────────────

    def get_fan_mode(self) -> int:
        """Fan level, 0 (off) to 4 (maximum)."""
        return self._mem_read_range(ADDR_FAN_MODE, 1)[0]

    def set_fan_mode(self, level: int) -> None:
        lo, hi = self.capabilities.fan_range
        if not lo <= level <= hi:
            raise ValueError(f"Fan mode must be {lo}–{hi}")
        self._write_value(ADDR_FAN_MODE, level)

    # ── Low power threshold ──────────────────────────────────────────────

    def get_low_power_threshold(self) -> int:
        """Battery percentage at which the mouse enters low power mode."""
        return self._mem_read_range(ADDR_LOW_POWER_PERCENT, 1)[0]

    def set_low_power_threshold(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError("Low power threshold must be 0–100")
        self._write_value(ADDR_LOW_POWER_PERCENT, percent)
