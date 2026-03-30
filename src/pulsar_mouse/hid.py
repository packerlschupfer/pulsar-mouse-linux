"""
Shared HID constants and utilities for Pulsar mouse drivers.

Button function types, action tables, HID keycodes, and the
describe_button / parse_button_function helpers are device-independent
and shared across all drivers.
"""

# ── Button function types ────────────────────────────────────────────────────

BTN_TYPE_MOUSE     = 0x01   # standard mouse click
BTN_TYPE_KEYBOARD  = 0x02   # keyboard shortcut
BTN_TYPE_SCROLL    = 0x03   # scroll wheel
BTN_TYPE_PROFILE   = 0x08   # profile switch
BTN_TYPE_DPI       = 0x09   # DPI function
BTN_TYPE_MEDIA     = 0x0d   # HID Consumer Control (multimedia)
BTN_TYPE_XCLICK    = 0x0e   # double-click

# ── Mouse click actions (type 0x01) ─────────────────────────────────────────

MOUSE_ACTIONS = {
    'left': 0x01, 'right': 0x02, 'wheel': 0x03,
    'forward': 0x04, 'backward': 0x05,
}
MOUSE_ACTION_NAMES = {v: k for k, v in MOUSE_ACTIONS.items()}

# ── DPI actions (type 0x09) ─────────────────────────────────────────────────

DPI_ACTIONS = {'dpi+': 0x01, 'dpi-': 0x02, 'dpiloop': 0x03}
DPI_ACTION_NAMES = {v: k for k, v in DPI_ACTIONS.items()}

# ── Profile actions (type 0x08) ─────────────────────────────────────────────

PROFILE_ACTIONS = {'profile+': 0x03, 'profile-': 0x04}
PROFILE_ACTION_NAMES = {v: k for k, v in PROFILE_ACTIONS.items()}

# ── Scroll actions (type 0x03) ──────────────────────────────────────────────

SCROLL_ACTIONS = {'scrollup': 0x01, 'scrolldown': 0xff}
SCROLL_ACTION_NAMES = {0x01: 'scrollup', 0xff: 'scrolldown'}

# ── HID Consumer Usage codes for multimedia (type 0x0d) ─────────────────────

MEDIA_CODES = {
    'play':       0x00cd,
    'next':       0x00b5,
    'prev':       0x00b6,
    'stop':       0x00b7,
    'mute':       0x00e2,
    'vol+':       0x00e9,
    'vol-':       0x00ea,
    'mediaplayer':0x0183,
}
MEDIA_CODE_NAMES = {v: k for k, v in MEDIA_CODES.items()}

# ── HID keyboard modifier bits (type 0x02, byte[9]) ────────────────────────

HID_MODS = {
    'ctrl': 0x01, 'lctrl': 0x01,
    'shift': 0x02, 'lshift': 0x02,
    'alt': 0x04, 'lalt': 0x04,
    'gui': 0x08, 'super': 0x08, 'win': 0x08,
    'rctrl': 0x10, 'rshift': 0x20, 'ralt': 0x40, 'rgui': 0x80,
}

# ── HID keyboard usage codes (type 0x02, byte[10]) ─────────────────────────

_alpha = {chr(ord('a') + i): 0x04 + i for i in range(26)}
_digits = {'1': 0x1e, '2': 0x1f, '3': 0x20, '4': 0x21, '5': 0x22,
           '6': 0x23, '7': 0x24, '8': 0x25, '9': 0x26, '0': 0x27}
_special = {
    'enter': 0x28, 'esc': 0x29, 'backspace': 0x2a, 'tab': 0x2b, 'space': 0x2c,
    'minus': 0x2d, 'equal': 0x2e, 'lbracket': 0x2f, 'rbracket': 0x30,
    'backslash': 0x31, 'semicolon': 0x33, 'quote': 0x34, 'grave': 0x35,
    'comma': 0x36, 'dot': 0x37, 'slash': 0x38, 'capslock': 0x39,
    'f1': 0x3a, 'f2': 0x3b, 'f3': 0x3c, 'f4': 0x3d, 'f5': 0x3e,
    'f6': 0x3f, 'f7': 0x40, 'f8': 0x41, 'f9': 0x42, 'f10': 0x43,
    'f11': 0x44, 'f12': 0x45, 'delete': 0x4c, 'home': 0x4a, 'end': 0x4d,
    'pageup': 0x4b, 'pagedown': 0x4e, 'right': 0x4f, 'left': 0x50,
    'down': 0x51, 'up': 0x52, 'insert': 0x49,
}
HID_KEYS = {**_alpha, **_digits, **_special}


