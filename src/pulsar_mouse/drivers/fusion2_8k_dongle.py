"""
Pulsar 8K wireless dongle (3710:5403) carrying a Fusion2 mouse.

The 8K dongle (read here with an X3 LHD on it) speaks the same protocol as the
1K dongle (xlite_v4_wireless.py), so this is that driver under the 8K
dongle's own ID and name.

One difference: it answers "which mouse is on the dongle" (01/84/04) with
the right payload under the *previous* request's header.  After a firmware
read (01/87/04) it sends `01 01 87 04 00 00 10 37 10 34` for an X3 LHD
(3710:3410), so _echoes() never accepts it and the read timed out.  Every
other request read through it echoes as on the 1K dongle.

It also goes past the 1K dongle's 1000 Hz.  01/09/02 keeps doubling: 0x10,
0x20 and 0x40 read back as written, and an X3 LHD moved in circles sent 1999,
3988 and 7200 reports a second on the dongle's mouse interface (999 at 0x08).

USB:
    VID 0x3710
    PID 0x5403
    Interface 3

Confirmed on hardware with a Pulsar X3 LHD (2026-10).
"""

import struct
import time
from dataclasses import replace

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.x2a import POLL_HZ_TO_VAL
from pulsar_mouse.drivers.xlite_v4_wireless import (
    PulsarXliteV4Wireless, _cabled_name,
)

POLL_HZ_TO_VAL_8K = {**POLL_HZ_TO_VAL, 2000: 0x10, 4000: 0x20, 8000: 0x40}
POLL_VAL_TO_HZ_8K = {v: k for k, v in POLL_HZ_TO_VAL_8K.items()}


class PulsarFusion28KDongle(PulsarXliteV4Wireless):
    """Driver for Pulsar Fusion2 mice through the 8K wireless dongle."""

    capabilities: DeviceCapabilities = replace(
        PulsarXliteV4Wireless.capabilities,
        name='Pulsar 8K Wireless Dongle',
        vid_pid_pairs=[(0x3710, 0x5403)],
        polling_rates=sorted(POLL_HZ_TO_VAL_8K),
    )

    _DONGLE_LABEL = '8K dongle'

    def get_polling_rate(self) -> int:
        rsp = self._read(0x01, 0x09, 0x02, 0x00)
        return POLL_VAL_TO_HZ_8K.get(rsp[7], rsp[7] * 125)

    def set_polling_rate(self, hz: int) -> None:
        val = POLL_HZ_TO_VAL_8K.get(hz)
        if val is None:
            raise ValueError(f"Polling rate must be one of {sorted(POLL_HZ_TO_VAL_8K)}")
        self._cmd(0x01, 0x09, 0x02, 0x00, [val])

    def get_connected_mouse(self) -> dict:
        # Same request as the 1K dongle's; take the first done reply that
        # carries a Pulsar VID instead of one that echoes the request.
        self._set_report(self._build_read(0x01, 0x04, 0x04, 0x00))
        deadline = time.monotonic() + self._REPLY_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(self._REPLY_POLL)
            rsp = self._get_report()
            vid, pid = struct.unpack_from('<HH', rsp, 6)
            if rsp[0] == 0x01 and vid == 0x3710:
                # The reply doesn't echo this request, so don't let the
                # next one treat it as the previous request's answer.
                self._last_sent = None
                return {'vid': vid, 'pid': pid, 'name': _cabled_name(vid, pid)}
        raise IOError("no reply from the mouse to cat=0x01 reg=0x84 sub=0x04 "
                      f"within {self._REPLY_TIMEOUT * 1000:.0f} ms "
                      "(is the mouse awake and paired?)")
