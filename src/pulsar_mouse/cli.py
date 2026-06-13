#!/usr/bin/env python3
"""
pulsar-mouse — Generic CLI for Pulsar gaming mice.

Adapts dynamically to the connected device's capabilities.
"""

import sys
import argparse

from pulsar_mouse import find_device, __version__
from pulsar_mouse.base import PulsarDevice
from pulsar_mouse.drivers import discover_all
from pulsar_mouse.hid import describe_button, parse_button_function


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
            if effect == 'breath' and caps.has_breath_speed:
                speed = device.get_breath_speed(profile)
                print(f"  LED:              {effect}  speed={speed}/{caps.breath_speed_range[1]}  brightness={bright}/{caps.brightness_range[1]}")
            else:
                print(f"  LED:              {effect}  brightness={bright}/{caps.brightness_range[1]}")
        except Exception as e:
            print(f"  LED:              error ({e})")
    try:
        print(f"  Buttons:")
        for name, bid in caps.buttons.items():
            t, a1, a2 = device.get_button(bid, profile)
            print(f"    {name:<8} (0x{bid:02x}): {describe_button(t, a1, a2)}")
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

  %(prog)s --poll 1000              # set polling rate to 1000 Hz
  %(prog)s --debounce 3             # set debounce time to 3 ms
  %(prog)s --angle-snap on
  %(prog)s --ripple on
  %(prog)s --motion-sync off

  %(prog)s --profile 1 --lod 1              # set LOD to 1 mm
  %(prog)s --profile 1 --dpi 400,800,1600   # set 3 DPI stages (active=1)
  %(prog)s --profile 1 --dpi 400,800,1600 --active-stage 2
  %(prog)s --profile 1 --brightness 200
  %(prog)s --profile 1 --led steady
  %(prog)s --profile 1 --led breath --breath-speed 50
  %(prog)s --profile 1 --stage-color 1 29 96 cd   # R G B for stage 1

  %(prog)s --profile 1 --button thumb1 dpi+
  %(prog)s --profile 1 --button thumb1 ctrl+c
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

    # Global settings
    g = p.add_argument_group('global settings (shared across all profiles)')
    g.add_argument('--poll', type=int, metavar='HZ',
                   help='Polling rate (Hz)')
    g.add_argument('--debounce', type=int, metavar='MS',
                   help='Debounce time in ms')
    g.add_argument('--angle-snap', metavar='on|off')
    g.add_argument('--ripple', metavar='on|off')
    g.add_argument('--motion-sync', metavar='on|off')

    # Per-profile settings
    pp = p.add_argument_group('per-profile settings (require --profile N)')
    pp.add_argument('--lod', type=int, metavar='MM',
                    help='Lift-off distance in mm')
    pp.add_argument('--dpi', metavar='D1[,D2,...]',
                    help='Comma-separated DPI stage values')
    pp.add_argument('--active-stage', type=int, metavar='N',
                    help='Active DPI stage index')
    pp.add_argument('--brightness', type=int, metavar='0-255')
    pp.add_argument('--led', metavar='steady|breath',
                    help='LED effect')
    pp.add_argument('--breath-speed', type=int, metavar='0-100')
    pp.add_argument('--stage-color', nargs=4, metavar=('STAGE', 'R', 'G', 'B'),
                    type=int, help='Set DPI stage LED color (RGB 0-255)')
    pp.add_argument('--button', nargs=2, metavar=('BTN', 'FUNC'),
                    help='Remap a button')
    pp.add_argument('--reset', action='store_true',
                    help='Reset profile to factory defaults')

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    device_name = getattr(args, 'device', None)

    write_ops = any([
        args.poll, args.debounce,
        args.angle_snap, args.ripple, args.motion_sync,
        args.lod, args.dpi, args.active_stage,
        args.brightness, args.led, args.breath_speed,
        args.stage_color, args.button, args.reset,
    ])

    profile_required = any([
        args.lod, args.dpi, args.active_stage,
        args.brightness, args.led, args.breath_speed,
        args.stage_color, args.button, args.reset,
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

    device.open()
    try:
        if write_ops:
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

            if args.led is not None:
                device.set_led_effect(args.led, prof)
                print(f"Profile {prof} LED effect: {args.led}")

            if args.breath_speed is not None:
                device.set_breath_speed(args.breath_speed, prof)
                print(f"Profile {prof} breath speed: {args.breath_speed}/{caps.breath_speed_range[1]}")

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
                    t, a1, a2 = parse_button_function(func_spec)
                except ValueError as e:
                    sys.exit(str(e))
                device.set_button(btn_id, t, a1, a2, prof)
                print(f"Profile {prof} {btn_name} → {describe_button(t, a1, a2)}")

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
