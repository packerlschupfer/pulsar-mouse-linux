"""Media keys on the Nordic family (#30).

Fusion does not give this family a media *mode* the way the Sonix mice have
one (0x0d).  It writes mode 0x05 - the shortcut mode - and puts a HID
Consumer Control event in the 32-byte record the button's index points at.
@bluesceada bound an Xlite V3 thumb button to Volume Up, dumped it, bound
the same button to Volume Down and dumped it again; the two records differ
only in the usage code and the checksum, which is what pins the layout.
"""

import unittest

from pulsar_mouse.drivers.nordic import (
    BUTTON_MODE_CUSTOM,
    HID_EVT_CONSUMER,
    SHORTCUT_SIZE,
    _build_media_blob,
    _build_shortcut_blob,
    _parse_shortcut_blob,
    _shortcut_addr,
)
from pulsar_mouse.hid import (BTN_TYPE_KEYBOARD, BTN_TYPE_MEDIA, MEDIA_CODES,
                              describe_button, parse_button_function)

# Measured on an Xlite V3, profile 1, thumb1's record at 0x180.  The 24
# bytes after these are ff on the mouse, which is Fusion's padding.
VOLUP = bytes.fromhex('0282e9004 2e900bd'.replace(' ', ''))
VOLDOWN = bytes.fromhex('0282ea004 2ea00bb'.replace(' ', ''))
# The same dump's button slot: mode 05, and Fusion leaves the pointer 00 00.
SLOT = bytes.fromhex('05000050')


def nordic(mem=None):
    """A Nordic mouse whose memory is a dict, with nothing else attached."""
    from pulsar_mouse.drivers.nordic import PulsarNordic

    class Scripted(PulsarNordic):
        def __init__(self):
            super().__init__()
            self._mem = dict(mem or {})
            self.writes = []

        def _ensure_profile(self, profile):
            pass

        def _mem_write(self, addresses):
            self.writes.append(dict(addresses))
            self._mem.update(addresses)

        def _mem_write_bytes(self, start, data):
            self.writes.append((start, bytes(data)))
            self._mem.update({start + i: b for i, b in enumerate(data)})

        def _mem_read_range(self, start, length):
            return bytes(self._mem.get(start + i, 0xff) for i in range(length))

    return Scripted()


class RecordLayoutTests(unittest.TestCase):
    def test_the_builder_reproduces_the_bytes_fusion_wrote(self):
        self.assertEqual(_build_media_blob(MEDIA_CODES['vol+']), VOLUP)
        self.assertEqual(_build_media_blob(MEDIA_CODES['vol-']), VOLDOWN)

    def test_the_two_records_differ_only_where_the_diff_said(self):
        # 0x182 and 0x185 carry the usage, 0x187 the checksum - the three
        # addresses nordic-snapshot reported between the two dumps.
        moved = [i for i in range(len(VOLUP)) if VOLUP[i] != VOLDOWN[i]]
        self.assertEqual(moved, [2, 5, 7])

    def test_every_byte_of_the_record_is_accounted_for(self):
        self.assertEqual(VOLUP[0], 2)                   # one press, one release
        self.assertEqual(VOLUP[1], 0x80 | HID_EVT_CONSUMER)
        self.assertEqual(VOLUP[4], 0x40 | HID_EVT_CONSUMER)
        self.assertEqual(VOLUP[2] | (VOLUP[3] << 8), MEDIA_CODES['vol+'])
        self.assertEqual(VOLUP[5] | (VOLUP[6] << 8), MEDIA_CODES['vol+'])
        self.assertEqual(VOLUP[7], (0x55 - sum(VOLUP[:7])) & 0xff)

    def test_a_media_record_reads_back_as_the_key_it_is(self):
        self.assertEqual(_parse_shortcut_blob(VOLUP),
                         (BTN_TYPE_MEDIA, 0xe9, 0x00))
        self.assertEqual(describe_button(*_parse_shortcut_blob(VOLUP)), 'vol+')
        self.assertEqual(describe_button(*_parse_shortcut_blob(VOLDOWN)), 'vol-')

    def test_all_eight_media_keys_survive_the_round_trip(self):
        for name in MEDIA_CODES:
            _, a1, a2 = parse_button_function(name)
            blob = _build_media_blob(a1 | (a2 << 8))
            self.assertEqual(describe_button(*_parse_shortcut_blob(blob)), name)

    def test_keyboard_records_are_unchanged(self):
        # The refactor that added media must not move the key path: ctrl+c
        # is one modifier and one key, pressed in order and released in
        # reverse, and reads back as itself.
        blob = _build_shortcut_blob(0x01, 0x06)
        self.assertEqual(blob.hex(' '), '04 80 01 00 81 06 00 41 06 00 40 01 00 c1')
        self.assertEqual(_parse_shortcut_blob(blob),
                         (BTN_TYPE_KEYBOARD, 0x01, 0x06))
        self.assertEqual(describe_button(*_parse_shortcut_blob(blob)), 'ctrl+c')

    def test_a_record_nobody_wrote_is_not_a_media_key(self):
        for blob in (b'', b'\xff' * SHORTCUT_SIZE, bytes(SHORTCUT_SIZE)):
            self.assertEqual(_parse_shortcut_blob(blob),
                             (BTN_TYPE_KEYBOARD, 0, 0))

    def test_an_empty_media_binding_is_refused(self):
        with self.assertRaises(ValueError):
            _build_media_blob(0)


