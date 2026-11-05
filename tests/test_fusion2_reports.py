"""The unprompted reports on the Fusion2 config interface (#21).

Three arrive there.  `05 05` has been read since the start.  @f-o-o-g-s
decoded the other two on an X3, identically on its cable and through the 1K
dongle: `05 01 <profile>` when the active profile changes, and `04 <mask>`
saying which buttons are down, with `04 00` when they come up.

The button report is tested at the length it actually arrives in - 24
bytes, the size report 4 is declared at.  The first version of these tests
used the 2-byte form the issue quoted, which is that report with its
trailing zeros trimmed for legibility and not something any read returns,
so they passed against a branch that could never fire on a mouse.
"""

import unittest

from pulsar_mouse.drivers.feinmann8k import PulsarFeinmann8K
from pulsar_mouse.drivers.x2a import PulsarX2A
from pulsar_mouse.drivers.x3 import PulsarX3
from pulsar_mouse.drivers.xlite_v4_wireless import PulsarXliteV4Wireless


class ProfileChangedTests(unittest.TestCase):
    def test_a_switch_says_which_profile_it_is_now_on(self):
        for profile in range(1, 7):
            self.assertEqual(
                PulsarX3().parse_hidraw_event(bytes([0x05, 0x01, profile])),
                {'profile_changed': True, 'profile': profile})

    def test_it_asks_for_a_re_read_the_way_the_nordic_mice_do(self):
        # The GUI acts on this key alone (gui.py's listener): no 05 05 comes
        # with the switch, even onto a profile sitting on another DPI stage,
        # so the page has to be read again rather than patched.
        event = PulsarX2A().parse_hidraw_event(b'\x05\x01\x02')
        self.assertTrue(event['profile_changed'])

    def test_the_dongle_passes_it_through(self):
        self.assertEqual(
            PulsarXliteV4Wireless().parse_hidraw_event(b'\x05\x01\x03'),
            {'profile_changed': True, 'profile': 3})

    def test_a_truncated_one_is_not_read(self):
        self.assertIsNone(PulsarX2A().parse_hidraw_event(b'\x05\x01'))


def pressed(mask):
    """The 24 bytes a press really arrives in."""
    return bytes([0x04, mask]) + bytes(22)


class ButtonMaskTests(unittest.TestCase):
    def test_every_bit_was_pressed_on_hardware(self):
        # All six, on an X3 through the 1K dongle and on its cable.  None of
        # these is a guess, so the table is a closed statement about this
        # mouse rather than a partial one.
        mouse = PulsarX3()
        for mask, name in ((0x01, 'left'), (0x02, 'right'), (0x04, 'wheel'),
                           (0x08, 'backward'), (0x10, 'forward'),
                           (0x20, 'dpi')):
            self.assertEqual(mouse.parse_hidraw_event(pressed(mask)),
                             {'buttons_down': mask})
            self.assertEqual(PulsarX3._BUTTON_BITS[mask], name)
        self.assertEqual(len(PulsarX3._BUTTON_BITS), 6)

    def test_the_bytes_straight_out_of_the_log(self):
        # @f-o-o-g-s's X3 through the 1K dongle: left, right, wheel click
        # and the DPI button, each followed by the same 04 00 on release.
        log = {
            0x01: '040100000000000000000000000000000000000000000000',
            0x02: '040200000000000000000000000000000000000000000000',
            0x04: '040400000000000000000000000000000000000000000000',
            0x20: '042000000000000000000000000000000000000000000000',
        }
        up = bytes.fromhex('040000000000000000000000000000000000000000000000')
        self.assertEqual(len(up), 24)
        for mouse in (PulsarX3(), PulsarXliteV4Wireless()):
            for mask, line in log.items():
                data = bytes.fromhex(line)
                self.assertEqual(len(data), 24)
                self.assertEqual(mouse.parse_hidraw_event(data),
                                 {'buttons_down': mask})
                self.assertEqual(mouse.parse_hidraw_event(up),
                                 {'buttons_down': 0x00})

    def test_a_release_is_an_empty_mask(self):
        self.assertEqual(PulsarX2A().parse_hidraw_event(pressed(0x00)),
                         {'buttons_down': 0x00})

    def test_two_buttons_at_once_are_one_mask(self):
        self.assertEqual(PulsarX2A().parse_hidraw_event(pressed(0x18)),
                         {'buttons_down': 0x18})

    def test_the_dongle_passes_it_through(self):
        self.assertEqual(
            PulsarXliteV4Wireless().parse_hidraw_event(pressed(0x20)),
            {'buttons_down': 0x20})

    def test_every_named_bit_is_a_button_the_mouse_has(self):
        # The names are only worth having if they match the button table.
        # back/forward are mouse *actions* there, not button names, so only
        # the four that are both get checked.
        for cls in (PulsarX2A, PulsarX3, PulsarXliteV4Wireless):
            for name in cls._BUTTON_BITS.values():
                if name in ('left', 'right', 'wheel', 'dpi'):
                    self.assertIn(name, cls.capabilities.buttons,
                                  f'{cls.__name__} {name}')


class StillReadsWhatItAlwaysDidTests(unittest.TestCase):
    def test_the_dpi_report_is_untouched(self):
        # The X2A counts stages from zero and the X3 from one.
        self.assertEqual(
            PulsarX2A().parse_hidraw_event(b'\x05\x05\x00\x90\x01\x00\x00'),
            {'dpi': 400, 'stage': 1})
        self.assertEqual(
            PulsarX3().parse_hidraw_event(b'\x05\x05\x01\x90\x01\x00\x00'),
            {'dpi': 400, 'stage': 1})

    def test_the_reports_the_dongle_owns_still_win(self):
        dongle = PulsarXliteV4Wireless()
        self.assertEqual(dongle.parse_hidraw_event(b'\x05\x08\x01\x01'),
                         {'link_up': True})
        self.assertEqual(dongle.parse_hidraw_event(b'\x05\x0d\x64'),
                         {'signal_percent': 100})
        self.assertEqual(
            dongle.parse_hidraw_event(b'\x05\x07\x01\x51\x01')['battery_percent'],
            0x51)

    def test_nothing_else_on_the_node_is_guessed_at(self):
        mouse = PulsarX2A()
        for data in (b'', b'\x04', b'\x05', b'\x05\x40\x00',
                     b'\x03\x20', b'\x06' + bytes(23)):
            self.assertIsNone(mouse.parse_hidraw_event(data), data)

    def test_the_8k_dongle_keeps_its_own_decoding(self):
        # It is a different protocol family with its own parser; this must
        # not have reached into it.
        self.assertIsNone(PulsarFeinmann8K().parse_hidraw_event(pressed(0x20)))


if __name__ == '__main__':
    unittest.main()
