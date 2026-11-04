"""Media bindings in hid.py (#30).

The eight names this table started with are a vocabulary, not the set a
mouse will send: @f-o-o-g-s bound three usages that were not in it on an X3
on its cable and evdev produced the right key each time, because the X3's
descriptor for that report is a plain 16-bit Consumer Control usage.  So any
usage has to be bindable, and - the part that was actually broken - a
binding has to survive describe_button() -> parse_button_function(), which
is what export/import and the GUI's remap dialog both go through.
"""

import unittest

from pulsar_mouse.hid import (BTN_TYPE_KEYBOARD, BTN_TYPE_MEDIA, HID_KEYS,
                              HID_MODS, MEDIA_CODES, describe_button,
                              parse_button_function)

# Measured end to end on an X3: binding, the report the mouse sent, the key
# evdev produced.
MEASURED = {
    'calc': (0x0192, 'KEY_CALC'),
    'homepage': (0x0223, 'KEY_HOMEPAGE'),
    'eject': (0x00b8, 'KEY_EJECTCD'),
}


class NamedCodeTests(unittest.TestCase):
    def test_the_three_that_were_measured_are_named(self):
        for name, (code, _key) in MEASURED.items():
            self.assertEqual(MEDIA_CODES[name], code)
            self.assertEqual(parse_button_function(name),
                             (BTN_TYPE_MEDIA, code & 0xff, code >> 8))

    def test_no_media_name_steals_a_keyboard_key(self):
        # parse_button_function() checks MEDIA_CODES before the keyboard
        # path, so a name in both tables takes the key away from --button.
        # 'home' is why this test exists: the media usage is 'homepage'.
        for name in MEDIA_CODES:
            self.assertNotIn(name, HID_KEYS, name)
            self.assertNotIn(name, HID_MODS, name)
        self.assertEqual(parse_button_function('home'),
                         (BTN_TYPE_KEYBOARD, 0x00, HID_KEYS['home']))

    def test_every_name_round_trips(self):
        for name in MEDIA_CODES:
            self.assertEqual(describe_button(*parse_button_function(name)),
                             name)

    def test_the_codes_are_distinct(self):
        self.assertEqual(len(set(MEDIA_CODES.values())), len(MEDIA_CODES))


class UnnamedCodeTests(unittest.TestCase):
    def test_a_usage_with_no_name_still_round_trips(self):
        # What describe_button() prints for one, fed straight back - the
        # trap 'xclick-double' fell into, and the reason export -> import
        # could not carry a binding Fusion made outside this table.
        for code in (0x0226, 0x0221, 0x018a, 0x0194, 0x0227, 0x022a, 0x1fff):
            spec = describe_button(BTN_TYPE_MEDIA, code & 0xff, code >> 8)
            self.assertEqual(spec, f'media(0x{code:04x})')
            self.assertEqual(parse_button_function(spec),
                             (BTN_TYPE_MEDIA, code & 0xff, code >> 8))

    def test_a_named_usage_written_the_long_way_is_still_the_name(self):
        for name, code in MEDIA_CODES.items():
            self.assertEqual(parse_button_function(f'media(0x{code:04x})'),
                             parse_button_function(name))

    def test_short_hex_is_accepted(self):
        self.assertEqual(parse_button_function('media(0xb8)'),
                         parse_button_function('eject'))

    def test_a_usage_of_nothing_is_refused(self):
        with self.assertRaises(ValueError):
            parse_button_function('media(0x0)')

    def test_nonsense_in_the_brackets_is_refused(self):
        for spec in ('media()', 'media(0x12345)', 'media(zz)', 'media(0x)',
                     'media', 'media(192)x'):
            with self.assertRaises(ValueError):
                parse_button_function(spec)


if __name__ == '__main__':
    unittest.main()
