#!/usr/bin/env python3
"""
pulsar-mouse — Generic CLI for Pulsar gaming mice.

Adapts dynamically to the connected device's capabilities.
"""

import sys
import json
import argparse

from pulsar_mouse import find_device, __version__
from pulsar_mouse.base import PulsarDevice
from pulsar_mouse.drivers import discover_all
from pulsar_mouse.hid import describe_button, parse_button_function  # fallback for non-device contexts


def _on_off(val: bool) -> str:
    return 'on' if val else 'off'


def _parse_bool(s, name):
    if s in ('on', '1', 'yes', 'true'):
        return True
    if s in ('off', '0', 'no', 'false'):
        return False
    raise ValueError(f"--{name}: expected on or off, got '{s}'")


def print_global(device: PulsarDevice):
    caps = device.capabilities
    try:
        fw = device.get_firmware_version()
        if fw != 'unknown':
            print(f"  Firmware:         {fw}")
    except Exception:
        pass
    try:
        print(f"  Active profile:   {device.get_active_profile()}")
    except Exception:
        pass
    if hasattr(device, 'get_power'):
        try:
            pwr = device.get_power()
            charging = "  (charging)" if pwr['power_connected'] else ""
            mv = f"  {pwr['battery_mv']} mV" if pwr.get('battery_mv') is not None else ""
            print(f"  Battery:          {pwr['battery_percent']}%{mv}{charging}")
        except Exception as e:
            print(f"  Battery:          error ({e})")
    try:
        print(f"  Polling rate:     {device.get_polling_rate()} Hz")
    except Exception as e:
        print(f"  Polling rate:     error ({e})")
    if caps.has_debounce:
        try:
            print(f"  Debounce:         {device.get_debounce()} ms")
        except Exception as e:
            print(f"  Debounce:         error ({e})")
    if caps.has_angle_snap:
        try:
            print(f"  Angle snap:       {_on_off(device.get_angle_snap())}")
        except Exception as e:
            print(f"  Angle snap:       error ({e})")
    if caps.has_ripple_control:
        try:
            print(f"  Ripple control:   {_on_off(device.get_ripple_control())}")
        except Exception as e:
            print(f"  Ripple control:   error ({e})")
    if caps.has_motion_sync:
        try:
            print(f"  Motion sync:      {_on_off(device.get_motion_sync())}")
        except Exception as e:
            print(f"  Motion sync:      error ({e})")
    if hasattr(device, 'get_power_saving_timeout'):
        try:
            print(f"  Power saving:     {device.get_power_saving_timeout()} s")
        except Exception as e:
            print(f"  Power saving:     error ({e})")
    if hasattr(device, 'get_low_power_threshold'):
        try:
            print(f"  Low power mode:   {device.get_low_power_threshold()}%")
        except Exception as e:
            print(f"  Low power mode:   error ({e})")


def print_profile(device: PulsarDevice, profile: int):
    caps = device.capabilities
    print(f"\n── Profile {profile} {'─'*47}")
    try:
        info = device.get_dpi_stages(profile)
        print(f"  DPI stages ({info['count']} active, stage {info['active']} selected):")
        for i, (dx, dy) in enumerate(info['stages'], 1):
            marker = " ◄" if i == info['active'] else ""
            if caps.has_stage_colors:
                color = device.get_stage_color(i, profile)
                print(f"    Stage {i}: {dx} DPI  #{color[0]:02X}{color[1]:02X}{color[2]:02X}{marker}")
            else:
                print(f"    Stage {i}: {dx} DPI{marker}")
    except Exception as e:
        print(f"  DPI:              error ({e})")
    if caps.lod_values:
        try:
            print(f"  LOD:              {device.get_lod(profile)} mm")
        except Exception as e:
            print(f"  LOD:              error ({e})")
    if caps.has_led:
        try:
            effect = device.get_led_effect(profile)
            bright = device.get_brightness(profile)
            # Every driver's pulsing/breathing effect is named 'breathe',
            # but check by position (always the last entry in led_effects
            # by convention) rather than the literal name, in case a
            # future driver ever needs its own different name again.
            if caps.has_breathe_speed and effect == caps.led_effects[-1]:
                speed = device.get_breathe_speed(profile)
                print(f"  LED:              {effect}  speed={speed}/{caps.breathe_speed_range[1]}  brightness={bright}/{caps.brightness_range[1]}")
            else:
                print(f"  LED:              {effect}  brightness={bright}/{caps.brightness_range[1]}")
        except Exception as e:
            print(f"  LED:              error ({e})")
    try:
        print(f"  Buttons:")
        for name, bid in caps.buttons.items():
            t, a1, a2 = device.get_button(bid, profile)
            print(f"    {name:<8} (0x{bid:02x}): {device.describe_button(t, a1, a2)}")
    except Exception as e:
        print(f"  Buttons:          error ({e})")


