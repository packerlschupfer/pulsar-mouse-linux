"""The low-battery warning decision.  No display, no mouse, no D-Bus.

The wiring tests below import the GUI module.  They drive the method with
stand-ins and never open a window or touch the session bus.
"""

import os
import tempfile
import types
import unittest
from unittest.mock import patch

from pulsar_mouse.low_battery import (
    latch_released, notification_hints, notification_timeout, should_notify,
    warning_body, warning_summary)

try:
    from pulsar_mouse.gui import PulsarMouseApp
except (ImportError, ValueError):
    PulsarMouseApp = None


class ShouldNotifyTests(unittest.TestCase):
    def test_a_reading_at_the_threshold_notifies(self):
        self.assertTrue(should_notify(20, False, False, 20))

    def test_a_reading_below_the_threshold_notifies(self):
        self.assertTrue(should_notify(18, False, False, 20))

    def test_a_reading_above_the_threshold_does_not(self):
        self.assertFalse(should_notify(21, False, False, 20))

    def test_charging_does_not_notify(self):
        self.assertFalse(should_notify(5, True, False, 20))

    def test_a_latched_discharge_does_not_notify_again(self):
        # The 60-second poll, and a pushed report, both land here.  The
        # second reading of a discharge that already warned stays quiet.
        self.assertFalse(should_notify(15, False, True, 20))

    def test_startup_is_an_unlatched_reading(self):
        # A session starts unlatched, so a mouse that is already low warns
        # on the first reading rather than waiting to cross the threshold.
        self.assertTrue(should_notify(19, False, False, 20))


class LatchReleasedTests(unittest.TestCase):
    def test_charging_releases_the_latch(self):
        self.assertTrue(latch_released(10, True, 20))

    def test_five_points_above_releases_the_latch(self):
        self.assertTrue(latch_released(25, False, 20))

    def test_four_points_above_keeps_the_latch(self):
        self.assertFalse(latch_released(24, False, 20))

    def test_still_low_keeps_the_latch(self):
        self.assertFalse(latch_released(18, False, 20))


class WarningTextTests(unittest.TestCase):
    def test_the_title_is_the_problem_and_the_body_names_the_mouse(self):
        self.assertEqual(warning_summary(), 'Battery low')
        self.assertEqual(
            warning_body('Pulsar X2 CrazyLight Wireless', 18),
            'Pulsar X2 CrazyLight Wireless is at 18%.')

    def test_a_normal_warning_uses_the_desktop_timeout(self):
        hints = notification_hints(False)
        self.assertEqual(hints['urgency'], 1)
        self.assertNotIn('resident', hints)
        self.assertEqual(hints['desktop-entry'], 'pulsar-mouse')
        self.assertEqual(hints['image-path'], 'battery-caution')
        self.assertEqual(notification_timeout(False), -1)

    def test_a_resolved_icon_file_is_the_picture(self):
        # A theme name does not fill Plasma's column beside the text.
        path = '/usr/share/icons/breeze-dark/status/22/battery-caution.svg'
        hints = notification_hints(False, path)
        self.assertEqual(hints['image-path'], path)
        self.assertEqual(hints['desktop-entry'], 'pulsar-mouse')

    def test_a_resident_warning_never_expires_and_stays_normal(self):
        # Critical urgency would also show during Do Not Disturb on Plasma.
        # The switch asks only to stay until dismissed.
        hints = notification_hints(True)
        self.assertEqual(hints['urgency'], 1)
        self.assertTrue(hints['resident'])
        self.assertEqual(notification_timeout(True), 0)


@unittest.skipIf(PulsarMouseApp is None, 'needs GTK 4 and libadwaita')
class ConsiderTests(unittest.TestCase):
    def setUp(self):
        app = PulsarMouseApp.__new__(PulsarMouseApp)
        app._device = types.SimpleNamespace(
            capabilities=types.SimpleNamespace(name='Pulsar X2 CrazyLight Wireless'))
        app._low_battery_latched = False
        app._low_battery_notification_id = 0
        app._low_battery_notify_warned = False
        app.sent = []
        app.closed = []

        def send(summary, body, resident):
            app.sent.append((summary, body, resident))
            return 9

        def withdraw():
            app.closed.append(app._low_battery_notification_id)
            app._low_battery_notification_id = 0

        app._send_low_battery = send
        app._withdraw_low_battery_notification = withdraw
        self.app = app

    def test_the_default_is_off_and_sends_nothing(self):
        from pulsar_mouse import config
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with patch.object(config, 'CONFIG_PATH', path):
                self.app._consider_low_battery(5, False)
        self.assertEqual(self.app.sent, [])
        self.assertFalse(os.path.exists(path))

    def test_one_discharge_notifies_once(self):
        with patch('pulsar_mouse.gui.load_settings', return_value={
                'low_battery_notify': True,
                'low_battery_percent': 20,
                'low_battery_resident': False}):
            self.app._consider_low_battery(18, False)
            self.app._consider_low_battery(16, False)
        self.assertEqual(self.app.sent, [(
            'Battery low', 'Pulsar X2 CrazyLight Wireless is at 18%.', False)])
        self.assertTrue(self.app._low_battery_latched)
        self.assertEqual(self.app._low_battery_notification_id, 9)

    def test_charging_closes_the_warning(self):
        self.app._low_battery_latched = True
        self.app._low_battery_notification_id = 9
        with patch('pulsar_mouse.gui.load_settings', return_value={
                'low_battery_notify': True,
                'low_battery_percent': 20,
                'low_battery_resident': True}):
            self.app._consider_low_battery(16, True)
        self.assertEqual(self.app.sent, [])
        self.assertEqual(self.app.closed, [9])
        self.assertFalse(self.app._low_battery_latched)

    def test_a_failed_send_does_not_latch_and_is_said_once(self):
        def boom(summary, body, resident):
            raise RuntimeError('no daemon')

        self.app._send_low_battery = boom
        with patch('pulsar_mouse.gui.load_settings', return_value={
                'low_battery_notify': True,
                'low_battery_percent': 20,
                'low_battery_resident': False}):
            self.app._consider_low_battery(18, False)
            self.app._consider_low_battery(18, False)
        self.assertFalse(self.app._low_battery_latched)
        self.assertTrue(self.app._low_battery_notify_warned)

    def test_withdrawing_with_no_id_does_not_touch_the_bus(self):
        app = PulsarMouseApp.__new__(PulsarMouseApp)
        app._low_battery_notification_id = 0

        def bus(*args):
            raise AssertionError('the bus was called')

        app._notifications_call = bus
        app._withdraw_low_battery_notification()


if __name__ == '__main__':
    unittest.main()
