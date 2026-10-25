"""Regression tests for the Fusion2 family's cabled read (x2a.py): a read the
mouse dropped is sent again (#21), until the open has had an answer (#25)."""

import unittest
from unittest.mock import patch

from pulsar_mouse.drivers.x3 import PulsarX3
from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless


class ScriptedX3(PulsarX3):
    """A cabled X3 whose USB transport is a script.

    The first `dropped` commands are never taken: every poll after one
    answers 0x00, as the X3 did straight after a swap from the 1K dongle to
    the cable.  After that a command is answered on the first poll with
    0x01 and a polling rate of 1000 Hz, or with `status` on every poll when
    one is given (0x02: the mouse is still working on it).
    """

    _ACK_DELAY = 0
    _READ_RESEND_DELAY = 0

    def __init__(self, dropped=0, status=None):
        super().__init__()
        self.dropped = dropped
        self.status = status
        self.sent = []
        self.polls = 0

    def _set_report(self, data):
        self.sent.append(bytes(data))

    def _get_report(self):
        self.polls += 1
        if len(self.sent) <= self.dropped:
            return bytes(64)
        rsp = bytearray(self.sent[-1])
        rsp[0] = 0x01 if self.status is None else self.status
        rsp[7] = 0x08
        return bytes(rsp)


class DroppedReadTests(unittest.TestCase):
    def test_answered_read_is_sent_once(self):
        mouse = ScriptedX3()
        self.assertEqual(mouse.get_polling_rate(), 1000)
        self.assertEqual(len(mouse.sent), 1)
        self.assertEqual(mouse.polls, 1)

    def test_dropped_read_is_sent_again_until_answered(self):
        # The two swaps measured with a resend: the 2026-10-22 probe (read
        # and first resend dropped) and this code on hardware (read and two
        # resends dropped).  Same command every time.
        for dropped in (2, 3):
            mouse = ScriptedX3(dropped=dropped)
            self.assertEqual(mouse.get_polling_rate(), 1000)
            self.assertEqual(len(mouse.sent), dropped + 1)
            self.assertEqual(len(set(mouse.sent)), 1)
            self.assertEqual(mouse.polls, dropped * PulsarX3._ACK_POLLS + 1)

    def test_read_never_taken_gives_up_after_the_resends(self):
        mouse = ScriptedX3(dropped=1000)
        with self.assertRaisesRegex(IOError, '0x00'):
            mouse.get_polling_rate()
        self.assertEqual(len(mouse.sent), PulsarX3._READ_RESENDS + 1)

    def test_first_answer_ends_the_resends(self):
        # After an answer a dropped read fails after its polls, as it did
        # before there were resends.
        mouse = ScriptedX3()
        self.assertEqual(mouse.get_polling_rate(), 1000)
        mouse.dropped = 1000    # takes nothing from here on
        with self.assertRaisesRegex(
                IOError, rf'0x00 \(after {PulsarX3._ACK_POLLS} polls\)'):
            mouse.get_polling_rate()
        self.assertEqual(len(mouse.sent), 2)

    def test_resends_are_shared_by_the_open(self):
        # A pass the mouse never answers spends them once, not once a read.
        mouse = ScriptedX3(dropped=1000)
        for _ in range(25):
            with self.assertRaisesRegex(IOError, '0x00'):
                mouse.get_polling_rate()
        self.assertEqual(len(mouse.sent), PulsarX3._READ_RESENDS + 25)

    def test_open_gives_the_resends_back(self):
        mouse = ScriptedX3(dropped=1000)
        with self.assertRaises(IOError):
            mouse.get_polling_rate()
        with patch('pulsar_mouse.drivers.x2a.usb.core.find'), \
                patch('pulsar_mouse.drivers.x2a.usb.util.claim_interface'):
            mouse.open()
        mouse.sent.clear()
        with self.assertRaises(IOError):
            mouse.get_polling_rate()
        self.assertEqual(len(mouse.sent), PulsarX3._READ_RESENDS + 1)

    def test_pending_read_is_not_sent_again(self):
        mouse = ScriptedX3(status=0x02)
        with self.assertRaisesRegex(IOError, '0x02'):
            mouse.get_polling_rate()
        self.assertEqual(len(mouse.sent), 1)

    def test_write_is_not_sent_again(self):
        mouse = ScriptedX3(dropped=1000)
        with self.assertRaisesRegex(IOError, '0x00'):
            mouse.set_polling_rate(1000)
        self.assertEqual(len(mouse.sent), 1)

    def test_dongle_does_not_come_through_here(self):
        # The 1K dongle has its own _read() (_transact()), which sends a
        # command again only once the dongle has gone back to an answer it
        # already gave (#24), never on a 00 as this cable path does.  One
        # send, then it gives up on its own deadline.
        class ScriptedDongle(PulsarXliteV4Wireless):
            _REPLY_TIMEOUT = 0
            _REPLY_POLL = 0

            def __init__(self):
                super().__init__()
                self.sent = []

            def _set_report(self, data):
                self.sent.append(bytes(data))

            def _get_report(self):
                return bytes(64)

        dongle = ScriptedDongle()
        with self.assertRaises(IOError):
            dongle.get_polling_rate()
        self.assertEqual(len(dongle.sent), 1)


if __name__ == '__main__':
    unittest.main()
