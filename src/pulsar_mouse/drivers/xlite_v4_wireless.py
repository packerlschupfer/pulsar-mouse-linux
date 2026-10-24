"""
Pulsar 1K wireless dongle (3710:5402) — protocol driver.

Pulsar Fusion2 drives this dongle with the same 64-byte HID Feature Report
protocol as the X2A / Xlite v4 (see x2a.py), so everything the Xlite v4
driver does works through it unchanged.  Added here is what only the
dongle has: the power category — battery, charging, the sleep timer and
the Low Power Mode threshold, the same four registers feinmann8k.py reads
on the 8K dongle — and a register that reports which mouse the dongle is
paired with, plus the once-a-second link-quality report the dongle pushes
on its event interface.

The dongle is not specific to the Xlite v4.  Pulsar ships it with the
rest of the Fusion2 line, and the one this was confirmed on carries an X3
(wired PID 0x3409).  get_connected_mouse() is how to tell them apart, so
this driver deliberately has no model_key: pairing 0x5402 with one cabled
PID would have the app follow a swap onto the wrong mouse for everyone
whose dongle carries a different model (issue #11).

USB:
    VID 0x3710
    PID 0x5402
    Interface 3

Power and identity registers decoded from USBPcap captures of Fusion2
1.0.5.4 on Windows, then confirmed live on Linux with a Pulsar X3 on the
dongle (2026-10).
"""

import struct
import time
from typing import Optional

from pulsar_mouse.base import DeviceCapabilities
from pulsar_mouse.drivers.xlite_v4 import PulsarXliteV4

# Cabled PIDs the dongle has been seen reporting through get_connected_mouse()
# for mice that have no cabled driver here yet; a mouse that has one is named
# after that driver instead.  Fusion2 covers more models than these; add a
# line once a dongle has been seen naming it.
CONNECTED_MOUSE_NAMES = {
    0x3401: 'Pulsar Xlite v4',   # xlite_v4.py's cabled PID
    0x3409: 'Pulsar X3',         # x3.py's; confirmed on hardware
}


def _cabled_name(vid: int, pid: int) -> Optional[str]:
    from pulsar_mouse.drivers import discover_all   # not at import: it imports us
    for cls in discover_all().values():
        caps = cls.capabilities
        if (vid, pid) in caps.vid_pid_pairs and caps.wireless is not True:
            return caps.name
    return CONNECTED_MOUSE_NAMES.get(pid)


