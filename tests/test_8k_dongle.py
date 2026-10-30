"""Regression tests for the 8K dongle (3710:5403): it answers "which mouse is
on the dongle" (01/84/04) under the previous request's header, which the 1K
dongle's echo check never accepts."""

import unittest

from pulsar_mouse.drivers.fusion2_8k_dongle import PulsarFusion28KDongle
from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless

X3_LHD_PID = 0x3410


class Scripted8KDongle(PulsarFusion28KDongle):
    """Echoes every request, except that 01/84/04 is answered with the
    previous request's header, as the 8K dongle was seen to do."""

    _REPLY_TIMEOUT = 0.2
    _REPLY_POLL = 0.001

    def __init__(self):
        super().__init__()
        self.sent = []
        self.poll = 0x08

    def _set_report(self, data):
        self.sent.append(bytes(data))

    def _get_report(self):
        rsp = bytearray(self.sent[-1])
        rsp[0] = 0x01
        if rsp[1:4] == bytes([0x01, 0x84, 0x04]):
            if len(self.sent) > 1:
                rsp[1:4] = self.sent[-2][1:4]
            rsp[6:10] = bytes([0x10, 0x37, X3_LHD_PID & 0xFF, X3_LHD_PID >> 8])
        elif rsp[1:4] == bytes([0x01, 0x87, 0x04]):
            rsp[6:8] = bytes([0x16, 0x10])
        elif rsp[1:4] == bytes([0x01, 0x89, 0x02]):
            rsp[7] = self.poll
        elif rsp[1:4] == bytes([0x01, 0x09, 0x02]):
            self.poll = rsp[7]
        return bytes(rsp)


class EightKDongleTests(unittest.TestCase):
    def test_it_names_the_mouse_despite_the_stale_header(self):
        dongle = Scripted8KDongle()
        dongle._read(0x01, 0x07, 0x04, 0x00)    # firmware, as the CLI reads first
        mouse = dongle.get_connected_mouse()
        self.assertEqual((mouse['vid'], mouse['pid']), (0x3710, X3_LHD_PID))
        self.assertEqual(mouse['name'], 'Pulsar X3 Wired')

    def test_the_next_read_is_not_mistaken_for_it(self):
        dongle = Scripted8KDongle()
        dongle.get_connected_mouse()
        self.assertEqual(dongle._read(0x01, 0x07, 0x04, 0x00)[6:8], b'\x16\x10')

    def test_the_rates_past_1000_keep_doubling(self):
        dongle = Scripted8KDongle()
        for hz, val in ((1000, 0x08), (2000, 0x10), (4000, 0x20), (8000, 0x40)):
            dongle.set_polling_rate(hz)
            self.assertEqual(dongle.sent[-1][7], val)
            self.assertEqual(dongle.get_polling_rate(), hz)

    def test_the_1k_dongle_still_stops_at_1000(self):
        self.assertEqual(max(PulsarXliteV4Wireless.capabilities.polling_rates), 1000)
        self.assertEqual(max(PulsarFusion28KDongle.capabilities.polling_rates), 8000)

    def test_it_is_its_own_driver(self):
        caps = PulsarFusion28KDongle.capabilities
        self.assertEqual(caps.vid_pid_pairs, [(0x3710, 0x5403)])
        self.assertNotIn((0x3710, 0x5403),
                         PulsarXliteV4Wireless.capabilities.vid_pid_pairs)
        self.assertTrue(caps.wireless)


if __name__ == '__main__':
    unittest.main()
