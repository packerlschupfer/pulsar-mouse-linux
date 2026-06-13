"""
Pulsar Xlite v4 — protocol driver.

The Xlite v4 uses the same Sonix chipset and 64-byte HID Feature Report protocol
as the X2A.  VID 0x3710, PID 0x3401, Interface 3.

The Xlite v4 is a right-handed ergonomic mouse (EC-clone shape) with 2 side
buttons on the left side plus a dedicated sniper/DPI button below the scroll
wheel (6 total buttons).

Status: UNTESTED — protocol assumed identical to X2A based on shared Sonix
        chipset (VID 0x3710).  Button IDs may need adjustment once tested
        with real hardware.
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2a import PulsarX2A


class PulsarXliteV4(PulsarX2A):
    """Driver for the Pulsar Xlite v4 mouse (Sonix chipset).

    Inherits the full X2A protocol — same 64-byte packets, same register
    addresses.  Right-handed ergonomic shape with 2 side buttons.
    """

    capabilities = DeviceCapabilities(
        name='Pulsar Xlite v4',
        vid_pid_pairs=[(0x3710, 0x3401)],
        interface_num=3,
        report_size=64,
        num_profiles=5,
        max_dpi_stages=6,
        dpi_min=100,
        dpi_max=26000,
        dpi_step=100,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,   # side front (default: forward)
            'thumb2': 0x05,   # side back  (default: backward)
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        button_labels={
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Front (forward)', 'thumb2': 'Side Back (backward)',
            'dpi': 'DPI Button',
        },
    )
