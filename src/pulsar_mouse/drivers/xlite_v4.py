"""
Pulsar Xlite v4 — protocol driver.

The Xlite v4 uses the same Sonix chipset and 64-byte HID Feature Report protocol
as the X2A.  VID 0x3710, PID 0x3401, Interface 3.

The Xlite v4 is a right-handed ergonomic mouse (EC-clone shape) with 2 side
buttons on the left side plus a dedicated sniper/DPI button below the scroll
wheel (6 total buttons).

Status: partly measured.  @variasjon read its lift-off register and its
        profile list on both connections (#34); the rest is still assumed
        identical to the X2A on the shared Sonix chipset (VID 0x3710).  Button IDs may need adjustment once tested
        with real hardware.  So does _DPI_EVENT_FIRST_STAGE, inherited as 0
        from the X2A: the two Fusion2 mice that have been measured, the X3
        on its cable and on the 1K dongle, both count from 1 instead.  If
        this mouse's DPI button reports one stage too high, that is why.
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2a import Fusion2LodTenths, PulsarX2A


class PulsarXliteV4(Fusion2LodTenths, PulsarX2A):
    """Driver for the Pulsar Xlite v4 mouse (Sonix chipset).

    Inherits the full X2A protocol — same 64-byte packets, same register
    addresses.  Right-handed ergonomic shape with 2 side buttons.

    Lift-off is the one register it does not share: @variasjon read all six
    profiles of his through the 1K dongle and then over its cable, and the
    five he had never touched hold 02 0a either way - the mode-and-tenths
    shape, where x2a.py's table would call that 2 mm (#34).  So
    Fusion2LodTenths, as on the X3 - which also means the two choices this
    driver offers now write 02 0a and 02 14 instead of 00 00 and 01 01, the
    pairs it had been writing over the mouse's own.

    It has the X3's third setting too.  Pulsar's page for this mouse lists
    2, 1 and 0.7 mm, which on its own would be the same kind of evidence as
    the Dongle Kit pages that were declined here - copy-paste, no capture of
    Fusion2 driving this mouse - so the mouse was asked instead: @variasjon
    wrote 02 07 to one profile and read it back, through the dongle, and it
    held at 0.7 before being put back to its 1 mm (#34).  The register and
    its encoding belong to the mouse rather than the connection, which is
    why a read over the dongle settles the cabled driver as well.
    """

    capabilities = DeviceCapabilities(
        name='Pulsar Xlite v4',
        vid_pid_pairs=[(0x3710, 0x3401)],
        interface_num=3,
        report_size=64,
        # Six, read off the mouse: @variasjon's answers 02/82/10 with a
        # count of 6 and the IDs 1-6 after it, over its cable and through
        # the dongle alike (#34).  The 5 here before was x2a.py's, a
        # different mouse's.
        num_profiles=6,
        max_dpi_stages=6,
        dpi_min=100,
        # The XS-1's range, as the 1K dongle this mouse is sold with now
        # carries (#35): every mouse in that kit has that sensor, listed at
        # 32,000 DPI in 10-unit steps.  26000 in steps of 100 was the X2A
        # Wired's PAW3395, inherited from x2a.py, and it left the same mouse
        # with two different ceilings depending on which end it was reached
        # from - a stage set to 32000 through the dongle then read back on
        # the cable is a value the page cannot show, which since caebe1b
        # stops it being applied at all.
        dpi_max=32000,
        dpi_step=10,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            # Rear and front as measured on the X3 and the X2A (#24); not
            # checked on an Xlite v4.
            'thumb1': 0x04,   # side back  (default: backward)
            'thumb2': 0x05,   # side front (default: forward)
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[0.7, 1, 2],
        button_labels={
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Back (backward)', 'thumb2': 'Side Front (forward)',
            'dpi': 'DPI Button',
        },
    )
