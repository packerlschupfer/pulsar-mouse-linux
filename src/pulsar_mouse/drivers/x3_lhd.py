"""
Pulsar X3 LHD on its cable (3710:3410) — protocol driver.

The left-handed X3.  Everything in x3.py applies: the same Fusion2 mouse,
the same registers, the same six profiles and the same lift-off encoding —
only the USB ID and the name differ, and the name is the point of this
module.  The mouse enumerates as "Pulsar X3 LHD", and a driver that told its
owner they had a "Pulsar X3 Wired" would be the mistake this project spent
#24, #30 and #34 undoing.

It is also what the 1K and 8K dongles' refined name is built from, so an LHD
behind one reads "Pulsar X3 LHD (8K dongle)" rather than the other model's
name (see xlite_v4_wireless.refine_capabilities).

Confirmed on hardware by @jonkristian, on its cable and through the 8K
dongle (#37): settings, stage tables for all six profiles, lift-off at
1.0 mm, and battery while charging.
"""

from dataclasses import replace

from pulsar_mouse.drivers.x3 import PulsarX3


class PulsarX3LHD(PulsarX3):
    """Driver for the left-handed Pulsar X3 on its cable."""

    capabilities = replace(
        PulsarX3.capabilities,
        name='Pulsar X3 LHD',
        vid_pid_pairs=[(0x3710, 0x3410)],
    )
