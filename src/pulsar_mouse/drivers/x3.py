"""
Pulsar X3 on its cable (3710:3409, LHD 3710:3410) — protocol driver.

The X3 is a Fusion2 mouse: the same Sonix 64-byte Feature Report protocol
as the X2A and Xlite v4, on interface 3, and the mouse the 1K dongle driver
(xlite_v4_wireless.py) was confirmed on.  Plugged in by its cable it
enumerates under its own ID and answers for itself, so this is the Xlite v4
driver with the X3's ID, name, button labels, six profiles and DPI range,
plus the Fusion2 power category: on the cable the mouse reports its own
battery and charging state and holds the sleep timer and Low Power Mode, so
the GUI can show it filling up while it charges.  Link quality and "which
mouse is on the dongle" are the dongle's business and stay there.

This driver has no model_key.  Its dongle, 0x5402, is shared across the
Fusion2 line, so a static pairing would be wrong for anyone whose dongle
carries another model; find_sibling() instead asks the dongle which mouse
it carries and matches the answer against this driver's ID.

USB:
    VID 0x3710
    PID 0x3409 (RHD), 0x3410 (LHD)
    Interface 3

Confirmed on hardware with a Pulsar X3 on its cable (2026-10), and the
LHD both on its cable and through its 8K dongle (fusion2_8k_dongle.py).
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.xlite_v4 import PulsarXliteV4
from pulsar_mouse.drivers.xlite_v4_wireless import Fusion2Power


class PulsarX3(Fusion2Power, PulsarXliteV4):
    """Driver for the Pulsar X3 on its cable (Sonix chipset)."""

    capabilities = DeviceCapabilities(
        name='Pulsar X3 Wired',
        vid_pid_pairs=[(0x3710, 0x3409), (0x3710, 0x3410)],
        interface_num=3,
        report_size=64,
        # The profile list (02/82/10) answers 06 01 02 03 04 05 06, and the
        # mouse switches to each of them, on the cable and through the 1K
        # dongle.  The X2A lists five (#24).
        num_profiles=6,
        max_dpi_stages=6,
        dpi_min=100,
        # Pulsar's X3 spec sheet: "Resolution (DPI) 32000", "Adjustable DPI
        # in 10-unit".  The mouse keeps exactly what it is given, with no
        # rounding to 10 or 100: 31990, 32000, 12345, 12340 and 26010 all
        # read back unchanged, on the cable and through the 1K dongle.
        dpi_max=32000,
        dpi_step=10,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,   # side, rear  (default: backward)
            'thumb2': 0x05,   # side, front (default: forward)
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        # Fusion2's three buttons for this mouse - see get_lod() below.
        lod_values=[0.7, 1, 2],
        button_labels={
            'left': 'Left Click',
            'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Back (backward)',
            'thumb2': 'Side Front (forward)',
            'dpi': 'DPI Button',
        },
        wireless=False,
    )

    # The X3 numbers its DPI stages from one in the event report, where the
    # X2A numbers them from zero: @f-o-o-g-s pressed its button six times
    # on the cable and six through the dongle and got 0x01..0x06 for
    # 400/800/1600/3200/6400/12800 both ways (#17).  Without this the tray
    # and Input Test showed the next stage up, and the sixth press showed a
    # stage the mouse does not have.
    _DPI_EVENT_FIRST_STAGE = 1

    # Its lift-off distance is not the X2A's single index but a mode and a
    # distance in tenths of a millimetre, with the three settings Fusion2
    # offers - see Fusion2LodTenths, which the 1K dongle shares when it is
    # carrying this mouse (#24).
