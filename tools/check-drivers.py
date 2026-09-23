#!/usr/bin/env python3
"""Consistency checks for the driver modules — run in CI, and worth running
before opening a driver PR:

    PYTHONPATH=src python3 tools/check-drivers.py

These catch mistakes that compile and import cleanly, so the rest of CI waves
them through.  Every one of them has reached main before:

  * two driver classes in one module.  Built-in discovery keys drivers by
    module name, so the .deb and AppImage silently lose one (PR #10).
  * an entry-point name that differs from its module, so a pip install
    registers the driver twice.
  * a VID:PID claimed by two drivers, or missing its udev rules.
  * Nordic family: a DPI value in the declared range that can't be encoded,
    doesn't read back, or has two byte encodings (PR #10).
  * Nordic family: a driver that can't send a command at all, for instance a
    method borrowed from another class whose super() no longer resolves
    (PR #10).

No hardware is needed.  The Nordic checks run the real encoder and command
path against a stub device.
"""

import importlib
import pkgutil
import re
import sys
import tomllib
from pathlib import Path

import pulsar_mouse.drivers as drivers_pkg
from pulsar_mouse.base import PulsarDevice
from pulsar_mouse.drivers.nordic import PulsarNordic, _dpi_to_raw, _raw_to_dpi

ROOT = Path(__file__).resolve().parent.parent
MAX_LINES_PER_DRIVER = 5


def driver_modules():
    return {name: importlib.import_module(f'{drivers_pkg.__name__}.{name}')
            for _finder, name, _pkg in pkgutil.iter_modules(drivers_pkg.__path__)}


def driver_classes(module):
    return [obj for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, PulsarDevice)
            and obj is not PulsarDevice and obj.__module__ == module.__name__]


def check_one_class_per_module(modules):
    """A module may hold helpers and no driver, but never two drivers."""
    return [f'{name}: {len(found)} driver classes ({", ".join(c.__name__ for c in found)}); '
            'built-in discovery keeps only one, so give each its own module'
            for name, module in modules.items()
            if len(found := driver_classes(module)) > 1]


def check_entry_points(modules, entry_points):
    problems, names = [], {}
    for name, module in modules.items():
        found = driver_classes(module)
        if len(found) == 1:
            names.setdefault(found[0], set()).add(name)
            if name not in entry_points:
                problems.append(f'{name}: no entry point in pyproject.toml')
    for ep_name, target in entry_points.items():
        module_path, _, class_name = target.partition(':')
        module_name = module_path.rsplit('.', 1)[-1]
        if module_name != ep_name:
            problems.append(f'entry point {ep_name!r} points at module {module_name!r}; '
                            'name it after the module')
        try:
            cls = getattr(importlib.import_module(module_path), class_name)
        except (ImportError, AttributeError) as e:
            problems.append(f'entry point {ep_name!r} does not resolve: {e}')
            continue
        names.setdefault(cls, set()).add(ep_name)
    problems += [f'{cls.__name__} is registered as {sorted(keys)}, so a pip install lists it twice'
                 for cls, keys in names.items() if len(keys) > 1]
    return problems


def check_usb_ids(modules, udev_text):
    live = '\n'.join(line for line in udev_text.splitlines()
                     if not line.lstrip().startswith('#'))
    ids = r'ATTRS\{idVendor\}=="([0-9a-f]{4})",\s*ATTRS\{idProduct\}=="([0-9a-f]{4})"'
    rules = {'usb': set(re.findall(r'SUBSYSTEM=="usb",\s*' + ids, live)),
             'hidraw': set(re.findall(r'KERNEL=="hidraw\*",\s*' + ids, live))}
    problems, claims = [], {}
    for module in modules.values():
        for cls in driver_classes(module):
            for vid, pid in cls.capabilities.vid_pid_pairs:
                claims.setdefault((vid, pid), []).append(cls.__name__)
                missing = [kind for kind, have in rules.items()
                           if (f'{vid:04x}', f'{pid:04x}') not in have]
                if missing:
                    problems.append(f'{vid:04x}:{pid:04x} ({cls.__name__}): no '
                                    f'{" or ".join(missing)} rule in udev/50-pulsar-mouse.rules')
    problems += [f'{vid:04x}:{pid:04x} is claimed by {", ".join(owners)}'
                 for (vid, pid), owners in claims.items() if len(owners) > 1]
    return problems