# ── Button description / parsing ────────────────────────────────────────────

def describe_button(btn_type: int, a1: int, a2: int) -> str:
    """Human-readable description of a button binding."""
    if btn_type == BTN_TYPE_MOUSE:
        return MOUSE_ACTION_NAMES.get(a1, f'mouse(0x{a1:02x})')
    if btn_type == BTN_TYPE_KEYBOARD:
        mod_parts = [n for n, v in HID_MODS.items()
                     if v & a1 and n in ('ctrl','shift','alt','gui','rctrl','rshift','ralt','rgui')]
        key_parts = [n for n, v in HID_KEYS.items() if v == a2]
        key = key_parts[0] if key_parts else f'0x{a2:02x}'
        if mod_parts:
            return '+'.join(mod_parts + [key])
        return key
    if btn_type == BTN_TYPE_SCROLL:
        return SCROLL_ACTION_NAMES.get(a1, f'scroll(0x{a1:02x})')
    if btn_type == BTN_TYPE_PROFILE:
        return PROFILE_ACTION_NAMES.get(a1, f'profile(0x{a1:02x})')
    if btn_type == BTN_TYPE_DPI:
        return DPI_ACTION_NAMES.get(a1, f'dpi(0x{a1:02x})')
    if btn_type == BTN_TYPE_MEDIA:
        code = a1 | (a2 << 8)
        return MEDIA_CODE_NAMES.get(code, f'media(0x{code:04x})')
    if btn_type == BTN_TYPE_XCLICK:
        return 'xclick-double'
    return f'type=0x{btn_type:02x} a1=0x{a1:02x} a2=0x{a2:02x}'


def parse_button_function(spec: str) -> tuple[int, int, int]:
    """
    Parse a button function string into (type, action1, action2).

    Formats:
      left / right / wheel / forward / backward
      scrollup / scrolldown
      dpi+ / dpi- / dpiloop
      profile+ / profile-
      xclick
      play / next / prev / stop / mute / vol+ / vol- / mediaplayer
      ctrl+c / shift+f5 / alt+tab / ...  (keyboard shortcut)
    """
    s = spec.strip().lower()

    if s in MOUSE_ACTIONS:
        return (BTN_TYPE_MOUSE, MOUSE_ACTIONS[s], 0x00)
    if s in SCROLL_ACTIONS:
        return (BTN_TYPE_SCROLL, SCROLL_ACTIONS[s], 0x00)
    if s in DPI_ACTIONS:
        return (BTN_TYPE_DPI, DPI_ACTIONS[s], 0x00)
    if s in PROFILE_ACTIONS:
        return (BTN_TYPE_PROFILE, PROFILE_ACTIONS[s], 0x00)
    if s == 'xclick':
        return (BTN_TYPE_XCLICK, 0x01, 0x00)
    if s in MEDIA_CODES:
        code = MEDIA_CODES[s]
        return (BTN_TYPE_MEDIA, code & 0xff, (code >> 8) & 0xff)

    # Keyboard shortcut: mod+mod+key
    mod = 0
    key = 0
    for part in s.split('+'):
        if part in HID_MODS:
            mod |= HID_MODS[part]
        elif part in HID_KEYS:
            key = HID_KEYS[part]
        else:
            raise ValueError(
                f"Unknown button function or key '{part}' in '{spec}'.\n"
                f"  Mouse: {', '.join(MOUSE_ACTIONS)}\n"
                f"  Scroll: scrollup, scrolldown\n"
                f"  DPI: dpi+, dpi-, dpiloop\n"
                f"  Profile: profile+, profile-\n"
                f"  Media: {', '.join(MEDIA_CODES)}\n"
                f"  Keyboard: [modifier+]key  e.g. ctrl+c, shift+f5"
            )
    if key:
        return (BTN_TYPE_KEYBOARD, mod, key)

    raise ValueError(f"Cannot parse button function: '{spec}'")
