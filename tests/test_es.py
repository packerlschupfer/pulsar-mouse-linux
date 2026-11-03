"""Tests for the eS driver (es.py, es_link_dongle.py) against a scripted hidraw
node: what it may send, how it reads replies, and that writes refuse."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from pulsar_mouse import cli
from pulsar_mouse.drivers.es import (
    DIR_READ, MAGIC, OP_BATTERY, POLL_VAL_TO_HZ, REPORT_ID, REPORT_SIZE,
    PulsarEs, _READS,
    _HIDIOCGFEATURE, _HIDIOCSFEATURE, _read_packet, _reply_data,
)
from pulsar_mouse.drivers.es_link_dongle import PulsarEsLinkDongle


def reply(op, *data):
    return bytes([REPORT_ID, MAGIC, op, DIR_READ, len(data), *data])


# What the mouse answers, keyed by (opcode, selector); values from captures
# and the espulse tests.  Stage 2 is separate X/Y.
ANSWERS = {
    (0x01, None): reply(0x01, 0x56, 0x00),
    (0x02, None): reply(0x02, 2, 2),
    (0x03, 1): reply(0x03, 1, 0xff, 0x00, 0x10),
    (0x03, 2): reply(0x03, 2, 0x00, 0xff, 0x00),
    (0x04, 1): reply(0x04, 1, 1, 0x20, 0x03, 0x20, 0x03),
    (0x04, 2): reply(0x04, 2, 0, 0x80, 0x0c, 0x10, 0x0e),
    (0x05, None): reply(0x05, 0x01),
    (0x07, None): reply(0x07, 0x05),
    (0x08, None): reply(0x08, 0x00, 0x01),
    (0x09, None): reply(0x09, 0x02),
    (0x0b, None): reply(0x0b, 0x0e),
    (0x0c, 0xb1): reply(0x0c, 0xb1, 0x01),
    (0x0c, 0xb2): reply(0x0c, 0xb2, 0x02),
    (0x0c, 0xb3): reply(0x0c, 0xb3, 0x03),
    (0x0c, 0xb4): reply(0x0c, 0xb4, 0x05),
    (0x0c, 0xb5): reply(0x0c, 0xb5, 0x04),
    (0x0f, None): reply(0x0f, 0x01),
    (0x10, None): reply(0x10, 0x01),
    (0x1c, None): reply(0x1c, 0x01),
    (0x1f, None): reply(0x1f, 0x01, 0x0a),
}

NOT_READY = bytes([REPORT_ID, MAGIC, 0xff])


class FakeNode:
    """Stands in for fcntl.ioctl on the mouse's hidraw node."""

    def __init__(self, answers=None, first=()):
        self.answers = dict(ANSWERS if answers is None else answers)
        self.first = list(first)    # replies served before the real answer
        self.sent = []

    def ioctl(self, fd, request, buf):
        if request == _HIDIOCSFEATURE:
            self.sent.append(bytes(buf))
            return len(buf)
        self.assert_get(request)
        sent = self.sent[-1]
        selector = sent[5] if sent[5] else None
        answer = (self.first.pop(0) if self.first
                  else self.answers.get((sent[2], selector), NOT_READY))
        buf[:len(answer)] = answer
        return REPORT_SIZE

    @staticmethod
    def assert_get(request):
        assert request == _HIDIOCGFEATURE, hex(request)


class EsTestCase(unittest.TestCase):
    driver = PulsarEs

    def setUp(self):
        self.device = self.make()

    def make(self, node=None):
        self.node = node or FakeNode()
        patcher = patch('pulsar_mouse.drivers.es.fcntl.ioctl', self.node.ioctl)
        patcher.start()
        self.addCleanup(patcher.stop)
        device = self.driver()
        device._fd = 99
        device._REPLY_POLL = 0
        return device


