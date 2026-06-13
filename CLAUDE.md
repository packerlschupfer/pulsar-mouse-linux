# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Linux configuration tool for Pulsar gaming mice. Plugin architecture where each mouse model has its own protocol driver. Currently supports Pulsar X2A Medium Wired. CLI and GTK4/libadwaita GUI adapt dynamically to device capabilities.

## Build & Validate

Pure Python project — no compilation step.

```bash
# Syntax check all modules
python3 -m py_compile src/pulsar_mouse/*.py src/pulsar_mouse/drivers/*.py

# Verify backend imports
PYTHONPATH=src python3 -c "from pulsar_mouse import scan_devices; from pulsar_mouse.drivers.x2a import PulsarX2A; print(PulsarX2A.capabilities.name)"

# Run CLI (requires USB device or sudo)
PYTHONPATH=src python3 -m pulsar_mouse.cli

# Run GUI
PYTHONPATH=src python3 -m pulsar_mouse.gui

# Or install in dev mode
pip install -e .
pulsar-mouse
pulsar-mouse-gui
```

No unit tests. CI (.github/workflows/ci.yml) runs `py_compile` and import validation on Ubuntu 24.04.

## Release Process

Tag with `vX.Y.Z` and push. GitHub Actions (release.yml) builds a `pulsar-mouse-linux` .deb package. The .deb `Replaces: pulsar-x2a-linux` for clean upgrades.

## Architecture

### Package layout (`src/pulsar_mouse/`)

- **`base.py`** — `DeviceCapabilities` dataclass and `PulsarDevice` ABC. The capabilities dataclass describes what a device supports (profiles, DPI range, buttons, features). The ABC defines required methods (polling rate, DPI stages) and optional ones (LED, debounce, etc. — default raises NotImplementedError).
- **`hid.py`** — Shared HID constants: button function types, action tables, keyboard/media codes, `describe_button()`, `parse_button_function()`. Device-independent.
- **`drivers/__init__.py`** — Two-tier driver discovery: scans built-in submodules + `importlib.metadata.entry_points(group='pulsar_mouse.drivers')` for external plugins.
- **`drivers/x2a.py`** — X2A protocol driver. 64-byte HID Feature Reports, Interface 3, LE uint16 checksum. All register addresses and encoding tables live here.
- **`__init__.py`** — Public API: `scan_devices()`, `find_device()`, version.
- **`cli.py`** — Generic argparse CLI. Adapts flags/ranges to `device.capabilities`.
- **`gui.py`** — Generic GTK4/libadwaita GUI. Builds UI dynamically from capabilities. Includes system tray (D-Bus SNI + Dbusmenu) and hidraw DPI listener.

### Adding a New Mouse Model

1. Create `src/pulsar_mouse/drivers/newmodel.py`
2. Define a class inheriting `PulsarDevice` with `capabilities` as a class variable
3. Implement `open()`, `close()`, and all required/applicable methods
4. Implement `find_hidraw()` and `parse_hidraw_event()` if the device emits DPI events
5. Register in `pyproject.toml` under `[project.entry-points."pulsar_mouse.drivers"]`
6. Add udev rules in `udev/50-pulsar-mouse.rules`

### Adding a New Setting to an Existing Driver

1. Add encoding constants in the driver file (e.g., `drivers/x2a.py`)
2. Add `get_X()` / `set_X()` methods to the driver class
3. If the setting is common across mice: add to `PulsarDevice` ABC in `base.py`, add `has_X` to `DeviceCapabilities`
4. Wire into `cli.py` argparse (conditionally based on `caps.has_X`)
5. Add Adw.PreferencesRow in `gui.py` (conditionally created based on caps)

### GUI Threading Model

All USB operations run in background threads gated by `_USB_LOCK` (threading.Lock). GUI callbacks snapshot widget values first, then launch threads. Threads update UI via `GLib.idle_add()`. A `_building` flag suppresses change callbacks during widget population.

The hidraw listener runs as a daemon thread. Each driver implements `find_hidraw()` to locate the device and `parse_hidraw_event()` to decode events.

### Plugin System

External driver packages register via Python entry points:
```toml
[project.entry-points."pulsar_mouse.drivers"]
mydriver = "my_package.driver:MyPulsarMouse"
```

Built-in drivers are discovered by scanning `pulsar_mouse.drivers` submodules. The .deb installs files directly (no pip), so built-in scanning is the primary mechanism; entry points work for pip-installed extras.

## Dependencies

- `python3-usb` (usb.core/usb.util) — USB communication
- `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1` — GUI
- `gir1.2-dbusmenu-glib-0.4` — system tray menu

## Key Constraints

- Device access requires either sudo or udev rules (`udev/50-pulsar-mouse.rules`)
- Button bindings are CLI-only for editing; GUI shows them read-only
- `capabilities` must be a class variable (not instance) so `scan_devices()` can check VID/PID without instantiating
- Each driver holds its own USB handle internally (no passing `dev` around)
