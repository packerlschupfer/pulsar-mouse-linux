"""
Host-side settings for the GUI.

Three keys, in ~/.config/pulsar-mouse/config.json. There was no app config
file before this: Start on Login is a desktop file under ~/.config/autostart,
which is the wrong place for a threshold.

The low-battery warning reads these. It ships off, so a missing file and a
broken one mean the same thing.
"""

import json
import os

CONFIG_DIR = os.path.expanduser('~/.config/pulsar-mouse')
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')

LOW_BATTERY_PERCENT_MIN = 5
LOW_BATTERY_PERCENT_MAX = 50
LOW_BATTERY_PERCENT_STEP = 5

DEFAULTS = {
    'low_battery_notify': False,
    'low_battery_percent': 20,
    'low_battery_resident': False,
}

_KEYS = tuple(DEFAULTS)


def load(path=None):
    """The three keys, with defaults for anything missing or unreadable."""
    raw = _read(path or CONFIG_PATH)
    return {key: _coerce(key, raw.get(key, DEFAULTS[key])) for key in _KEYS}


def save(values, path=None):
    """Merge `values` into the file. Keys this module does not know stay."""
    path = path or CONFIG_PATH
    current = _read(path)
    for key in _KEYS:
        if key in values:
            current[key] = _coerce(key, values[key])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A crash mid-write must not leave a file load() treats as "feature off".
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(current, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _coerce(key, value):
    if key == 'low_battery_percent':
        return _percent(value)
    if isinstance(value, bool):
        return value
    return DEFAULTS[key]


def _percent(value):
    """An int on the spin row's steps. Anything else is the default."""
    # bool is an int, and int(True) is 1, which would become a threshold.
    if isinstance(value, bool):
        return DEFAULTS['low_battery_percent']
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULTS['low_battery_percent']
    lo = LOW_BATTERY_PERCENT_MIN
    hi = LOW_BATTERY_PERCENT_MAX
    step = LOW_BATTERY_PERCENT_STEP
    n = min(hi, max(lo, n))
    n = lo + int(round((n - lo) / step)) * step
    return min(hi, n)
