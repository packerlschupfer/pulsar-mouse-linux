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
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, GLib, Gtk
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

    def test_start_in_tray_follows_the_config_into_the_login_file(self):
        # The flag is read back from the config when the desktop file is
        # written. Turning Start on Login on after Start in Tray was saved
        # has to include it, and turning Start in Tray on while the file
        # already exists has to rewrite that file.
        from pulsar_mouse import config
        with tempfile.TemporaryDirectory() as tmp:
            desktop = os.path.join(tmp, 'pulsar-mouse.desktop')
            path = os.path.join(tmp, 'config.json')
            plain = gui.MainWindow._AUTOSTART_CONTENT.format(
                exec='pulsar-mouse-gui')
            flagged = gui.MainWindow._AUTOSTART_CONTENT.format(
                exec='pulsar-mouse-gui --start-in-tray')
            with patch.object(config, 'CONFIG_PATH', path), \
                 patch.object(gui.MainWindow, '_AUTOSTART_PATH', desktop):
                win = self.build()
                self.assertFalse(os.path.exists(path))
                self.assertFalse(os.path.exists(desktop))
                self.assertFalse(win._start_in_tray_row.get_active())
                self.assertFalse(win._start_in_tray_row.get_sensitive())
                win._autostart_row.set_active(True)
                self.assertTrue(win._start_in_tray_row.get_sensitive())
                with open(desktop) as f:
                    self.assertEqual(f.read(), plain)
                win._start_in_tray_row.set_active(True)
                with open(desktop) as f:
                    self.assertEqual(f.read(), flagged)
                self.assertTrue(config.load(path)['start_in_tray'])
                win._start_in_tray_row.set_active(False)
                with open(desktop) as f:
                    self.assertEqual(f.read(), plain)
                win._start_in_tray_row.set_active(True)
                win._autostart_row.set_active(False)
                self.assertFalse(os.path.exists(desktop))
                self.assertFalse(win._start_in_tray_row.get_sensitive())
                self.assertTrue(config.load(path)['start_in_tray'])

                win2 = self.build()
                self.assertTrue(win2._start_in_tray_row.get_active())
                self.assertFalse(win2._start_in_tray_row.get_sensitive())
                win2._autostart_row.set_active(True)
                with open(desktop) as f:
                    self.assertEqual(f.read(), flagged)

    def test_the_option_marks_only_this_process(self):
        app = self._app_that_does_not_run()
        empty = GLib.VariantDict.new()
        self.assertEqual(app._on_local_options(app, empty), -1)
        self.assertFalse(app._start_in_tray)
        opts = GLib.VariantDict.new()
        opts.insert_value('start-in-tray', GLib.Variant('b', True))
        self.assertEqual(app._on_local_options(app, opts), -1)
        self.assertTrue(app._start_in_tray)

    def test_help_lists_the_option_and_an_unknown_one_still_fails(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        env['PYTHONPATH'] = os.path.join(root, 'src')
        cmd = [sys.executable, '-m', 'pulsar_mouse.gui']
        help_proc = subprocess.run(
            cmd + ['--help'], capture_output=True, text=True,
            env=env, timeout=20, check=False)
        self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
        self.assertIn('--start-in-tray', help_proc.stdout)
        bad = subprocess.run(
            cmd + ['--no-such-option'], capture_output=True, text=True,
            env=env, timeout=20, check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('Unknown option', bad.stderr)

    def test_the_first_activation_skips_the_window(self):
        app = self._app_that_does_not_run()
        device = object()
        seen = []
        app._start_in_tray = True
        app._find_or_create_device = lambda: device
        app._follow_device_swap = lambda *_a: None
        app._build_tray = lambda found: seen.append(found)
        app._open_main_window = lambda found: seen.append('window')
        app.hold = lambda: None
        app.get_windows = lambda: []
        app._on_activate(app)
        self.assertEqual(seen, [device])
        self.assertFalse(app._start_in_tray)
        self.assertTrue(app._open_window_if_no_tray)
        app._on_activate(app)
        self.assertEqual(seen, [device, 'window'])

    def test_no_mouse_still_opens_the_window(self):
        app = self._app_that_does_not_run()
        seen = []
        app._start_in_tray = True
        app._find_or_create_device = lambda: None
        app._follow_device_swap = lambda *_a: None
        app._build_tray = lambda found: seen.append('tray')
        app._open_main_window = lambda found: seen.append(found)
        app.hold = lambda: None
        app.get_windows = lambda: []
        app._on_activate(app)
        self.assertEqual(seen, [None])
        self.assertFalse(app._open_window_if_no_tray)

    def test_no_tray_host_opens_the_window_after_the_wait(self):
        app = self._app_that_does_not_run()
        opened = []
        app._open_main_window = lambda device: opened.append(device)
        app._device = 'mouse'
        app.get_windows = lambda: []
        app._tray_host_present = lambda: False
        scheduled = []

        def fake_add(interval, fn, *args):
            scheduled.append((interval, fn, args))
            return 1

        with patch.object(GLib, 'timeout_add', fake_add):
            self.assertFalse(app._await_tray_host(app._TRAY_WAIT_TRIES))
        self.assertEqual(opened, [])
        self.assertEqual(scheduled, [(
            app._TRAY_WAIT_MS, app._await_tray_host,
            (app._TRAY_WAIT_TRIES - 1,))])
        self.assertFalse(app._await_tray_host(1))
        self.assertEqual(opened, ['mouse'])

    def test_a_tray_host_or_an_open_window_ends_the_wait(self):
        app = self._app_that_does_not_run()
        opened = []
        app._open_main_window = lambda device: opened.append(device)
        app._tray_host_present = lambda: True
        app.get_windows = lambda: []
        self.assertFalse(app._await_tray_host(1))
        app.get_windows = lambda: [object()]
        app._tray_host_present = lambda: False
        self.assertFalse(app._await_tray_host(1))
        self.assertEqual(opened, [])

    def _app_that_does_not_run(self):
        app = gui.PulsarMouseApp()
        self.addCleanup(app.quit)
        return app

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
