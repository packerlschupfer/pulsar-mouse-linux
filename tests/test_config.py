"""The three low-battery keys. No display: this never imports GTK."""

import json
import os
import tempfile
import unittest

from pulsar_mouse import config


class ConfigTests(unittest.TestCase):
    def test_a_missing_file_is_the_defaults_and_stays_missing(self):
        path = os.path.join(self._tmpdir(), 'config.json')
        self.assertEqual(config.load(path), config.DEFAULTS)
        self.assertFalse(os.path.exists(path))

    def test_a_broken_file_is_the_defaults(self):
        self.assertEqual(config.load(self._write('not json')), config.DEFAULTS)
        self.assertEqual(config.load(self._write('[]')), config.DEFAULTS)

    def test_missing_keys_take_the_default(self):
        path = self._write(json.dumps({'low_battery_notify': True}))
        got = config.load(path)
        self.assertTrue(got['low_battery_notify'])
        self.assertEqual(got['low_battery_percent'], 20)
        self.assertFalse(got['low_battery_resident'])

    def test_the_threshold_snaps_onto_the_spin_steps(self):
        self.assertEqual(config._percent(23), 25)
        self.assertEqual(config._percent(22), 20)
        self.assertEqual(config._percent(1), 5)
        self.assertEqual(config._percent(90), 50)
        self.assertEqual(config._percent(True), 20)
        self.assertEqual(config._percent('nope'), 20)

    def test_a_string_bool_does_not_turn_the_warning_on(self):
        path = self._write(json.dumps({
            'low_battery_notify': 'yes',
            'low_battery_resident': 1,
        }))
        got = config.load(path)
        self.assertFalse(got['low_battery_notify'])
        self.assertFalse(got['low_battery_resident'])

    def test_save_keeps_keys_it_does_not_own(self):
        path = self._write(json.dumps({'theme': 'dark', 'low_battery_percent': 23}))
        config.save({'low_battery_notify': True}, path)
        with open(path) as f:
            raw = json.load(f)
        self.assertEqual(raw['theme'], 'dark')
        self.assertTrue(raw['low_battery_notify'])
        # Untouched, including a value load() would snap. Saving one key
        # must not rewrite the others.
        self.assertEqual(raw['low_battery_percent'], 23)

    def test_save_creates_the_directory(self):
        path = os.path.join(self._tmpdir(), 'nested', 'config.json')
        config.save({'low_battery_percent': 35, 'low_battery_resident': True}, path)
        self.assertEqual(config.load(path), {
            'low_battery_notify': False,
            'low_battery_percent': 35,
            'low_battery_resident': True,
        })

    def _tmpdir(self):
        return self.enterContext(tempfile.TemporaryDirectory())

    def _write(self, text):
        path = os.path.join(self._tmpdir(), 'config.json')
        with open(path, 'w') as f:
            f.write(text)
        return path


if __name__ == '__main__':
    unittest.main()
