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

Battery reads (command 0x04) still answer, but the mouse is charging while
wired and Fusion shows a charging icon in place of a percentage.

Status: UNTESTED on hardware, like the dongle driver.
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2_crazylight import PulsarX2CrazyLight
from dataclasses import replace


class PulsarX2CrazyLightWired(PulsarX2CrazyLight):
    """Driver for the Pulsar X2 CrazyLight over USB-C."""

    capabilities: DeviceCapabilities = replace(
        PulsarX2CrazyLight.capabilities,
        name='Pulsar X2 CrazyLight Wired',
        vid_pid_pairs=[(0x3710, 0x3414)],
        polling_rates=[125, 250, 500, 1000],
    )
