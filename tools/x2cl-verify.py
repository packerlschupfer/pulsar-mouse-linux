#!/usr/bin/env python3
"""Verify profile handling and writes on a real Pulsar X2 CrazyLight.

Written for issue #7.  It does write to the mouse, but everything is put back
before it exits, including when a check fails part-way:

  * Before writing anything it copies the settings area of every profile,
    byte for byte.  Afterwards it rewrites whatever differs, a whole record at
    a time.  The restore bypasses the setters under test, so a broken setter
    can't also break the restore, and the last check compares every byte.
  * The readable values are printed first as well, in case the mouse is
    unplugged mid-run and they have to be set back by hand.

    PYTHONPATH=src python3 tools/x2cl-verify.py

Close Pulsar's web configurator and the pulsar-mouse GUI first, since only one
program can hold the mouse at a time.  Don't touch the mouse while it runs:
its DPI and profile buttons change the very bytes being compared.  Works over
the cable or the dongle.
"""

import sys
import traceback

from pulsar_mouse import find_device
from pulsar_mouse.hid import BTN_TYPE_DPI

SETTINGS_END = 0xC0     # the per-profile settings the driver reads and writes

# The records in that area, from docs/protocol-x2-crazylight.md.  A restore
# rewrites whole records, so no value is ever left without its checksum.
RECORDS = (
    [(a, 2) for a in (0x00, 0x02, 0x04, 0x06, 0x08, 0x0A)]
    + [(0x0C + 4 * i, 4) for i in range(8)]            # DPI stages
    + [(0x2C + 4 * i, 4) for i in range(8)]            # stage colours
    + [(a, 2) for a in (0x4C, 0x4E, 0x50, 0x52)]       # LED effect, brightness, speed, on/off
    + [(0x54, 4)]                                      # LED colour
    + [(a, 2) for a in (0x58, 0x5A, 0x5C, 0x5E)]
    + [(0x60 + 4 * i, 4) for i in range(11)]           # buttons
    + [(a, 2) for a in (0xA9, 0xAB, 0xAD, 0xAF, 0xB1, 0xB3, 0xB5, 0xB7)]
)

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


def stored_poll(device, profile):
    """The rate the profile holds, not the one this link can carry.

    Over the cable get_polling_rate() reports at most 1 kHz however high the
    profile is set.
    """
    getter = getattr(device, 'get_stored_polling_rate', device.get_polling_rate)
    return getter(profile=profile)


def power_saving_supported(device):
    return (hasattr(device, 'get_power_saving_timeout')
            and device.capabilities.power_saving_range is not None)


# These three reach into the driver on purpose: saving and restoring has to
# work even when the public setters being tested don't.

def settings_bytes(device, profile):
    device._ensure_profile(profile)
    return bytes(device._mem.get(a, 0) for a in range(SETTINGS_END))


def put_back(device, profile, want):
    """Rewrite every record that differs from `want`, whole."""
    have = settings_bytes(device, profile)
    covered = set()
    for start, size in RECORDS:
        span = range(start, start + size)
        covered.update(span)
        if any(have[a] != want[a] for a in span):
            device._mem_write_bytes(start, want[start:start + size])
    for a in range(SETTINGS_END):
        if a not in covered and have[a] != want[a]:
            device._mem_write_bytes(a, want[a:a + 1])


def differences(before, after):
    return [f'0x{a:02x}' for a in range(SETTINGS_END) if before[a] != after[a]]


