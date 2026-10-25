"""Regression tests for the 1K dongle's _WRITE_GAP (xlite_v4_wireless.py): a
request sent after a write waits from that write's answer first; one sent
after a read does not wait."""

import unittest
from unittest.mock import patch

from pulsar_mouse.drivers import xlite_v4_wireless
from pulsar_mouse.drivers.x2a import PulsarX2A
from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless

GAP = getattr(PulsarXliteV4Wireless, '_WRITE_GAP', 0.03)


class Clock:
    """time.monotonic() and time.sleep() for the driver module, with time
    passing only when something sleeps or `pass_time()` is called."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def pass_time(self, s):
        self.now += s


class AnsweringDongle(PulsarXliteV4Wireless):
    """A 1K dongle that answers every request on the first poll, and notes
    the clock at each SET_REPORT."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.sent_at = []

    def _set_report(self, data):
        self.sent_at.append(self.clock.now)
        self.last = bytes(data)

    def _get_report(self):
        rsp = bytearray(self.last)
        rsp[0] = 0x01
        if rsp[1:3] == bytes([0x01, 0x89]):
            rsp[7] = 0x08                       # 1000 Hz
        return bytes(rsp)


class WriteGapTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        p = patch.object(xlite_v4_wireless, 'time', self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.dongle = AnsweringDongle(self.clock)

    def gap_before_last(self):
        return self.dongle.sent_at[-1] - self.dongle.sent_at[-2]

    def test_a_request_after_a_write_waits(self):
        self.dongle.set_polling_rate(1000)
        self.dongle.get_polling_rate()
        self.assertAlmostEqual(self.gap_before_last(), GAP)

    def test_a_write_after_a_write_waits(self):
        self.dongle.set_polling_rate(1000)
        self.dongle.set_debounce(3)
        self.assertAlmostEqual(self.gap_before_last(), GAP)

    def test_a_request_after_a_read_does_not_wait(self):
        self.dongle.get_polling_rate()
        self.dongle.set_polling_rate(1000)
        self.assertEqual(self.gap_before_last(), 0)

    def test_time_already_passed_counts(self):
        self.dongle.set_polling_rate(1000)
        self.clock.pass_time(GAP / 3)
        self.dongle.get_polling_rate()
        self.assertAlmostEqual(self.gap_before_last(), GAP)

    def test_no_wait_once_the_gap_has_passed(self):
        self.dongle.set_polling_rate(1000)
        self.clock.pass_time(GAP * 2)
        before = self.clock.now
        self.dongle.get_polling_rate()
        self.assertEqual(self.dongle.sent_at[-1], before)

    def test_a_new_open_inside_the_gap_still_waits(self):
        # The slot is the radio's, not the handle's: open() leaves it alone.
        self.dongle.set_polling_rate(1000)
        with patch.object(PulsarX2A, 'open', lambda self: None):
            self.dongle.open()
        self.dongle.get_polling_rate()
        self.assertAlmostEqual(self.gap_before_last(), GAP)

    def test_only_the_next_request_waits(self):
        self.dongle.set_polling_rate(1000)
        self.dongle.get_polling_rate()
        self.dongle.get_debounce()
        self.assertEqual(self.gap_before_last(), 0)


if __name__ == '__main__':
    unittest.main()
