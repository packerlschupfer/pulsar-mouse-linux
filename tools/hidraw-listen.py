#!/usr/bin/env python3
"""Record what a Pulsar mouse sends on its own, unprompted.

The GUI can show live "Connection Quality" and update the DPI when you press
the mouse's own DPI button — but only for drivers that know which reports the
mouse pushes.  The Sonix drivers (X2A, X2H, Xlite, Feinmann) know; the Nordic
ones (X2 CrazyLight, X2 Wireless/Areson, X2A Wireless) don't, because nobody
has seen those reports yet.  This collects them (issue #7).

Works with any Pulsar mouse, so it can also be pointed at a model that already
reports events, to check that the script itself is catching them.

    python3 tools/hidraw-listen.py

Read-only.  It listens on the mouse's hidraw nodes and never writes to the
mouse, so it can't change your settings.  It needs no extra Python packages.

Close the pulsar-mouse GUI first: while that holds the device, these reports
go to it instead of here.  If you get a permission error, the udev rules in
udev/50-pulsar-mouse.rules aren't installed for your mouse's USB ID.

It walks you through a few steps, then writes everything it saw to
pulsar-hidraw-log.txt in the current directory.  Attach that file.

Keep the mouse still except where a step says otherwise: moving it floods the
log with movement reports from its own HID interface.

Checked against a Pulsar X2A Wired, whose events are already known: six
presses of its DPI button arrived on interface 1 as
`05 05 <stage> <dpi lo> <dpi hi>`, alongside the movement flood.
"""

import argparse
import glob
import os
import select
import sys
import time

PULSAR_VIDS = (0x3710, 0x3554, 0x25A7)
MAX_EXAMPLES = 6     # per report shape, per step

STEPS = [
    ('Baseline', 20,
     "Don't touch the mouse. This catches anything it sends on a timer, which\n"
     "     is how signal quality arrives on the models that report it."),
    ('DPI button', 20,
     "Press the mouse's DPI button about once every 4 seconds.\n"
     "     Watch which DPI stage it lands on, and note the order afterwards."),
    ('Profile button', 20,
     "If the mouse has a profile button, press it a few times.\n"
     "     If it doesn't, just wait this one out."),
    ('Away from the dongle', 30,
     "Carry the mouse as far from the dongle as you can for ~15 seconds,\n"
     "     then bring it back. Skip if you're on the cable."),
    ('Settle', 10, "Leave the mouse alone again."),
]


def discover(vids=PULSAR_VIDS):
    """Every hidraw node belonging to a Pulsar mouse, with its USB interface."""
    found = []
    for uevent in sorted(glob.glob('/sys/class/hidraw/hidraw*/device/uevent')):
        try:
            info = dict(line.split('=', 1) for line in open(uevent).read().splitlines()
                        if '=' in line)
        except OSError:
            continue
        hid_id = info.get('HID_ID', '')                    # 0003:00003710:00005406
        parts = hid_id.split(':')
        if len(parts) != 3:
            continue
        try:
            vid, pid = int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            continue
        if vid not in vids:
            continue
        interface = None
        device_dir = os.path.realpath(os.path.dirname(uevent))
        for chunk in device_dir.split('/'):
            if ':1.' in chunk:                             # usb interface node, e.g. 3-2:1.1
                interface = chunk.rsplit('.', 1)[1]
        busnum = devnum = None
        probe = device_dir
        while probe != '/':                                # walk up to the USB device itself
            try:
                busnum = int(open(os.path.join(probe, 'busnum')).read())
                devnum = int(open(os.path.join(probe, 'devnum')).read())
                break
            except (OSError, ValueError):
                probe = os.path.dirname(probe)
        found.append({
            'node': '/dev/' + uevent.split('/')[4],
            'vid': vid, 'pid': pid,
            'interface': interface,
            'busnum': busnum, 'devnum': devnum,
            'phys': info.get('HID_PHYS', ''),
            'name': info.get('HID_NAME', ''),
        })
    return found


def holders(busnum, devnum):
    """Processes with this USB device open, found by matching open file
    descriptors rather than guessing from command lines."""
    if busnum is None or devnum is None:
        return []
    target = f'/dev/bus/usb/{busnum:03d}/{devnum:03d}'
    out = []
    for entry in glob.glob('/proc/[0-9]*/fd/*'):
        try:
            if os.readlink(entry) != target:
                continue
        except OSError:
            continue
        pid = entry.split('/')[2]
        try:
            cmd = open(f'/proc/{pid}/cmdline', 'rb').read().replace(b'\0', b' ').decode(
                'utf-8', 'replace').strip()
        except OSError:
            cmd = '?'
        if (pid, cmd) not in out:
            out.append((pid, cmd))
    return out


def warn_missing_config_interface(nodes):
    """The reports we're after arrive on the config interface, whose HID_PHYS
    ends in /input1.  If a device is missing that node, something has claimed
    the interface over libusb, which removes the node until it lets go — and
    then this script cannot see anything worth seeing."""
    warned = False
    for (vid, pid) in sorted({(n['vid'], n['pid']) for n in nodes}):
        same = [n for n in nodes if (n['vid'], n['pid']) == (vid, pid)]
        if any(n['phys'].endswith('/input1') for n in same):
            continue
        warned = True
        print(f"\n  !!  {vid:04x}:{pid:04x} has no hidraw node for its config interface "
              f"(only interface {', '.join(str(n['interface']) for n in same)}).")
        print("      That is the interface these reports arrive on, so this run would")
        print("      tell you nothing. Something has it claimed over USB.")
        for pid_, cmd in holders(same[0].get('busnum'), same[0].get('devnum')):
            print(f"      Holding it now: pid {pid_}  {cmd[:70]}")
        print("      The pulsar-mouse app keeps running in the tray after you close its")
        print("      window, and grabs the mouse every time it polls the battery. Quit it")
        print("      (pkill -f pulsar-mouse), or unplug and replug the device, then re-run.")
    return warned


