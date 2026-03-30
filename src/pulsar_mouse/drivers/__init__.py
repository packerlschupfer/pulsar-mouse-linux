"""
Built-in driver discovery for Pulsar mouse devices.

Scans this package's submodules for PulsarDevice subclasses and also
checks for externally installed drivers via entry points.
"""

import importlib
import importlib.metadata
import pkgutil
from typing import Type

from pulsar_mouse.base import PulsarDevice


def _discover_builtin() -> dict[str, Type[PulsarDevice]]:
    """Find all PulsarDevice subclasses in this drivers package."""
    drivers: dict[str, Type[PulsarDevice]] = {}
    for finder, name, _ispkg in pkgutil.iter_modules(__path__):
        mod = importlib.import_module(f'{__name__}.{name}')
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (isinstance(obj, type) and issubclass(obj, PulsarDevice)
                    and obj is not PulsarDevice):
                drivers[name] = obj
    return drivers


def _discover_entrypoints() -> dict[str, Type[PulsarDevice]]:
    """Find externally installed drivers via entry points."""
    drivers: dict[str, Type[PulsarDevice]] = {}
    try:
        eps = importlib.metadata.entry_points(group='pulsar_mouse.drivers')
    except TypeError:
        # Python < 3.12 compat
        eps = importlib.metadata.entry_points().get('pulsar_mouse.drivers', [])
    for ep in eps:
        try:
            cls = ep.load()
            if isinstance(cls, type) and issubclass(cls, PulsarDevice):
                drivers[ep.name] = cls
        except Exception:
            pass
    return drivers


def discover_all() -> dict[str, Type[PulsarDevice]]:
    """Return all known drivers (built-in + entry points), keyed by name."""
    drivers = _discover_builtin()
    drivers.update(_discover_entrypoints())
    return drivers
