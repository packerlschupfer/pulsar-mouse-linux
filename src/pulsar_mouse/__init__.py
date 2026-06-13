"""
pulsar-mouse-linux — Configuration tool for Pulsar gaming mice.

Provides a plugin architecture where each mouse model has its own
protocol driver, while sharing the CLI and GTK4 GUI framework.
"""

__version__ = '0.1.0'

from pulsar_mouse.base import PulsarDevice, DeviceCapabilities

import usb.core
from pulsar_mouse.drivers import discover_all


def scan_devices() -> list[PulsarDevice]:
    """Scan USB for any known Pulsar mouse, return instantiated (unopened) drivers."""
    found = []
    for name, cls in discover_all().items():
        caps = cls.capabilities
        for vid, pid in caps.vid_pid_pairs:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is not None:
                found.append(cls())
                break
    return found


def find_device(name: str | None = None) -> PulsarDevice:
    """Find a single Pulsar mouse device.

    If *name* is given, only that driver is tried.  Otherwise, scan for
    all known devices and return the first one found.
    Raises RuntimeError if no device is found.
    """
    if name is not None:
        drivers = discover_all()
        cls = drivers.get(name)
        if cls is None:
            avail = ', '.join(sorted(drivers))
            raise RuntimeError(
                f"Unknown device '{name}'. Available: {avail}")
        device = cls()
        return device

    devices = scan_devices()
    if not devices:
        all_drivers = discover_all()
        names = ', '.join(d.capabilities.name for d in all_drivers.values())
        raise RuntimeError(
            f"No supported Pulsar mouse found. Supported: {names}")
    return devices[0]
