"""
Pulsar Xlite Wired — protocol driver.

Uses the same Sonix chipset and 64-byte HID Feature Report protocol
as the X2A.  VID 0x3710, PID 0x1401, Interface 3.

The original Xlite Wired is a right-handed ergonomic mouse with 2 side
buttons plus a DPI button.  Uses 50 DPI steps (vs 100 for later models).
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2a import PulsarX2A


class PulsarXliteWired(PulsarX2A):
    """Driver for the Pulsar Xlite Wired mouse (Sonix chipset).

    Inherits the full X2A protocol — same 64-byte packets, same register
    addresses.  Only the USB PID, device name, and DPI step differ.
    """

    capabilities = DeviceCapabilities(
        name='Pulsar Xlite Wired',
        vid_pid_pairs=[(0x3710, 0x1401)],
        interface_num=3,
        report_size=64,
        num_profiles=5,
        max_dpi_stages=6,
        dpi_min=100,
        dpi_max=26000,
        dpi_step=50,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,
            'thumb2': 0x05,
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        button_labels={
            'left': 'Left Click', 'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Back (backward)', 'thumb2': 'Side Front (forward)',
            'dpi': 'DPI Button',
        },
    )