class PacketTests(unittest.TestCase):
    def test_reads_match_the_capture(self):
        self.assertEqual(bytes(_read_packet(0x04, 6, 1))[:6],
                         bytes.fromhex('0ea504010601'))
        self.assertEqual(bytes(_read_packet(0x0c, 2, 0xb1))[:6],
                         bytes.fromhex('0ea50c0102b1'))
        self.assertEqual(len(_read_packet(0x07, 1)), REPORT_SIZE)

    def test_factory_reset_and_pairing_cannot_be_built(self):
        for op in (0x17, 0x1a):
            with self.assertRaises(ValueError):
                _read_packet(op, 1)

    def test_nothing_outside_the_list_can_be_built(self):
        for op in set(range(256)) - _READS:
            with self.assertRaises(ValueError):
                _read_packet(op, 1)

    def test_every_packet_is_a_read(self):
        for op in _READS:
            self.assertEqual(_read_packet(op, 1)[3], DIR_READ)

    def test_replies_to_poll_past(self):
        self.assertIsNone(_reply_data(NOT_READY, 0x14, 1))
        self.assertIsNone(_reply_data(bytes.fromhex('00000000880f'), 0x14, 1))
        # Our opcode with direction 03: busy.
        self.assertIsNone(_reply_data(bytes.fromhex('0ea50b030112'), 0x0b, 1))
        # Another opcode, left over.
        self.assertIsNone(_reply_data(reply(0x07, 2), 0x0b, 1))
        # Too short for what was asked.
        self.assertIsNone(_reply_data(reply(0x0b), 0x0b, 1))
        # The right opcode, but left over from another stage.
        stage1 = reply(0x04, 1, 0, 0x90, 0x01, 0x90, 0x01)
        self.assertIsNone(_reply_data(stage1, 0x04, 6, 2))
        self.assertIsNotNone(_reply_data(stage1, 0x04, 6, 1))

    def test_a_longer_reply_is_cut_to_what_was_asked(self):
        self.assertEqual(_reply_data(reply(0x01, 0x56, 0x00, 0x99), 0x01, 2),
                         bytes([0x56, 0x00]))

    def test_every_rate_the_mouse_can_report_is_listed(self):
        # The GUI shows the last entry for a rate it can't find.
        self.assertEqual(sorted(POLL_VAL_TO_HZ.values()),
                         PulsarEs.capabilities.polling_rates)


class ReadTests(EsTestCase):
    def test_settings(self):
        d = self.device
        self.assertEqual(d.get_firmware_version(), '0056')
        self.assertEqual(d.get_polling_rate(), 8000)
        self.assertEqual(d.get_debounce(), 2)
        self.assertEqual(d.get_lod(1), 0.8)
        self.assertIs(d.get_angle_snap(), True)
        self.assertIs(d.get_motion_sync(), True)
        self.assertIs(d.get_turbo_mode(), False)
        self.assertEqual(d.get_brightness(1), 10)
        self.assertEqual(d.get_led_effect(1), 'steady')
        self.assertEqual(d.get_stage_color(1, 1), (0xff, 0x00, 0x10))

    def test_dpi_stages_keep_x_and_y(self):
        info = self.device.get_dpi_stages(profile=1)
        self.assertEqual(info, {'active': 2, 'count': 2,
                                'stages': [(800, 800), (3200, 3600)]})
        self.assertEqual(self.device.get_active_dpi_stage(1), 2)

    def test_impossible_dpi_selections_are_errors(self):
        for count, active in ((0, 0), (2, 3), (2, 0), (7, 1)):
            device = self.make(FakeNode({**ANSWERS,
                                         (0x02, None): reply(0x02, count, active)}))
            with self.assertRaises(IOError):
                device.get_dpi_stages()

    def test_power_has_no_voltage(self):
        self.assertEqual(self.device.get_power(),
                         {'battery_percent': 70, 'power_connected': True,
                          'battery_mv': None})

    def test_no_battery_reading_is_an_error(self):
        device = self.make(FakeNode({**ANSWERS, (OP_BATTERY, None): reply(OP_BATTERY, 0xff)}))
        with self.assertRaises(IOError):
            device.get_power()

    def test_button_names(self):
        t, a1, a2 = self.device.get_button(0xb5, 1)
        self.assertEqual(self.device.describe_button(t, a1, a2), 'backward')
        self.assertEqual(self.device.describe_button(0x7f, 0, 0), 'function(0x7f)')

    def test_polls_past_not_ready_and_busy(self):
        device = self.make(FakeNode(first=[
            bytes.fromhex('00000000880f'), NOT_READY,
            bytes.fromhex('0ea507030100'),
        ]))
        self.assertEqual(device.get_polling_rate(), 8000)
        self.assertEqual(len(self.node.sent), 1)

    def test_no_answer_times_out(self):
        device = self.make(FakeNode({}))
        with self.assertRaises(IOError):
            device.get_polling_rate()

    def test_unknown_codes_are_errors_not_guesses(self):
        device = self.make(FakeNode({(0x09, None): reply(0x09, 0x03),
                                     (0x05, None): reply(0x05, 0x03),
                                     (0x07, None): reply(0x07, 0x09),
                                     (0x1f, None): reply(0x1f, 0x02, 0x05)}))
        for get in (device.get_lod, device.get_debounce,
                    device.get_polling_rate, device.get_led_effect):
            with self.assertRaises(IOError):
                get()

    def test_every_getter_only_sends_reads(self):
        d = self.device
        for get in (d.get_firmware_version, d.get_polling_rate, d.get_debounce,
                    d.get_angle_snap, d.get_motion_sync, d.get_turbo_mode,
                    d.get_dpi_stages, d.get_lod, d.get_brightness,
                    d.get_led_effect, d.get_power):
            get()
        self.assertTrue(self.node.sent)
        for packet in self.node.sent:
            self.assertEqual(packet[3], DIR_READ)
            self.assertIn(packet[2], _READS)


