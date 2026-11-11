"""
Pulsar X3 LHD CrazyLight Medium on its USB-C cable (3710:3527) — protocol
driver.

The left-handed X3 in Pulsar's CrazyLight line, under its cabled ID.  Its
strings say it plainly — "Pulsar X3 LHD CrazyLight Medium", iProduct
"X3 LHD CrazyLight Medium" — and it is a CrazyLight mouse, so the protocol
is the X2 CrazyLight's Nordic 17-byte one under the Sonix VID (see
x2_crazylight.py), not the Sonix 64-byte Feature Report protocol the X3 LHD
on 3710:3410 speaks.  That driver would send Feature Reports at a mouse
that answers none of them; this one is x2_crazylight with the cabled ID and
the name the hardware carries.

One identity on both connections: the same cid 0x57 / mid 0x68 pair comes
back through the 5406 dongle and over the cable, which is what lets
x2_crazylight.refine_capabilities() name the dongle's half and
find_sibling() pair the two without a model_key — a key is exactly two
drivers, and the 5406 is one receiver for a whole line (its key still names
the X2 CrazyLight pair).

Wired differs from the dongle in the one way it did on the X2 CrazyLight
(see x2_crazylight_wired.py): **polling tops out at 1 kHz on the cable.**
Measured here, not inherited: on its own cable the mouse enumerates as a
full-speed device (12 Mbit/s) with every endpoint at bInterval = 1 ms, so
1000 packets/s is the bus's hard ceiling while a profile still stores 2000
from the dongle's run (wiggle counting agreed — the cable never passed
860 reports/s across two runs, while the same mouse through the 480M
dongle passed 1100).
The rate is stored per profile and shared by both connections, so this
driver reports what the mouse actually runs here and declines to write
1 kHz over a stored higher rate — that write would change nothing on the
cable and silently cut the profile for the next time it's on the dongle.

USB:
    VID 0x3710
    PID 0x3527 (bcdDevice 0x0304, matching the "3.04" firmware it reports)
    Interface 1, IN endpoint 0x82, 17-byte reports, report ID 0x08
    (interface 0 boot keyboard, interface 2 boot mouse)

Confirmed on hardware (2026-11): identity over cable and dongle alike,
settings and stage tables read per profile over the cable, writes read
back (debounce, lift-off), 4 profiles (5 and 6 refused, status 0x01),
battery while charging — and the 5406 dongle answers "which mouse do you
carry" from its own pairing memory even while this mouse sits on the
cable, which is what lets find_sibling() pair them on a cold start.

The six buttons are the mouse's own, which was the one thing inherited
from the X2 CrazyLight on nothing but a shape argument.  All six read on
every profile and over both connections; all six are real buttons on this
left-handed body and behave as labelled, side front included.  A write
round-trip on thumb1 moved that slot's own four bytes and nothing else in
0x000-0x1ff, with the checksum recomputing as 0x55 minus the record and
the factory block restored byte for byte.

What it does *not* have is the right-hand flank: 0x78 and 0x7c are
thumb4 and thumb3 in the family table - the slots #30 is about - and this
driver offers neither.  They hold 02 02 and 02 03 on this mouse, the same
as the X2 CrazyLight and Xlite V3 dumps carry.  Through this family's own
tables that reads as DPI-change with actions 2 and 3, i.e. shaped exactly
like a dpi- and a dpiloop binding, so what those slots are *for* on a
six-button mouse is still unidentified rather than known to be firmware
data.  Not written here, which is the only part that matters.
"""

from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2_crazylight_wired import PulsarX2CrazyLightWired


class PulsarX3LHDCrazyLight(PulsarX2CrazyLightWired):
    """Driver for the X3 LHD CrazyLight Medium over USB-C."""

    capabilities: DeviceCapabilities = replace(
        PulsarX2CrazyLightWired.capabilities,
        name='Pulsar X3 LHD CrazyLight Medium Wired',
        vid_pid_pairs=[(0x3710, 0x3527)],
        wireless=False,
        # No key: the dongle that pairs with this mouse already carries one,
        # for the X2 CrazyLight, and a third driver on a key tells
        # check-drivers it names two mice.  A key of our own would have no
        # wireless half to name.  find_sibling() asks the dongle which mouse
        # it carries instead - see x2_crazylight._connected_mouse.
        model_key=None,
    )
