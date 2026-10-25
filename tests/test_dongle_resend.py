"""Regression tests for the 1K dongle's _transact() (xlite_v4_wireless.py): a
request the dongle took and then lost, shown by the reply buffer going back
to an answer it had already given, is sent again (#24)."""

import unittest
from unittest.mock import patch

from pulsar_mouse.drivers.x2a import PulsarX2A
from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless

BATTERY = bytes([0x01, 0x08, 0x81, 0x01] + [0] * 60)    # another program's
BATTERY_PENDING = bytes([0x02, 0x08, 0x81, 0x01] + [0] * 60)


class LosingDongle(PulsarXliteV4Wireless):
    """A 1K dongle that answers every send at once, except the ones given a
    script.

    `scripts` maps a send's position (1 for the first frame sent) to what
    the reply buffer shows on each poll after it, the last entry repeating:
    'pending' (ours, 02), 'answer' (ours, 01), 'previous' (the answer
    before ours), 'older' (the one before that, as past
    get_connected_mouse(), which the dongle answers itself), or a reply as
    bytes.  A resend is a new position, answered unless scripted too.
    """

    _REPLY_POLL = 0
    _STALE_RESEND_ROOM = 0

    def __init__(self, scripts=None):
        super().__init__()
        self.scripts = scripts or {}
        self.sent = []
        self.answers = []
        self.poll = 0

    def _set_report(self, data):
        self.sent.append(bytes(data))
        self.poll = 0

    def _answer(self, req):
        rsp = bytearray(req)
        rsp[0] = 0x01
        if rsp[1:3] == bytes([0x01, 0x89]):
            rsp[7] = 0x08                       # 1000 Hz
        if rsp[1:3] == bytes([0x01, 0x84]):
            rsp[6:10] = bytes([0x10, 0x37, 0x09, 0x34])     # an X3
        if rsp[1:3] == bytes([0x04, 0x81]):
            rsp[8:11] = bytes([0x01, rsp[7], 0x00])         # button N -> N
        if rsp[1:3] == bytes([0x08, 0x85]):
            rsp[7:9] = (300).to_bytes(2, 'little')          # sleep timer
        return bytes(rsp)

    def _get_report(self):
        script = self.scripts.get(len(self.sent), ['answer'])
        step = script[min(self.poll, len(script) - 1)]
        self.poll += 1
        req = self.sent[-1]
        if step == 'answer':
            rsp = self._answer(req)
            if not self.answers or self.answers[-1] != rsp:
                self.answers.append(rsp)
            return rsp
        if step == 'pending':
            return bytes([0x02]) + req[1:]
        if step == 'previous':
            return self.answers[-1]
        if step == 'older':
            return self.answers[-2]
        return step


def lost(*then):
    """Ours pending on the first poll, then the previous answer for good:
    every loss but one on hardware."""
    return ['pending'] + list(then or ['previous'])


def opened(dongle):
    """dongle.open() without USB."""
    with patch.object(PulsarX2A, 'open', lambda self: None):
        dongle.open()