def check_nordic_dpi(cls):
    """Every step from dpi_min to dpi_max must encode, read back, and have a
    single byte form.  Snapping to a coarser granularity higher up the range
    is allowed, as long as the value moves by less than one of the coarsest
    steps."""
    caps, modes = cls.capabilities, cls._DPI_MODES
    coarsest = caps.dpi_step * max(mult for _mode, mult, _base, _limit in modes)
    problems = []
    for dpi in range(caps.dpi_min, caps.dpi_max + 1, caps.dpi_step):
        try:
            raw = _dpi_to_raw(dpi, caps.dpi_step, modes)
            back = _raw_to_dpi(raw, caps.dpi_step, modes)
        except ValueError as e:
            problems.append(f'{dpi} DPI cannot be encoded ({e})')
            continue
        if abs(back - dpi) >= coarsest:
            problems.append(f'{dpi} DPI reads back as {back}')
        elif _dpi_to_raw(back, caps.dpi_step, modes) != raw:
            problems.append(f'{dpi} DPI and {back} DPI read back alike but encode differently')
    return problems


class _EchoDevice:
    """Answers every command with a reply carrying the same command byte."""

    def __init__(self):
        self._replies = []

    def ctrl_transfer(self, request_type, request, value, index, data):
        reply = list(data[:16])
        reply.append((0x55 - sum(reply)) & 0xFF)
        self._replies.append(bytes(reply))
        return len(data)

    def read(self, endpoint, size, timeout=None):
        return self._replies.pop(0)


def check_nordic_dispatch(cls):
    device = cls()
    device._dev = _EchoDevice()
    try:
        device._command(0x03)   # link status: harmless on every Nordic device
    except Exception as e:
        return [f'cannot send a command: {type(e).__name__}: {e}']
    return []


def check_nordic_signal_quality(cls):
    """No Nordic device has ever sent an unprompted signal-quality report.

    A driver that inherits its capabilities picks this up for free; one that
    builds a fresh DeviceCapabilities silently takes the True default and the
    GUI then shows a Signal row that can only ever read "--".
    """
    if cls.capabilities.reports_signal_quality:
        return ['declares reports_signal_quality=True, but the family has no '
                'signal channel']
    return []


def per_driver(classes, check):
    lines = []
    for cls in classes:
        found = check(cls)
        lines += [f'{cls.__name__}: {p}' for p in found[:MAX_LINES_PER_DRIVER]]
        if len(found) > MAX_LINES_PER_DRIVER:
            lines.append(f'{cls.__name__}: ... and {len(found) - MAX_LINES_PER_DRIVER} more')
    return lines


def main() -> int:
    modules = driver_modules()
    pyproject = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    entry_points = pyproject['project']['entry-points']['pulsar_mouse.drivers']
    udev_text = (ROOT / 'udev' / '50-pulsar-mouse.rules').read_text()
    nordic = [cls for module in modules.values() for cls in driver_classes(module)
              if issubclass(cls, PulsarNordic)]

    results = [
        ('one driver class per module', check_one_class_per_module(modules)),
        ('entry points match their modules', check_entry_points(modules, entry_points)),
        ('USB IDs are unique and have udev rules', check_usb_ids(modules, udev_text)),
        ('Nordic: every DPI in range encodes and reads back', per_driver(nordic, check_nordic_dpi)),
        ('Nordic: every driver can send a command', per_driver(nordic, check_nordic_dispatch)),
        ('Nordic: no driver claims a signal-quality channel',
         per_driver(nordic, check_nordic_signal_quality)),
    ]
    for title, problems in results:
        print(f'{"PASS" if not problems else "FAIL"}  {title}')
        for problem in problems:
            print(f'      - {problem}')
    failed = sum(1 for _title, problems in results if problems)
    print(f'\n{len(results) - failed}/{len(results)} checks passed '
          f'({len(modules)} driver modules, {len(nordic)} Nordic-family drivers)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
