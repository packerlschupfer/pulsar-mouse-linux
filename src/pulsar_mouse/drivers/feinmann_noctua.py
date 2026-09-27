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

Both sit *above* the 0x00–0xBF window the CrazyLight stores a profile in,
which read as device-wide.  It isn't: @Wyatt-Robinson set the fan, switched
profile and watched it change back, so this model's per-profile window
reaches further than that.  Both are per profile, and both follow the
per_profile_globals contract — no profile argument means the active one.

No LEDs.  Fusion still shows an LED panel and the firmware still accepts
writes to the LED registers — the Noctua edition shares firmware with its
lit siblings — but the hardware has none, confirmed by the owner, so the
capability says so and the CLI and GUI omit those controls.
"""

from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.nordic import (
    ADDR_ACTIVE_DPI_STAGE, ADDR_DPI_STAGE_COUNT,
)
from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight

# The DPI table this mouse actually uses.  Not 0x000C, where the CrazyLight
# keeps its stages: that area exists here too and its first four entries
# happen to agree, which is exactly why the first reading of this protocol
# got it wrong.  Stages 5 and 6 disagreed, and the disagreement was the table
# saying it lived somewhere else.  Fusion reads and writes only this one
# (capture of a DPI drag, issue #12), six bytes per stage:
#
#     [x_lo, x_hi, y_lo, y_hi, 0x00, checksum]
#
# X and Y are separate 16-bit values holding DPI - 1, and the checksum is the
# usual 0x55 minus the sum of the bytes before it.  A write takes effect
# immediately: Fusion sends one per slider position as you drag, and nothing
# else follows to commit it.
ADDR_DPI_TABLE   = 0x1B00
DPI_RECORD_SIZE  = 6

# Outside the per-profile window (0x00–0xBF); see the module docstring.
ADDR_FAN_MODE          = 0x00E7
ADDR_LOW_POWER_PERCENT = 0x00D7

# Level 0 reads "OFF" in Fusion; 4 sits at the right-hand end of its slider.
FAN_OFF = 0
FAN_MAX = 4

# Observed: stored 0x04 while Fusion read 1.00 mm, written 0x06 for 1.20 mm.
# Two points, 0.1 mm apart per step, giving mm = (code + 6) / 10.  The line
# through them was a guess at the time; every value from 0.7 to 2.0 mm has
# since been written and read back on the mouse (issue #12), so the whole
# range is confirmed.
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
        # 42000 max, reported by the owner and matching the largest value
        # his drag produced (0xA40F = 41999, so 42000).  The device takes any
        # integer in between - Fusion's slider lands on values like 28393 -
        # but a 1 DPI spinner over a 42000 range is unusable, so the UI steps
        # by 10 while the CLI and the encoder accept anything.
        dpi_min=50,
        dpi_max=42000,
        dpi_step=10,
        # No LEDs on this edition — see the module docstring.
        has_led=False,
        has_breathe_speed=False,
        has_stage_colors=False,
        lod_values=[0.7, 2.0],
        lod_step=_LOD_STEP_MM,
        fan_range=(FAN_OFF, FAN_MAX),
        wireless=True,
    )

    # The inherited (mode, mult, base, limit) encoding doesn't describe this
    # model at all - see ADDR_DPI_TABLE.  Kept only because the base class
    # reads it; nothing here uses it.
    _DPI_MODES = ((0, 1, 1, None),)

    # ── DPI stages ───────────────────────────────────────────────────────

    @staticmethod
    def _dpi_encode(dpi: int) -> bytes:
        """One stage record.  X and Y get the same value."""
        v = dpi - 1
        body = bytes([v & 0xFF, (v >> 8) & 0xFF, v & 0xFF, (v >> 8) & 0xFF, 0x00])
        return body + bytes([(0x55 - sum(body)) & 0xFF])

    @staticmethod
    def _dpi_decode(record: bytes) -> int:
        return (record[0] | (record[1] << 8)) + 1

    def get_dpi_stages(self, profile: int) -> dict:
        self._ensure_profile(profile)
        count = self._mem.get(ADDR_DPI_STAGE_COUNT, self.capabilities.max_dpi_stages)
        count = max(1, min(count, self.capabilities.max_dpi_stages))
        blob = self._mem_read_range(ADDR_DPI_TABLE, DPI_RECORD_SIZE * count)
        stages = []
        for i in range(count):
            rec = blob[i * DPI_RECORD_SIZE:(i + 1) * DPI_RECORD_SIZE]
            stages.append(((rec[0] | (rec[1] << 8)) + 1,
                           (rec[2] | (rec[3] << 8)) + 1))
        return {'active': self._mem.get(ADDR_ACTIVE_DPI_STAGE, 0) + 1,
                'count': count, 'stages': stages}

    def set_dpi_stages(self, stages: list[int], active: int, profile: int) -> None:
        caps = self.capabilities
        if not 1 <= len(stages) <= caps.max_dpi_stages:
            raise ValueError(f"Must have 1–{caps.max_dpi_stages} DPI stages")
        if not 1 <= active <= len(stages):
            raise ValueError(f"Active stage must be 1–{len(stages)}")
        self._ensure_profile(profile)
        for i, dpi in enumerate(stages):
            if not caps.dpi_min <= dpi <= caps.dpi_max:
                raise ValueError(f"DPI {dpi} out of range {caps.dpi_min}–{caps.dpi_max}")
            record = self._dpi_encode(dpi)
            base = ADDR_DPI_TABLE + i * DPI_RECORD_SIZE
            self._mem_write({base + j: record[j] for j in range(DPI_RECORD_SIZE)})
        self._write_value(ADDR_DPI_STAGE_COUNT, len(stages))
        self._write_value(ADDR_ACTIVE_DPI_STAGE, active - 1)

    # mm -> code, from the line described at the top of this module.
    _LOD_CODES = {round(_LOD_MM_OFFSET + _LOD_STEP_MM * code, 1): code
                  for code in range(1, 15)}

    # ── Fan ──────────────────────────────────────────────────────────────

    def get_fan_mode(self, profile=None) -> int:
        """Fan level, 0 (off) to 4 (maximum), of `profile`.

        Read straight from the device rather than the cache: _mem_read_all()
        stops at 0xC0 and this register lives past it.
        """
        self._tunable_profile(profile)
        return self._mem_read_range(ADDR_FAN_MODE, 1)[0]

    def set_fan_mode(self, level: int, profile=None) -> None:
        lo, hi = self.capabilities.fan_range
        if not lo <= level <= hi:
            raise ValueError(f"Fan mode must be {lo}–{hi}")
        self._tunable_profile(profile)
        self._write_value(ADDR_FAN_MODE, level)

    # ── Low power threshold ──────────────────────────────────────────────

    def get_low_power_threshold(self, profile=None) -> int:
        """Battery percentage at which the mouse enters low power mode.

        Per profile, on the evidence that its neighbour 0x00E7 is; not
        separately confirmed.
        """
        self._tunable_profile(profile)
        return self._mem_read_range(ADDR_LOW_POWER_PERCENT, 1)[0]

    def set_low_power_threshold(self, percent: int, profile=None) -> None:
        if not 0 <= percent <= 100:
            raise ValueError("Low power threshold must be 0–100")
        self._tunable_profile(profile)
        self._write_value(ADDR_LOW_POWER_PERCENT, percent)