class Fusion2Power:
    """The power category of the Fusion2 line: cat 0x08 on the Sonix protocol.

    Shared by the 1K dongle, which relays the questions to whichever mouse it
    carries, and by a Fusion2 mouse on its cable, which answers them itself
    (confirmed with a Pulsar X3: 87% and charging, on the cable, in 1 ms).
    Mixed into a PulsarX2A-family driver; needs only its _read()/_cmd().
    """

    # ── Power ─────────────────────────────────────────────────────────────

    def get_power(self) -> dict:
        # cat=0x08/reg=0x81(=0x01|0x80)/sub=0x01, profile 0, no payload:
        # reply byte[6] is the battery percentage, as on the 8K dongle.
        # The dongle answers 0xFF when it has no mouse to ask; the mouse on
        # its cable answers for itself.
        pct = self._read(0x08, 0x01, 0x01, 0x00)[6]
        if pct == 0xFF:
            raise IOError('no battery reading — is the mouse awake and paired?')
        # cat=0x08/reg=0x83(=0x03|0x80)/sub=0x01: byte[6] is 1 while the
        # mouse is on its charging cable, 0 otherwise.  Both states seen in
        # the capture, where the flag drops as the cable comes out.
        charging = bool(self._read(0x08, 0x03, 0x01, 0x00)[6])
        return {
            'battery_percent': pct,
            'power_connected': charging,
            'battery_mv': None,
        }

    # ── Pushed power reports ──────────────────────────────────────────────

    def parse_hidraw_event(self, data: bytes) -> Optional[dict]:
        """`05 07 <charging> <percent> 01`, sent without being asked.

        The same three fields get_power() asks for, arriving on their own:
        byte 2 is the charging flag and byte 3 the percentage, plain hex,
        not BCD.  @f-o-o-g-s caught it twice - in a fresh hidraw log where
        the dongle sent `05 07 00 50 01` (80%) just after the mouse
        reconnected and the mouse on its cable sent `05 07 01 51 01` (81%,
        charging), and again in USBPcap captures from a year earlier, where
        0x54 and 0x53 matched the battery register read in the same capture
        (#21).  Both percentages matched what get_power() said at the time,
        and the flag matched which end the cable was in.

        Used alongside the minute-long battery poll rather than instead of
        it: nobody knows yet how often the mouse volunteers this, and a
        reading that silently stops updating is worse than one that is up
        to a minute old.
        """
        if len(data) >= 4 and data[0] == 0x05 and data[1] == 0x07:
            return {
                'battery_percent': data[3],
                'power_connected': bool(data[2]),
                'battery_mv': None,
            }
        return super().parse_hidraw_event(data)

    def get_power_saving_timeout(self) -> int:
        # cat=0x08/reg=0x85(=0x05|0x80)/sub=0x03, no payload: reply bytes
        # [7:9] are the sleep timer in seconds, uint16 little-endian.  In the
        # capture Fusion2's slider produced 30, 60, 120, 180 and 300 s here,
        # each byte-exact between the write and the read that followed it.
        # Stored per profile, as on the 8K dongle: Fusion2 asks profile by
        # profile, and a write through profile 0 changed profile 1's copy
        # (the active one) and left profiles 2-5 alone.  Profile 0 is used
        # here as for the rest of this family's unprofiled settings.
        rsp = self._read(0x08, 0x05, 0x03, 0x00)
        return struct.unpack_from('<H', rsp, 7)[0]

    def set_power_saving_timeout(self, seconds: int) -> None:
        # cat=0x08/reg=0x05/sub=0x03, payload uint16 LE seconds, the same
        # layout the read returns.  Range from Fusion2's slider (30 s to
        # 15 min), as on the 8K dongle.
        lo, hi, _step = self.capabilities.power_saving_range
        if not lo <= seconds <= hi:
            raise ValueError(f"Power-saving timeout must be {lo}-{hi} seconds")
        self._cmd(0x08, 0x05, 0x03, 0x00, list(struct.pack('<H', seconds)))

    def get_low_power_threshold(self) -> int:
        # cat=0x08/reg=0x88(=0x08|0x80)/sub=0x02, no payload: reply byte[7]
        # is the Low Power Mode percentage.  Fusion2 wrote 5, 6, 10, 14, 15,
        # 25, 26 and 47 here while its slider was dragged, so the byte is
        # the percentage itself and not an index into a table.  Per profile
        # and addressed through profile 0, like the sleep timer above.
        return self._read(0x08, 0x08, 0x02, 0x00)[7]

    def set_low_power_threshold(self, percent: int) -> None:
        # cat=0x08/reg=0x08/sub=0x02, payload [percent].
        if not 0 <= percent <= 100:
            raise ValueError("Low power threshold must be 0-100")
        self._cmd(0x08, 0x08, 0x02, 0x00, [percent])