class ResendTests(unittest.TestCase):
    def test_a_lost_read_is_sent_again(self):
        dongle = LosingDongle({2: lost()})
        dongle.get_polling_rate()
        self.assertEqual(dongle.get_debounce(), 0)
        self.assertEqual(len(dongle.sent), 3)
        self.assertEqual(dongle.sent[1], dongle.sent[2])

    def test_a_lost_write_is_sent_again_unchanged(self):
        dongle = LosingDongle({2: lost()})
        dongle.get_polling_rate()
        dongle.set_polling_rate(500)
        self.assertEqual(len(dongle.sent), 3)
        self.assertEqual(dongle.sent[1], dongle.sent[2])
        self.assertEqual(dongle.sent[2][1:3], bytes([0x01, 0x09]))

    def test_not_on_the_first_sight_of_it(self):
        # An earlier answer that gives way to ours within four polls is
        # left alone.  Not seen on hardware - the margin is for this.
        dongle = LosingDongle({2: lost('previous', 'previous', 'previous',
                                       'previous', 'answer')})
        dongle.get_polling_rate()
        dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)

    def test_resent_on_the_fifth_poll_of_it(self):
        dongle = LosingDongle({2: lost('previous', 'previous', 'previous',
                                       'previous', 'previous', 'answer')})
        dongle.get_polling_rate()
        dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 3)

    def test_ours_again_starts_the_count_over(self):
        dongle = LosingDongle({2: lost('previous', 'previous', 'previous',
                                       'previous', 'pending', 'previous',
                                       'previous', 'previous', 'previous',
                                       'answer')})
        dongle.get_polling_rate()
        dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)

    def test_the_count_starts_over_after_a_resend(self):
        # The resend gets four polls of the old answer before its own, as a
        # first send would, not a second resend on the first of them.
        dongle = LosingDongle({2: lost(), 3: ['previous'] * 4 + ['answer']})
        dongle.get_polling_rate()
        dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 3)

    def test_the_previous_answer_from_the_first_poll(self):
        # Ours never shown: 3 losses of 216 on hardware.
        dongle = LosingDongle({2: ['previous']})
        dongle.get_polling_rate()
        dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 3)

    def test_back_past_the_identity_read_once_ours_showed(self):
        # set_lod() asks which mouse the dongle has first, and the dongle
        # answers that itself, so a LOD write lost after it goes back to
        # the request before - the low-power write here, as in Apply.
        dongle = LosingDongle({3: lost('older')})
        dongle.set_low_power_threshold(10)
        dongle.set_lod(2, 1)
        writes = [f for f in dongle.sent if f[1:3] == bytes([0x07, 0x02])]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1][7:9], bytes([0x02, 0x14]))

    def test_an_older_answer_from_the_start_is_left_to_the_deadline(self):
        # Not the previous request's, and ours never shown.
        dongle = LosingDongle({3: ['older']})
        dongle._REPLY_TIMEOUT = 0.02
        dongle.set_low_power_threshold(10)
        with self.assertRaisesRegex(IOError, r'within 20 ms'):
            dongle.set_lod(2, 1)
        self.assertEqual(len(dongle.sent), 3)

    def test_another_programs_answer_is_left_to_the_deadline(self):
        # There before ours ever showed (#28).  One send, then the deadline.
        dongle = LosingDongle({2: [BATTERY]})
        dongle._REPLY_TIMEOUT = 0.02
        dongle.get_polling_rate()
        with self.assertRaisesRegex(
                IOError, r'within 20 ms \(last status byte 0x01;'):
            dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)

    def test_another_programs_answer_after_ours_showed(self):
        # The dongle showed ours pending and then something else's answer:
        # it let ours go, whoever asked.
        dongle = LosingDongle({2: lost(BATTERY)})
        dongle.get_polling_rate()
        self.assertEqual(dongle.get_debounce(), 0)
        self.assertEqual(len(dongle.sent), 3)

    def test_another_programs_pending_request_is_not_a_loss(self):
        # Another request in flight is the dongle working; a second copy of
        # ours then is how it drops replies (#15).
        dongle = LosingDongle({2: lost(BATTERY_PENDING)})
        dongle._REPLY_TIMEOUT = 0.02
        dongle.get_polling_rate()
        with self.assertRaisesRegex(IOError, r'last status byte 0x02;'):
            dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)

    def test_a_pending_request_is_never_resent(self):
        dongle = LosingDongle({2: ['pending'] * 40 + ['answer']})
        dongle.get_polling_rate()
        dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)

    def test_a_new_open_forgets_the_previous_request(self):
        dongle = LosingDongle({2: ['previous']})
        dongle._REPLY_TIMEOUT = 0.02
        dongle.get_polling_rate()
        opened(dongle)
        with self.assertRaisesRegex(IOError, r'within 20 ms'):
            dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)

    def test_the_open_befores_answer_once_ours_showed(self):
        # A LOD write that is the first of its open to reach the mouse goes
        # back past the identity read to the last answer of the open before.
        dongle = LosingDongle({4: lost('older')})
        dongle.get_lod(1)
        opened(dongle)
        dongle.set_lod(2, 1)
        writes = [f for f in dongle.sent if f[1:3] == bytes([0x07, 0x02])]
        self.assertEqual(len(writes), 2)

    def test_the_budget_is_per_request(self):
        # Three losses in one request: two resends, then the deadline, and
        # the error counts the sends.  The next request in the same open
        # still has its own two.
        dongle = LosingDongle({n: lost() for n in range(2, 7)})
        dongle._REPLY_TIMEOUT = 0.05
        dongle.get_polling_rate()
        with self.assertRaisesRegex(IOError, r'sent 3 times;'):
            dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 4)
        self.assertEqual(dongle.get_angle_snap(), False)
        self.assertEqual(len(dongle.sent), 7)

    def test_no_resend_too_close_to_the_deadline(self):
        # It would be left pending for the next request (#15).
        dongle = LosingDongle({2: lost()})
        dongle._REPLY_TIMEOUT = 0.05
        dongle._STALE_RESEND_ROOM = 0.05
        dongle.get_polling_rate()
        with self.assertRaisesRegex(IOError, r'within 50 ms \(last status '
                                             r'byte 0x01;'):
            dongle.get_debounce()
        self.assertEqual(len(dongle.sent), 2)


class IndexEchoTests(unittest.TestCase):
    """Buttons and stage colours go out six in a row with the same category,
    register and profile; byte 7 says which one an answer is for."""

    def test_a_lost_button_read_is_not_the_previous_buttons(self):
        # On hardware it came back with button 1's binding as button 2's.
        dongle = LosingDongle({2: lost()})
        dongle.get_button(1, 1)
        self.assertEqual(dongle.get_button(2, 1), (0x01, 0x02, 0x00))
        self.assertEqual(len(dongle.sent), 3)

    def test_a_lost_stage_colour_write_is_sent_again(self):
        dongle = LosingDongle({2: lost()})
        dongle.set_stage_color(1, 10, 10, 220, 1)
        dongle.set_stage_color(2, 0, 0, 220, 1)
        self.assertEqual(len(dongle.sent), 3)
        self.assertEqual(dongle.sent[2][7:11], bytes([2, 0, 0, 220]))

    def test_other_requests_do_not_compare_byte_7(self):
        # The sleep timer's answer carries its seconds there, not an echo.
        dongle = LosingDongle()
        self.assertEqual(dongle.get_power_saving_timeout(), 300)
        self.assertEqual(len(dongle.sent), 1)


if __name__ == '__main__':
    unittest.main()
