"""
When the low-battery warning fires.

The GUI holds the latch and talks to the notification daemon.  This module
only decides, so the rules can be tested without a display or a mouse.
"""

# Five points above the threshold, so a reading that flickers on the
# threshold does not send the warning again.
HYSTERESIS = 5

# The picture beside the text.  A standard theme icon, so it does not
# depend on the app logo being installed.  The logo stays the application
# icon, from the desktop file.  Plasma draws that column only from a file
# it can load, and hides it when the icon is the same as the logo, so the
# GUI sends a resolved path of this icon when it can find one.
NOTIFICATION_IMAGE = 'battery-caution'


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


def warning_summary() -> str:
    """The title.  The app name is already shown, so this is the problem."""
    return 'Battery low'


def warning_body(name, percent) -> str:
    """The line under the title: which mouse, and the percentage."""
    return f'{name} is at {percent}%.'


def notification_timeout(resident: bool) -> int:
    """How long the desktop may keep the warning up, in milliseconds.

    0 means never expire, which is "keep it until I dismiss it" without
    raising the urgency.  Critical urgency would also show during Do Not
    Disturb on Plasma, and the switch does not ask for that.  -1 lets the
    desktop choose for an ordinary warning.
    """
    return 0 if resident else -1


def notification_hints(resident: bool, image=None) -> dict:
    """Freedesktop hint values.  The GUI wraps them as D-Bus variants.

    Urgency stays normal either way.  `resident` means a click on an
    action does not dismiss the warning, which is separate from how long
    it stays up.  `image-path` is the picture beside the text: the theme
    name, or a file of that same icon when the caller resolved one.
    `desktop-entry` is the installed desktop file, without the suffix,
    and that is where the logo comes from.
    """
    hints = {
        'urgency': 1,
        'desktop-entry': 'pulsar-mouse',
        'image-path': image or NOTIFICATION_IMAGE,
    }
    if resident:
        hints['resident'] = True
    return hints
