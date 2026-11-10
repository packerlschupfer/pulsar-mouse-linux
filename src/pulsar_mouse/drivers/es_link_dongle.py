"""
Pulsar eS Xlite through its eS LINK dongle (3710:5602) - read-only so far.

Same report, framing and opcodes as the cable (es.py).  The dongle answers
0x0f itself, and without a mouse on the other end it answers `0e a5 ff` (not
ready) to everything it would have to pass on, so open() asks first rather
than spending every later read on the timeout.
"""

from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.es import OP_LINK, PulsarEs


class PulsarEsLinkDongle(PulsarEs):
    """Driver for the Pulsar eS Xlite through the eS LINK dongle."""

    capabilities: DeviceCapabilities = replace(
        PulsarEs.capabilities,
        name='Pulsar eS Xlite (eS LINK Dongle)',
        vid_pid_pairs=[(0x3710, 0x5602)],
        wireless=True,
    )

    def _after_open(self) -> None:
        if self._read(OP_LINK, 1)[0] == 0:
            raise IOError('the eS LINK dongle has no mouse connected: '
                          'the mouse is off, asleep, or on its cable')
