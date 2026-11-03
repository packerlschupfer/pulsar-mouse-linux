"""
When the low-battery warning fires.

The GUI holds the latch and talks to the notification daemon.  This module
only decides, so the rules can be tested without a display or a mouse.
"""

import os

# Five points above the threshold, so a reading that flickers on the
# threshold does not send the warning again.
HYSTERESIS = 5


def should_notify(percent, charging, latched, threshold) -> bool:
    """True for the one reading that should send the warning.

    A later poll of the same discharge stays quiet while `latched` is set.
    Charging is never a warning.  The first reading of a session is just
    `latched` being false, so a mouse that is already low warns on startup.
    """
    if charging or latched:
        return False
    return percent <= threshold


def latch_released(percent, charging, threshold) -> bool:
    """True when a warning already sent no longer applies.

    Charging ends it immediately.  Otherwise the percentage has to be five
    points above the threshold.
    """
    if charging:
        return True
    return percent >= threshold + HYSTERESIS


def warning_summary(name, percent) -> str:
    """The notification title: which mouse, and the percentage."""
    return f'{name} — {percent}%'


def notification_icon(paths) -> str:
    """The first logo file that exists, otherwise the icon name.

    A name is resolved by the icon theme the notification daemon loaded
    at login.  A logo installed after that still shows as a blank
    document.  The file is loaded directly.
    """
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return 'pulsar-mouse'


def notification_hints(resident: bool) -> dict:
    """Freedesktop hint values.  The GUI wraps them as D-Bus variants.

    Urgency 1 uses the desktop's own timeout.  Urgency 2 plus `resident`
    asks it to stay until dismissed.  `desktop-entry` is the installed
    desktop file, without the suffix.
    """
    hints = {
        'urgency': 2 if resident else 1,
        'desktop-entry': 'pulsar-mouse',
    }
    if resident:
        hints['resident'] = True
    return hints
