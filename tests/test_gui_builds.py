"""Does the window actually build? (PR #36)

CI runs on Ubuntu 24.04, whose libadwaita is 1.5, and the only GUI check
was an import - so Adw.ButtonRow, which arrived in 1.6, passed CI and then
failed at page-build time on the distro CI itself runs.  @bluesceada hit it.

Both branches of _button_row() are built here whatever this machine has, so
the 1.5 path is covered on a 1.7 box and the other way round.  Needs a
display: CI runs the suite under xvfb-run.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gtk
    _HAVE_DISPLAY = Gtk.init_check()
    if _HAVE_DISPLAY:
        Adw.init()
        from pulsar_mouse import gui
except (ImportError, ValueError):            # no GTK 4 / libadwaita here
    _HAVE_DISPLAY = False

from pulsar_mouse.drivers.x2a import PulsarX2A

TOOLS_ROWS = ['Test Input', 'Export Profile to JSON',
              'Import Profile from JSON',
              'Low Battery Warning', 'Threshold',
              'Keep the warning until I dismiss it']


class StubX2A(PulsarX2A):
    """Answers every request with zeros, so a read pass cannot raise."""

    def open(self):
        self._dev = object()

    def close(self):
        self._dev = None

    def find_hidraw(self, root='/sys/class/hidraw'):
        return None

    def _set_report(self, data):
        self._last = bytes(data)

    def _get_report(self):
        rsp = bytearray(self.capabilities.report_size)
        rsp[0] = 0x01
        rsp[1:4] = self._last[1:4]
        rsp[6] = self._last[6]
        return bytes(rsp)


def titles(widget, found=None):
    """Every row title under `widget`, in tree order."""
    found = [] if found is None else found
    if isinstance(widget, Adw.PreferencesRow):
        found.append(widget.get_title())
    child = widget.get_first_child()
    while child is not None:
        titles(child, found)
        child = child.get_next_sibling()
    return found


@unittest.skipUnless(_HAVE_DISPLAY, 'needs a display (run under xvfb-run)')
class WindowBuildTests(unittest.TestCase):
    def build(self):
        win = gui.MainWindow(device=StubX2A())
        self.addCleanup(win._stop_listeners)
        return win

    def test_every_page_in_the_sidebar_exists(self):
        win = self.build()
        for name, _label, _icon in win._nav_pages:
            self.assertIsNotNone(win._view_stack.get_child_by_name(name), name)

    def test_the_tools_page_has_its_buttons(self):
        win = self.build()
        found = titles(win._view_stack.get_child_by_name('tools'))
        for title in TOOLS_ROWS:
            self.assertIn(title, found)

    def test_the_low_battery_rows_write_the_config(self):
        # A temp path: building the window must not create the user's file,
        # and the rows must write the one they are given.
        from pulsar_mouse import config
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with patch.object(config, 'CONFIG_PATH', path):
                win = self.build()
                self.assertFalse(os.path.exists(path))
                self.assertFalse(win._low_battery_notify_row.get_active())
                self.assertFalse(win._low_battery_percent_row.get_sensitive())
                self.assertFalse(win._low_battery_resident_row.get_sensitive())
                win._low_battery_notify_row.set_active(True)
                self.assertTrue(win._low_battery_percent_row.get_sensitive())
                win._low_battery_percent_row.set_value(35)
                win._low_battery_resident_row.set_active(True)
                with open(path) as f:
                    saved = json.load(f)
        self.assertEqual(saved, {
            'low_battery_notify': True,
            'low_battery_percent': 35,
            'low_battery_resident': True,
        })

    def test_it_builds_the_way_libadwaita_1_5_would(self):
        # No Adw.ButtonRow before 1.6, which is what Ubuntu 24.04 has.
        real = gui._button_row
        gui._button_row = lambda: Adw.ActionRow(activatable=True)
        try:
            found = titles(self.build()._view_stack.get_child_by_name('tools'))
        finally:
            gui._button_row = real
        for title in TOOLS_ROWS:
            self.assertIn(title, found)

    def test_the_rows_this_mouse_has_are_all_there(self):
        win = self.build()
        found = titles(win._view_stack.get_child_by_name('customize'))
        for label in StubX2A.capabilities.button_labels.values():
            self.assertIn(label, found)


if __name__ == '__main__':
    unittest.main()
