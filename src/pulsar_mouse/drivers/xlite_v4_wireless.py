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
from pulsar_mouse.drivers.x2a import (
    Fusion2LodTenths, LOD_TENTHS_PIDS, PulsarX2A,
)
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
        # Every mouse Pulsar sells this dongle for - its Dongle Kit page
        # names the Xlite v4, X2 v3, X2H v3, X2A v3 and X3 - has the XS-1
        # sensor, listed on each product page at 32,000 DPI, "Adjustable DPI
        # in 10-unit", and the X3 keeps 26010, 31990 and 32000 exactly
        # through it (x3.py).  26000 in steps of 100 is the X2A Wired's
        # PAW3395, from x2a.py, and was too low here: the GUI's stage rows
        # stop at dpi_max, so a stage above it set on the cable read back as
        # 26000 and any Apply through the dongle wrote that over it.
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

    # A request can also be lost after the dongle has taken it.  With the
    # mouse moving, 216 of 5155 exchanges on an X3 went that way: the reply
    # buffer showed the request pending (02), then, up to 147 ms in, went
    # back to an answer it had already given.  The 67 watched for a second
    # or more stayed there, never answered.  All 211 sent again, the same
    # frame, were answered after that one resend, 150 on the first poll
    # after it (about 11 ms), the slowest in 168 ms.  None was lost in 2295
    # exchanges with the mouse still.  A gap before each request does not
    # prevent it - 17 of 660 back to back, 20 of 660 with 10 ms, 14 of 660
    # with Fusion2's 30 ms - so it is recovered from here rather than
    # avoided.  Apply showed it first, after a LOD change (#24), but it is
    # not about the LOD: 10 kinds of write and 11 kinds of read were lost.
    #
    # Two things count as the buffer going back.  Any answer at all in
    # place of ours once the dongle has shown ours pending: 213 of the 216
    # did, and 35 of those went back further than the previous request,
    # past get_connected_mouse(), which the dongle answers itself (all 421
    # on the first poll, within 0.5 ms, where nothing from the mouse comes
    # back in under 10), to the request before it or to the last answer of
    # the open before.  And the previous request's answer even when ours
    # never showed, as the other 3 did, from the first poll.  An answer
    # that was there before ours showed, to anything else, is another
    # program's sitting in the buffer, and goes to the deadline as before
    # (#28).
    #
    # Not on the first sight of it: _STALE_POLLS polls in a row, which put
    # the resend 41-42 ms after the first such poll.  The exact number
    # matters little by the logs: none of the 7234 exchanges answered on
    # their first send showed an earlier answer at any poll, not even the
    # first, straight after the SET_REPORT, and none of the 67 losses
    # watched for a second showed ours again once it was gone.  So this is
    # margin against what has not been seen.  And not in the last
    # _STALE_RESEND_ROOM before the deadline: a resend there would be left
    # pending when this gives up, for the next request to be dropped behind
    # (#15); the slowest answer here took 336 ms.
    #
    # Writes too.  The risk #25 left alone on the cable is resending a
    # write the mouse did take.  Here a resend only goes out after the
    # dongle has stopped showing ours as pending (this never returns on 02)
    # and gone back to an answer it had already given, so it is no longer
    # working on ours; and the frame is the same byte for byte, so a write
    # that did land is written again with the value it already has.  58 of
    # the resent writes changed the LOD (1 mm <-> 2 mm), and all 58 read
    # back as written.
    #
    # A budget per request, not per open as #25's: on the cable the losses
    # all come at the start of an open, here at any point - 45 opens lost
    # two requests and 10 lost three or more, one of them six - so one budget
    # for the open would be spent by the first and fail the rest.  Each loss
    # so far took one resend; two are allowed, inside the same deadline.
    _STALE_POLLS = 5
    _STALE_RESENDS = 2
    _STALE_RESEND_ROOM = 0.35

    # A button or stage-colour request is told apart from its neighbours by
    # byte 7, the button or stage it is for: six of each go out back to back
    # with the same category, register and profile, so without it a lost
    # one going back to the previous one's answer passes for ours.  3 button
    # reads did, here, before this, and came back with the previous button's
    # binding.  The other 1047 button answers echo the button there and all
    # 240 stage-colour answers the stage, as they do in a Fusion2 capture
    # through this dongle; feinmann8k.py checks the same byte on the 8K
    # dongle.
    _INDEX_ECHO = ((0x04, 0x01), (0x05, 0x05))

    # A request sent straight after a write's answer is answered in the
    # dongle's next slot, about 230 ms later.  One sent 30 ms after it is
    # answered about 74 ms after that.  On an X3, a page read and an Apply
    # as the GUI makes them, 20 rounds of each gap with the mouse still and
    # 20 moving: with no gap, 507 of 623 requests after a write waited for
    # that slot, and with 30 ms none of 628 did, so Apply took 1.72 s
    # instead of 3.42 with the mouse still and 1.81 instead of 3.01 moving.
    # 20 ms left 30 of 627 in the slot, and 50 ms was no faster.  Reads
    # after reads are answered on the second poll anyway, so nothing else
    # waits, and the page read took 0.40 s either way.  It does not stop
    # the losses _STALE_POLLS recovers from: 27, 20, 19, 18 and 30 of 920
    # exchanges were lost moving at 0, 10, 20, 30 and 50 ms.
    #
    # open() leaves _write_answered alone on purpose, where it clears
    # _last_sent and _lod_tenths.  The slot belongs to the dongle's radio,
    # not to this handle, and monotonic() runs on across a close, so an
    # open that starts inside the gap still waits out the rest of it and a
    # later one pays nothing.
    _WRITE_GAP = 0.03

    def _echoes(self, rsp: bytes, req: bytes) -> bool:
        if rsp[1:3] != req[1:3] or (req[6] != 0x00 and rsp[6] != req[6]):
            return False
        return ((req[1], req[2] & 0x7F) not in self._INDEX_ECHO
                or rsp[7] == req[7])

    def _transact(self, cmd: bytes) -> bytes:
        if self._write_answered is not None:
            wait = self._WRITE_GAP - (time.monotonic() - self._write_answered)
            if wait > 0:
                time.sleep(wait)
            self._write_answered = None
        prev, self._last_sent = self._last_sent, cmd
        self._set_report(cmd)
        deadline = time.monotonic() + self._REPLY_TIMEOUT
        sends, stale, shown = 1, 0, False
        while True:
            rsp = self._get_report()
            if self._echoes(rsp, cmd):
                stale = 0
                if rsp[0] == 0x01:
                    if not cmd[2] & 0x80:   # a write; see _WRITE_GAP
                        self._write_answered = time.monotonic()
                    return rsp
                if rsp[0] == self._NO_LINK:
                    raise IOError(
                        f"no link from the dongle to the mouse for "
                        f"cat=0x{cmd[1]:02x} reg=0x{cmd[2]:02x} "
                        f"sub=0x{cmd[3]:02x} (status byte 0x{rsp[0]:02x}; "
                        f"is the mouse awake and paired?)")
                shown = True
            elif rsp[0] == 0x01 and (
                    shown or (prev is not None and self._echoes(rsp, prev))):
                stale += 1
                if (stale >= self._STALE_POLLS
                        and sends <= self._STALE_RESENDS
                        and time.monotonic() + self._STALE_RESEND_ROOM
                        < deadline):
                    self._set_report(cmd)
                    sends += 1
                    stale = 0
            else:
                stale = 0
            if time.monotonic() >= deadline:
                sent = f", sent {sends} times" if sends > 1 else ""
                raise IOError(
                    f"no reply from the mouse to cat=0x{cmd[1]:02x} "
                    f"reg=0x{cmd[2]:02x} sub=0x{cmd[3]:02x} within "
                    f"{self._REPLY_TIMEOUT * 1000:.0f} ms (last status byte "
                    f"0x{rsp[0]:02x}{sent}; is the mouse awake and paired?)")
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

    # ── Per-profile: LOD ──────────────────────────────────────────────────
    #
    # Which encoding 07/02/03 uses belongs to the mouse, not to the dongle,
    # and this driver is one class for the whole Fusion2 line - see the
    # header above on why it has no model_key.  So it asks.
    # get_connected_mouse() comes out of the dongle's pairing memory in about
    # a millisecond and answers whether the mouse is awake, asleep or on its
    # cable, and what it answers cannot change inside one open, so it is
    # asked once per open.
    #
    # The settings offered stay the two every Fusion2 mouse has: capabilities
    # are a class variable, and this dongle cannot name the X3's third one
    # (0.7 mm) for a mouse it may not be carrying.  What changes is what gets
    # written for them - on an X3, 02 0a and 02 14, so its "2 mm" really is
    # 2 mm and Apply stops replacing the mouse's own value.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lod_tenths = None     # see _lod_is_tenths()
        self._last_sent = None      # see _STALE_POLLS
        self._write_answered = None  # see _WRITE_GAP

    def open(self) -> None:
        super().open()
        self._lod_tenths = None     # the dongle may have been re-paired
        self._last_sent = None
        # Not _write_answered - see _WRITE_GAP.

    # Which dongle the refined name says the mouse is on.
    _DONGLE_LABEL = '1K dongle'

    # What belongs to the mouse rather than to the link: its profiles, its
    # DPI range and its lift-off distances are stored in the mouse and come
    # back through whichever end is plugged in.  Polling rates are not here
    # on purpose - those are the dongle's own ceiling - and neither is
    # anything that identifies a driver (vid_pid_pairs, model_key, wireless).
    _CARRIED_FIELDS = ('num_profiles', 'max_dpi_stages', 'dpi_min', 'dpi_max',
                       'dpi_step', 'lod_values', 'lod_step')

    def refine_capabilities(self) -> bool:
        """Describe the mouse this dongle is carrying, not the line.

        One dongle serves the whole Fusion2 line, so its class-level
        description has to fit all of them: five profiles where an X3 and an
        Xlite v4 both have six, and two lift-off distances where the X3 has
        three.  Both were wrong for every mouse actually measured on it
        (#24, #34), and the cure is the question it already asks - which
        mouse is on the other end - and then that driver's own numbers.

        Nothing new is read off the mouse: get_connected_mouse() answers
        from the dongle's pairing memory in about a millisecond, whether the
        mouse is awake, asleep or on its cable.  A mouse this repo has no
        cabled driver for leaves the description alone.
        """
        from pulsar_mouse.drivers import discover_all

        try:
            carried = self.get_connected_mouse()
        except Exception:
            return False
        ids = (carried['vid'], carried['pid'])
        for cls in discover_all().values():
            caps = cls.capabilities
            if ids in caps.vid_pid_pairs and caps.wireless is not True:
                changes = {f: getattr(caps, f) for f in self._CARRIED_FIELDS}
                # "Pulsar X3 Wired (1K dongle)" would be two connections at
                # once; the cabled driver's name says Wired because that is
                # which half it is.
                base = caps.name.removesuffix(' Wired')
                changes['name'] = f'{base} ({self._DONGLE_LABEL})'
                return self._narrow_capabilities(**changes)
        return False

    def _lod_is_tenths(self) -> bool:
        """Whether the mouse on the dongle uses Fusion2LodTenths' encoding.

        Raises when the dongle will not say, which makes the lift-off field
        one that did not come back - the whole-pass rule then stops the page
        from being applied, and nothing is written in a shape that might not
        be the mouse's.  An answer is remembered for the rest of the open, so
        a pass that gets one asks once.

        Guessing here cannot be made safe.  The X2A encoding was the guess,
        on the grounds that a read and the write after it would at least
        agree; @f-o-o-g-s showed they still don't, because the GUI reads the
        page in one open and applies in a later one, and the answer is only
        remembered within an open.  A first ask that fails and a second that
        answers leaves an X3 on 1.0 mm shown as 2 mm and written to 2.0, and
        it goes the other way too: a 1 mm row nobody touched writes 00 00
        when only the Apply's ask fails (#24).
        """
        if self._lod_tenths is None:
            self._lod_tenths = (
                self.get_connected_mouse()['pid'] in LOD_TENTHS_PIDS)
        return self._lod_tenths

    # PulsarX2A's by name, not super()'s: PulsarXliteV4 carries the tenths
    # mixin now (its own mouse needs it), so super() would reach that and
    # both branches would be the same one.
    def get_lod(self, profile: int) -> float:
        if self._lod_is_tenths():
            return Fusion2LodTenths.get_lod(self, profile)
        return PulsarX2A.get_lod(self, profile)

    def set_lod(self, mm: float, profile: int) -> None:
        if self._lod_is_tenths():
            return Fusion2LodTenths.set_lod(self, mm, profile)
        return PulsarX2A.set_lod(self, mm, profile)

    def _lod_export_is_mode_byte(self) -> bool:
        # A profile file does not record which mouse was on the dongle when
        # it was saved, so this goes by the one that is on it now.
        return self._lod_is_tenths()
