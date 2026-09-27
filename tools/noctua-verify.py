#!/usr/bin/env python3
"""Verify the Feinmann F01 Noctua Edition driver against the real mouse.

Written for issue #12.  The driver inherits most of its behaviour from the X2
CrazyLight, and the DPI table already turned out to live somewhere else on
this model — four of its six stages agreed anyway, which is exactly why the
mistake survived so long.  Everything else inherited is equally unproven, so
this writes each setting, reads it back, and says which ones the mouse kept.

It does write to the mouse, and puts everything back before it exits,
including when a check fails part-way:

  * Before writing anything it copies every area this driver touches, byte
    for byte, for every profile — the settings block, the two registers above
    it (fan and low power), and the DPI table at 0x1B00.  Afterwards it
    rewrites whatever differs, a whole record at a time, and the last check
    compares every byte.
  * The readable values are printed first as well, in case the mouse is
    unplugged mid-run and they have to be set back by hand.

    PYTHONPATH=src python3 tools/noctua-verify.py

Close Pulsar's web configurator and the pulsar-mouse GUI first, since only
one program can hold the mouse at a time.  Don't touch the mouse while it
runs: its own DPI button changes the very bytes being compared.  Works over
the cable or the dongle.

Do NOT use tools/x2cl-verify.py on this mouse.  It assumes the CrazyLight's
layout, where DPI lives at 0x000C, so its restore would miss this model's
real DPI table and put back bytes that mean nothing.
"""

import sys
import traceback

from pulsar_mouse import find_device
from pulsar_mouse.drivers.feinmann_noctua import (
    ADDR_DPI_TABLE, ADDR_FAN_MODE, ADDR_LOW_POWER_PERCENT, DPI_RECORD_SIZE,
)
from pulsar_mouse.hid import BTN_TYPE_DPI

# Everything the driver reads or writes, as (start, length).  The settings
# block is the CrazyLight's; the other two are this model's own.
REGIONS = ((0x0000, 0xC0), (0x00C0, 0x30), (ADDR_DPI_TABLE, 0x30))

# The records inside those regions.  A restore rewrites whole records, so no
# value is ever left without its checksum.
RECORDS = (
    [(a, 2) for a in (0x00, 0x02, 0x04, 0x06, 0x08, 0x0A)]
    + [(0x0C + 4 * i, 4) for i in range(8)]            # the vestigial DPI area
    + [(0x2C + 4 * i, 4) for i in range(8)]            # stage colours (no LEDs here)
    + [(a, 2) for a in (0x4C, 0x4E, 0x50, 0x52)]
    + [(0x54, 4)]
    + [(a, 2) for a in (0x58, 0x5A, 0x5C, 0x5E)]
    + [(0x60 + 4 * i, 4) for i in range(11)]           # buttons
    + [(a, 2) for a in (0xA9, 0xAB, 0xAD, 0xAF, 0xB1, 0xB3, 0xB5, 0xB7)]
    + [(ADDR_LOW_POWER_PERCENT, 2), (ADDR_FAN_MODE, 2)]
    + [(ADDR_DPI_TABLE + DPI_RECORD_SIZE * i, DPI_RECORD_SIZE) for i in range(8)]
)

results = []


def check(name, ok, detail=''):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f'  — {detail}' if detail else ''}")


def note(name, detail):
    """Something observed rather than judged — no pass or fail attached."""
    print(f"  note  {name}  — {detail}")


def session(device, fn):
    """One open→work→close cycle, the way the GUI and CLI use the driver."""
    device.open()
    try:
        return fn(device)
    finally:
        device.close()


# These reach into the driver on purpose: saving and restoring has to work
# even when the public setters being tested don't.

def raw_bytes(device, profile):
    """Every byte this driver can touch, for one profile."""
    device._ensure_profile(profile)
    out = {}
    for start, length in REGIONS:
        blob = device._mem_read_range(start, length)
        for i, value in enumerate(blob):
            out[start + i] = value
    return out


def put_back(device, profile, want):
    """Rewrite every record that differs from `want`, whole."""
    have = raw_bytes(device, profile)
    covered = set()
    for start, size in RECORDS:
        span = range(start, start + size)
        covered.update(span)
        if any(have.get(a) != want.get(a) for a in span):
            device._mem_write_bytes(start, bytes(want[a] for a in span))
    for addr, value in want.items():
        if addr not in covered and have.get(addr) != value:
            device._mem_write_bytes(addr, bytes([value]))


def differences(before, after):
    return {f'0x{a:04X}': (before[a], after[a])
            for a in sorted(before) if before.get(a) != after.get(a)}