def readable(device, profile):
    caps = device.capabilities
    dpi = device.get_dpi_stages(profile)
    values = {
        'lod': device.get_lod(profile),
        'poll': stored_poll(device, profile),
        'live': device.get_polling_rate(profile=profile),
        'dpi': dpi,
        'debounce': device.get_debounce(profile=profile),
        'angle': device.get_angle_snap(profile=profile),
        'ripple': device.get_ripple_control(profile=profile),
        'motion': device.get_motion_sync(profile=profile),
        'led': device.get_led_effect(profile),
        'brightness': device.get_brightness(profile),
        'breathe': device.get_breathe_speed(profile),
        'colours': [device.get_stage_color(i, profile) for i in range(1, dpi['count'] + 1)],
        'buttons': {name: device.describe_button(*device.get_button(bid, profile))
                    for name, bid in caps.buttons.items()},
    }
    if caps.has_turbo:
        values['turbo'] = device.get_turbo_mode(profile=profile)
    if power_saving_supported(device):
        values['sleep'] = device.get_power_saving_timeout(profile=profile)
    return values


def describe(profile, v):
    on = lambda flag: 'on' if flag else 'off'
    stages = [dx for dx, _ in v['dpi']['stages'][:v['dpi']['count']]]
    rate = (f"{v['poll']} Hz" if v['poll'] == v['live']
            else f"{v['live']} Hz here, stores {v['poll']} Hz")
    extra = ''.join([f", turbo {on(v['turbo'])}" if 'turbo' in v else '',
                     f", auto sleep {v['sleep']} s" if 'sleep' in v else ''])
    colours = ' '.join(f'#{r:02X}{g:02X}{b:02X}' for r, g, b in v['colours'])
    return (f"  profile {profile}: LOD {v['lod']} mm, {rate}, stage {v['dpi']['active']} of {stages}\n"
            f"             debounce {v['debounce']} ms, angle snap {on(v['angle'])}, "
            f"ripple {on(v['ripple'])}, motion sync {on(v['motion'])}{extra}\n"
            f"             LED {v['led']} (brightness {v['brightness']}, speed {v['breathe']}), "
            f"colours {colours}\n"
            f"             buttons {', '.join(f'{k}={b}' for k, b in v['buttons'].items())}")


def pick(candidates, current):
    return next(c for c in candidates if c != current)


