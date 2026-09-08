"""
Pulsar X2 CrazyLight (X2 CL) Wireless — protocol driver.

Despite the VID it shares with the Sonix mice (0x3710), the X2 CL dongle
speaks the **Nordic 17-byte protocol**, not the 64-byte Sonix Feature Report
protocol — which is why the x2a / xlite_v4 / feinmann8k drivers return
garbage for it.  It therefore inherits everything from PulsarNordic and only
overrides the model-specific layout.

USB:
    VID 0x3710
    PID 0x5406 (2.4 GHz dongle)
    Interface 1, IN endpoint 0x82, 17-byte reports, report ID 0x08

Protocol decoded from a Pulsar Fusion USB capture contributed by
@iamtherobin in issue #7; see docs/protocol-x2-crazylight.md for the full
register map and the correlation method.

Differences from the X2A Wireless (PulsarNordic):

  * DPI is stored in 10 DPI steps, not 50 (0x27 -> 400 DPI).
  * Polling goes up to 8 kHz (0x10/0x20/0x40 for 2 K/4 K/8 K).
  * A third lift-off distance, 0.7 mm, stored as code 0x03.
  * A sixth button slot at 0x74, bound to DPI Loop by default.
  * Four onboard profiles rather than one.  The memory map is whichever
    profile is active, so reading or writing a different one switches first
    (command 0x0F) and reloads the map — the same thing Fusion does.

Status: UNTESTED on hardware.  Every register below was confirmed by matching
        each write in the capture against the Fusion UI frame at the same
        timestamp, but nothing has been written back to a real device yet.

Known gaps (need a follow-up capture — see issue #7):

  * DPI above 6400.  Fusion's own dialog gives the range as 10–32000 and
    the high-nibble encoding is understood (a 4-bit rotate left by 2), but
    above 6400 the low byte stops being (n & 0xFF): Fusion writes
    77 77 33 for 32000 where the rule predicts 7f 7f 33, and 17 17 66 for
    24000 against a predicted 5f 5f 66.  Writes are capped at 10240 DPI —
    the top of the verified range — and above that reads land within a few
    percent instead of returning nonsense.
  * Factory reset (command 0x09) was never issued by Fusion in either
    capture, so has_reset is False rather than guessing.
  * The wired half of the device (PID 0x3414) is still unmapped: the wired
    capture was taken on a root hub the mouse was not attached to.
"""

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.nordic import PulsarNordic


class PulsarX2CrazyLight(PulsarNordic):
    """Driver for the Pulsar X2 CrazyLight through its 2.4 GHz dongle."""

    capabilities: DeviceCapabilities = DeviceCapabilities(
        name='Pulsar X2 CrazyLight Wireless',
        vid_pid_pairs=[(0x3710, 0x5406)],
        interface_num=1,
        report_size=17,
        num_profiles=4,
        # Fusion showed a stage count of 4.  The memory map reserves eight
        # stage records (0x0C–0x2B) and eight colours (0x2C–0x4B), but only
        # four were ever in use, so stay with what the capture proves.
        max_dpi_stages=4,
        # Command 0x0A answers a profile switch with the count: 0x04.
        dpi_min=10,
        dpi_max=10240,
        dpi_step=10,
        buttons={
            'left':   0x01,
            'right':  0x02,
            'wheel':  0x03,
            'thumb1': 0x04,   # side front (default: forward)
            'thumb2': 0x05,   # side back  (default: backward)
            'dpi':    0x0b,   # default: DPI Loop
        },
        polling_rates=[125, 250, 500, 1000, 2000, 4000, 8000],
        lod_values=[0.7, 1, 2],
        has_led=True,
        has_breathe_speed=True,
        brightness_range=(0x10, 0xFF),
        breathe_speed_range=(1, 5),
        has_angle_snap=True,
        has_ripple_control=True,
        has_motion_sync=True,
        has_debounce=True,
        debounce_range=(0, 20),
        has_stage_colors=True,
        has_reset=False,
        button_labels={
            'left': 'Left Click',
            'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Front (forward)',
            'thumb2': 'Side Back (backward)',
            'dpi': 'DPI Button',
        },
    )

    # Slot order in the button table at 0x60, one 4-byte record each.
    # Defaults in the capture: 0x60 (01,01) left, 0x64 (01,02) right,
    # 0x68 (01,04) wheel, 0x6C (01,08) back, 0x70 (01,10) forward,
    # 0x74 (02,01) DPI loop.
    _BUTTON_ADDRS = {
        'left':   0x60,
        'right':  0x64,
        'wheel':  0x68,
        'thumb2': 0x6C,
        'thumb1': 0x70,
        'dpi':    0x74,
    }

    # 0.7 mm is stored as 0x03 — it was added after 1 mm and 2 mm, so the
    # codes are not in physical order.
    _LOD_CODES = {1: 0x01, 2: 0x02, 0.7: 0x03}