class WriteTests(unittest.TestCase):
    def test_binding_a_media_key_writes_the_record_and_the_slot(self):
        mouse = nordic()
        btn_id = mouse.capabilities.buttons['thumb1']
        mouse.set_button(btn_id, *parse_button_function('vol+'), profile=1)
        addr = _shortcut_addr('thumb1', mouse._BUTTON_ADDRS)
        start, data = mouse.writes[0]
        self.assertEqual(start, addr)
        self.assertEqual(data[:len(VOLUP)], VOLUP)
        # Fusion's padding, so a snapshot diff after our write is readable.
        self.assertEqual(set(data[len(VOLUP):]), {0xff})
        self.assertEqual(len(data), SHORTCUT_SIZE)
        slot = mouse.writes[1]
        base = mouse._BUTTON_ADDRS['thumb1']
        self.assertEqual(slot[base], BUTTON_MODE_CUSTOM)
        self.assertEqual((slot[base + 1] << 8) | slot[base + 2], addr)

    def test_the_mouse_reads_back_the_key_it_was_given(self):
        mouse = nordic()
        btn_id = mouse.capabilities.buttons['thumb1']
        mouse.set_button(btn_id, *parse_button_function('mute'), profile=1)
        self.assertEqual(describe_button(*mouse.get_button(btn_id, 1)), 'mute')

    def test_a_slot_fusion_wrote_is_read_from_the_implied_address(self):
        # Fusion leaves the pointer 00 00, so the record address has to come
        # from the button's index rather than the slot.
        mouse = nordic()
        base = mouse._BUTTON_ADDRS['thumb1']
        mem = {base + i: b for i, b in enumerate(SLOT)}
        mem.update({_shortcut_addr('thumb1', mouse._BUTTON_ADDRS) + i: b
                    for i, b in enumerate(VOLUP)})
        mouse = nordic(mem)
        btn_id = mouse.capabilities.buttons['thumb1']
        self.assertEqual(describe_button(*mouse.get_button(btn_id, 1)), 'vol+')

    def test_a_keyboard_shortcut_still_writes_a_keyboard_record(self):
        mouse = nordic()
        btn_id = mouse.capabilities.buttons['thumb1']
        mouse.set_button(btn_id, *parse_button_function('ctrl+c'), profile=1)
        self.assertEqual(describe_button(*mouse.get_button(btn_id, 1)), 'ctrl+c')


if __name__ == '__main__':
    unittest.main()