def main() -> int:
    device = find_device()
    caps = device.capabilities
    n = caps.num_profiles
    print(f"{caps.name}  ({n} profiles)\n")

    def snapshot(d):
        active = d.get_active_profile()
        per_profile = {p: (settings_bytes(d, p), readable(d, p)) for p in range(1, n + 1)}
        return {'active': active, 'fw': d.get_firmware_version(),
                'bytes': {p: b for p, (b, _v) in per_profile.items()},
                'values': {p: v for p, (_b, v) in per_profile.items()}}

    orig = session(device, snapshot)
    home = orig['active']
    print(f"Firmware {orig['fw']}, active profile {home}.  Original values, in case"
          f" they need restoring by hand:")
    for p, v in orig['values'].items():
        print(describe(p, v))
    print()

    other = 2 if home != 2 else 3
    o = orig['values'][other]
    try:
        # Reading every profile must not move the mouse.
        after = session(device, lambda d: d.get_active_profile())
        check('reading all profiles leaves the mouse where it was',
              after == home, f'started on {home}, ended on {after}')

        # An explicit switch must stick after the connection closes.
        session(device, lambda d: d.set_active_profile(other))
        now = session(device, lambda d: d.get_active_profile())
        check(f'switching to profile {other} sticks', now == other, f'mouse is on {now}')

        # Switch back before writing.  Profile-less settings follow the active
        # profile, so with the mouse left on `other` a setter that ignored its
        # profile argument would still land there, and pass by accident.
        session(device, lambda d: d.set_active_profile(home))
        now = session(device, lambda d: d.get_active_profile())
        check(f'switching back to profile {home} sticks', now == home, f'mouse is on {now}')

        # DPI values, including the coarse range.
        for want in (1230, 12800):
            stages = [dx for dx, _ in o['dpi']['stages'][:o['dpi']['count']]]
            stages[0] = want
            session(device, lambda d: d.set_dpi_stages(stages, o['dpi']['active'], other))
            got = session(device, lambda d: d.get_dpi_stages(other)['stages'][0][0])
            check(f'DPI {want} on profile {other} stage 1 reads back', got == want, f'read {got}')

        # Everything else: write it all in one session, read it back in another.
        writes = []

        def plan(label, setter, getter, expected):
            writes.append((label, setter, getter, expected))

        new_lod = pick(caps.lod_values, o['lod'])
        plan(f'LOD {new_lod} mm', lambda d: d.set_lod(new_lod, other),
             lambda d: d.get_lod(other), new_lod)
        new_poll = pick(caps.polling_rates, o['poll'])
        plan(f'polling {new_poll} Hz', lambda d: d.set_polling_rate(new_poll, profile=other),
             lambda d: stored_poll(d, other), new_poll)
        new_stage = pick(range(1, o['dpi']['count'] + 1), o['dpi']['active'])
        plan(f'active DPI stage {new_stage}', lambda d: d.set_active_dpi_stage(new_stage, other),
             lambda d: d.get_active_dpi_stage(other), new_stage)
        lo, hi = caps.debounce_range
        new_debounce = pick([v for v in (4, 8) if lo <= v <= hi], o['debounce'])
        plan(f'debounce {new_debounce} ms', lambda d: d.set_debounce(new_debounce, profile=other),
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
        if power_saving_supported(device):
            s_lo, s_hi, _step = caps.power_saving_range
            new_sleep = pick([v for v in (300, 600) if s_lo <= v <= s_hi], o['sleep'])
            plan(f'auto sleep {new_sleep} s',
                 lambda d: d.set_power_saving_timeout(new_sleep, profile=other),
                 lambda d: d.get_power_saving_timeout(profile=other), new_sleep)
        b_lo, b_hi = caps.brightness_range
        new_brightness = pick([v for v in (0x3C, 0x96) if b_lo <= v <= b_hi], o['brightness'])
        plan(f'LED brightness {new_brightness}', lambda d: d.set_brightness(new_brightness, other),
             lambda d: d.get_brightness(other), new_brightness)
        sp_lo, sp_hi = caps.breathe_speed_range
        new_speed = pick([v for v in (2, 4) if sp_lo <= v <= sp_hi], o['breathe'])
        plan(f'LED breathing speed {new_speed}', lambda d: d.set_breathe_speed(new_speed, other),
             lambda d: d.get_breathe_speed(other), new_speed)
        new_effect = pick(caps.led_effects, o['led'])
        plan(f'LED effect {new_effect}', lambda d: d.set_led_effect(new_effect, other),
             lambda d: d.get_led_effect(other), new_effect)
        new_colour = pick([(0x12, 0x34, 0x56), (0x65, 0x43, 0x21)], o['colours'][0])
        plan('stage 1 colour #{:02X}{:02X}{:02X}'.format(*new_colour),
             lambda d: d.set_stage_color(1, *new_colour, other),
             lambda d: d.get_stage_color(1, other), new_colour)
        button = next(name for name in ('thumb1', 'thumb2', 'wheel') if name in caps.buttons)
        bid = caps.buttons[button]
        plan(f'{button} remapped to dpi+', lambda d: d.set_button(bid, BTN_TYPE_DPI, 0x01, 0x00, other),
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

        # One byte-level check covers every setting above: nothing written to
        # `other` may show up anywhere else.
        now = session(device, lambda d: {p: settings_bytes(d, p) for p in range(1, n + 1)})
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
            active, raw = session(device, lambda d: (
                d.get_active_profile(),
                {p: settings_bytes(d, p) for p in range(1, n + 1)}))
            left = {p: diff for p in raw if (diff := differences(orig['bytes'][p], raw[p]))}
            detail = (f'still different: {left}' if left
                      else f'mouse is on profile {active}, not {home}' if active != home else '')
            check('everything restored, byte for byte', not left and active == home, detail)
        except Exception:
            traceback.print_exc()
            check('everything restored, byte for byte', False,
                  'restore by hand from the values above')

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — failed: {', '.join(failed)}" if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
