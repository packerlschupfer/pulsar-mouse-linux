"""Regression tests for tools/nordic-snapshot.py (#30).

The tool exists so that a contributor can find which byte a Fusion setting
lives in without capturing USB traffic: snapshot, change one thing, snapshot
again, read the diff.  What is tested here is the part that can be got wrong
silently - which address gets which label, and whether a change is reported
at all - since the person running it has no way to tell a missing line from
a setting that did not move.
"""

import importlib.util
import pathlib
import unittest

from pulsar_mouse.drivers.nordic import (
    ADDR_AUTOSLEEP,
    ADDR_AUTOSLEEP_ALT,
    ADDR_DEBOUNCE,
    ADDR_POLLING_RATE,
    PulsarNordic,
)

TOOL = pathlib.Path(__file__).resolve().parent.parent / 'tools' / 'nordic-snapshot.py'
_spec = importlib.util.spec_from_file_location('nordic_snapshot', TOOL)
snap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snap)


def mouse(mem=None):
    """A Nordic mouse whose settings block is whatever is passed in."""

    class Scripted(PulsarNordic):
        def __init__(self):
            super().__init__()
            self._fd = 17
            self._mem = {a: 0 for a in range(snap.SETTINGS_END + 1)}
            self._mem.update({a: 0xff for a in
                              range(0x100, snap.SHORTCUT_END + 1)})
            self._mem.update(mem or {})
            self._profile = 1

        def _send(self, packet):
            self.last = packet

        def _recv(self, timeout_ms=2000):
            body = bytearray(16)
            body[0], body[1] = 0x08, self.last[1]
            if self.last[1] == 0x01:
                body[10], body[11] = 0x06, 0x07      # an Xlite V3
            elif self.last[1] == 0x12:
                body[6], body[7] = 0x02, 0x18
            return bytes(body) + bytes([self._checksum(*body)])

        def _ensure_profile(self, profile):
            pass

        def _mem_read_range(self, start, length):
            return bytes(self._mem.get(start + i, 0) for i in range(length))

    return Scripted()


class SnapshotTests(unittest.TestCase):
    def test_it_records_what_the_mouse_says_about_itself(self):
        taken = snap.snapshot(mouse({ADDR_DEBOUNCE: 3}))
        self.assertEqual(taken['firmware'], '2.18')
        self.assertEqual(taken['get_model_ids'], (6, 7))
        block = taken['profiles']['1']
        self.assertEqual(len(bytes.fromhex(block['settings'])),
                         snap.SETTINGS_END + 1)
        self.assertEqual(len(bytes.fromhex(block['shortcuts'])),
                         snap.SHORTCUT_END - 0x100 + 1)

    def test_a_changed_byte_is_named_and_decoded(self):
        device = mouse({ADDR_DEBOUNCE: 3})
        before = snap.snapshot(device)
        device._mem[ADDR_DEBOUNCE] = 2
        after = snap.snapshot(device)
        lines = snap.diff_lines(before, after, device)
        self.assertEqual(len(lines), 1)
        self.assertIn('0x00a9', lines[0])
        self.assertIn('03 -> 02', lines[0])
        self.assertIn('debounce: 3 ms -> 2 ms', lines[0])

    def test_the_sleep_timer_is_shown_in_seconds(self):
        device = mouse({ADDR_AUTOSLEEP: 6})
        before = snap.snapshot(device)
        device._mem[ADDR_AUTOSLEEP] = 5
        lines = snap.diff_lines(before, snap.snapshot(device), device)
        self.assertIn('auto-sleep: 60 s -> 50 s', lines[0])

    def test_the_byte_nobody_has_explained_says_so(self):
        # 0xAD is the one this tool would be pointed at next.
        device = mouse({ADDR_AUTOSLEEP_ALT: 6})
        before = snap.snapshot(device)
        device._mem[ADDR_AUTOSLEEP_ALT] = 7
        lines = snap.diff_lines(before, snap.snapshot(device), device)
        self.assertIn('unidentified', lines[0])

    def test_an_address_with_no_label_is_still_reported(self):
        # The interesting case: a setting nobody has found yet.
        device = mouse()
        before = snap.snapshot(device)
        device._mem[0x58] = 0x42
        lines = snap.diff_lines(before, snap.snapshot(device), device)
        self.assertIn('0x0058', lines[0])
        self.assertIn('unidentified', lines[0])

    def test_polling_is_shown_in_hertz(self):
        device = mouse({ADDR_POLLING_RATE: 0x08})
        before = snap.snapshot(device)
        device._mem[ADDR_POLLING_RATE] = 0x01
        lines = snap.diff_lines(before, snap.snapshot(device), device)
        self.assertIn('polling rate: 125 Hz -> 1000 Hz', lines[0])

    def test_a_record_s_checksum_is_named_not_mystified(self):
        # 0x55 minus the record's bytes.  Unlabelled, it reads as a find:
        # @bluesceada chased thumb1's as unidentified (#30).
        device = mouse({0x70: 0x01, 0x71: 0x10, 0x73: 0x44})
        before = snap.snapshot(device)
        device._mem.update({0x70: 0x05, 0x71: 0x00, 0x73: 0x50})
        lines = snap.diff_lines(before, snap.snapshot(device), device)
        self.assertTrue(any('checksum' in l for l in lines), lines)
        self.assertFalse(any('unidentified' in l for l in lines), lines)

    def test_a_binding_in_the_shortcut_table_shows_up(self):
        # Mode 0x05 keeps the binding itself at 0x100 + 32 * button index,
        # which is where a media key set in Fusion would land - and what a
        # snapshot that stopped at 0xC9 could never show.
        device = mouse()
        before = snap.snapshot(device)
        device._mem[0x120] = 0x02       # thumb1's record: two events
        device._mem[0x121] = 0x82
        lines = snap.diff_lines(before, snap.snapshot(device), device)
        self.assertTrue(any('0x0120' in l and 'shortcut record' in l
                            for l in lines), lines)

    def test_a_snapshot_from_before_the_table_was_covered_still_diffs(self):
        device = mouse({ADDR_DEBOUNCE: 3})
        old_style = {'profiles': {'1': snap.snapshot(device)
                                  ['profiles']['1']['settings']}}
        device._mem[ADDR_DEBOUNCE] = 2
        lines = snap.diff_lines(old_style, snap.snapshot(device), device)
        self.assertTrue(any('debounce: 3 ms -> 2 ms' in l for l in lines), lines)

    def test_nothing_changed_says_so(self):
        device = mouse()
        taken = snap.snapshot(device)
        self.assertEqual(snap.diff_lines(taken, taken, device),
                         ['nothing changed'])


if __name__ == '__main__':
    unittest.main()
