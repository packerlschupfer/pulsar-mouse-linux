"""
Pulsar Xlite v4 Wireless — protocol driver.

The Xlite v4 1K wireless dongle uses the same 64-byte HID Feature
Report protocol as the existing X2A/Xlite v4 implementation.

USB:
    VID 0x3710
    PID 0x5402
    Interface 3
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2a import PulsarX2A


class PulsarXliteV4Wireless(PulsarX2A):
    """Driver for the Pulsar Xlite v4 through its 1K wireless dongle."""

    capabilities: DeviceCapabilities = DeviceCapabilities(
        name='Pulsar Xlite v4 Wireless',
        vid_pid_pairs=[(0x3710, 0x5402)],
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
            'thumb1': 0x04,
            'thumb2': 0x05,
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        button_labels={
            'left': 'Left Click',
            'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Front (forward)',
            'thumb2': 'Side Back (backward)',
            'dpi': 'DPI Button',
        },
    )
