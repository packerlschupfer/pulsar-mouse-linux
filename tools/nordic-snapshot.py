#!/usr/bin/env python3
"""Find which byte a Pulsar setting lives in, without capturing USB traffic.

For the wireless mice on the Nordic chipset — Xlite, Xlite V3, X2, X2A, X2H,
X2 V2 and their variants, plus the X2 CrazyLight and the Areson X2.  Their
settings all live in one block of memory in the mouse, and this prints that
block.  Run it, change one thing in Pulsar Fusion, run it again, and the
diff says which address moved and by how much:

    python3 tools/nordic-snapshot.py before.json
    (change one setting in Fusion, and nothing else)
    python3 tools/nordic-snapshot.py after.json --diff before.json

    profile 1  0x00a9  03 -> 02   debounce: 3 ms -> 2 ms
    profile 1  0x00b7  06 -> 05   auto-sleep: 60 s -> 50 s

That is how the sleep timer, the per-profile settings and the lift-off
encoding on these mice were each pinned down (#30), and it needs no capture,
no Windows tooling and no video.  An address with no label is one nobody has
identified: those are the interesting ones.

Read-only by default.  It reads the mouse's settings memory and never writes
to it, so it cannot change your settings.  --all-profiles is the one
exception worth knowing about: a Nordic mouse shows one profile's settings
at a time, so reading the others means asking it to switch, which is what
this app and Fusion both do anyway.  It switches back afterwards.  Without
that flag it only reads the profile the mouse is already on.

Close the pulsar-mouse GUI first: while that holds the device, this cannot.
If you get a permission error, the udev rules in udev/50-pulsar-mouse.rules
are not installed for your mouse's USB ID.

Attach both JSON files, or just the diff if you would rather not share the
rest.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from pulsar_mouse import find_device                        # noqa: E402
from pulsar_mouse.drivers import nordic as N                # noqa: E402

SETTINGS_END = 0xC9     # the block every driver here reads
SHORTCUT_END = N.SHORTCUT_BASE + 8 * N.SHORTCUT_SIZE - 1    # 0x100-0x1ff


def _hz(val):
    return f'{N.POLL_VAL_TO_HZ.get(val, val)} Hz'


def _labels(device):
    """Address -> (label, formatter), from the driver's own constants."""
    caps = device.capabilities
    out = {
        N.ADDR_POLLING_RATE: ('polling rate', _hz),
        N.ADDR_DPI_STAGE_COUNT: ('DPI stage count', str),
        N.ADDR_ACTIVE_DPI_STAGE: ('active DPI stage', lambda v: str(v + 1)),
        N.ADDR_LOD_MM: ('lift-off', lambda v: f'code {v:#04x}'),
        N.ADDR_LED_EFFECT: ('LED effect', str),
        N.ADDR_LED_BRIGHTNESS: ('LED brightness', str),
        N.ADDR_LED_BREATHE_SPEED: ('breathe speed', str),
        N.ADDR_LED_ENABLED: ('LED on', str),
        N.ADDR_DEBOUNCE: ('debounce', lambda v: f'{v} ms'),
        N.ADDR_MOTION_SYNC: ('motion sync', str),
        N.ADDR_ANGLE_SNAP: ('angle snap', str),
        N.ADDR_RIPPLE_CONTROL: ('ripple control', str),
        N.ADDR_TURBO_MODE: ('turbo mode', str),
        N.ADDR_AUTOSLEEP: ('auto-sleep', lambda v: f'{v * 10} s'),
        N.ADDR_AUTOSLEEP_ALT: ('0xAD, unidentified', str),
    }
    # Every record here is its bytes plus a checksum, 0x55 minus their sum.
    # Labelling that byte too, because an unlabelled one reads as a find:
    # @bluesceada chased 0x0073 as unidentified when it was thumb1's (#30).
    for i in range(caps.max_dpi_stages):
        base = N.ADDR_DPI_BASE + i * N.DPI_STAGE_SIZE
        for j in range(3):
            out.setdefault(base + j, (f'DPI stage {i + 1}', str))
        out.setdefault(base + 3, (f'DPI stage {i + 1} checksum', str))
        base = N.ADDR_LED_COLOR_BASE + i * N.LED_COLOR_SIZE
        for j, channel in enumerate('RGB'):
            out.setdefault(base + j, (f'stage {i + 1} colour {channel}', str))
        out.setdefault(base + 3, (f'stage {i + 1} colour checksum', str))
    addrs = getattr(device, '_BUTTON_ADDRS', N.BUTTON_ADDRS)
    for name, addr in addrs.items():
        out.setdefault(addr, (f'button {name} (what it does)', str))
        out.setdefault(addr + 1, (f'button {name} (first argument)', str))
        out.setdefault(addr + 2, (f'button {name} (second argument)', str))
        out.setdefault(addr + 3, (f'button {name} checksum', str))
        # Mode 0x05 means the binding itself is a 32-byte record up here.
        start = N.SHORTCUT_BASE + N.SHORTCUT_SIZE * (
            (addr - N.ADDR_BUTTON_BASE) // N.BUTTON_SIZE)
        for j in range(N.SHORTCUT_SIZE):
            out.setdefault(start + j, (f'{name}: shortcut record', str))
    return out


def snapshot(device, all_profiles=False):
    """Everything this mouse will say about itself, as plain data."""
    caps = device.capabilities
    out = {'driver': type(device).__name__, 'name': caps.name, 'profiles': {}}
    try:
        out['firmware'] = device.get_firmware_version()
    except Exception as why:
        out['firmware'] = f'unavailable ({why})'
    for extra in ('get_model_ids', 'get_power'):
        try:
            out[extra] = getattr(device, extra)()
        except Exception:
            pass
    try:
        active = device.get_active_profile()
    except Exception:
        active = 1
    out['active_profile'] = active
    wanted = range(1, caps.num_profiles + 1) if all_profiles else [active]
    for profile in wanted:
        device._ensure_profile(profile)
        block = bytes(device._mem.get(a, 0) for a in range(SETTINGS_END + 1))
        shortcuts = b''
        try:
            # Not in the memory map open() caches: a keyboard or media
            # binding is a record up at 0x100, and without it a button
            # changed in Fusion shows only its slot moving to mode 05 (#30).
            shortcuts = device._mem_read_range(
                N.SHORTCUT_BASE, SHORTCUT_END - N.SHORTCUT_BASE + 1)
        except Exception as why:
            print(f'  (profile {profile}: could not read the shortcut '
                  f'table: {why})')
        out['profiles'][str(profile)] = {'settings': block.hex(),
                                         'shortcuts': shortcuts.hex()}
    return out


def diff_lines(old, new, device=None):
    """What changed between two snapshots, address by address."""
    labels = _labels(device) if device is not None else {}
    lines = []
    for key in ('firmware', 'name', 'active_profile', 'get_model_ids'):
        if key in old and old.get(key) != new.get(key):
            lines.append(f'{key}: {old[key]} -> {new.get(key)}')
    for profile, after in sorted(new['profiles'].items()):
        before = old['profiles'].get(profile)
        if before is None:
            lines.append(f'profile {profile}: not in the first snapshot')
            continue
        for part, base in (('settings', 0), ('shortcuts', N.SHORTCUT_BASE)):
            # A snapshot taken before the shortcut table was covered is a
            # bare settings string; it can still be diffed, as far as it goes.
            old_part = before.get(part, '') if isinstance(before, dict) else (
                before if part == 'settings' else '')
            new_part = after.get(part, '') if isinstance(after, dict) else (
                after if part == 'settings' else '')
            if not old_part or not new_part:
                if new_part and part == 'shortcuts':
                    lines.append(f'profile {profile}: the earlier snapshot '
                                 'has no shortcut table to compare')
                continue
            lines.extend(_part_lines(profile, base, old_part, new_part,
                                     labels))
    return lines or ['nothing changed']


def _part_lines(profile, base, before, after, labels):
    lines = []
    was, now = bytes.fromhex(before), bytes.fromhex(after)
    for offset in range(min(len(was), len(now))):
        addr = base + offset
        if was[offset] == now[offset]:
            continue
        label, show = labels.get(addr, ('unidentified', None))
        decoded = (f'   {label}: {show(was[offset])} -> {show(now[offset])}'
                   if show else f'   {label}')
        lines.append(f'profile {profile}  {addr:#06x}  '
                     f'{was[offset]:02x} -> {now[offset]:02x}{decoded}')
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('out', nargs='?',
                        help='JSON file to write this snapshot to; leave it '
                             'out with --diff to compare without saving')
    parser.add_argument('--diff', metavar='EARLIER',
                        help='an earlier snapshot to compare against')
    parser.add_argument('--all-profiles', action='store_true',
                        help='read every profile, not just the active one '
                             '(asks the mouse to switch, and switches back)')
    parser.add_argument('--device', metavar='NAME',
                        help='driver to use, if more than one mouse is here')
    args = parser.parse_args()
    if not args.out and not args.diff:
        parser.error('give a file to write, or --diff to compare without one')

    device = find_device(args.device)
    if not isinstance(device, N.PulsarNordic):
        sys.exit(f"{device.capabilities.name} is not a Nordic-protocol mouse; "
                 "this script only knows that memory layout")
    device.open()
    try:
        taken = snapshot(device, args.all_profiles)
    finally:
        device.close()
    if args.out:
        with open(args.out, 'w') as handle:
            json.dump(taken, handle, indent=2)
    print(f"{taken['name']}, firmware {taken['firmware']}, "
          f"profile {taken['active_profile']}"
          + (f' -> {args.out}' if args.out else ' (not saved)'))
    if args.diff:
        with open(args.diff) as handle:
            earlier = json.load(handle)
        print()
        for line in diff_lines(earlier, taken, device):
            print(line)


if __name__ == '__main__':
    main()
