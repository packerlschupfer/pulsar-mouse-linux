#!/usr/bin/env python3
"""Verify profile handling and writes on a real Pulsar X2 CrazyLight.

Written for issue #7.  Every check that writes restores the original value
before it finishes, including when something fails part-way — the original
values are also printed up front so they can be put back by hand if the
mouse is unplugged mid-run.

    PYTHONPATH=src python3 tools/x2cl-verify.py

Close Pulsar's web configurator and the pulsar-mouse GUI first: only one
program can hold the mouse at a time.  Works over the cable or the dongle.
"""

import sys
import traceback

from pulsar_mouse import find_device

results = []


def check(name, ok, detail=''):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f'  — {detail}' if detail else ''}")


def session(device, fn):
    """One open→work→close cycle, the way the GUI and CLI use the driver."""
    device.open()
    try:
        return fn(device)
    finally:
        device.close()


def main() -> int:
    device = find_device()
    caps = device.capabilities
    n = caps.num_profiles
    print(f"{caps.name}  ({n} profiles)\n")

    def snapshot(d):
        return {
            'active': d.get_active_profile(),
            'fw': d.get_firmware_version(),
            'profiles': {p: {'lod': d.get_lod(p),
                             'poll': d.get_polling_rate(profile=p),
                             'dpi': d.get_dpi_stages(p)}
                         for p in range(1, n + 1)},
        }

    orig = session(device, snapshot)
    home = orig['active']
    print(f"Firmware {orig['fw']}, active profile {home}.  Original values, in case"
          f" they need restoring by hand:")
    for p, v in orig['profiles'].items():
        stages = [dx for dx, _ in v['dpi']['stages'][:v['dpi']['count']]]
        print(f"  profile {p}: LOD {v['lod']} mm, {v['poll']} Hz, "
              f"stage {v['dpi']['active']} of {stages}")
    print()

    other = 2 if home != 2 else 3
    try:
        # 1. Reading every profile must not move the mouse.
        after = session(device, lambda d: d.get_active_profile())
        check('reading all profiles leaves the mouse where it was',
              after == home, f'started on {home}, ended on {after}')

        # 2. An explicit switch must stick after the connection closes.
        session(device, lambda d: d.set_active_profile(other))
        now = session(device, lambda d: d.get_active_profile())
        check(f'switching to profile {other} sticks', now == other, f'mouse is on {now}')

        # 3. A write to one profile lands there and nowhere else.
        lod_values = [v for v in caps.lod_values if v != orig['profiles'][other]['lod']]
        new_lod = lod_values[0]
        session(device, lambda d: d.set_lod(new_lod, other))
        seen = session(device, lambda d: {p: d.get_lod(p) for p in range(1, n + 1)})
        check(f'LOD write to profile {other} reads back', seen[other] == new_lod,
              f'wrote {new_lod}, read {seen[other]}')
        leaked = [p for p in seen if p != other and seen[p] != orig['profiles'][p]['lod']]
        check('the other profiles kept their LOD', not leaked,
              f'changed: {leaked}' if leaked else '')

        # 4. A DPI value round-trips, including the coarse range.
        for want in (1230, 12800):
            dpi = orig['profiles'][other]['dpi']
            stages = [dx for dx, _ in dpi['stages'][:dpi['count']]]
            stages[0] = want
            session(device, lambda d: d.set_dpi_stages(stages, dpi['active'], other))
            got = session(device, lambda d: d.get_dpi_stages(other)['stages'][0][0])
            check(f'DPI {want} on profile {other} stage 1 reads back', got == want, f'read {got}')

        # 5. A profile-less setting goes to the profile it was aimed at.
        poll = orig['profiles'][other]['poll']
        target = next(hz for hz in caps.polling_rates if hz != poll)
        session(device, lambda d: d.set_polling_rate(target, profile=other))
        seen = session(device, lambda d: {p: d.get_polling_rate(profile=p) for p in range(1, n + 1)})
        check(f'polling {target} Hz lands on profile {other}', seen[other] == target,
              f'read {seen[other]}')
        leaked = [p for p in seen if p != other and seen[p] != orig['profiles'][p]['poll']]
        check('the other profiles kept their polling rate', not leaked,
              f'changed: {leaked}' if leaked else '')
    except Exception:
        traceback.print_exc()
        check('script ran to completion', False)
    finally:
        print('\nRestoring…')
        def restore(d):
            for p, v in orig['profiles'].items():
                d.set_lod(v['lod'], p)
                d.set_polling_rate(v['poll'], profile=p)
                stages = [dx for dx, _ in v['dpi']['stages'][:v['dpi']['count']]]
                d.set_dpi_stages(stages, v['dpi']['active'], p)
            d.set_active_profile(home)
        try:
            session(device, restore)
            back = session(device, snapshot)
            same = back['profiles'] == orig['profiles'] and back['active'] == home
            check('everything restored', same)
        except Exception:
            traceback.print_exc()
            check('everything restored', False, 'restore by hand from the values above')

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — failed: {', '.join(failed)}" if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
