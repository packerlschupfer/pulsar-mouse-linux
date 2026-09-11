"""
Pulsar X2 CrazyLight (X2 CL) Wired — protocol driver.

The same mouse as `x2_crazylight`, over its USB-C cable instead of the
2.4 GHz dongle.  Protocol, register map and profile handling are identical —
a capture of Fusion driving both at once shows the wired PID answering
exactly the same commands on the same interface — so this only narrows the
capabilities.

USB:
    VID 0x3710
    PID 0x3414 (bcdDevice 0x0305, matching the "Mouse v3.05" Fusion reports)
    Interface 1, IN endpoint 0x82, 17-byte reports, report ID 0x08

Wired differs from the dongle in one way: **polling tops out at 1 kHz.**
Fusion offers only 125/250/500/1K here, against 125 Hz–8 K over the dongle.

That matters more than it sounds, because the rate is stored per profile and
shared by both connections.  A profile set to 8 kHz over the dongle still
holds 8 kHz while cabled, and the mouse runs it at 1 kHz.  So this driver
reports the rate the mouse actually uses here, and declines to write 1 kHz
over a stored higher rate: that write would change nothing on the cable and
silently cut the profile back to 1 kHz for the next time it's on the dongle.
Any lower rate is written as asked — that one is a real change.

Battery reads (command 0x04) still answer, but the mouse is charging while
wired and Fusion shows a charging icon in place of a percentage.

Status: UNTESTED on hardware, like the dongle driver.
"""

from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.nordic import ADDR_POLLING_RATE, POLL_VAL_TO_HZ
from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight


class PulsarX2CrazyLightWired(PulsarX2CrazyLight):
    """Driver for the Pulsar X2 CrazyLight over USB-C."""

    capabilities: DeviceCapabilities = replace(
        PulsarX2CrazyLight.capabilities,
        name='Pulsar X2 CrazyLight Wired',
        vid_pid_pairs=[(0x3710, 0x3414)],
        polling_rates=[125, 250, 500, 1000],
        wireless=False,
    )

    _WIRED_MAX_HZ = 1000

    def _stored_polling_rate(self, profile=None) -> int:
        self._tunable_profile(profile)
        return POLL_VAL_TO_HZ.get(self._mem.get(ADDR_POLLING_RATE, 0x01), 1000)

    def get_polling_rate(self, profile=None) -> int:
        # What the mouse runs at on the cable, not what the profile stores
        # for the dongle.  Also keeps the value inside polling_rates, which
        # the GUI's dropdown can't represent otherwise.
        return min(self._stored_polling_rate(profile), self._WIRED_MAX_HZ)

    def set_polling_rate(self, hz: int, profile=None) -> None:
        if hz == self._WIRED_MAX_HZ and self._stored_polling_rate(profile) > hz:
            return   # already effectively 1 kHz here; keep the dongle's rate
        super().set_polling_rate(hz, profile)