def readable(device, profile):
    caps = device.capabilities
    dpi = device.get_dpi_stages(profile)
    values = {
        'lod': device.get_lod(profile),
        'poll': device.get_polling_rate(profile=profile),
        'dpi': dpi,
        'debounce': device.get_debounce(profile=profile),
        'angle': device.get_angle_snap(profile=profile),
        'ripple': device.get_ripple_control(profile=profile),
        'motion': device.get_motion_sync(profile=profile),
        'fan': device.get_fan_mode(profile=profile),
        'low_power': device.get_low_power_threshold(profile=profile),
        'buttons': {name: device.describe_button(*device.get_button(bid, profile))
                    for name, bid in caps.buttons.items()},
    }
    if caps.has_turbo:
        values['turbo'] = device.get_turbo_mode(profile=profile)
    if caps.power_saving_range is not None:
        values['sleep'] = device.get_power_saving_timeout(profile=profile)
    return values


def describe(profile, v):
    on = lambda flag: 'on' if flag else 'off'
    stages = [dx for dx, _ in v['dpi']['stages'][:v['dpi']['count']]]
    extra = ''.join([f", turbo {on(v['turbo'])}" if 'turbo' in v else '',
                     f", auto sleep {v['sleep']} s" if 'sleep' in v else ''])
    return (f"  profile {profile}: LOD {v['lod']} mm, {v['poll']} Hz, "
            f"stage {v['dpi']['active']} of {stages}\n"
            f"             debounce {v['debounce']} ms, angle snap {on(v['angle'])}, "
            f"ripple {on(v['ripple'])}, motion sync {on(v['motion'])}{extra}\n"
            f"             fan {v['fan']}, low power {v['low_power']}%\n"
            f"             buttons {', '.join(f'{k}={b}' for k, b in v['buttons'].items())}")


def pick(candidates, current):
    return next(c for c in candidates if c != current)


def profile_count(device, claimed):
    """How many profiles the mouse really has.

    The driver inherits its count from the CrazyLight with no evidence, so
    ask: switch to each one and see whether the mouse agrees it went there.
    Tries two past the claimed number in case there are more.
    """
    found = 0
    for p in range(1, claimed + 3):
        try:
            device.set_active_profile(p)
            if device.get_active_profile() == p:
                found = p
            else:
                break
        except Exception:
            break
    return found


