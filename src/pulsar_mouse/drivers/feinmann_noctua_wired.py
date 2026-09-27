"""
Pulsar Feinmann F01 Noctua Edition over USB-C — protocol driver.

Same mouse as feinmann_noctua.py, reached through its cable instead of its
dongle, and so a separate PID and a separate module.  This is the half the
protocol was decoded from: the capture in issue #12 is this device, VID
0x3710 PID 0x7507, reporting firmware 5.09.

The cable caps the polling rate the link will carry, the same way the X2
CrazyLight's wired half does.
"""

from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.feinmann_noctua import PulsarFeinmannNoctua


class PulsarFeinmannNoctuaWired(PulsarFeinmannNoctua):
    """Driver for the Feinmann F01 Noctua Edition over USB-C."""

    capabilities: DeviceCapabilities = replace(
        PulsarFeinmannNoctua.capabilities,
        name='Feinmann F01 Noctua Edition Wired',
        vid_pid_pairs=[(0x3710, 0x7507)],
        polling_rates=[125, 250, 500, 1000],
        wireless=False,
    )

    _WIRED_MAX_HZ = 1000

    def get_polling_rate(self, profile=None) -> int:
        # What the mouse runs at on the cable, not what the profile stores
        # for the dongle.  Also keeps the value inside polling_rates, which
        # the GUI's dropdown can't represent otherwise.  The captured device
        # held 0x40 (8 kHz) in its profile while plugged in.
        return min(self.get_stored_polling_rate(profile), self._WIRED_MAX_HZ)

    def set_polling_rate(self, hz: int, profile=None) -> None:
        if hz == self._WIRED_MAX_HZ and self.get_stored_polling_rate(profile) > hz:
            return   # already effectively 1 kHz here; keep the dongle's rate
        super().set_polling_rate(hz, profile)
