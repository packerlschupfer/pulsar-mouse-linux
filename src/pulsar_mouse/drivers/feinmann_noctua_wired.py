"""
Pulsar Feinmann F01 Noctua Edition over USB-C — protocol driver.

Same mouse as feinmann_noctua.py, reached through its cable instead of its
dongle, and so a separate PID and a separate module.  This is the half the
protocol was decoded from: the capture in issue #12 is this device, VID
0x3710 PID 0x7507, reporting firmware 5.09.

Unlike the X2 CrazyLight's wired half, the cable here does *not* cap the
polling rate: the owner reports 8 kHz working over USB-C, so this driver is
the dongle one with nothing but the PID and the name changed.
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
        wireless=False,
    )