def main() -> int:
    device = find_device()
    caps = device.capabilities
    if caps.fan_range is None:
        print(f"This script is for the Noctua Edition; found {caps.name}.")
        return 2
    n = caps.num_profiles
    print(f"{caps.name}  (driver claims {n} profiles)\n")

    home = session(device, lambda d: d.get_active_profile())
    real_profiles = session(device, lambda d: profile_count(d, n))
    session(device, lambda d: d.set_active_profile(home))
    check(f'the driver\'s profile count ({n}) matches the mouse',
          real_profiles == n, f'mouse has {real_profiles}')
    n = min(n, real_profiles) or 1

    print('\nBefore:')
    orig = session(device, lambda d: {
        'bytes': {p: raw_bytes(d, p) for p in range(1, n + 1)},
        'values': {p: readable(d, p) for p in range(1, n + 1)},
    })
    for p in range(1, n + 1):
        print(describe(p, orig['values'][p]))

    other = 2 if n > 1 else 1
    o = orig['values'][other]
    print(f'\nWriting to profile {other} and reading it back:')
    writes = []

    try:
        def plan(label, setter, getter, expected):
            writes.append((label, setter, getter, expected))

        # DPI first, since it is the one already known to differ from the
        # CrazyLight.  A high value exercises the 16-bit encoding properly.
        new_stages = [400, 1600, 28393, caps.dpi_max]
        plan(f'DPI stages {new_stages}',
             lambda d: d.set_dpi_stages(new_stages, 2, other),
             lambda d: [dx for dx, _ in d.get_dpi_stages(other)['stages']], new_stages)

        new_fan = pick(range(caps.fan_range[0], caps.fan_range[1] + 1), o['fan'])
        plan(f'fan mode {new_fan}', lambda d: d.set_fan_mode(new_fan, profile=other),
             lambda d: d.get_fan_mode(profile=other), new_fan)

        new_low = pick((20, 35), o['low_power'])
        plan(f'low power {new_low}%',
             lambda d: d.set_low_power_threshold(new_low, profile=other),
             lambda d: d.get_low_power_threshold(profile=other), new_low)

        new_poll = pick(caps.polling_rates, o['poll'])
        plan(f'polling {new_poll} Hz',
             lambda d: d.set_polling_rate(new_poll, profile=other),
             lambda d: d.get_polling_rate(profile=other), new_poll)

        lo, hi = caps.debounce_range
        new_debounce = pick([v for v in (4, 8) if lo <= v <= hi], o['debounce'])
        plan(f'debounce {new_debounce} ms',
             lambda d: d.set_debounce(new_debounce, profile=other),
             lambda d: d.get_debounce(profile=other), new_debounce)

        plan(f'angle snap {"on" if not o["angle"] else "off"}',
             lambda d: d.set_angle_snap(not o['angle'], profile=other),
             lambda d: d.get_angle_snap(profile=other), not o['angle'])
        plan(f'ripple control {"on" if not o["ripple"] else "off"}',
             lambda d: d.set_ripple_control(not o['ripple'], profile=other),
             lambda d: d.get_ripple_control(profile=other), not o['ripple'])
        plan(f'motion sync {"on" if not o["motion"] else "off"}',
             lambda d: d.set_motion_sync(not o['motion'], profile=other),
             lambda d: d.get_motion_sync(profile=other), not o['motion'])

        if caps.has_turbo:
            plan(f'turbo mode {"on" if not o["turbo"] else "off"}',
                 lambda d: d.set_turbo_mode(not o['turbo'], profile=other),
                 lambda d: d.get_turbo_mode(profile=other), not o['turbo'])

        if caps.power_saving_range is not None:
            s_lo, s_hi, _step = caps.power_saving_range
            new_sleep = pick([v for v in (300, 600) if s_lo <= v <= s_hi], o['sleep'])
            plan(f'auto sleep {new_sleep} s',
                 lambda d: d.set_power_saving_timeout(new_sleep, profile=other),
                 lambda d: d.get_power_saving_timeout(profile=other), new_sleep)

        button = next(name for name in ('thumb1', 'thumb2', 'wheel') if name in caps.buttons)
        bid = caps.buttons[button]
        plan(f'{button} remapped to dpi+',
             lambda d: d.set_button(bid, BTN_TYPE_DPI, 0x01, 0x00, other),
             lambda d: d.describe_button(*d.get_button(bid, other)), 'dpi+')

        write_errors = {}

        def write_all(d):
            for label, setter, _getter, _expected in writes:
                try:
                    setter(d)
                except Exception as e:
                    write_errors[label] = f'{type(e).__name__}: {e}'

        def read_all(d):
            seen = {}
            for label, _setter, getter, _expected in writes:
                try:
                    seen[label] = getter(d)
                except Exception as e:
                    seen[label] = f'error: {e}'
            return seen

        session(device, write_all)
        seen = session(device, read_all)
        for label, _setter, _getter, expected in writes:
            if label in write_errors:
                check(f'{label} on profile {other}', False, write_errors[label])
            else:
                check(f'{label} on profile {other} reads back', seen[label] == expected,
                      f'read {seen[label]}')

        # LOD gets its own pass: only 1.0 and 1.2 mm are confirmed from the
        # capture and the rest of the table is a straight line through those
        # two, so report what each value does instead of failing on it.
        print('\nLOD, one value at a time (only 1.0 and 1.2 mm are confirmed):')
        for mm in sorted(device._LOD_CODES):
            def try_lod(d, mm=mm):
                d.set_lod(mm, other)
                return d.get_lod(other)
            try:
                got = session(device, try_lod)
                note(f'{mm} mm', 'kept' if got == mm else f'reads back as {got}')
            except Exception as e:
                note(f'{mm} mm', f'{type(e).__name__}: {e}')

        # One byte-level check covers every setting above: nothing written to
        # `other` may show up anywhere else.  This is also what would catch a
        # setting that is device-wide when the driver thinks it is per
        # profile, which is how the fan was got wrong the first time.
        now = session(device, lambda d: {p: raw_bytes(d, p) for p in range(1, n + 1)})
        leaked = {p: diff for p in now if p != other
                  if (diff := differences(orig['bytes'][p], now[p]))}
        check('the other profiles are unchanged, byte for byte', not leaked,
              f'changed: {leaked}' if leaked else '')
    except Exception:
        traceback.print_exc()
        check('script ran to completion', False)
    finally:
        print('\nRestoring…')

        def restore(d):
            for p in range(1, n + 1):
                put_back(d, p, orig['bytes'][p])
            d.set_active_profile(home)

        try:
            session(device, restore)
            after = session(device, lambda d: {p: raw_bytes(d, p) for p in range(1, n + 1)})
            wrong = {p: diff for p in after
                     if (diff := differences(orig['bytes'][p], after[p]))}
            check('every byte is back where it started', not wrong,
                  f'still different: {wrong}' if wrong else '')
        except Exception:
            traceback.print_exc()
            check('restore ran to completion', False)

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name in failed:
        print(f"  failed: {name}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
