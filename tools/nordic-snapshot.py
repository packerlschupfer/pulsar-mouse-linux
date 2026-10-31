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
    for i in range(caps.max_dpi_stages):
        base = N.ADDR_DPI_BASE + i * N.DPI_STAGE_SIZE
        for j in range(3):
            out.setdefault(base + j, (f'DPI stage {i + 1}', str))
        base = N.ADDR_LED_COLOR_BASE + i * N.LED_COLOR_SIZE
        for j, channel in enumerate('RGB'):
            out.setdefault(base + j, (f'stage {i + 1} colour {channel}', str))
    for name, addr in getattr(device, '_BUTTON_ADDRS', N.BUTTON_ADDRS).items():
        for j in range(3):
            out.setdefault(addr + j, (f'button {name}', str))
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
        out['profiles'][str(profile)] = block.hex()
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
        was, now = bytes.fromhex(before), bytes.fromhex(after)
        for addr in range(min(len(was), len(now))):
            if was[addr] == now[addr]:
                continue
            label, show = labels.get(addr, ('unidentified', None))
            decoded = (f'   {label}: {show(was[addr])} -> {show(now[addr])}'
                       if show else f'   {label}')
            lines.append(f'profile {profile}  {addr:#06x}  '
                         f'{was[addr]:02x} -> {now[addr]:02x}{decoded}')
    return lines or ['nothing changed']


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('out', help='JSON file to write this snapshot to')
    parser.add_argument('--diff', metavar='EARLIER',
                        help='an earlier snapshot to compare against')
    parser.add_argument('--all-profiles', action='store_true',
                        help='read every profile, not just the active one '
                             '(asks the mouse to switch, and switches back)')
    parser.add_argument('--device', metavar='NAME',
                        help='driver to use, if more than one mouse is here')
    args = parser.parse_args()

    device = find_device(args.device)
    if not isinstance(device, N.PulsarNordic):
        sys.exit(f"{device.capabilities.name} is not a Nordic-protocol mouse; "
                 "this script only knows that memory layout")
    device.open()
    try:
        taken = snapshot(device, args.all_profiles)
    finally:
        device.close()
    with open(args.out, 'w') as handle:
        json.dump(taken, handle, indent=2)
    print(f"{taken['name']}, firmware {taken['firmware']}, "
          f"profile {taken['active_profile']} -> {args.out}")
    if args.diff:
        with open(args.diff) as handle:
            earlier = json.load(handle)
        print()
        for line in diff_lines(earlier, taken, device):
            print(line)


if __name__ == '__main__':
    main()