class CliTests(EsTestCase):
    """The CLI's own paths, which assume things about profiles and buttons."""

    def run_cli(self, *argv):
        self.device.open = lambda: None
        self.device.close = lambda: None
        out = io.StringIO()
        with patch.object(cli, 'find_device', lambda name=None: self.device), \
                patch.object(sys, 'argv', ['pulsar-mouse', *argv]), \
                redirect_stdout(out):
            cli.main()
        return out.getvalue()

    def test_read_mode_shows_one_profile(self):
        text = self.run_cli()
        self.assertIn('Profile 1', text)
        self.assertNotIn('Profile 2', text)
        self.assertIn('Stage 2: 3200 DPI', text)
        self.assertIn('Battery:          70%', text)
        self.assertNotIn('error', text)

    def test_battery_json_shape(self):
        self.assertEqual(json.loads(self.run_cli('--battery-json')),
                         {'wireless': True, 'battery_percent': 70,
                          'power_connected': True, 'battery_mv': None})

    def test_status_json(self):
        status = json.loads(self.run_cli('--status-json'))
        self.assertEqual(status['num_profiles'], 1)
        self.assertIs(status['wireless'], False)

    def test_read_mode_sends_only_reads(self):
        self.run_cli()
        self.run_cli('--status-json')
        for packet in self.node.sent:
            self.assertEqual(packet[3], DIR_READ)

    def test_a_write_flag_gives_a_clean_error_and_sends_nothing(self):
        for flag, says in ((('--poll', '1000'), 'read-only'),
                           (('--active-profile', '1'), 'not supported')):
            with self.assertRaises(SystemExit) as caught:
                self.run_cli(*flag)
            self.assertIn(says, str(caught.exception.code))
        self.assertTrue(all(p[3] == DIR_READ for p in self.node.sent))


class WriteTests(EsTestCase):
    def test_every_setter_refuses_and_sends_nothing(self):
        d = self.device
        calls = [
            lambda: d.set_polling_rate(1000),
            lambda: d.set_debounce(2),
            lambda: d.set_angle_snap(True),
            lambda: d.set_motion_sync(True),
            lambda: d.set_turbo_mode(True),
            lambda: d.set_dpi_stages([800], 1, 1),
            lambda: d.set_active_dpi_stage(1, 1),
            lambda: d.set_stage_color(1, 1, 2, 3, 1),
            lambda: d.set_lod(0.8, 1),
            lambda: d.set_brightness(5, 1),
            lambda: d.set_led_effect('off', 1),
            lambda: d.set_button(0xb5, 4, 0, 0, 1),
            lambda: d.set_active_profile(1),
            lambda: d.reset_to_defaults(1),
        ]
        for call in calls:
            with self.assertRaises(NotImplementedError):
                call()
        self.assertEqual(self.node.sent, [])

    def test_the_remap_dialog_can_show_the_refusal(self):
        # RemapButtonDialog only catches ValueError.
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.device.parse_button_function('forward')
        self.assertEqual(self.node.sent, [])

    def test_refusals_say_why(self):
        # The GUI shows this message; base's is empty.
        with self.assertRaisesRegex(NotImplementedError, 'read-only'):
            self.device.set_polling_rate(1000)

    def test_reset_is_not_offered(self):
        self.assertFalse(PulsarEs.capabilities.has_reset)