def print_all(device: PulsarDevice):
    print_global(device)
    for p in range(1, device.capabilities.num_profiles + 1):
        print_profile(device, p)


def build_parser(caps=None):
    """Build argparse parser. If caps is provided, tailor help text."""
    p = argparse.ArgumentParser(
        prog='pulsar-mouse',
        description='Pulsar Mouse Linux configuration tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # show all settings
  %(prog)s --profile 1              # show profile 1 only
  %(prog)s --active-profile 2       # switch the mouse to profile 2

  %(prog)s --poll 1000              # set polling rate on the ACTIVE profile
  %(prog)s --active-profile 2 --debounce 3  # combining both is safe -
  %(prog)s --debounce 3 --active-profile 2  # --active-profile always
                                     #   applies first regardless of order
  %(prog)s --angle-snap on
  %(prog)s --ripple on
  %(prog)s --motion-sync off

  %(prog)s --profile 1 --lod 1              # set LOD to 1 mm
  %(prog)s --profile 1 --dpi 400,800,1600   # set 3 DPI stages (active=1)
  %(prog)s --profile 1 --dpi 400,800,1600 --active-stage 2
  %(prog)s --profile 1 --brightness 200
  %(prog)s --profile 1 --led steady
  %(prog)s --profile 1 --led breathe --breathe-speed 50
  %(prog)s --profile 1 --stage-color 1 29 96 cd   # R G B for stage 1

  %(prog)s --profile 1 --button thumb1 dpi+
  %(prog)s --profile 1 --button thumb1 ctrl+c
  %(prog)s --profile 1 --export profile1.json
  %(prog)s --profile 1 --import profile1.json
  %(prog)s --profile 1 --reset
""")

    # Device selection
    drivers = discover_all()
    if len(drivers) > 1:
        p.add_argument('--device', metavar='NAME',
                       choices=sorted(drivers),
                       help=f'Device driver to use ({", ".join(sorted(drivers))})')

    p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')

    p.add_argument('--profile', type=int, metavar='N',
                   help='Profile to read/write (default: all for read)')

    p.add_argument('--active-profile', type=int, metavar='N',
                   help="Switch the mouse's active profile (which profile it "
                        "actually uses during normal operation - independent "
                        "of --profile above, which only targets a profile's "
                        "stored settings for this command)")

    p.add_argument('--battery-json', action='store_true',
                   help='Print battery/charging status as one JSON line and exit '
                        '(for scripts - e.g. {"battery_percent": 85, '
                        '"power_connected": false, "battery_mv": null})')

    p.add_argument('--status-json', action='store_true',
                   help='Print polling rate/DPI stages as one JSON line and exit '
                        '(for scripts - polling_rate, polling_rates, and dpi for '
                        '--profile, or profile 1 if omitted)')

    # Settings tracked per the mouse's *active* profile - unlike the
    # per-profile settings below, these have no --profile N targeting: the
    # only way to change one for a specific profile is to first switch to
    # it with --active-profile N. Live-verified 2026-08-09 (feinmann8k.py)
    # that these actually vary by active profile on at least that driver,
    # despite the name this group used to have ("global settings, shared
    # across all profiles") - that was wrong, not just imprecise: writing
    # one of these while a non-default profile was active used to silently
    # land on profile 1 instead, a real bug now fixed at the driver level.
    g = p.add_argument_group(
        "settings tracked per the mouse's active profile "
        "(use --active-profile to change which one)")
    g.add_argument('--poll', type=int, metavar='HZ',
                   help='Polling rate (Hz)')
    g.add_argument('--debounce', type=int, metavar='MS',
                   help='Debounce time in ms')
    g.add_argument('--angle-snap', metavar='on|off')
    g.add_argument('--ripple', metavar='on|off')
    g.add_argument('--motion-sync', metavar='on|off')
    g.add_argument('--power-saving', type=int, metavar='SECONDS',
                   help='Wireless power-saving timeout (30-900 seconds)')
    g.add_argument('--low-power', type=int, metavar='PERCENT',
                   help='Low power mode battery threshold (0-100)')

    # Per-profile settings
    pp = p.add_argument_group('per-profile settings (require --profile N)')
    pp.add_argument('--lod', type=float, metavar='MM',
                    help='Lift-off distance in mm (some devices support 0.1mm steps)')
    pp.add_argument('--dpi', metavar='D1[,D2,...]',
                    help='Comma-separated DPI stage values')
    pp.add_argument('--active-stage', type=int, metavar='N',
                    help='Active DPI stage index')
    pp.add_argument('--brightness', type=int, metavar='0-255')
    pp.add_argument('--brightness-percent', type=int, metavar='0-100',
                    help='Same as --brightness, but 0-100%% scaled to the '
                         'device\'s actual raw range (matches the GUI/'
                         '--status-json\'s brightness_percent)')
    pp.add_argument('--led', metavar='steady|breathe',
                    help='LED effect')
    pp.add_argument('--breathe-speed', type=int, metavar='0-100')
    pp.add_argument('--stage-color', nargs=4, metavar=('STAGE', 'R', 'G', 'B'),
                    type=int, help='Set DPI stage LED color (RGB 0-255)')
    pp.add_argument('--button', nargs=2, metavar=('BTN', 'FUNC'),
                    help='Remap a button')
    pp.add_argument('--export', metavar='FILE',
                    help='Export profile settings to JSON file')
    pp.add_argument('--import', metavar='FILE', dest='import_file',
                    help='Import profile settings from JSON file')
    pp.add_argument('--reset', action='store_true',
                    help='Reset profile to factory defaults')

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    device_name = getattr(args, 'device', None)

    # `is not None` (not plain truthiness) - `--debounce 0`, `--brightness 0`,
    # `--breathe-speed 0`, etc. are legitimate values, and a truthy check made
    # them indistinguishable from the flag being omitted entirely, silently
    # falling through to read-mode instead of performing the write.
    # `args.reset` is a plain store_true flag, not an optional value, so it's
    # checked separately rather than folded into the `is not None` list.
    write_ops = args.reset or any(x is not None for x in [
        args.poll, args.debounce,
        args.angle_snap, args.ripple, args.motion_sync,
        args.power_saving, args.low_power, args.active_profile,
        args.lod, args.dpi, args.active_stage,
        args.brightness, args.brightness_percent, args.led, args.breathe_speed,
        args.stage_color, args.button,
        args.import_file,
    ])

    profile_required = args.reset or any(x is not None for x in [
        args.lod, args.dpi, args.active_stage,
        args.brightness, args.brightness_percent, args.led, args.breathe_speed,
        args.stage_color, args.button,
        args.export, args.import_file,
    ])

    if profile_required and args.profile is None:
        sys.exit("Error: --profile N is required for per-profile settings")

    try:
        device = find_device(device_name)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")

    caps = device.capabilities

    if args.profile is not None and not 1 <= args.profile <= caps.num_profiles:
        sys.exit(f"Error: --profile must be 1–{caps.num_profiles}")

    if args.active_profile is not None and not 1 <= args.active_profile <= caps.num_profiles:
        sys.exit(f"Error: --active-profile must be 1–{caps.num_profiles}")

    if args.battery_json:
        if not hasattr(device, 'get_power'):
            # Not an error - a wired mouse just has no battery to report.
            # Exit 0 so scripts can tell "no battery" apart from "device
            # not found"/a failed read (both of those exit non-zero).
            print(json.dumps({'wireless': False}))
            return
        device.open()
        try:
            pwr = device.get_power()
            if hasattr(device, 'get_low_power_threshold'):
                pwr['low_power_threshold'] = device.get_low_power_threshold()
            print(json.dumps({'wireless': True, **pwr}))
        finally:
            device.close()
        return

    if args.status_json:
        # This response mixes two different "which profile" scopes: dpi/
        # lod/led/brightness/breathe_speed below are read from `profile`
        # (the --profile N argument, or 1 if omitted), while
        # polling_rate/debounce/angle_snap/ripple_control/motion_sync/
        # power_saving/low_power have no --profile N targeting at all and
        # always reflect whichever profile is `active_profile` (which may
        # differ from `profile`) - see cli.py's argparse group help text
        # for "settings tracked per the mouse's active profile" for the
        # underlying reason. A consumer that needs both a specific
        # profile's stored settings AND that profile actually active
        # should call --active-profile first (see main()'s ordering
        # comment) so `profile` and `active_profile` end up equal.
        profile = args.profile if args.profile is not None else 1
        device.open()
        try:
            dpi_info = device.get_dpi_stages(profile=profile)
            status = {
                'wireless': hasattr(device, 'get_power'),
                'profile': profile,
                'num_profiles': caps.num_profiles,
                'active_profile': (device.get_active_profile()
                                    if hasattr(device, 'get_active_profile') else None),
                'polling_rate': device.get_polling_rate(),
                'polling_rates': caps.polling_rates,
                'dpi': {
                    'active': dpi_info['active'],
                    'stages': [dx for dx, _dy in dpi_info['stages'][:dpi_info['count']]],
                },
            }
            if caps.has_debounce:
                lo, hi = caps.debounce_range
                status['debounce'] = {'value': device.get_debounce(), 'min': lo, 'max': hi}
            if caps.has_angle_snap:
                status['angle_snap'] = device.get_angle_snap()
            if caps.has_ripple_control:
                status['ripple_control'] = device.get_ripple_control()
            if caps.has_motion_sync:
                status['motion_sync'] = device.get_motion_sync()
            if caps.lod_values:
                lod_val = device.get_lod(profile)
                if caps.lod_step is not None:
                    status['lod'] = {
                        'value': lod_val,
                        'min': caps.lod_values[0],
                        'max': caps.lod_values[-1],
                        'step': caps.lod_step,
                    }
                else:
                    status['lod'] = {'value': lod_val, 'options': caps.lod_values}
            # Not DeviceCapabilities fields - purely hasattr-gated, same as
            # the GUI's own Power Management page (see gui.py). Bounds
            # (30-900s, 0-100%) match that page's sliders and feinmann8k.py's
            # own set_power_saving_timeout() validation - there's no
            # discoverable capability constant for them to read instead.
            if hasattr(device, 'get_power_saving_timeout'):
                status['power_saving'] = {
                    'value': device.get_power_saving_timeout(), 'min': 30, 'max': 900,
                }
            if hasattr(device, 'get_low_power_threshold'):
                status['low_power'] = {
                    'value': device.get_low_power_threshold(), 'min': 0, 'max': 100,
                }
            if caps.has_led:
                lo, hi = caps.brightness_range
                brightness = device.get_brightness(profile)
                led = {
                    'effects': caps.led_effects,
                    'effect': device.get_led_effect(profile),
                    # Same 0-100% convention as the GUI (raw range varies
                    # by driver, usually 0-255) - round-trips through
                    # --brightness-percent below, not raw --brightness.
                    'brightness_percent': round((brightness - lo) / (hi - lo) * 100) if hi > lo else 0,
                }
                if caps.has_breathe_speed:
                    led['breathe_speed'] = device.get_breathe_speed(profile)
                status['led'] = led
            print(json.dumps(status))
        finally:
            device.close()
        return

    if args.export:
        device.open()
        try:
            data = device.export_profile(args.profile)
            with open(args.export, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"Profile {args.profile} exported to {args.export}")
        finally:
            device.close()
        return

    device.open()
    try:
        if args.import_file:
            with open(args.import_file) as f:
                data = json.load(f)
            if data.get('format') != 'pulsar-mouse-profile':
                sys.exit("Error: not a valid pulsar-mouse profile file")
            warnings = device.import_profile(args.profile, data)
            print(f"Profile {args.profile} imported from {args.import_file}")
            for w in warnings:
                print(f"  Warning: {w}")

        if write_ops:
            # active-profile first: every "global" write below actually
            # targets whichever profile is active at the moment it runs
            # (see cli.py's own settings-group help text) - applying it
            # after them, as this used to, meant
            # `--active-profile 2 --poll 1000` silently wrote the polling
            # rate to the OLD active profile and only switched afterward.
            if args.active_profile is not None:
                device.set_active_profile(args.active_profile)
                print(f"Active profile set to {args.active_profile}")

            # ── Global writes ─────────────────────────────────────────
            if args.poll is not None:
                device.set_polling_rate(args.poll)
                print(f"Polling rate set to {args.poll} Hz")

            if args.debounce is not None:
                device.set_debounce(args.debounce)
                print(f"Debounce set to {args.debounce} ms")

            if args.angle_snap is not None:
                v = _parse_bool(args.angle_snap, 'angle-snap')
                device.set_angle_snap(v)
                print(f"Angle snap: {_on_off(v)}")

            if args.ripple is not None:
                v = _parse_bool(args.ripple, 'ripple')
                device.set_ripple_control(v)
                print(f"Ripple control: {_on_off(v)}")

            if args.motion_sync is not None:
                v = _parse_bool(args.motion_sync, 'motion-sync')
                device.set_motion_sync(v)
                print(f"Motion sync: {_on_off(v)}")

            if args.power_saving is not None:
                device.set_power_saving_timeout(args.power_saving)
                print(f"Power saving timeout set to {args.power_saving} s")

            if args.low_power is not None:
                device.set_low_power_threshold(args.low_power)
                print(f"Low power mode threshold set to {args.low_power}%")

            # ── Per-profile writes ────────────────────────────────────
            prof = args.profile
            if args.lod is not None:
                device.set_lod(args.lod, prof)
                print(f"Profile {prof} LOD set to {args.lod} mm")

            if args.dpi is not None:
                stages = [int(x.strip()) for x in args.dpi.split(',')]
                active = args.active_stage if args.active_stage else 1
                device.set_dpi_stages(stages, active, prof)
                print(f"Profile {prof} DPI stages: {stages}  active={active}")
            elif args.active_stage is not None:
                device.set_active_dpi_stage(args.active_stage, prof)
                print(f"Profile {prof} active DPI stage: {args.active_stage}")

            if args.brightness is not None:
                device.set_brightness(args.brightness, prof)
                print(f"Profile {prof} brightness: {args.brightness}/{caps.brightness_range[1]}")

            if args.brightness_percent is not None:
                lo, hi = caps.brightness_range
                raw = round(lo + args.brightness_percent / 100 * (hi - lo))
                device.set_brightness(raw, prof)
                print(f"Profile {prof} brightness: {args.brightness_percent}% ({raw}/{hi})")

            if args.led is not None:
                device.set_led_effect(args.led, prof)
                print(f"Profile {prof} LED effect: {args.led}")

            if args.breathe_speed is not None:
                device.set_breathe_speed(args.breathe_speed, prof)
                print(f"Profile {prof} breathe speed: {args.breathe_speed}/{caps.breathe_speed_range[1]}")

            if args.stage_color is not None:
                stage, r, g, b = args.stage_color
                device.set_stage_color(stage, r, g, b, prof)
                print(f"Profile {prof} stage {stage} color: #{r:02X}{g:02X}{b:02X}")

            if args.button is not None:
                btn_name, func_spec = args.button
                btn_id = caps.buttons.get(btn_name.lower())
                if btn_id is None:
                    sys.exit(f"Unknown button '{btn_name}'. "
                             f"Use: {', '.join(caps.buttons)}")
                try:
                    t, a1, a2 = device.parse_button_function(func_spec)
                except ValueError as e:
                    sys.exit(str(e))
                device.set_button(btn_id, t, a1, a2, prof)
                print(f"Profile {prof} {btn_name} → {device.describe_button(t, a1, a2)}")

            if args.reset:
                device.reset_to_defaults(prof)
                print(f"Profile {prof} reset to factory defaults")
        else:
            # ── Read mode ─────────────────────────────────────────────
            print(f"{caps.name} — current settings")
            print("══════════════════════════════════════════════════════")
            print()
            print("Global:")
            if args.profile is None:
                print_all(device)
            else:
                try:
                    print_global(device)
                except Exception as e:
                    print(f"  (error reading global settings: {e})")
                print_profile(device, args.profile)
            print()
    finally:
        device.close()


if __name__ == '__main__':
    main()
