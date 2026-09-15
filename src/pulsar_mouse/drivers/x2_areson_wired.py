"""Pulsar X2 Wireless (Areson IDs) over its USB cable.

The same mouse as ``x2_areson_wireless``, which identifies itself with a
different PID when cabled.  All the protocol differences live in
``_AresonMixin``; see that module for the notes and test coverage.
"""

from dataclasses import replace

from pulsar_mouse.drivers.x2_crazylight_wired import PulsarX2CrazyLightWired
from pulsar_mouse.drivers.x2_areson_wireless import (
    PulsarX2AresonWireless,
    _AresonMixin,
)


class PulsarX2AresonWired(_AresonMixin, PulsarX2CrazyLightWired):
    """Driver for the Areson X2 Wireless mouse over USB cable."""

    capabilities = replace(
        PulsarX2CrazyLightWired.capabilities,
        name='Pulsar X2 Wireless (Areson cable)',
        vid_pid_pairs=[(0x25A7, 0xFA7B)],
        dpi_min=50,
        dpi_max=26000,
        dpi_step=50,
        # Inherited from the X2 CrazyLight, but not seen on this model.
        has_turbo=False,
        power_saving_range=None,
        buttons=PulsarX2AresonWireless.capabilities.buttons,
        button_labels=PulsarX2AresonWireless.capabilities.button_labels,
    )