class NodeTests(unittest.TestCase):
    def test_gui_starts_no_listener(self):
        for driver in (PulsarEs, PulsarEsLinkDongle):
            self.assertIsNone(driver._HIDRAW_INTERFACE)
            self.assertIsNone(driver().find_hidraw())

    def test_each_driver_finds_its_own_config_node(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            n = 1
            for pid in (0x5602, 0x9604):
                for iface in (0, 1):
                    d = Path(root) / f'hidraw{n}' / 'device'
                    d.mkdir(parents=True)
                    (d / 'uevent').write_text(
                        f'HID_ID=0003:00003710:0000{pid:04X}\n'
                        f'HID_PHYS=usb-0000:00:14.0-9/input{iface}\n')
                    n += 1
            self.assertEqual(PulsarEsLinkDongle()._config_node(root), '/dev/hidraw2')
            self.assertEqual(PulsarEs()._config_node(root), '/dev/hidraw4')


class DongleTests(EsTestCase):
    driver = PulsarEsLinkDongle

    def test_declares_the_dongle(self):
        caps = PulsarEsLinkDongle.capabilities
        self.assertEqual(caps.vid_pid_pairs, [(0x3710, 0x5602)])
        self.assertIs(caps.wireless, True)
        self.assertIs(PulsarEs.capabilities.wireless, False)
        self.assertEqual(caps.model_key, PulsarEs.capabilities.model_key)

    def test_open_with_a_mouse_connected(self):
        with patch.object(PulsarEsLinkDongle, '_config_node', return_value='/dev/x'), \
                patch('pulsar_mouse.drivers.es.os.open', return_value=99), \
                patch('pulsar_mouse.drivers.es.os.close') as close:
            device = self.make()
            device._fd = None
            device.open()
            self.assertEqual(device._fd, 99)
            close.assert_not_called()

    def test_open_without_a_mouse_says_so_and_closes(self):
        with patch.object(PulsarEsLinkDongle, '_config_node', return_value='/dev/x'), \
                patch('pulsar_mouse.drivers.es.os.open', return_value=99), \
                patch('pulsar_mouse.drivers.es.os.close') as close:
            device = self.make(FakeNode({(0x0f, None): reply(0x0f, 0x00)}))
            device._fd = None
            with self.assertRaisesRegex(IOError, 'no mouse connected'):
                device.open()
            close.assert_called_once_with(99)
            self.assertIsNone(device._fd)


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


class StubEs(PulsarEs):
    def open(self):
        self._fd = 99

    def close(self):
        self._fd = None


@unittest.skipUnless(_HAVE_DISPLAY, 'needs a display (run under xvfb-run)')
class WindowTests(unittest.TestCase):
    def test_the_window_builds_with_only_what_the_mouse_has(self):
        def titles(widget, found):
            if isinstance(widget, Adw.PreferencesRow):
                found.append(widget.get_title())
            child = widget.get_first_child()
            while child is not None:
                titles(child, found)
                child = child.get_next_sibling()
            return found

        # Anything that does read gets the script, never a real node.
        patcher = patch('pulsar_mouse.drivers.es.fcntl.ioctl', FakeNode().ioctl)
        patcher.start()
        self.addCleanup(patcher.stop)
        win = gui.MainWindow(device=StubEs())
        self.addCleanup(win._stop_listeners)
        found = titles(win, [])
        self.assertIn('Debounce', found)
        for absent in ('Pulse Speed', 'Ripple Control', 'Connection Quality'):
            self.assertNotIn(absent, found)


if __name__ == '__main__':
    unittest.main()
