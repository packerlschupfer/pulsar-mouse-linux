"""Every binding this app can describe, it can parse back.

`base.export_profile` stores `describe_button()`'s string (base.py:420) and
`import_profile` feeds it straight to `parse_button_function()`
(base.py:481), so a binding whose description does not parse back as itself
is a binding that changes when a profile is saved and restored - and the
GUI's remap dialog pre-selects from the same string.

The hole this file was written for: `parse_button_function` resolves
MOUSE_ACTIONS before the keyboard path, so a button bound to Left Arrow
described as `left` came back as a left *click*.  Found reviewing #44.
"""

import unittest

from pulsar_mouse.drivers.x3 import PulsarX3
from pulsar_mouse.hid import (BTN_TYPE_DISABLED, BTN_TYPE_DPI,
                              BTN_TYPE_KEYBOARD, BTN_TYPE_MEDIA,
                              BTN_TYPE_MOUSE, BTN_TYPE_PROFILE,
                              BTN_TYPE_SCROLL, DPI_ACTIONS, HID_KEYS,
                              HID_MODS, MEDIA_CODES, MOUSE_ACTIONS,
                              PROFILE_ACTIONS, SCROLL_ACTIONS,
                              describe_button, parse_button_function)
from pulsar_mouse.hid import _CLAIMED_BEFORE_KEYS, _KEY_NAMES

MODS = (0x00, 0x01, 0x02, 0x04, 0x08, 0x05, 0x81, 0xff)


class KeyboardRoundTripTests(unittest.TestCase):
    def test_every_key_survives_describe_then_parse(self):
        for code in sorted(set(HID_KEYS.values())):
            for mod in MODS:
                said = describe_button(BTN_TYPE_KEYBOARD, mod, code)
                self.assertEqual(parse_button_function(said),
                                 (BTN_TYPE_KEYBOARD, mod, code),
                                 f'{said!r} (mod {mod:#04x} key {code:#04x})')

    def test_the_arrows_are_described_unambiguously(self):
        for word, spelling in (('left', 'arrowleft'), ('right', 'arrowright'),
                               ('up', 'arrowup'), ('down', 'arrowdown')):
            self.assertEqual(
                describe_button(BTN_TYPE_KEYBOARD, 0, HID_KEYS[word]),
                spelling)

    def test_the_bare_words_still_parse_for_files_already_written(self):
        # 'up' and 'down' were never ambiguous, so an old export holding
        # them must still restore the key.
        for word in ('up', 'down'):
            self.assertEqual(parse_button_function(word),
                             (BTN_TYPE_KEYBOARD, 0x00, HID_KEYS[word]))
        # 'left' and 'right' stay the mouse buttons, which is what someone
        # typing them means and what every export of a click contains.
        # An export written before this that held an *arrow* binding cannot
        # be told from a click and does not heal; new ones are unambiguous.
        self.assertEqual(parse_button_function('left'),
                         (BTN_TYPE_MOUSE, MOUSE_ACTIONS['left'], 0x00))
        self.assertEqual(parse_button_function('right'),
                         (BTN_TYPE_MOUSE, MOUSE_ACTIONS['right'], 0x00))

    def test_a_modifier_was_never_the_problem(self):
        self.assertEqual(describe_button(*parse_button_function('ctrl+left')),
                         'ctrl+arrowleft')


class NothingDescribedIsClaimedElsewhereTests(unittest.TestCase):
    def test_no_key_is_printed_under_a_name_another_table_owns(self):
        # The guard that stops this coming back: add a key called 'mute' or
        # 'dpi+' tomorrow and this fails rather than silently losing it.
        self.assertFalse(set(_KEY_NAMES.values()) & _CLAIMED_BEFORE_KEYS)

    def test_the_claimed_set_really_is_what_parses_first(self):
        for name in _CLAIMED_BEFORE_KEYS:
            kind = parse_button_function(name)[0]
            self.assertNotEqual(kind, BTN_TYPE_KEYBOARD, name)

    def test_every_action_name_parses_to_its_own_kind(self):
        for table, kind in ((MOUSE_ACTIONS, BTN_TYPE_MOUSE),
                            (SCROLL_ACTIONS, BTN_TYPE_SCROLL),
                            (DPI_ACTIONS, BTN_TYPE_DPI),
                            (PROFILE_ACTIONS, BTN_TYPE_PROFILE),
                            (MEDIA_CODES, BTN_TYPE_MEDIA)):
            for name in table:
                self.assertEqual(parse_button_function(name)[0], kind, name)
        self.assertEqual(parse_button_function('disabled')[0],
                         BTN_TYPE_DISABLED)

    def test_no_modifier_name_is_claimed_either(self):
        self.assertFalse(set(HID_MODS) & _CLAIMED_BEFORE_KEYS)


class ThroughExportAndImportTests(unittest.TestCase):
    """The path that actually loses a binding, not just the two helpers."""

    def exported(self, bindings):
        class Recording(PulsarX3):
            def get_button(self, btn_id, profile):
                return bindings[btn_id]

            def get_dpi_stages(self, profile):
                return {'stages': [(800, 800)], 'active': 1}

        return Recording().export_profile(1)['buttons']

    def imported(self, buttons):
        calls = []

        class Recording(PulsarX3):
            def set_button(self, btn_id, btn_type, a1, a2, profile):
                calls.append((btn_id, btn_type, a1, a2))

        warnings = Recording().import_profile(
            1, {'format': 'pulsar-mouse-profile', 'version': 3,
                'buttons': buttons})
        return calls, warnings

    def test_an_arrow_binding_comes_back_as_an_arrow(self):
        caps = PulsarX3.capabilities
        arrow = (BTN_TYPE_KEYBOARD, 0x00, HID_KEYS['left'])
        bindings = {bid: arrow for bid in caps.buttons.values()}
        buttons = self.exported(bindings)
        self.assertEqual(set(buttons.values()), {'arrowleft'})
        calls, warnings = self.imported(buttons)
        self.assertEqual(warnings, [])
        self.assertEqual({c[1:] for c in calls}, {arrow})

    def test_a_click_still_comes_back_as_a_click(self):
        caps = PulsarX3.capabilities
        click = (BTN_TYPE_MOUSE, MOUSE_ACTIONS['left'], 0x00)
        bindings = {bid: click for bid in caps.buttons.values()}
        buttons = self.exported(bindings)
        self.assertEqual(set(buttons.values()), {'left'})
        calls, _warnings = self.imported(buttons)
        self.assertEqual({c[1:] for c in calls}, {click})

    def test_a_file_written_before_the_fix_still_imports(self):
        calls, warnings = self.imported({'thumb1': 'up', 'thumb2': 'forward'})
        self.assertEqual(warnings, [])
        self.assertEqual(calls, [
            (PulsarX3.capabilities.buttons['thumb1'],
             BTN_TYPE_KEYBOARD, 0x00, HID_KEYS['up']),
            (PulsarX3.capabilities.buttons['thumb2'],
             BTN_TYPE_MOUSE, MOUSE_ACTIONS['forward'], 0x00),
        ])


if __name__ == '__main__':
    unittest.main()
