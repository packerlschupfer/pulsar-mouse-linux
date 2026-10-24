"""Regression tests for the 1K dongle's _transact() (xlite_v4_wireless.py): a
`05`, the dongle saying it has no link to the mouse, ends the request at the
poll it arrives on instead of at the deadline (#21)."""

import unittest

from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless


class ScriptedDongle(PulsarXliteV4Wireless):
    """A 1K dongle whose USB transport is a script.

    Each poll answers the request echoed with the next status byte in
    `statuses`, the last one repeating: 0x05 for no link to the mouse, 0x02
    for a request still on its way to it, 0x01 for the answer, a polling
    rate of 1000 Hz.  `echo` and `profile` replace the echoed category and
    register and the echoed profile byte, as another program's request left
    in the dongle's reply buffer would.
    """

    _REPLY_POLL = 0

    def __init__(self, *statuses, echo=None, profile=None):
        super().__init__()
        self.statuses = statuses
        self.echo = echo
        self.profile = profile
        self.sent = []
        self.polls = 0

    def _set_report(self, data):
        self.sent.append(bytes(data))

    def _get_report(self):
        rsp = bytearray(self.sent[-1])
        rsp[0] = self.statuses[min(self.polls, len(self.statuses) - 1)]
        if self.echo:
            rsp[1:3] = self.echo
        if self.profile is not None:
            rsp[6] = self.profile
        if rsp[0] == 0x01:
            rsp[7] = 0x08
        self.polls += 1
        return bytes(rsp)


class NoLinkTests(unittest.TestCase):
    def test_no_link_ends_a_read_at_the_first_poll(self):
        # The X3 asleep: 05 from the first poll, which used to be polled
        # for the whole second.
        dongle = ScriptedDongle(0x05)
        with self.assertRaisesRegex(IOError, 'no link'):
            dongle.get_polling_rate()
        self.assertEqual(dongle.polls, 1)
        self.assertEqual(len(dongle.sent), 1)

    def test_no_link_ends_a_write_at_the_first_poll(self):
        dongle = ScriptedDongle(0x05)
        with self.assertRaisesRegex(IOError, 'no link'):
            dongle.set_polling_rate(1000)
        self.assertEqual(dongle.polls, 1)
        self.assertEqual(len(dongle.sent), 1)

    def test_no_link_ends_a_per_profile_request(self):
        # The 05 echoes the profile byte too, as on the X3 for get_lod(1)
        # and set_lod(1, 1).
        dongle = ScriptedDongle(0x05)
        with self.assertRaisesRegex(IOError, 'no link'):
            dongle.get_lod(1)
        self.assertEqual(dongle.polls, 1)

    def test_pending_request_is_still_waited_for(self):
        # An awake mouse: the request echoed with 02 until it has answered.
        dongle = ScriptedDongle(0x02, 0x02, 0x02, 0x01)
        self.assertEqual(dongle.get_polling_rate(), 1000)
        self.assertEqual(dongle.polls, 4)

    def test_no_link_for_another_request_is_left_to_the_deadline(self):
        # Another program's battery read (08/81/01) in the reply buffer
        # with a 05 of its own, as in the 2026-10-23 dongle log.
        dongle = ScriptedDongle(0x05, echo=b'\x08\x81')
        dongle._REPLY_TIMEOUT = 0.02
        with self.assertRaisesRegex(
                IOError, r'within 20 ms \(last status byte 0x05'):
            dongle.get_polling_rate()
        self.assertGreater(dongle.polls, 1)

    def test_no_link_for_another_profile_is_left_to_the_deadline(self):
        dongle = ScriptedDongle(0x05, profile=2)
        dongle._REPLY_TIMEOUT = 0.02
        with self.assertRaisesRegex(
                IOError, r'within 20 ms \(last status byte 0x05'):
            dongle.get_lod(1)
        self.assertGreater(dongle.polls, 1)

    def test_answer_to_another_request_is_left_to_the_deadline(self):
        # The latched reply _transact() was written for, unchanged.
        dongle = ScriptedDongle(0x01, echo=b'\x08\x81')
        dongle._REPLY_TIMEOUT = 0.02
        with self.assertRaisesRegex(
                IOError, r'within 20 ms \(last status byte 0x01'):
            dongle.get_polling_rate()
        self.assertGreater(dongle.polls, 1)


if __name__ == '__main__':
    unittest.main()