def collect(fds, seconds, on_report):
    """Read every report that arrives within `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        ready, _w, _x = select.select(list(fds), [], [], min(remaining, 0.5))
        for fd in ready:
            try:
                data = os.read(fd, 256)
            except BlockingIOError:
                continue
            except OSError as e:
                on_report(fd, None, e)
                fds.pop(fd, None)
                continue
            if data:
                on_report(fd, data, None)


def shape(data):
    """Reports are grouped by their first two bytes, which is what separates a
    mouse-movement report from an event."""
    return data[:2].hex(' ')


def annotate(data):
    # The two shapes the Sonix drivers already decode, so a known-good mouse
    # shows recognisable output and an unknown one can be compared against it.
    if len(data) >= 5 and data[0] == 0x05 and data[1] == 0x05:
        dpi = int.from_bytes(data[3:5], 'little')
        return f'  <- DPI event: stage byte {data[2]}, {dpi} DPI'
    if len(data) >= 3 and data[0] == 0x05 and data[1] == 0x0d:
        return f'  <- signal quality {data[2]}%'
    if len(data) == 17 and data[0] == 0x08:
        ok = ((0x55 - sum(data[:16])) & 0xFF) == data[16]
        return f'  <- config report, command 0x{data[1]:02x}{"" if ok else ", BAD CHECKSUM"}'
    return ''


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--log', default='pulsar-hidraw-log.txt', help='where to write the full log')
    ap.add_argument('--scale', type=float, default=1.0,
                    help='shorten every step by this factor, for a quick trial run')
    ap.add_argument('--vid', type=lambda s: int(s, 16), action='append',
                    help='extra USB vendor ID to match, in hex')
    args = ap.parse_args(argv)

    nodes = discover(tuple(PULSAR_VIDS) + tuple(args.vid or ()))
    if not nodes:
        print('No Pulsar mouse found among the hidraw devices.')
        print('Plug it in (or plug in its dongle) and try again.')
        return 1

    print('Keep the mouse still except where a step asks otherwise.\n')
    print('Found:')
    for n in nodes:
        picked = '   <- the driver would listen here' if n['phys'].endswith('/input1') else ''
        print(f"  {n['node']}  {n['vid']:04x}:{n['pid']:04x}  interface {n['interface']}"
              f"  {n['name']}{picked}")

    if warn_missing_config_interface(nodes):
        print()

    fds = {}
    for n in nodes:
        try:
            fds[os.open(n['node'], os.O_RDONLY | os.O_NONBLOCK)] = n
        except OSError as e:
            print(f"  (can't open {n['node']}: {e})")
    if not fds:
        print('\nNothing could be opened — see the udev note at the top of this script.')
        return 1

    log = open(args.log, 'w')
    log.write('# pulsar-mouse: unprompted hidraw reports\n')
    for n in nodes:
        log.write(f"# {n['node']} {n['vid']:04x}:{n['pid']:04x} interface {n['interface']} "
                  f"phys={n['phys']} name={n['name']}\n")
    started = time.monotonic()
    summary = []

    try:
        for title, seconds, instruction in STEPS:
            seconds = max(seconds * args.scale, 0.05)
            print(f'\n=== {title} ({seconds:.0f}s) ===\n     {instruction}')
            log.write(f'\n## {title}\n')
            counts, examples, errors = {}, {}, []

            def on_report(fd, data, error, counts=counts, examples=examples, errors=errors):
                node = fds.get(fd, {}).get('node', '?')
                when = time.monotonic() - started
                if error is not None:
                    errors.append(f'{node}: {error}')
                    log.write(f'{when:8.3f}  {node}  read error: {error}\n')
                    return
                key = (node, shape(data))
                counts[key] = counts.get(key, 0) + 1
                if counts[key] <= MAX_EXAMPLES:
                    examples.setdefault(key, []).append((when, data))
                log.write(f'{when:8.3f}  {node}  len={len(data):3d}  {data.hex(" ")}\n')

            collect(fds, seconds, on_report)
            log.flush()
            summary.append((title, counts, examples, errors))
            if counts:
                for (node, key), count in sorted(counts.items(), key=lambda kv: -kv[1]):
                    print(f'     {count:5d} x  {node}  starting {key}')
            else:
                print('     (nothing)')
    except KeyboardInterrupt:
        print('\nStopped early.')
    finally:
        for fd in list(fds):
            os.close(fd)
        log.close()

    print('\n' + '=' * 60 + '\nWhat arrived, step by step:')
    for title, counts, examples, errors in summary:
        print(f'\n{title}:')
        if not counts:
            print('  nothing')
        for key, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            node, prefix = key
            print(f'  {count} report(s) on {node} starting {prefix}')
            for when, data in examples.get(key, []):
                print(f'    {when:7.2f}s  {data.hex(" ")}{annotate(data)}')
            if count > MAX_EXAMPLES:
                print(f'    … and {count - MAX_EXAMPLES} more like it')
        for error in errors:
            print(f'  read error: {error}')
    print(f'\nFull log written to {args.log} — please attach that file.')
    print('Also worth mentioning: which DPI stages the button stepped through,')
    print('and whether the mouse has a profile button at all.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
