"""
Pulsar X2H Wired Medium — protocol driver.

Uses the same Sonix chipset and 64-byte HID Feature Report protocol
as the X2A.  VID 0x3710, PID 0x1403, Interface 3.

Note: The original X2H (non-v3) uses a Nordic chipset with VID 0x3554 and a
completely different 17-byte protocol — it is NOT supported by this driver.
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2a import PulsarX2A


class PulsarX2H(PulsarX2A):
    """Driver for the Pulsar X2H Wired Medium (Sonix chipset).

    Inherits the full X2A protocol — same 64-byte packets, same register
    addresses.  Only the USB PID and device name differ.
    """

    capabilities = DeviceCapabilities(
        name='Pulsar X2H Wired Medium',
        vid_pid_pairs=[(0x3710, 0x1403)],
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
            'thumb1': 0x04,   # left side front (default: forward)
            'thumb2': 0x05,   # left side back  (default: backward)
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
