#!/usr/bin/env python3
"""Protocol soak test — read every setting hundreds of times and report errors.

Catches ack-polling regressions, checksum failures, and intermittent
read timeouts that only surface under sustained load.

Usage:
    sudo PYTHONPATH=src python3 tools/protocol-soak.py [--rounds 500]
"""

import argparse
import sys
import time

from pulsar_mouse import find_device


def soak(device, rounds):
    caps = device.capabilities
    errors = []
    ok = 0

    for r in range(1, rounds + 1):
        try:
            device.get_polling_rate()
            ok += 1
        except Exception as e:
            errors.append((r, 'get_polling_rate', str(e)))

        if caps.has_debounce:
            try:
                device.get_debounce()
                ok += 1
            except Exception as e:
                errors.append((r, 'get_debounce', str(e)))

        if caps.has_angle_snap:
            try:
                device.get_angle_snap()
                ok += 1
            except Exception as e:
                errors.append((r, 'get_angle_snap', str(e)))

        for p in range(1, min(caps.num_profiles + 1, 3)):
            try:
                device.get_dpi_stages(p)
                ok += 1
            except Exception as e:
                errors.append((r, f'get_dpi_stages(p={p})', str(e)))

            if caps.has_led:
                try:
                    device.get_brightness(p)
                    device.get_led_effect(p)
                    ok += 2
                except Exception as e:
                    errors.append((r, f'get_led(p={p})', str(e)))

        if r % 50 == 0:
            print(f'  round {r}/{rounds}: {ok} ok, {len(errors)} errors')

    return ok, errors


def main():
    parser = argparse.ArgumentParser(description='Protocol soak test')
    parser.add_argument('--rounds', type=int, default=500)
    args = parser.parse_args()

    # find_device() raises RuntimeError when nothing is found, it never
    # returns None - the `if device is None: sys.exit(...)` that used to be
    # here was unreachable.
    device = find_device()

    print(f'Soak testing {device.capabilities.name} for {args.rounds} rounds...')
    device.open()
    try:
        t0 = time.monotonic()
        ok, errors = soak(device, args.rounds)
        elapsed = time.monotonic() - t0
    finally:
        device.close()

    print(f'\nDone in {elapsed:.1f}s: {ok} reads OK, {len(errors)} errors')
    if errors:
        print('\nErrors:')
        for r, fn, msg in errors[:20]:
            print(f'  round {r}: {fn}: {msg}')
        if len(errors) > 20:
            print(f'  ... and {len(errors) - 20} more')
        sys.exit(1)


if __name__ == '__main__':
    main()