class PulsarXliteV4Wireless(Fusion2Power, PulsarXliteV4):
    """Driver for Pulsar Fusion2 mice through the 1K wireless dongle."""

    capabilities: DeviceCapabilities = DeviceCapabilities(
        name='Pulsar 1K Wireless Dongle',
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
            'thumb1': 0x04,   # side, rear  (default: backward)
            'thumb2': 0x05,   # side, front (default: forward)
            'dpi':    0x0b,
        },
        polling_rates=[125, 250, 500, 1000],
        lod_values=[1, 2],
        button_labels={
            'left': 'Left Click',
            'right': 'Right Click',
            'wheel': 'Wheel Click',
            'thumb1': 'Side Back (backward)',
            'thumb2': 'Side Front (forward)',
            'dpi': 'DPI Button',
        },
        wireless=True,
    )

    # ── Transport ─────────────────────────────────────────────────────────
    #
    # Every command here is a round trip over the air, and the dongle says
    # so: after a SET_REPORT, GET_REPORT returns the request echoed with
    # byte[0]=0x02 until the mouse has answered, then the answer (or, for a
    # write, the written bytes echoed back) with byte[0]=0x01.  x2a.py's
    # helpers were written for a cabled mouse and take the 0x02 as the end
    # of the exchange: _cmd() returns on it at once and _read() gives up
    # after 160 ms.  Through the dongle that loses data.  A second
    # SET_REPORT sent while the first is still pending is dropped, the
    # dongle then stays busy for longer than _read() waits, and the reply
    # that eventually lands is latched and handed to whichever read comes
    # next.  Seen on hardware while testing the power setters below: a Low
    # Power Mode write silently not taken, and the battery reported as 1%
    # because byte[6] of a latched reply for another register, its profile
    # byte, was read as the percentage.
    #
    # Fusion2 in the captures never sends the next command until the 0x01
    # is in hand, re-polling the same pending request every 15-30 ms, and
    # the 8K dongle driver does the same.  So do that here, for writes as
    # well as reads: accept only a 0x01 whose category and register echo
    # the request, and whose profile byte echoes a per-profile request
    # (profile 0 replies carry data there instead).  The sub-register is
    # not compared: get_dpi_stages() asks with sub 0x15 and is answered
    # with 0x21 on this family too.  Answers arrived on the second poll,
    # 11 ms after the request, for every register exercised.  The deadline
    # is for a reply that never comes; a mouse that is asleep is told apart
    # sooner, by _NO_LINK below.

    _REPLY_TIMEOUT = 1.0
    _REPLY_POLL = 0.01

    # A mouse that is asleep, or not back on the dongle yet, isn't slow to
    # answer.  The dongle says it has no link to it, with byte[0]=0x05 and
    # the request echoed, on the first poll, and a request that got one has
    # never been answered.  With an X3 asleep, a polling-rate read got 05
    # on all 5606 polls over 90 s, and on all 286 more over the 3 s after
    # the mouse woke and its reports were coming through the dongle again;
    # the same read sent again was answered in 33 ms.  A LOD write held the
    # same way stayed on 05 through the wake too, and the mouse never took
    # it.  In every log that kept each poll, none of 100 answered exchanges
    # had a 05 before its 0x01, and every 05 came on the first poll (#21).
    # So a 05 that echoes the request ends it there, for writes as well as
    # reads.  One that echoes some other request is left to the deadline
    # like any other reply that isn't ours: the reply buffer belongs to the
    # dongle, not to this process, and another program's battery read has
    # been seen sitting in it with a 05 of its own.
    _NO_LINK = 0x05

    def _transact(self, cmd: bytes) -> bytes:
        self._set_report(cmd)
        deadline = time.monotonic() + self._REPLY_TIMEOUT
        while True:
            rsp = self._get_report()
            if rsp[1:3] == cmd[1:3] and (cmd[6] == 0x00 or rsp[6] == cmd[6]):
                if rsp[0] == 0x01:
                    return rsp
                if rsp[0] == self._NO_LINK:
                    raise IOError(
                        f"no link from the dongle to the mouse for "
                        f"cat=0x{cmd[1]:02x} reg=0x{cmd[2]:02x} "
                        f"sub=0x{cmd[3]:02x} (status byte 0x{rsp[0]:02x}; "
                        f"is the mouse awake and paired?)")
            if time.monotonic() >= deadline:
                raise IOError(
                    f"no reply from the mouse to cat=0x{cmd[1]:02x} "
                    f"reg=0x{cmd[2]:02x} sub=0x{cmd[3]:02x} within "
                    f"{self._REPLY_TIMEOUT * 1000:.0f} ms (last status byte "
                    f"0x{rsp[0]:02x}; is the mouse awake and paired?)")
            time.sleep(self._REPLY_POLL)

    def _cmd(self, cat, reg, sub, profile, payload=()):
        return self._transact(self._build(cat, reg, sub, profile, payload))

    def _read(self, cat, reg, sub, profile, payload=()):
        return self._transact(self._build_read(cat, reg, sub, profile, payload))

    # ── Identity ──────────────────────────────────────────────────────────

    def get_connected_mouse(self) -> dict:
        """Return {'vid', 'pid', 'name'} for the mouse behind the dongle.

        The dongle shares one USB identity across the whole Fusion2 line,
        so this is the only way to learn which mouse is actually on it.
        'vid'/'pid' are the IDs the mouse enumerates with on a cable, which
        is what find_sibling() matches a cabled driver on; 'name' is that
        driver's, or a known name for a mouse without one, or None.
        """
        # cat=0x01/reg=0x84(=0x04|0x80)/sub=0x04, profile 0, no payload:
        # reply bytes [6:8] and [8:10] are the paired mouse's VID and PID,
        # little-endian (10 37 09 34 -> 3710:3409 in the capture).
        rsp = self._read(0x01, 0x04, 0x04, 0x00)
        vid, pid = struct.unpack_from('<HH', rsp, 6)
        return {'vid': vid, 'pid': pid, 'name': _cabled_name(vid, pid)}

    # ── Hidraw events ─────────────────────────────────────────────────────

    # The dongle passes the mouse's own DPI report straight through, so the
    # stage numbering is the mouse's, not the dongle's.  @f-o-o-g-s saw the
    # same 0x01..0x06 through this dongle as on the X3's cable (#17), and
    # the X3 is the only mouse confirmed behind it.  If a Fusion2 mouse
    # turns up that counts from zero, this can't stay a constant - the
    # dongle would have to ask get_connected_mouse() who it is carrying.
    _DPI_EVENT_FIRST_STAGE = 1

    def parse_hidraw_event(self, data: bytes) -> Optional[dict]:
        # Once a second the dongle pushes `05 0d <value>` on its event
        # interface, the same report the 8K dongle sends (see feinmann8k.py).
        # Seen holding 100 with the mouse next to the dongle while the
        # battery read 88, so it is link quality, not a second battery
        # channel.
        if len(data) >= 3 and data[0] == 0x05 and data[1] == 0x0d:
            return {'signal_percent': data[2]}
        return super().parse_hidraw_event(data)
