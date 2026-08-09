#!/usr/bin/env python3
"""
pulsar-mouse-gui — GTK4/libadwaita settings GUI + system-tray applet
                    for Pulsar gaming mice.

Run directly:
    python3 -m pulsar_mouse.gui

Single-instance: a second launch will focus the existing window.
The system-tray icon requires the GNOME AppIndicator extension:
    sudo apt install gnome-shell-extension-appindicator
    gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
"""

import sys
import os
import json
import re
import select
import struct
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Dbusmenu', '0.4')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, Dbusmenu

from pulsar_mouse import find_device, scan_devices, __version__
from pulsar_mouse.base import PulsarDevice, DeviceCapabilities
from pulsar_mouse.drivers import discover_all
from pulsar_mouse.hid import (
    describe_button, parse_button_function,
    MOUSE_ACTIONS, SCROLL_ACTIONS, DPI_ACTIONS, PROFILE_ACTIONS,
    MEDIA_CODES, HID_MODS, HID_KEYS,
)

APP_ID = 'io.github.packerlschupfer.PulsarMouse'

# Serialises all USB open/close operations so the tray and window don't collide.
_USB_LOCK = threading.Lock()

# Minimum time between forwarded signal_percent hidraw events (the device
# pushes these at ~1Hz - that's overkill for a status readout, so both the
# Home page and the tray throttle down to this).
_SIGNAL_UPDATE_INTERVAL = 20.0


def _connection_quality_label(pct):
    # Bands are this app's own estimate, not confirmed against Fusion's
    # exact thresholds - see feinmann8k.py's parse_hidraw_event() docstring
    # for how the underlying value was confirmed to mean signal quality at
    # all (a capture where moving the mouse away from the dongle dropped it
    # from ~90 to ~39).
    if pct >= 80:
        return 'Excellent'
    if pct >= 60:
        return 'Good'
    if pct >= 40:
        return 'Fair'
    if pct >= 20:
        return 'Weak'
    return 'Poor'


def _battery_icon_name(pct, charging):
    # Standard freedesktop/Adwaita battery icon levels - each has a
    # "-charging-" variant the icon theme provides, so charging just
    # inserts that segment rather than needing a separate icon set.
    if pct >= 90:
        level = 'full'
    elif pct >= 60:
        level = 'good'
    elif pct >= 30:
        level = 'low'
    elif pct >= 15:
        level = 'caution'
    else:
        level = 'empty'
    charging_part = '-charging' if charging else ''
    return f'battery-{level}{charging_part}-symbolic'


def _is_gnome_detected() -> bool:
    """Whether GNOME is the running desktop - the org.gnome.desktop.
    peripherals.mouse gsettings schema this app's OS-settings group reads/
    writes only meaningfully affects cursor behaviour under GNOME (Mutter/
    GNOME Shell), not other compositors like Hyprland/Sway, even though the
    schema itself may still be technically settable elsewhere."""
    desktop = (os.environ.get('XDG_CURRENT_DESKTOP', '') + ':' +
              os.environ.get('DESKTOP_SESSION', '')).lower()
    return 'gnome' in desktop


def _is_hyprland_detected() -> bool:
    """HYPRLAND_INSTANCE_SIGNATURE is set by Hyprland itself specifically
    (unlike XDG_CURRENT_DESKTOP, which some other compositors also set to
    misleading values) - it's the same variable hyprctl itself relies on
    to find the right instance, so it's the most direct signal available."""
    return bool(os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'))


def _is_kde_detected() -> bool:
    """KDE_FULL_SESSION is set specifically by Plasma sessions; falls back
    to XDG_CURRENT_DESKTOP for setups that only set that."""
    if os.environ.get('KDE_FULL_SESSION'):
        return True
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
    return 'kde' in desktop or 'plasma' in desktop


def _parse_svg_path(d: str) -> list[tuple]:
    """Parse an SVG path 'd' string into Cairo-ready segments.

    Only supports absolute M/L/C/Z commands with exactly one coordinate
    set per command token (no implicit repeats, no relative m/l/c) -
    that's the subset actual vector-editor exports (Illustrator/Figma/
    Inkscape "Export As SVG") produce, and all InputTestDialog's traced
    icon uses. Returns a list of ('move', x, y) / ('line', x, y) /
    ('curve', x1,y1,x2,y2,x3,y3) / ('close',) tuples.
    """
    segments = []
    for cmd, arg in re.findall(r'([MLCZ])([^MLCZ]*)', d):
        nums = [float(n) for n in re.findall(r'-?\d+\.?\d*', arg)]
        if cmd == 'M':
            segments.append(('move', nums[0], nums[1]))
        elif cmd == 'L':
            segments.append(('line', nums[0], nums[1]))
        elif cmd == 'C':
            segments.append(('curve', *nums[:6]))
        elif cmd == 'Z':
            segments.append(('close',))
    return segments


def _svg_path_bbox(segments: list[tuple]) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) across every point in the segments,
    including curve control points (not just endpoints) so a bulging
    curve doesn't get clipped - slightly looser than the curve's true
    tight bbox, which is fine for fitting a diagram into a widget."""
    xs, ys = [], []
    for seg in segments:
        if seg[0] in ('move', 'line'):
            xs.append(seg[1]); ys.append(seg[2])
        elif seg[0] == 'curve':
            xs += [seg[1], seg[3], seg[5]]
            ys += [seg[2], seg[4], seg[6]]
    return min(xs), min(ys), max(xs), max(ys)


def _draw_svg_path(cr, segments: list[tuple], tx: float, ty: float,
                    sx: float, sy: float):
    """Replay parsed segments as Cairo calls, mapping each SVG-space
    point (x,y) to widget-space via (x*sx+tx, y*sy+ty)."""
    for seg in segments:
        if seg[0] == 'move':
            cr.move_to(seg[1] * sx + tx, seg[2] * sy + ty)
        elif seg[0] == 'line':
            cr.line_to(seg[1] * sx + tx, seg[2] * sy + ty)
        elif seg[0] == 'curve':
            cr.curve_to(seg[1] * sx + tx, seg[2] * sy + ty,
                        seg[3] * sx + tx, seg[4] * sy + ty,
                        seg[5] * sx + tx, seg[6] * sy + ty)
        elif seg[0] == 'close':
            cr.close_path()


_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category"     type="s" access="read"/>
    <property name="Id"           type="s" access="read"/>
    <property name="Title"        type="s" access="read"/>
    <property name="Status"       type="s" access="read"/>
    <property name="IconName"     type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu"         type="o" access="read"/>
    <property name="ItemIsMenu"   type="b" access="read"/>
    <property name="ToolTip"      type="(sa(iiay)ss)" access="read"/>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg type="s"/></signal>
    <signal name="NewToolTip"/>
    <signal name="XAyatanaNewLabel"><arg type="s"/><arg type="s"/></signal>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  </interface>
</node>
"""


class _StatusNotifierItem:
    """Minimal StatusNotifierItem D-Bus service (no GTK3 dependency)."""

    _MENU_PATH = '/MenuBar'

    def __init__(self, app_id, icon_name, title):
        self._app_id    = app_id
        self._icon_name = icon_name
        self._title     = title
        self._label     = ''
        self._tooltip_text = ''
        self._conn      = None
        self._obj_id    = 0
        self._sni_server = None
        self._poll_items = {}
        self._on_activate = None

    def start(self, dbus_conn):
        self._conn = dbus_conn
        node  = Gio.DBusNodeInfo.new_for_xml(_SNI_XML)
        iface = node.lookup_interface('org.kde.StatusNotifierItem')
        self._obj_id = self._conn.register_object(
            '/StatusNotifierItem', iface,
            self._on_method, self._on_get_prop, None)
        svc = f'org.kde.StatusNotifierItem-{os.getpid()}-1'
        Gio.bus_own_name_on_connection(
            self._conn, svc, Gio.BusNameOwnerFlags.NONE, None, None)
        self._conn.call(
            'org.kde.StatusNotifierWatcher', '/StatusNotifierWatcher',
            'org.kde.StatusNotifierWatcher', 'RegisterStatusNotifierItem',
            GLib.Variant('(s)', (svc,)), None,
            Gio.DBusCallFlags.NONE, -1, None, None, None)

    def set_label(self, label, guide=''):
        self._label = label
        if self._conn and self._obj_id:
            self._conn.emit_signal(
                None, '/StatusNotifierItem',
                'org.kde.StatusNotifierItem', 'XAyatanaNewLabel',
                GLib.Variant('(ss)', (label, guide)))

    def set_icon(self, icon_name):
        if icon_name == self._icon_name:
            return
        self._icon_name = icon_name
        if self._conn and self._obj_id:
            self._conn.emit_signal(
                None, '/StatusNotifierItem',
                'org.kde.StatusNotifierItem', 'NewIcon', None)

    def set_tooltip(self, text):
        """Native hover tooltip (StatusNotifierItem ToolTip property), as
        opposed to set_label()'s always-visible text-next-to-icon label."""
        self._tooltip_text = text
        if self._conn and self._obj_id:
            self._conn.emit_signal(
                None, '/StatusNotifierItem',
                'org.kde.StatusNotifierItem', 'NewToolTip', None)

    def set_poll_items(self, items):
        self._poll_items = items

    def set_on_activate(self, cb):
        self._on_activate = cb

    def set_dbusmenu_server(self, server):
        self._sni_server = server

    def _on_method(self, conn, sender, path, iface, method, params, inv):
        if method == 'Activate' and self._on_activate:
            GLib.idle_add(self._on_activate)
        inv.return_value(None)

    def _on_get_prop(self, conn, sender, path, iface, prop):
        return {
            'Category':     GLib.Variant('s', 'Hardware'),
            'Id':           GLib.Variant('s', self._app_id),
            'Title':        GLib.Variant('s', self._title),
            'Status':       GLib.Variant('s', 'Active'),
            'IconName':     GLib.Variant('s', self._icon_name),
            'IconThemePath':GLib.Variant('s', ''),
            'Menu':         GLib.Variant('o', self._MENU_PATH),
            'ItemIsMenu':   GLib.Variant('b', True),
            'ToolTip':      GLib.Variant('(sa(iiay)ss)',
                                (self._icon_name, [], self._title, self._tooltip_text)),
        }.get(prop)


class PulsarMouseApp(Adw.Application):
    # Last-known battery reading, written on every poll so other tools
    # (e.g. a desktop-widget/lock-screen plugin) can show it without doing
    # their own redundant USB reads - see _set_battery_label().
    _BATTERY_STATE_PATH = os.path.expanduser('~/.cache/pulsar-mouse/battery.json')

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connect('activate', self._on_activate)
        self._sni = None
        self._win = None
        self._poll_items = {}
        self._tray_refresh_busy = False
        # Last-known signal quality - see _set_conn_quality_label(). Unlike
        # battery, there's no synchronous getter for this (it only ever
        # arrives via async hidraw events), so it's carried into the state
        # file write from whatever's most recently seen, possibly None if
        # no event has arrived yet this session.
        self._last_signal_percent = None
        self._device = None  # PulsarDevice instance

    def _find_or_create_device(self) -> PulsarDevice | None:
        if self._device is not None:
            return self._device
        try:
            self._device = find_device()
        except RuntimeError:
            self._device = None
        return self._device

    def _on_activate(self, app):
        # get_windows(), not self._win/in_destruction() - GtkWidget's
        # in_destruction() is only True *during* the "destroy" signal
        # itself, not afterward (live-verified, Opus review this session:
        # it reads False on an already-closed window, so the earlier
        # version of this method took the wrong branch and the window
        # never reappeared - the exact bug this method exists to fix).
        # get_windows() correctly reflects [] once a window is really
        # closed (no more set_hide_on_close - see MainWindow.__init__),
        # which is the one thing this always got right, both before any
        # of today's changes and after.
        wins = self.get_windows()
        if wins:
            wins[0].present()
            return
        device = self._find_or_create_device()
        win = MainWindow(application=app, device=device)
        win.present()
        self._win = win
        if device and self._sni is None:
            self._build_tray(device)
        self.hold()

    # ── System-tray ──────────────────────────────────────────────────────────

    def _build_tray(self, device: PulsarDevice):
        caps = device.capabilities
        sni = _StatusNotifierItem('pulsar-mouse', 'input-mouse', caps.name)
        sni.set_on_activate(lambda: self.activate())
        self._sni = sni

        root = Dbusmenu.Menuitem.new()

        item_open = Dbusmenu.Menuitem.new()
        item_open.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Open Settings')
        item_open.connect('item-activated', lambda _i, _t: self.activate())
        root.child_append(item_open)

        self._battery_text = None
        self._conn_text = None

        self._battery_item = None
        if hasattr(device, 'get_power'):
            battery_item = Dbusmenu.Menuitem.new()
            battery_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Battery: —')
            try:
                battery_item.property_set_bool(Dbusmenu.MENUITEM_PROP_ENABLED, False)
            except Exception:
                pass  # cosmetic only - fine if this binding doesn't support it
            root.child_append(battery_item)
            self._battery_item = battery_item

        # Connection Quality has no synchronous getter (see the Home page's
        # own comment on this) - it only ever arrives via async hidraw
        # events, so unlike Battery this item stays "—" until the first one
        # shows up rather than being read up front.
        #
        # Gated on get_power (not find_hidraw): find_hidraw has a base-class
        # default (returns None) so hasattr() on it is true for every
        # driver, wired or wireless - it doesn't actually indicate this
        # device can report signal strength. get_power only exists on
        # wireless drivers, and RF signal quality is meaningless for a
        # wired connection anyway.
        self._conn_item = None
        if hasattr(device, 'get_power'):
            conn_item = Dbusmenu.Menuitem.new()
            conn_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Signal: —')
            try:
                conn_item.property_set_bool(Dbusmenu.MENUITEM_PROP_ENABLED, False)
            except Exception:
                pass
            root.child_append(conn_item)
            self._conn_item = conn_item

        sep1 = Dbusmenu.Menuitem.new()
        sep1.property_set(Dbusmenu.MENUITEM_PROP_TYPE, Dbusmenu.CLIENT_TYPES_SEPARATOR)
        root.child_append(sep1)

        dpi_root = Dbusmenu.Menuitem.new()
        dpi_root.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Quick DPI (profile 1)')
        for dv in (400, 800, 1200, 1600, 3200):
            sub = Dbusmenu.Menuitem.new()
            sub.property_set(Dbusmenu.MENUITEM_PROP_LABEL, f'{dv} DPI')
            sub.connect('item-activated', lambda _i, _t, d=dv: self._set_dpi(d))
            dpi_root.child_append(sub)
        root.child_append(dpi_root)

        poll_root = Dbusmenu.Menuitem.new()
        poll_root.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Polling Rate')
        for hz in caps.polling_rates:
            sub = Dbusmenu.Menuitem.new()
            sub.property_set(Dbusmenu.MENUITEM_PROP_LABEL, f'{hz} Hz')
            sub.property_set(Dbusmenu.MENUITEM_PROP_TOGGLE_TYPE,
                             Dbusmenu.MENUITEM_TOGGLE_RADIO)
            sub.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                 Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED)
            sub.connect('item-activated', lambda _i, _t, h=hz: self._set_poll(h))
            poll_root.child_append(sub)
            self._poll_items[hz] = sub
        root.child_append(poll_root)

        sep2 = Dbusmenu.Menuitem.new()
        sep2.property_set(Dbusmenu.MENUITEM_PROP_TYPE, Dbusmenu.CLIENT_TYPES_SEPARATOR)
        root.child_append(sep2)

        item_quit = Dbusmenu.Menuitem.new()
        item_quit.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Quit')
        item_quit.connect('item-activated', lambda _i, _t: self.quit())
        root.child_append(item_quit)

        server = Dbusmenu.Server.new(_StatusNotifierItem._MENU_PATH)
        server.set_root(root)
        sni.set_dbusmenu_server(server)

        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sni.start(conn)

        GLib.timeout_add(500, self._start_tray_updates)

    def _start_tray_updates(self):
        self._read_initial_state()
        threading.Thread(target=self._hidraw_listener, daemon=True).start()
        # Battery percent drains slowly, but power_connected (charging)
        # can flip the moment a cable is plugged/unplugged - 60s keeps
        # that (and the tray icon + Home page it drives, see
        # _set_battery_label()) reasonably responsive without polling so
        # often it risks waking the mouse from its own power-saving sleep
        # more than necessary.
        GLib.timeout_add_seconds(60, self._refresh_battery)
        return False

    def _read_initial_state(self):
        device = self._device

        def _read():
            if device is None:
                return
            try:
                with _USB_LOCK:
                    device.open()
                    # device.close() must run even if a getter below raises
                    # (e.g. NotImplementedError on a partial driver like
                    # feinmann8k.py) - otherwise the device is left open with
                    # both interfaces claimed for the rest of the process's
                    # life, and every later _open_dev() call fails with
                    # "Resource busy" since nothing else can release a claim
                    # this thread never lets go of.
                    try:
                        hz = device.get_polling_rate()
                        dpi_info = device.get_dpi_stages(profile=1)
                        pwr = None
                        if hasattr(device, 'get_power'):
                            # Guarded separately from hz/dpi_info above - a
                            # battery-read failure shouldn't also take out
                            # the DPI/Hz label.
                            try:
                                pwr = device.get_power()
                                if hasattr(device, 'get_low_power_threshold'):
                                    pwr['low_power_threshold'] = device.get_low_power_threshold()
                            except Exception:
                                pass
                    finally:
                        device.close()
                dpi = dpi_info['stages'][dpi_info['active'] - 1][0]
                GLib.idle_add(self._update_tray_label, dpi, hz, True)
                if pwr is not None:
                    GLib.idle_add(self._set_battery_label, pwr)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()

    def _refresh_battery(self):
        device = self._device
        if device is None or not hasattr(device, 'get_power') or self._battery_item is None:
            return True  # keep the timer alive in case device/UI state changes

        def _read():
            try:
                with _USB_LOCK:
                    device.open()
                    try:
                        pwr = device.get_power()
                        if hasattr(device, 'get_low_power_threshold'):
                            pwr['low_power_threshold'] = device.get_low_power_threshold()
                    finally:
                        device.close()
                GLib.idle_add(self._set_battery_label, pwr)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()
        return True  # repeat

    def _set_battery_label(self, pwr):
        pct = pwr['battery_percent']
        charging = pwr['power_connected']
        label = ' (charging)' if charging else ''
        self._battery_text = f'Battery: {pct}%{label}'
        if self._battery_item is not None:
            self._battery_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL, self._battery_text)
        if self._sni is not None:
            # Swap to a charging-battery icon while plugged in (also
            # reflects actual level, e.g. battery-low-charging), otherwise
            # keep the generic mouse icon - no need to always show battery
            # level in the tray when nothing's actively happening with it.
            self._sni.set_icon(_battery_icon_name(pct, True) if charging else 'input-mouse')
        self._update_tray_tooltip()
        try:
            os.makedirs(os.path.dirname(self._BATTERY_STATE_PATH), exist_ok=True)
            with open(self._BATTERY_STATE_PATH, 'w') as f:
                json.dump({
                    'wireless': True,
                    **pwr,
                    'signal_percent': self._last_signal_percent,
                    'updated_at': time.time(),
                }, f)
        except OSError:
            pass  # cosmetic (external readers only) - never worth crashing over
        # Home page has no polling timer of its own - it shares this read
        # rather than doubling up on USB traffic (and possibly waking the
        # mouse from its own power-saving sleep twice as often).
        # get_windows(), not self._win - same reasoning as _on_activate:
        # self._win can point at an already-closed window in the gap
        # between a close and the next reopen (nothing nulls it out on
        # close), and this periodic poll can land in exactly that gap.
        wins = self.get_windows()
        if wins:
            wins[0]._set_home_battery(pwr)

    def _set_conn_quality_label(self, pct):
        self._last_signal_percent = pct
        self._conn_text = f'Signal: {pct}% ({_connection_quality_label(pct)})'
        if self._conn_item is not None:
            self._conn_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL, self._conn_text)
        self._update_tray_tooltip()

    def _update_tray_tooltip(self):
        if self._sni is None:
            return
        lines = [t for t in (self._battery_text, self._conn_text) if t]
        if lines:
            self._sni.set_tooltip('\n'.join(lines))

    def _hidraw_listener(self):
        device = self._device
        if device is None:
            return
        path = device.find_hidraw()
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        last_signal_update = 0.0
        try:
            while True:
                data = os.read(fd, 256)
                if not data:
                    break
                event = device.parse_hidraw_event(data)
                if not event:
                    continue
                if 'dpi' in event:
                    GLib.idle_add(self._update_tray_label, event['dpi'], None)
                elif 'signal_percent' in event:
                    now = time.monotonic()
                    if now - last_signal_update >= _SIGNAL_UPDATE_INTERVAL:
                        last_signal_update = now
                        GLib.idle_add(self._set_conn_quality_label, event['signal_percent'])
        except OSError:
            pass
        finally:
            os.close(fd)

    def _update_tray_label(self, dpi=None, hz=None, initial=False):
        if not hasattr(self, '_cur_dpi'):
            self._cur_dpi = 0
            self._cur_hz = 0
            self._label_hide_seq = 0
        if dpi is not None:
            self._cur_dpi = dpi
        if hz is not None:
            self._cur_hz = hz

        if initial or (dpi is not None and hz is not None):
            label = f'{self._cur_dpi} DPI  {self._cur_hz} Hz'
        elif dpi is not None:
            label = f'{self._cur_dpi} DPI'
        else:
            label = f'{self._cur_hz} Hz'

        if self._sni:
            self._sni.set_label(label, '')

        self._label_hide_seq += 1
        seq = self._label_hide_seq
        GLib.timeout_add_seconds(3, self._hide_label, seq)

        if hz is not None:
            checked   = Dbusmenu.MENUITEM_TOGGLE_STATE_CHECKED
            unchecked = Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED
            for h, item in self._poll_items.items():
                item.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                      checked if h == hz else unchecked)

    def _hide_label(self, seq):
        if seq == self._label_hide_seq and self._sni:
            self._sni.set_label('', '')
        return False

    def _set_dpi(self, dpi_val: int):
        device = self._device

        def _write():
            if device is None:
                return
            try:
                with _USB_LOCK:
                    device.open()
                    # device.close() must run even if a getter/setter above
                    # raises - see _read_initial_state's comment on this
                    # same pattern; without it this leaks the device handle
                    # and every later _open_dev() call fails "Resource busy".
                    try:
                        info = device.get_dpi_stages(profile=1)
                        for i, (dx, _dy) in enumerate(info['stages']):
                            if dx == dpi_val:
                                device.set_active_dpi_stage(i + 1, profile=1)
                                break
                        else:
                            stages = [dx for dx, _dy in info['stages']]
                            stages[info['active'] - 1] = dpi_val
                            device.set_dpi_stages(stages, info['active'], profile=1)
                    finally:
                        device.close()
                GLib.idle_add(self._update_tray_label, dpi_val, None)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()

    def _set_poll(self, hz: int):
        device = self._device

        def _write():
            if device is None:
                return
            try:
                with _USB_LOCK:
                    device.open()
                    try:
                        device.set_polling_rate(hz)
                    finally:
                        device.close()
                GLib.idle_add(self._update_tray_label, None, hz)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()


class _ColorPickerButton(Gtk.Button):
    """LED color swatch button that opens its own picker window.

    Gtk.ColorDialogButton's built-in Gtk.ColorDialog renders as a small
    fixed-size window that can't fit its own alpha-slider content without
    scrolling; on this system that mismatch also triggers a GSK compositing
    bug where the custom HSV editor page paints transparently (the desktop
    wallpaper shows through instead of an opaque swatch). Hosting a plain
    Gtk.ColorChooserWidget in a window we own avoids both: the window sizes
    itself to its natural content size and renders opaquely.
    """

    def __init__(self):
        super().__init__(css_classes=['flat'])
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = 1.0, 1.0, 1.0, 1.0
        self._rgba = rgba
        self._swatch = Gtk.DrawingArea(content_width=24, content_height=24)
        self._swatch.set_draw_func(self._draw_swatch)
        self.set_child(self._swatch)
        self.connect('clicked', self._on_clicked)

    def _draw_swatch(self, _area, cr, w, h):
        cr.set_source_rgba(self._rgba.red, self._rgba.green, self._rgba.blue, self._rgba.alpha)
        cr.rectangle(0, 0, w, h)
        cr.fill()

    def get_rgba(self) -> Gdk.RGBA:
        return self._rgba

    def set_rgba(self, rgba: Gdk.RGBA):
        self._rgba = rgba
        self._swatch.queue_draw()

    def _on_clicked(self, _btn):
        win = Gtk.Window(transient_for=self.get_root(), modal=True)
        win.set_title('Pick a Color')
        win.set_resizable(True)

        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label='Pick a Color'))
        cancel_btn = Gtk.Button(label='Cancel')
        select_btn = Gtk.Button(label='Select', css_classes=['suggested-action'])
        header.pack_start(cancel_btn)
        header.pack_end(select_btn)
        win.set_titlebar(header)

        # use_alpha=False - the on-wire stage-color command is RGB-only
        # (see set_stage_color()/get_stage_color() below), so an alpha
        # slider here would be a no-op that misleads users into thinking
        # it does something.
        chooser = Gtk.ColorChooserWidget(use_alpha=False)
        chooser.set_rgba(self._rgba)
        win.set_child(chooser)

        # None of these three handlers capture `win` directly - closing
        # via `b.get_root()`/`c.get_root()` instead - a captured `win`
        # here would create win -> {cancel_btn,select_btn} -> GClosure ->
        # (this lambda) -> win, a reference cycle Python's GC can't break
        # on its own since it crosses into GObject-land (confirmed via a
        # live create/close/gc.collect() test leaking one window per
        # click before this fix).
        cancel_btn.connect('clicked', lambda b, *_a: b.get_root().close())

        def on_select(b, *_a):
            self.set_rgba(chooser.get_rgba())
            b.get_root().close()
        select_btn.connect('clicked', on_select)

        def on_color_activated(c, rgba):
            self.set_rgba(rgba)
            c.get_root().close()
        chooser.connect('color-activated', on_color_activated)

        win.present()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, device: PulsarDevice | None = None, **kwargs):
        super().__init__(**kwargs)
        self._device = device
        self._caps = device.capabilities if device else None
        self.set_title(self._caps.name if self._caps else 'Pulsar Mouse')
        self.set_default_size(560, 740)
        self.set_icon_name('input-mouse')
        # Not set_hide_on_close(True) - a hidden-not-destroyed GtkWindow
        # can't be reliably re-shown later (present(), even preceded by
        # set_visible(True), does not re-map it as a real compositor
        # surface on this Wayland/Hyprland + GTK4 combo - live-verified
        # both ways failing, see PulsarMouseApp._on_activate for the
        # actual fix: always construct a fresh MainWindow instead of
        # trying to revive a hidden one, since that's the one path
        # that's always worked reliably). Closing this window really
        # destroys it - PulsarMouseApp.hold() keeps the app itself alive
        # regardless, so the tray survives.
        self._profile = 1
        self._building = False
        # A plain bool, not an fd - _home_hidraw_listener owns opening and
        # closing its own fd entirely on its own thread now (see that
        # method's docstring for why, this used to try to close the fd
        # from here instead and had two real problems: (1) GTK4's
        # "destroy" signal does not reliably fire on a plain close() while
        # something still holds a Python reference to the window
        # (PulsarMouseApp._win does, until the next reopen reassigns it),
        # live-verified via Opus review this session, so the cleanup
        # never ran at all; (2) even connected to close-request instead,
        # os.close()ing an fd from this thread while the listener thread
        # is blocked in os.read() on it is not guaranteed by Linux to
        # unblock that read promptly - see close(2), "Multithreaded
        # processes and close()" - empirically it did eventually clear
        # via the device's own ~1Hz heartbeat waking the read, but that's
        # an incidental timing coincidence to rely on, not a real fix).
        self._home_hidraw_stop = False
        self.connect('close-request', self._on_close_request)

        self._build_ui()
        GLib.idle_add(self._reload)
        GLib.idle_add(self._start_home_updates)
        GLib.idle_add(self._refresh_firmware_version)

    def _on_close_request(self, _win):
        # Just a flag - _home_hidraw_listener polls it via select()'s
        # timeout (at most ~1s latency) rather than this thread trying to
        # directly unblock or close the listener's fd - see this class's
        # own comment above for why that used to be here and didn't work.
        self._home_hidraw_stop = True
        return False  # False = allow the close to proceed as normal

    def _build_ui(self):
        caps = self._caps

        if not caps:
            # No device found - a minimal single-pane message instead of
            # the tabbed layout below, which assumes caps/device exist
            # throughout.
            toolbar_view = Adw.ToolbarView()
            self.set_content(toolbar_view)
            toolbar_view.add_top_bar(Adw.HeaderBar())
            self._banner = Adw.Banner()
            self._banner.set_title('No supported mouse found')
            self._banner.set_revealed(True)
            toolbar_view.set_content(self._banner)
            self._toast_overlay = None
            return

        split_view = Adw.NavigationSplitView()
        split_view.set_min_sidebar_width(180)
        split_view.set_max_sidebar_width(220)
        self.set_content(split_view)

        # ── Sidebar navigation ─────────────────────────────────────────
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)
        sidebar_toolbar.add_top_bar(sidebar_header)

        nav_list = Gtk.ListBox()
        nav_list.add_css_class('navigation-sidebar')
        nav_list.set_selection_mode(Gtk.SelectionMode.SINGLE)

        # (view-stack page name, sidebar label, icon) - order here is the
        # order rows appear in the sidebar and must line up with
        # nav_list's row index in _on_nav_row_selected() below.
        self._has_power_page = hasattr(self._device, 'get_power')
        self._nav_pages = [
            ('home', 'Home', 'go-home-symbolic'),
            ('performance', 'Performance', 'input-mouse-symbolic'),
            ('customize', 'Customize', 'preferences-desktop-symbolic'),
        ]
        if self._has_power_page:
            self._nav_pages.append(('power', 'Power', 'battery-good-symbolic'))
        self._nav_pages.append(('tools', 'Tools', 'applications-utilities-symbolic'))
        for _name, label, icon in self._nav_pages:
            row = Adw.ActionRow()
            row.set_title(label)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            nav_list.append(row)
        nav_list.connect('row-selected', self._on_nav_row_selected)
        sidebar_toolbar.set_content(nav_list)
        sidebar_page = Adw.NavigationPage.new(sidebar_toolbar, 'Pulsar Mouse')
        split_view.set_sidebar(sidebar_page)

        # ── Content: header bar (Apply/Reload/profile - shared across all
        # tabs, not per-tab, since Apply always writes everything pending
        # regardless of which tab is showing) + a ViewStack switched by
        # the sidebar ─────────────────────────────────────────────────
        content_toolbar = Adw.ToolbarView()
        content_header = Adw.HeaderBar()

        reload_btn = Gtk.Button(icon_name='view-refresh-symbolic')
        reload_btn.set_tooltip_text('Reload from mouse')
        reload_btn.connect('clicked', lambda _: self._reload())
        content_header.pack_start(reload_btn)

        apply_btn = Gtk.Button(label='Apply')
        apply_btn.add_css_class('suggested-action')
        apply_btn.connect('clicked', lambda _: self._apply())
        # Packed before profile_box below: pack_end's call order runs
        # outward from the edge, so whichever widget is packed *first*
        # ends up rightmost - this has to go first for Apply to stay the
        # outermost element with the profile dropdown to its left.
        content_header.pack_end(apply_btn)

        if caps.num_profiles > 1:
            profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._profile_combo = Gtk.DropDown.new_from_strings(
                [f'Profile {i}' for i in range(1, caps.num_profiles + 1)]
            )
            self._profile_combo.connect('notify::selected', self._on_profile_changed)
            profile_box.append(self._profile_combo)
            content_header.pack_end(profile_box)
        else:
            self._profile_combo = None

        content_toolbar.add_top_bar(content_header)

        toast_overlay = Adw.ToastOverlay()
        self._toast_overlay = toast_overlay
        content_toolbar.set_content(toast_overlay)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toast_overlay.set_child(content_box)

        # Error banner - shared across tabs (sits above whichever page is
        # visible), same as the single-page layout this replaced.
        self._banner = Adw.Banner()
        self._banner.set_revealed(False)
        content_box.append(self._banner)

        self._view_stack = Adw.ViewStack()
        self._view_stack.set_vexpand(True)
        content_box.append(self._view_stack)

        self._view_stack.add_named(self._build_home_page(), 'home')
        self._view_stack.add_named(self._build_performance_page(), 'performance')
        self._view_stack.add_named(self._build_customize_page(), 'customize')
        if self._has_power_page:
            self._view_stack.add_named(self._build_power_page(), 'power')
        self._view_stack.add_named(self._build_tools_page(), 'tools')

        content_page = Adw.NavigationPage.new(content_toolbar, '')
        split_view.set_content(content_page)

        nav_list.select_row(nav_list.get_row_at_index(0))

    def _on_nav_row_selected(self, _listbox, row):
        if row is None:
            return
        name = self._nav_pages[row.get_index()][0]
        self._view_stack.set_visible_child_name(name)

    def _wrap_page(self, *groups):
        """Common scrolled+clamp wrapper for a ViewStack page - same
        max-width clamp and margins the single-page layout used to use
        for its one long page, just repeated per tab now."""
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroll.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(box)
        for g in groups:
            box.append(g)
        return scroll

    @staticmethod
    def _logo_image(size=96):
        icon = Gtk.Image.new_from_icon_name('pulsar-mouse')
        svg = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'data', 'pulsar-mouse.svg')
        if os.path.isfile(svg):
            icon = Gtk.Image.new_from_file(svg)
        icon.set_pixel_size(size)
        icon.set_halign(Gtk.Align.CENTER)
        return icon

    def _build_home_page(self):
        """Status/landing tab: device name, connection, battery, and
        wireless signal quality - populated live by _start_home_updates(),
        called once from __init__ after the window itself exists.
        """
        caps = self._caps
        device = self._device
        # get_power only exists on wireless drivers - unlike find_hidraw,
        # which has a base-class default (returns None) that makes
        # hasattr() on it true for every driver, wired or wireless.
        is_wireless = hasattr(device, 'get_power')

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        icon = self._logo_image(96)
        box.append(icon)

        title = Gtk.Label(label=caps.name)
        title.add_css_class('title-1')
        title.set_margin_top(12)
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        status_group = Adw.PreferencesGroup()
        status_group.set_margin_top(24)
        box.append(status_group)

        model_row = Adw.ActionRow()
        model_row.set_title('Model')
        model_row.set_subtitle(caps.name)
        model_row.add_prefix(Gtk.Image.new_from_icon_name('input-mouse-symbolic'))
        status_group.add(model_row)

        self._firmware_row = Adw.ActionRow()
        self._firmware_row.set_title('Firmware Version')
        self._firmware_row.set_subtitle('—')
        self._firmware_row.add_prefix(Gtk.Image.new_from_icon_name('application-x-firmware-symbolic'))
        self._firmware_row.set_visible(False)
        status_group.add(self._firmware_row)

        conn_row = Adw.ActionRow()
        conn_row.set_title('Connection')
        conn_row.set_subtitle('Wireless' if is_wireless else 'Wired')
        conn_row.add_prefix(Gtk.Image.new_from_icon_name(
            'network-wireless-symbolic' if is_wireless else 'network-wired-symbolic'))
        connected_label = Gtk.Label(label='Connected')
        connected_label.add_css_class('success')
        conn_row.add_suffix(connected_label)
        status_group.add(conn_row)

        # Polling rate, kept in sync elsewhere: _populate_global() (on
        # reload) and _apply() (immediately on Apply, before the write even
        # finishes) - not event-driven, since polling rate only ever
        # changes via this app, not something the device pushes on its own.
        self._home_mode_row = Adw.ActionRow()
        self._home_mode_row.set_title('Polling Rate')
        self._home_mode_row.set_subtitle('—')
        self._home_mode_row.add_prefix(
            Gtk.Image.new_from_icon_name('network-wireless-symbolic'))
        status_group.add(self._home_mode_row)

        # DPI, kept in sync the same way as Polling Rate above:
        # _populate_profile() (on reload) and _apply() (immediately on
        # Apply, before the write even finishes).
        self._home_dpi_row = Adw.ActionRow()
        self._home_dpi_row.set_title('DPI')
        self._home_dpi_row.set_subtitle('—')
        self._home_dpi_row.add_prefix(
            Gtk.Image.new_from_icon_name('zoom-in-symbolic'))
        status_group.add(self._home_dpi_row)

        # Battery and RF signal quality are both meaningless for a wired
        # mouse, so both rows are gated on is_wireless (mirrored in the
        # tray's _build_tray() for its own Battery/Connection Quality menu
        # items).
        self._home_signal_row = None
        if is_wireless:
            self._home_signal_row = Adw.ActionRow()
            self._home_signal_row.set_title('Connection Quality')
            self._home_signal_row.set_subtitle('—')
            self._home_signal_row.add_prefix(
                Gtk.Image.new_from_icon_name('network-wireless-signal-good-symbolic'))
            status_group.add(self._home_signal_row)

        self._home_battery_row = None
        self._home_battery_icon = None
        if is_wireless:
            self._home_battery_row = Adw.ActionRow()
            self._home_battery_row.set_title('Battery')
            self._home_battery_row.set_subtitle('—')
            self._home_battery_icon = Gtk.Image.new_from_icon_name('battery-good-symbolic')
            self._home_battery_row.add_prefix(self._home_battery_icon)
            status_group.add(self._home_battery_row)

        return box

    def _build_performance_page(self):
        caps = self._caps

        global_group = Adw.PreferencesGroup()
        global_group.set_title('Global Settings')
        global_group.set_description('Applies to all profiles')

        self._poll_row = Adw.ComboRow()
        self._poll_row.set_title('Polling Rate')
        self._poll_row.set_model(
            Gtk.StringList.new([f'{hz} Hz' for hz in caps.polling_rates])
        )
        self._poll_row.set_tooltip_text(
            '1000Hz: Balanced latency and battery life; stable on all PCs.\n'
            '4000Hz / 8000Hz: Smooth tracking and lowest latency; drains '
            'battery fast.\n\n'
            'A high polling rate requires a high-end CPU to prevent '
            'stutters. On Linux, 8000Hz may cause severe lag in non-native '
            '(Proton/Wine) games.')
        global_group.add(self._poll_row)

        self._debounce_row = None
        if caps.has_debounce:
            lo, hi = caps.debounce_range
            row, self._debounce_row = self._make_slider_row(
                'Debounce', f'milliseconds ({lo} – {hi})', lo, hi, 1,
                format_value=lambda v: f'{int(v)} ms')
            row.set_tooltip_text(
                'Adjusts the delay buffer (in ms) to prevent accidental '
                'double-clicks or slam-clicks.\n\n'
                'Lower (0-2ms): Fastest response time; best for Optical switches.\n'
                'Higher (3-5ms+): Eliminates chatter or accidental clicks; '
                'required for older Mechanical switches.')
            global_group.add(row)

        self._angle_row = None
        if caps.has_angle_snap:
            self._angle_row = Adw.SwitchRow()
            self._angle_row.set_title('Angle Snap')
            self._angle_row.set_subtitle('Straightens cursor movement to horizontal/vertical lines')
            self._angle_row.set_tooltip_text('May add latency, not recommended for gaming')
            global_group.add(self._angle_row)

        self._ripple_row = None
        if caps.has_ripple_control:
            self._ripple_row = Adw.SwitchRow()
            self._ripple_row.set_title('Ripple Control')
            self._ripple_row.set_subtitle('Smooths out sensor jitter at low speeds')
            self._ripple_row.set_tooltip_text('May add latency, not recommended for gaming')
            global_group.add(self._ripple_row)

        self._motion_row = None
        if caps.has_motion_sync:
            self._motion_row = Adw.SwitchRow()
            self._motion_row.set_title('Motion Sync')
            self._motion_row.set_subtitle('Synchronises sensor reads with USB polling (Recommended)')
            self._motion_row.set_tooltip_text(
                'Great for tracking smoothness at 1000Hz, but should be '
                'disabled if you run at 8K polling rate to guarantee the '
                'lowest possible latency')
            global_group.add(self._motion_row)

        # LOD lives here (not on the Customize tab with the rest of
        # "Profile Settings") to match Fusion's own Performance tab, which
        # groups Lift-off Distance with DPI/polling rate rather than LED.
        tracking_group = Adw.PreferencesGroup()
        tracking_group.set_title('Tracking')

        self._lod_row = None
        if caps.lod_values and caps.lod_step:
            lo, hi = caps.lod_values[0], caps.lod_values[-1]
            row, self._lod_row = self._make_slider_row(
                'Lift-off Distance', 'Height at which tracking stops when lifting the mouse',
                lo, hi, caps.lod_step, format_value=lambda v: f'{v:.1f} mm')
            row.set_tooltip_text(
                'Lower (0.7-1.0mm): Prevents unwanted cursor drift when '
                'resetting your mouse; ideal for low-sensitivity gaming.\n\n'
                'Higher (1.5-2.0mm): Prevents sensor skipping on textured '
                'pads or when using thick aftermarket skates.')
            tracking_group.add(row)
        elif caps.lod_values:
            self._lod_row = Adw.ComboRow()
            self._lod_row.set_title('Lift-off Distance')
            self._lod_row.set_subtitle('Height at which tracking stops when lifting the mouse')
            self._lod_row.set_model(
                Gtk.StringList.new([f'{v} mm' for v in caps.lod_values])
            )
            tracking_group.add(self._lod_row)

        dpi_group = Adw.PreferencesGroup()
        dpi_group.set_title('DPI Stages')

        self._stage_count_row = Adw.SpinRow.new_with_range(1, caps.max_dpi_stages, 1)
        self._stage_count_row.set_title('Number of Stages')
        self._stage_count_row.connect('notify::value', self._on_stage_count_changed)
        dpi_group.add(self._stage_count_row)

        self._active_stage_row = Adw.ComboRow()
        self._active_stage_row.set_title('Active Stage')
        self._active_stage_row.set_model(
            Gtk.StringList.new([f'Stage {i}' for i in range(1, caps.max_dpi_stages + 1)])
        )
        dpi_group.add(self._active_stage_row)

        self._dpi_rows = []
        self._color_buttons = []
        for i in range(1, caps.max_dpi_stages + 1):
            row = Adw.SpinRow.new_with_range(caps.dpi_min, caps.dpi_max, caps.dpi_step)
            row.set_title(f'Stage {i}')
            if caps.has_stage_colors:
                color_btn = _ColorPickerButton()
                color_btn.set_valign(Gtk.Align.CENTER)
                color_btn.set_tooltip_text(f'Stage {i} LED color')
                row.add_suffix(color_btn)
                self._color_buttons.append(color_btn)
            dpi_group.add(row)
            self._dpi_rows.append(row)

        groups = [global_group]
        if caps.lod_values:
            groups.append(tracking_group)
        groups.append(dpi_group)

        self._accel_row = None
        self._speed_row = None
        if _is_gnome_detected():
            os_group = Adw.PreferencesGroup()
            os_group.set_title('Desktop Mouse Settings')
            os_group.set_description('System-wide GNOME settings (affects all mice)')

            self._accel_row = Adw.ComboRow()
            self._accel_row.set_title('Acceleration Profile')
            self._accel_row.set_subtitle('Use "Flat" for raw input (recommended for gaming)')
            self._accel_row.set_model(Gtk.StringList.new(['Flat (raw)', 'Adaptive (default)']))
            os_group.add(self._accel_row)

            adj_speed = Gtk.Adjustment(lower=-1.0, upper=1.0, step_increment=0.05,
                                       page_increment=0.1)
            self._speed_row = Adw.SpinRow(adjustment=adj_speed, digits=2)
            self._speed_row.set_title('Pointer Speed')
            self._speed_row.set_subtitle('Multiplier from -1.0 (slow) to 1.0 (fast), 0 = no change')
            os_group.add(self._speed_row)

            speed_apply = Adw.ButtonRow()
            speed_apply.set_title('Apply Desktop Settings')
            speed_apply.connect('activated', self._on_os_apply)
            os_group.add(speed_apply)

            groups.append(os_group)
            self._load_os_settings()

        self._hypr_accel_row = None
        self._hypr_speed_row = None
        if _is_hyprland_detected():
            hypr_group = Adw.PreferencesGroup()
            hypr_group.set_title('Hyprland Mouse Settings')

            self._hypr_accel_row = Adw.ComboRow()
            self._hypr_accel_row.set_title('Acceleration Profile')
            self._hypr_accel_row.set_subtitle('Use "Flat" for raw input (recommended for gaming)')
            self._hypr_accel_row.set_model(Gtk.StringList.new(['Flat (raw)', 'Adaptive (default)']))
            hypr_group.add(self._hypr_accel_row)

            adj_hypr_speed = Gtk.Adjustment(lower=-1.0, upper=1.0, step_increment=0.05,
                                            page_increment=0.1)
            self._hypr_speed_row = Adw.SpinRow(adjustment=adj_hypr_speed, digits=2)
            self._hypr_speed_row.set_title('Sensitivity')
            self._hypr_speed_row.set_subtitle('Multiplier from -1.0 (slow) to 1.0 (fast), 0 = no change')
            hypr_group.add(self._hypr_speed_row)

            hypr_apply = Adw.ButtonRow()
            hypr_apply.set_title('Apply Hyprland Settings')
            hypr_apply.set_tooltip_text(
                f'Applied live via hyprctl, and written to {self._HYPR_CONF_PATH} '
                'so it persists - add "source = ~/.config/hypr/'
                'pulsar-mouse-input.conf" to your hyprland.conf once so it '
                'takes effect on restart too')
            hypr_apply.connect('activated', self._on_hypr_apply)
            hypr_group.add(hypr_apply)

            groups.append(hypr_group)
            self._load_hypr_settings()

        self._kde_accel_row = None
        self._kde_speed_row = None
        if _is_kde_detected():
            kde_group = Adw.PreferencesGroup()
            kde_group.set_title('KDE Plasma Mouse Settings')
            kde_group.set_description(
                'Written to kcminputrc - persists across restarts, but '
                'requires kwriteconfig5/6 to be installed')

            self._kde_accel_row = Adw.ComboRow()
            self._kde_accel_row.set_title('Acceleration Profile')
            self._kde_accel_row.set_subtitle('Use "Flat" for raw input (recommended for gaming)')
            self._kde_accel_row.set_model(Gtk.StringList.new(['Flat (raw)', 'Adaptive (default)']))
            kde_group.add(self._kde_accel_row)

            adj_kde_speed = Gtk.Adjustment(lower=-1.0, upper=1.0, step_increment=0.05,
                                           page_increment=0.1)
            self._kde_speed_row = Adw.SpinRow(adjustment=adj_kde_speed, digits=2)
            self._kde_speed_row.set_title('Pointer Acceleration')
            self._kde_speed_row.set_subtitle('Multiplier from -1.0 (slow) to 1.0 (fast), 0 = no change')
            kde_group.add(self._kde_speed_row)

            kde_apply = Adw.ButtonRow()
            kde_apply.set_title('Apply KDE Settings')
            kde_apply.connect('activated', self._on_kde_apply)
            kde_group.add(kde_apply)

            groups.append(kde_group)
            self._load_kde_settings()

        return self._wrap_page(*groups)

    def _load_os_settings(self):
        import subprocess
        try:
            accel = subprocess.check_output(
                ['gsettings', 'get', 'org.gnome.desktop.peripherals.mouse', 'accel-profile'],
                text=True).strip().strip("'")
            speed = subprocess.check_output(
                ['gsettings', 'get', 'org.gnome.desktop.peripherals.mouse', 'speed'],
                text=True).strip()
            self._building = True
            self._accel_row.set_selected(0 if accel == 'flat' else 1)
            self._speed_row.set_value(float(speed))
            self._building = False
        except Exception:
            pass

    def _on_os_apply(self, _row):
        import subprocess
        accel = 'flat' if self._accel_row.get_selected() == 0 else 'adaptive'
        speed = self._speed_row.get_value()
        try:
            subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.mouse',
                            'accel-profile', accel], check=True)
            subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.mouse',
                            'speed', str(speed)], check=True)
            self._show_toast('Desktop settings applied')
        except Exception as e:
            self._show_error(f'Could not apply desktop settings: {e}')

    # Dedicated snippet file rather than editing hyprland.conf directly -
    # that file's structure varies too much per user (multiple `input {}`
    # blocks, source-split configs, comments) to safely locate-and-patch
    # without risking corrupting something we don't understand. Requires a
    # one-time `source = ~/.config/hypr/pulsar-mouse-input.conf` line in
    # the user's own hyprland.conf, same pattern many other Hyprland addon
    # tools use for the same reason.
    _HYPR_CONF_PATH = os.path.expanduser('~/.config/hypr/pulsar-mouse-input.conf')

    def _load_hypr_settings(self):
        import subprocess, json
        try:
            accel_raw = json.loads(subprocess.check_output(
                ['hyprctl', 'getoption', 'input:accel_profile', '-j'], text=True))
            speed_raw = json.loads(subprocess.check_output(
                ['hyprctl', 'getoption', 'input:sensitivity', '-j'], text=True))
            accel = accel_raw.get('str', 'adaptive')
            speed = speed_raw.get('float', 0.0)
            self._building = True
            self._hypr_accel_row.set_selected(0 if accel == 'flat' else 1)
            self._hypr_speed_row.set_value(speed)
            self._building = False
        except Exception:
            pass

    def _on_hypr_apply(self, _row):
        import subprocess
        accel = 'flat' if self._hypr_accel_row.get_selected() == 0 else 'adaptive'
        speed = self._hypr_speed_row.get_value()
        try:
            # Live effect, immediately - runtime-only, hence also writing
            # the snippet file below for the setting to survive a restart.
            subprocess.run(['hyprctl', 'keyword', 'input:accel_profile', accel], check=True)
            subprocess.run(['hyprctl', 'keyword', 'input:sensitivity', f'{speed:.6f}'], check=True)
            with open(self._HYPR_CONF_PATH, 'w') as f:
                f.write(
                    'input {\n'
                    f'    sensitivity = {speed:.6f}    # Range: -1.0 to 1.0 (0 = default)\n'
                    f'    accel_profile = {accel} # Optional: removes mouse acceleration ("flat" or "adaptive")\n'
                    '}\n'
                )
            self._show_toast('Hyprland settings applied')
        except Exception as e:
            self._show_error(f'Could not apply Hyprland settings: {e}')

    def _load_kde_settings(self):
        import subprocess, shutil
        reader = shutil.which('kreadconfig6') or shutil.which('kreadconfig5')
        if reader is None:
            return
        try:
            flat = subprocess.check_output(
                [reader, '--file', 'kcminputrc', '--group', 'Mouse',
                 '--key', 'XLbInptAccelProfileFlat', '--default', 'false'],
                text=True).strip()
            speed = subprocess.check_output(
                [reader, '--file', 'kcminputrc', '--group', 'Mouse',
                 '--key', 'XLbInptPointerAcceleration', '--default', '0.0'],
                text=True).strip()
            self._building = True
            self._kde_accel_row.set_selected(0 if flat == 'true' else 1)
            self._kde_speed_row.set_value(float(speed))
            self._building = False
        except Exception:
            pass

    def _on_kde_apply(self, _row):
        import subprocess, shutil
        writer = shutil.which('kwriteconfig6') or shutil.which('kwriteconfig5')
        if writer is None:
            self._show_error('kwriteconfig5/6 not found - cannot write KDE settings')
            return
        flat = 'true' if self._kde_accel_row.get_selected() == 0 else 'false'
        speed = self._kde_speed_row.get_value()
        try:
            subprocess.run([writer, '--file', 'kcminputrc', '--group', 'Mouse',
                            '--key', 'XLbInptAccelProfileFlat', flat], check=True)
            subprocess.run([writer, '--file', 'kcminputrc', '--group', 'Mouse',
                            '--key', 'XLbInptPointerAcceleration', str(speed)], check=True)
            # Best-effort live reload - harmless no-op if kwin isn't the
            # compositor or the dbus call isn't available; the config file
            # write above is what actually matters for persistence.
            for qdbus in ('qdbus6', 'qdbus'):
                if shutil.which(qdbus):
                    subprocess.run([qdbus, 'org.kde.KWin', '/KWin', 'reconfigure'])
                    break
            self._show_toast('KDE settings applied')
        except Exception as e:
            self._show_error(f'Could not apply KDE settings: {e}')

    def _build_customize_page(self):
        caps = self._caps

        led_group = Adw.PreferencesGroup()
        led_group.set_title('LED')

        # LED brightness - shown/edited as 0-100%, converted to the
        # device's raw brightness_range (usually 0-255) at apply/reload
        # time (see _apply()/_populate_profile()) so the on-wire value and
        # the on-screen value don't have to match.
        self._bright_row = None
        if caps.has_led:
            row, self._bright_row = self._make_slider_row(
                'LED Brightness', '', 0, 100, 5,
                format_value=lambda v: f'{int(v)}%')
            led_group.add(row)

        self._led_row = None
        if caps.has_led and caps.led_effects:
            self._led_row = Adw.ComboRow()
            self._led_row.set_title('LED Effect')
            self._led_row.set_model(
                Gtk.StringList.new([e.capitalize() for e in caps.led_effects])
            )
            self._led_row.connect('notify::selected', self._on_led_changed)
            led_group.add(self._led_row)

        # Breathe speed - self._breathe_row is the Gtk.Scale (get_value/
        # set_value, as elsewhere), self._breathe_row_container is the
        # wrapping Adw.ActionRow (title + slider together) - the visibility
        # toggle below needs to hide/show the whole row, not just the
        # slider inside it.
        self._breathe_row = None
        self._breathe_row_container = None
        if caps.has_led and caps.has_breathe_speed:
            lo, hi = caps.breathe_speed_range
            self._breathe_row_container, self._breathe_row = self._make_slider_row(
                'Pulse Speed', '', lo, hi, 1,
                marks=[(lo, 'Slow'), (hi, 'Fast')])
            self._breathe_row.set_draw_value(False)
            self._breathe_row_container.set_visible(False)
            led_group.add(self._breathe_row_container)

        btn_group = Adw.PreferencesGroup()
        btn_group.set_title('Button Bindings')
        btn_group.set_description('Click a button to remap it')

        self._btn_rows = {}
        for btn_name, btn_id in caps.buttons.items():
            row = Adw.ActionRow()
            label = caps.button_labels.get(btn_name, btn_name.capitalize())
            row.set_title(label)
            row.set_subtitle('–')
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
            row.connect('activated', self._on_button_row_activated, btn_id, label)
            btn_group.add(row)
            self._btn_rows[btn_id] = row

        groups = []
        if caps.has_led:
            groups.append(led_group)
        groups.append(btn_group)
        return self._wrap_page(*groups)

    _AUTOSTART_PATH = os.path.expanduser('~/.config/autostart/pulsar-mouse.desktop')
    _AUTOSTART_CONTENT = """\
[Desktop Entry]
Name=Pulsar Mouse
Comment=Pulsar Mouse system-tray applet
Exec=pulsar-mouse-gui
Icon=input-mouse
Type=Application
X-GNOME-Autostart-enabled=true
"""

    def _autostart_enabled(self):
        return os.path.exists(self._AUTOSTART_PATH)

    def _set_autostart(self, enabled):
        if enabled:
            os.makedirs(os.path.dirname(self._AUTOSTART_PATH), exist_ok=True)
            with open(self._AUTOSTART_PATH, 'w') as f:
                f.write(self._AUTOSTART_CONTENT)
        else:
            os.remove(self._AUTOSTART_PATH)

    def _on_autostart_row_toggled(self, row, _pspec):
        if row.get_active() != self._autostart_enabled():
            self._set_autostart(row.get_active())

    def _build_tools_page(self):
        caps = self._caps

        startup_group = Adw.PreferencesGroup()
        autostart_row = Adw.SwitchRow()
        autostart_row.set_title('Start on Login')
        autostart_row.set_active(self._autostart_enabled())
        autostart_row.connect('notify::active', self._on_autostart_row_toggled)
        startup_group.add(autostart_row)

        diagnostics_group = Adw.PreferencesGroup()
        diagnostics_group.set_title('Diagnostics')

        test_row = Adw.ButtonRow()
        test_row.set_title('Test Input')
        test_row.connect('activated', self._on_test_clicked)
        diagnostics_group.add(test_row)

        if caps.has_reset:
            reset_row = Adw.ButtonRow()
            reset_row.set_title('Reset to Factory Defaults')
            reset_row.add_css_class('destructive-action')
            reset_row.connect('activated', self._on_reset_clicked)
            diagnostics_group.add(reset_row)

        io_group = Adw.PreferencesGroup()
        io_group.set_title('Profile Import / Export')
        io_group.set_description(
            'Save the current profile\'s settings to a JSON file, or load them from one')

        export_row = Adw.ButtonRow()
        export_row.set_title('Export Profile to JSON')
        export_row.connect('activated', self._on_export_clicked)
        io_group.add(export_row)

        import_row = Adw.ButtonRow()
        import_row.set_title('Import Profile from JSON')
        import_row.connect('activated', self._on_import_clicked)
        io_group.add(import_row)

        return self._wrap_page(startup_group, diagnostics_group, io_group)

    def _build_power_page(self):
        device = self._device
        self._power_saving_row = None
        self._low_power_row = None
        groups = []

        if hasattr(device, 'set_power_saving_timeout') or hasattr(device, 'set_low_power_threshold'):
            power_group = Adw.PreferencesGroup()
            power_group.set_title('Power Management')
            power_group.set_description('Wireless power-saving behaviour')

            if hasattr(device, 'set_power_saving_timeout'):
                row, self._power_saving_row = self._make_slider_row(
                    'Wireless Power Saving',
                    'Inactivity before the mouse sleeps (30 sec – 15 min)',
                    30, 900, 30, snap=True,
                    format_value=lambda v: f'{int(v) // 60}:{int(v) % 60:02d}')
                power_group.add(row)

            if hasattr(device, 'set_low_power_threshold'):
                row, self._low_power_row = self._make_slider_row(
                    'Low Power Mode',
                    'Battery percentage that triggers low power mode',
                    0, 100, 5, snap=True)
                power_group.add(row)

            groups.append(power_group)

        return self._wrap_page(*groups)

    # ── Home tab live updates ───────────────────────────────────────────

    def _start_home_updates(self):
        """The hidraw listener below is independent of the tray's own
        (PulsarMouseApp._hidraw_listener) - deliberately not shared, so
        this window stays self-contained regardless of whether a tray
        exists. hidraw supports multiple concurrent readers, so a second
        listener on the same device is safe, just a bit redundant.

        The periodic *battery* poll is NOT duplicated the same way - it's
        driven entirely by PulsarMouseApp._set_battery_label() (see its
        call into _set_home_battery()), since the tray is always built
        alongside this window (see _on_activate()) and there's no reason
        to read the same USB endpoint twice on the same timer.
        """
        device = self._device
        if device is None:
            return
        self._refresh_home_battery()
        threading.Thread(target=self._home_hidraw_listener, daemon=True).start()

    def _refresh_home_battery(self):
        device = self._device
        if device is None or not hasattr(device, 'get_power'):
            return

        def _read():
            try:
                with _USB_LOCK:
                    device.open()
                    try:
                        pwr = device.get_power()
                    finally:
                        device.close()
                GLib.idle_add(self._set_home_battery, pwr)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()

    def _set_home_battery(self, pwr):
        if self._home_battery_row is None:
            return
        pct = pwr['battery_percent']
        charging = pwr['power_connected']
        label = '  (charging)' if charging else ''
        self._home_battery_row.set_subtitle(f'{pct}%{label}')
        if self._home_battery_icon is not None:
            self._home_battery_icon.set_from_icon_name(_battery_icon_name(pct, charging))

    def _refresh_firmware_version(self):
        device = self._device
        if device is None or self._firmware_row is None:
            return

        def _read():
            try:
                with _USB_LOCK:
                    device.open()
                    try:
                        fw = device.get_firmware_version()
                    finally:
                        device.close()
                if fw != 'unknown':
                    def _show_fw(v):
                        self._firmware_row.set_subtitle(v)
                        self._firmware_row.set_visible(True)
                    GLib.idle_add(_show_fw, fw)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()

    def _home_hidraw_listener(self):
        # Only ever acts on signal_percent events (unlike the tray's own
        # _hidraw_listener, which also handles DPI) - gated on get_power,
        # not find_hidraw, for the same reason as _home_signal_row's
        # construction above: find_hidraw's base-class default makes
        # hasattr() on it true for every driver, wired or wireless.
        #
        # This thread owns its fd exclusively, start to finish - opens
        # it, and is the only thing that ever closes it. Shutdown is
        # cooperative: _on_close_request just sets self._home_hidraw_stop
        # and this loop notices it within one select() timeout (at most
        # ~1s), rather than another thread reaching in to close the fd
        # itself to unblock a plain blocking read() - Linux doesn't
        # guarantee that actually wakes a concurrent read() promptly (see
        # close(2)), and it invited a check-then-act double-close race on
        # top. A single stop flag has neither problem.
        device = self._device
        if device is None or self._home_signal_row is None:
            return
        path = device.find_hidraw()
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        last_update = 0.0
        try:
            while not self._home_hidraw_stop:
                ready, _w, _x = select.select([fd], [], [], 1.0)
                if not ready:
                    continue  # timeout - just a chance to re-check the stop flag
                data = os.read(fd, 256)
                if not data:
                    break
                event = device.parse_hidraw_event(data)
                if event and 'signal_percent' in event:
                    now = time.monotonic()
                    if now - last_update >= _SIGNAL_UPDATE_INTERVAL:
                        last_update = now
                        GLib.idle_add(self._set_home_signal, event['signal_percent'])
        except OSError:
            pass
        finally:
            os.close(fd)

    def _set_home_signal(self, pct):
        if self._home_signal_row is None:
            return
        self._home_signal_row.set_subtitle(f'{pct}%  —  {_connection_quality_label(pct)}')

    def _make_slider_row(self, title, subtitle, lo, hi, step,
                         format_value=None, marks=None, snap=False):
        """An Adw.ActionRow with a real Gtk.Scale slider as its suffix,
        rather than a SpinRow's +/- stepper. Gtk.Scale (a Gtk.Range
        subclass) already has the same get_value()/set_value() API a
        SpinRow does, so callers can use the returned scale exactly like
        the SpinRow fields elsewhere in this file - no other code needs
        to know the difference.

        `format_value(value) -> str`, if given, replaces the plain numeric
        readout (e.g. seconds -> "4:30", a raw 0-255 brightness -> "62%").
        `marks`, if given, is a list of (value, label) endpoint captions
        drawn below the trough (e.g. [(0, 'Slow'), (100, 'Fast')]) -
        these are direction cues alongside the numeric readout, not a
        replacement for it. `snap`, if True, rounds the value to the
        nearest multiple of `step` (relative to `lo`) as the user drags -
        step_increment on its own only affects keyboard/scroll-wheel
        nudges, not mouse-drag positioning, which GtkRange otherwise
        computes continuously from pointer position.
        """
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                             page_increment=step * 2)
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        scale.set_valign(Gtk.Align.CENTER)
        scale.set_hexpand(True)
        scale.set_size_request(180, -1)
        for mark_value, label in (marks or []):
            scale.add_mark(mark_value, Gtk.PositionType.BOTTOM, label)
        if snap:
            def _snap(s, _step=step, _lo=lo):
                # Skip while populating from a real device read (see
                # _populate_global()'s self._building guard) - the stored
                # value on real hardware isn't guaranteed to already be a
                # step multiple (seen e.g. 297s power-saving, not a
                # multiple of 30), and snapping it here would silently
                # change what the next Apply writes back to the device
                # without the user ever touching this slider.
                if self._building:
                    return
                snapped = _lo + round((s.get_value() - _lo) / _step) * _step
                if snapped != s.get_value():
                    s.set_value(snapped)
            scale.connect('value-changed', _snap)
        if format_value is not None:
            # GtkScale's own "format-value" is a C-only vfunc, not
            # connectable as a signal in this GI binding (raises
            # "unknown signal name" at runtime) - a plain label kept in
            # sync via the standard value-changed signal works everywhere.
            scale.set_draw_value(False)
            value_label = Gtk.Label(label=format_value(adj.get_value()))
            value_label.set_valign(Gtk.Align.CENTER)
            value_label.set_width_chars(5)
            scale.connect('value-changed',
                          lambda s: value_label.set_label(format_value(s.get_value())))
            row.add_suffix(scale)
            row.add_suffix(value_label)
        else:
            scale.set_digits(0)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            row.add_suffix(scale)
        return row, scale

    # ── UI event handlers ────────────────────────────────────────────────

    def _on_test_clicked(self, _row):
        dialog = InputTestDialog(transient_for=self, device=self._device)
        dialog.present()

    def _on_button_row_activated(self, _row, btn_id, label):
        current = self._btn_rows[btn_id].get_subtitle()
        dialog = RemapButtonDialog(
            transient_for=self, btn_label=label, current_spec=current,
            device=self._device,
            on_applied=lambda t, a1, a2: self._run_bg(
                lambda: self._do_remap_button(btn_id, t, a1, a2)))
        dialog.present()

    def _do_remap_button(self, btn_id, btn_type, a1, a2):
        if not self._open_dev():
            return
        try:
            self._device.set_button(btn_id, btn_type, a1, a2, self._profile)
            t, ra1, ra2 = self._device.get_button(btn_id, self._profile)
            GLib.idle_add(self._btn_rows[btn_id].set_subtitle, self._device.describe_button(t, ra1, ra2))
            GLib.idle_add(self._show_toast, 'Button remapped')
        except Exception as e:
            GLib.idle_add(self._show_error, f'Remap error: {e}')
        self._close_dev()

    def _on_profile_changed(self, combo, _param):
        if self._building:
            return
        self._profile = combo.get_selected() + 1
        GLib.idle_add(self._reload_profile)

    def _on_led_changed(self, combo, _param):
        if self._building:
            return
        caps = self._caps
        if caps and self._breathe_row_container:
            effects = caps.led_effects
            selected = effects[combo.get_selected()] if combo.get_selected() < len(effects) else ''
            # The breathing effect is always the last entry in led_effects,
            # by convention of every driver in this codebase (['off',
            # 'steady', 'breathe']) - checked by position rather than the
            # literal name in case a future driver ever needs a different
            # one.
            self._breathe_row_container.set_visible(selected == effects[-1])

    def _on_stage_count_changed(self, row, _param):
        if self._building:
            return
        n = int(row.get_value())
        for i, dpi_row in enumerate(self._dpi_rows):
            dpi_row.set_sensitive(i < n)

    def _on_reset_clicked(self, _row):
        dialog = Adw.AlertDialog()
        dialog.set_heading('Reset to Factory Defaults?')
        dialog.set_body(
            'This sends the firmware reset command to the mouse.\n'
            'All profiles will be restored to factory defaults.'
        )
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('reset', 'Reset')
        dialog.set_response_appearance('reset', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', self._on_reset_response)
        dialog.present(self)

    def _on_reset_response(self, _dialog, response):
        if response == 'reset':
            self._run_bg(self._do_reset)

    def _on_export_clicked(self, _row):
        dialog = Gtk.FileDialog()
        dialog.set_title('Export Profile')
        dialog.set_initial_name(f'profile-{self._profile}.json')
        json_filter = Gtk.FileFilter()
        json_filter.set_name('JSON files')
        json_filter.add_pattern('*.json')
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.save(self, None, self._on_export_finish)

    def _on_export_finish(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        profile = self._profile

        def _do():
            if not self._open_dev():
                return
            try:
                data = self._device.export_profile(profile)
            finally:
                self._close_dev()
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            GLib.idle_add(self._show_toast, f'Profile {profile} exported')
        self._run_bg(_do)

    def _on_import_clicked(self, _row):
        dialog = Gtk.FileDialog()
        dialog.set_title('Import Profile')
        json_filter = Gtk.FileFilter()
        json_filter.set_name('JSON files')
        json_filter.add_pattern('*.json')
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(json_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_import_finish)

    def _on_import_finish(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        path = gfile.get_path()
        profile = self._profile

        def _do():
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception as e:
                GLib.idle_add(self._show_error, f'Cannot read file: {e}')
                return
            if data.get('format') != 'pulsar-mouse-profile':
                GLib.idle_add(self._show_error, 'Not a valid profile file')
                return
            if not self._open_dev():
                return
            try:
                warnings = self._device.import_profile(profile, data)
            finally:
                self._close_dev()
            if warnings:
                msg = '\n'.join(warnings)
                GLib.idle_add(self._show_error, f'Imported with warnings:\n{msg}')
            else:
                GLib.idle_add(self._show_toast, f'Profile {profile} imported')
            GLib.idle_add(self._reload_profile)
        self._run_bg(_do)

    # ── Thread management ────────────────────────────────────────────────

    def _run_bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _reload(self):
        self._run_bg(self._do_reload)

    def _reload_profile(self):
        self._run_bg(self._do_reload_profile)

    def _apply(self):
        caps = self._caps
        if not caps:
            return
        num_stages = int(self._stage_count_row.get_value())
        poll_idx = self._poll_row.get_selected()
        s = {
            'poll_hz':    caps.polling_rates[poll_idx] if poll_idx < len(caps.polling_rates) else caps.polling_rates[-1],
            'profile':    self._profile,
            'num_stages': num_stages,
            'active':     min(self._active_stage_row.get_selected() + 1, num_stages),
            'dpi_values': [int(r.get_value()) for r in self._dpi_rows],
        }
        if self._home_mode_row is not None:
            self._home_mode_row.set_subtitle(f"{s['poll_hz']} Hz")
        if self._home_dpi_row is not None:
            self._home_dpi_row.set_subtitle(f"{s['dpi_values'][s['active'] - 1]} DPI")
        if self._debounce_row:
            s['debounce'] = int(self._debounce_row.get_value())
        if self._angle_row:
            s['angle'] = self._angle_row.get_active()
        if self._ripple_row:
            s['ripple'] = self._ripple_row.get_active()
        if self._motion_row:
            s['motion'] = self._motion_row.get_active()
        if self._power_saving_row:
            s['power_saving'] = int(self._power_saving_row.get_value())
        if self._low_power_row:
            s['low_power'] = int(self._low_power_row.get_value())
        if self._lod_row:
            if caps.lod_step:
                s['lod'] = round(self._lod_row.get_value(), 1)
            else:
                s['lod'] = caps.lod_values[self._lod_row.get_selected()]
        if self._bright_row:
            lo, hi = caps.brightness_range
            pct = self._bright_row.get_value()
            s['brightness'] = round(lo + pct / 100 * (hi - lo))
        if self._led_row:
            s['led'] = caps.led_effects[self._led_row.get_selected()]
        if self._breathe_row:
            s['breathe'] = int(self._breathe_row.get_value())
        if self._color_buttons:
            colors = []
            for btn in self._color_buttons:
                rgba = btn.get_rgba()
                colors.append((round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)))
            s['colors'] = colors
        self._run_bg(lambda: self._do_apply(s))

    # ── USB workers (run in background threads) ──────────────────────────

    def _open_dev(self) -> bool:
        device = self._device
        if device is None:
            GLib.idle_add(self._show_error, 'No device available')
            return False
        _USB_LOCK.acquire()
        try:
            device.open()
            GLib.idle_add(self._banner.set_revealed, False)
            return True
        except RuntimeError as e:
            _USB_LOCK.release()
            GLib.idle_add(self._show_error, str(e))
            return False
        except Exception as e:
            _USB_LOCK.release()
            GLib.idle_add(self._show_error, f'Error opening device: {e}')
            return False

    def _close_dev(self):
        if self._device:
            try:
                self._device.close()
            except Exception:
                pass
        _USB_LOCK.release()

    def _read_and_populate_global(self):
        # Each field guarded independently via _read_field (same reasoning
        # as _do_reload_profile_inner() below) - a reload is 20+ individual
        # USB reads in quick succession, and a transient timeout on any one
        # of them (mouse's RF link briefly asleep) shouldn't blank out
        # every other already-successful field, including these. These
        # fields have no --profile N-style targeting at all (see cli.py's
        # own comment on this split, next to --status-json) - they always
        # reflect whichever profile is *actually* active on the device,
        # regardless of which profile's settings the rest of this window
        # is showing, so this needs re-running any time the active profile
        # might have changed, not just on a full window reload.
        caps = self._caps
        device = self._device
        poll_hz = self._read_field(device.get_polling_rate)
        debounce = self._read_field(device.get_debounce) if caps.has_debounce else None
        angle = self._read_field(device.get_angle_snap) if caps.has_angle_snap else None
        ripple = self._read_field(device.get_ripple_control) if caps.has_ripple_control else None
        motion = self._read_field(device.get_motion_sync) if caps.has_motion_sync else None
        power_saving = (self._read_field(device.get_power_saving_timeout)
                        if hasattr(device, 'get_power_saving_timeout') else None)
        low_power = (self._read_field(device.get_low_power_threshold)
                    if hasattr(device, 'get_low_power_threshold') else None)
        GLib.idle_add(self._populate_global, poll_hz, debounce, angle, ripple, motion,
                      power_saving, low_power)

    def _do_reload(self):
        if not self._open_dev():
            return
        self._read_and_populate_global()
        self._do_reload_profile_inner()
        self._close_dev()

    def _do_reload_profile(self):
        if not self._open_dev():
            return
        device = self._device
        # Selecting a profile in the dropdown switches the mouse's live
        # active profile, not just which profile's settings this window is
        # showing - matches the behaviour of a physical profile button.
        # set_active_profile() is a concrete base-class method (defaults to
        # NotImplementedError) rather than one only some drivers define, so
        # hasattr() can't tell support apart here - same reasoning as
        # _read_field()'s NotImplementedError handling below.
        try:
            device.set_active_profile(self._profile)
        except NotImplementedError:
            pass
        except Exception as e:
            GLib.idle_add(self._show_error, f'Could not switch active profile: {e}')
        # Real bug, found in review: this used to skip
        # _read_and_populate_global() entirely, so switching profiles left
        # the poll/debounce/angle/ripple/motion/power widgets showing the
        # PREVIOUS active profile's values - and the next Apply would then
        # write those stale values back over the profile that's actually
        # active now, silently clobbering it. The Noctalia panel already
        # got this right (full re-fetch in its own profile-switch
        # handler); this was the one place that missed it.
        self._read_and_populate_global()
        self._do_reload_profile_inner()
        self._close_dev()

    def _read_field(self, fn, *args):
        # Drivers may only implement a subset of get_* methods (e.g. a
        # write-only-so-far driver like feinmann8k.py) - one missing getter
        # shouldn't block every other field from loading. Also covers a
        # read that times out (raises IOError/OSError - see
        # feinmann8k.py's _query_ctrl()) because the mouse's RF link
        # happened to be asleep for that one field: a reload can involve
        # 20+ individual reads in quick succession, so hitting a transient
        # timeout on any single one of them is expected, not exceptional -
        # it shouldn't blank out every other already-successful field.
        try:
            return fn(*args)
        except Exception:
            return None

    def _do_reload_profile_inner(self):
        caps = self._caps
        device = self._device
        p = self._profile
        try:
            lod = self._read_field(device.get_lod, p) if caps.lod_values else None
            brightness = self._read_field(device.get_brightness, p) if caps.has_led else None
            led = self._read_field(device.get_led_effect, p) if caps.has_led else None
            breathe = (self._read_field(device.get_breathe_speed, p)
                      if caps.has_led and caps.has_breathe_speed else None)
            try:
                # NOTE: on some drivers, get_dpi_stages() is known to
                # mutate the device's active DPI stage as a side effect of
                # reading it (see that driver's docstring, if so) - every
                # reload here can silently change what the user thinks is
                # selected. Not fixed here; tracked as a driver-level issue.
                dpi_info = device.get_dpi_stages(p)
            except (NotImplementedError, OSError):
                dpi_info = {'active': -1, 'count': caps.max_dpi_stages, 'stages': []}
            colors = None
            if caps.has_stage_colors:
                colors = [self._read_field(device.get_stage_color, i, p)
                         for i in range(1, caps.max_dpi_stages + 1)]
            buttons = {bid: self._read_field(device.get_button, bid, p)
                       for bid in caps.buttons.values()}
            GLib.idle_add(self._populate_profile,
                          lod, brightness, led, breathe, dpi_info, colors, buttons)
        except Exception as e:
            GLib.idle_add(self._show_error, f'Read error (profile {p}): {e}')

    def _do_apply(self, s: dict):
        if not self._open_dev():
            return
        caps = self._caps
        device = self._device
        try:
            device.set_polling_rate(s['poll_hz'])
            if 'debounce' in s:
                device.set_debounce(s['debounce'])
            if 'angle' in s:
                device.set_angle_snap(s['angle'])
            if 'ripple' in s:
                device.set_ripple_control(s['ripple'])
            if 'motion' in s:
                device.set_motion_sync(s['motion'])
            if 'power_saving' in s:
                device.set_power_saving_timeout(s['power_saving'])
            if 'low_power' in s:
                device.set_low_power_threshold(s['low_power'])

            p = s['profile']
            if 'lod' in s:
                device.set_lod(s['lod'], p)
            if 'brightness' in s:
                device.set_brightness(s['brightness'], p)
            if 'led' in s:
                device.set_led_effect(s['led'], p)
                # See _on_led_changed()'s comment - checked by position,
                # not the literal name.
                if s['led'] == caps.led_effects[-1] and 'breathe' in s:
                    device.set_breathe_speed(s['breathe'], p)

            stages = s['dpi_values'][:s['num_stages']]
            try:
                device.set_dpi_stages(stages, s['active'], p)
            except NotImplementedError:
                # Per-stage DPI values aren't writable on this driver yet -
                # still apply the active-stage selection on its own so that
                # part of the DPI Stages section isn't silently a no-op.
                device.set_active_dpi_stage(s['active'], p)

            if 'colors' in s:
                for i, (r, g, b) in enumerate(s['colors'][:s['num_stages']], start=1):
                    device.set_stage_color(i, r, g, b, p)

            GLib.idle_add(self._show_toast, 'Settings applied')
        except Exception as e:
            GLib.idle_add(self._show_error, f'Write error: {e}')
        self._close_dev()

    def _do_reset(self):
        if not self._open_dev():
            return
        try:
            self._device.reset_to_defaults(self._profile)
            GLib.idle_add(self._show_toast, 'Reset to factory defaults')
        except Exception as e:
            GLib.idle_add(self._show_error, f'Reset error: {e}')
        self._close_dev()
        time.sleep(1.0)
        GLib.idle_add(self._reload)

    # ── UI population helpers ────────────────────────────────────────────

    def _populate_global(self, poll_hz, debounce, angle, ripple, motion,
                         power_saving=None, low_power=None):
        caps = self._caps
        self._building = True
        # Find index of polling rate
        try:
            poll_idx = caps.polling_rates.index(poll_hz)
        except ValueError:
            poll_idx = len(caps.polling_rates) - 1
        self._poll_row.set_selected(poll_idx)
        if self._home_mode_row is not None:
            self._home_mode_row.set_subtitle(f'{poll_hz} Hz')
        if self._debounce_row and debounce is not None:
            self._debounce_row.set_value(debounce)
        if self._angle_row and angle is not None:
            self._angle_row.set_active(angle)
        if self._ripple_row and ripple is not None:
            self._ripple_row.set_active(ripple)
        if self._motion_row and motion is not None:
            self._motion_row.set_active(motion)
        if self._power_saving_row and power_saving is not None:
            self._power_saving_row.set_value(power_saving)
        if self._low_power_row and low_power is not None:
            self._low_power_row.set_value(low_power)
        self._building = False

    def _populate_profile(self, lod, brightness, led, breathe, dpi_info, colors, buttons):
        caps = self._caps
        self._building = True
        if self._lod_row and lod is not None:
            if caps.lod_step:
                self._lod_row.set_value(lod)
            else:
                try:
                    lod_idx = caps.lod_values.index(lod)
                except ValueError:
                    lod_idx = 0
                self._lod_row.set_selected(lod_idx)
        if self._bright_row and brightness is not None:
            lo, hi = caps.brightness_range
            pct = round((brightness - lo) / (hi - lo) * 100) if hi > lo else 0
            self._bright_row.set_value(pct)
        if self._led_row and led is not None:
            try:
                led_idx = caps.led_effects.index(led)
            except ValueError:
                led_idx = 1
            self._led_row.set_selected(led_idx)
        if self._breathe_row and breathe is not None:
            self._breathe_row.set_value(breathe)
            if led and self._breathe_row_container:
                self._breathe_row_container.set_visible(led == caps.led_effects[-1])

        stages = dpi_info['stages']
        num    = dpi_info['count']
        active = dpi_info['active']
        self._stage_count_row.set_value(num)
        self._active_stage_row.set_selected(max(0, active - 1))
        for i, row in enumerate(self._dpi_rows):
            row.set_value(stages[i][0] if i < len(stages) else 800)
            row.set_sensitive(i < num)
        if self._home_dpi_row is not None and 1 <= active <= len(stages):
            self._home_dpi_row.set_subtitle(f'{stages[active - 1][0]} DPI')

        if self._color_buttons and colors:
            for i, btn in enumerate(self._color_buttons):
                rgb = colors[i] if i < len(colors) else None
                if rgb is not None:
                    r, g, b = rgb
                    rgba = Gdk.RGBA()
                    rgba.red, rgba.green, rgba.blue, rgba.alpha = r / 255, g / 255, b / 255, 1.0
                    btn.set_rgba(rgba)
                btn.set_sensitive(i < num)

        for btn_id, bind in buttons.items():
            if bind is not None and btn_id in self._btn_rows:
                t, a1, a2 = bind
                self._btn_rows[btn_id].set_subtitle(self._device.describe_button(t, a1, a2))
        self._building = False

    def _show_error(self, msg: str):
        self._banner.set_title(msg)
        self._banner.set_revealed(True)

    def _show_toast(self, msg: str):
        toast = Adw.Toast.new(msg)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)


class RemapButtonDialog(Adw.Window):
    """Dialog for remapping a single mouse button via category + preset
    dropdowns, rather than typing a raw --button spec string like the CLI.
    Builds the same spec strings parse_button_function() already accepts
    (e.g. 'left', 'dpi+', 'ctrl+c') from the selected dropdowns/checkboxes,
    so it reuses all of hid.py's existing parsing/validation instead of
    duplicating it.
    """

    _CATEGORIES = ['Mouse Click', 'Scroll', 'DPI', 'Profile Switch',
                   'Media Key', 'Keyboard Shortcut', 'Disabled']
    _MOD_NAMES = ['ctrl', 'shift', 'alt', 'super']

    def __init__(self, btn_label: str, current_spec: str, on_applied, device=None, **kwargs):
        super().__init__(**kwargs, title=f'Remap {btn_label}',
                         default_width=360, default_height=-1)
        self.set_modal(True)
        self._on_applied = on_applied
        self._device = device

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)
        toolbar.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        toolbar.set_content(box)

        group = Adw.PreferencesGroup()
        box.append(group)

        self._category_row = Adw.ComboRow()
        self._category_row.set_title('Function')
        self._category_row.set_model(Gtk.StringList.new(self._CATEGORIES))
        self._category_row.connect('notify::selected', self._on_category_changed)
        group.add(self._category_row)

        self._sub_row = Adw.ComboRow()
        self._sub_row.set_title('Action')
        group.add(self._sub_row)

        self._mod_row = Adw.ActionRow()
        self._mod_row.set_title('Modifiers')
        mod_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._mod_checks = {}
        for name in self._MOD_NAMES:
            cb = Gtk.CheckButton(label=name.capitalize())
            mod_box.append(cb)
            self._mod_checks[name] = cb
        self._mod_row.add_suffix(mod_box)
        group.add(self._mod_row)

        self._error_label = Gtk.Label()
        self._error_label.add_css_class('error')
        self._error_label.set_wrap(True)
        self._error_label.set_visible(False)
        box.append(self._error_label)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END)
        cancel_btn = Gtk.Button(label='Cancel')
        cancel_btn.connect('clicked', lambda _: self.close())
        set_btn = Gtk.Button(label='Set')
        set_btn.add_css_class('suggested-action')
        set_btn.connect('clicked', self._on_set_clicked)
        btn_box.append(cancel_btn)
        btn_box.append(set_btn)
        box.append(btn_box)

        self._preselect(current_spec)

    def _category_options(self, cat: str):
        return {
            'Mouse Click': list(MOUSE_ACTIONS),
            'Scroll': list(SCROLL_ACTIONS),
            'DPI': list(DPI_ACTIONS),
            'Profile Switch': list(PROFILE_ACTIONS),
            'Media Key': list(MEDIA_CODES),
            'Keyboard Shortcut': sorted(HID_KEYS),
        }.get(cat, [])

    def _on_category_changed(self, *_args):
        self._update_visible_rows()

    def _update_visible_rows(self):
        # Only reached via _on_category_changed() (the user picking a
        # different category), never during __init__ - _preselect() below
        # already fully owns initial model/visibility/selection, and
        # rebuilding the model here would reset that selection back to 0.
        cat = self._CATEGORIES[self._category_row.get_selected()]
        if cat == 'Disabled':
            self._sub_row.set_visible(False)
            self._mod_row.set_visible(False)
            return
        self._sub_row.set_visible(True)
        self._sub_row.set_title('Key' if cat == 'Keyboard Shortcut' else 'Action')
        options = self._category_options(cat)
        self._sub_row.set_model(Gtk.StringList.new([o.capitalize() for o in options]))
        self._mod_row.set_visible(cat == 'Keyboard Shortcut')

    def _preselect(self, spec: str):
        spec = (spec or '').strip().lower()
        if not spec or spec == '–' or spec == 'disabled':
            self._category_row.set_selected(self._CATEGORIES.index('Disabled'))
            self._sub_row.set_visible(False)
            self._mod_row.set_visible(False)
            return
        if spec in MOUSE_ACTIONS:
            self._select(spec, 'Mouse Click', list(MOUSE_ACTIONS))
            return
        if spec in SCROLL_ACTIONS:
            self._select(spec, 'Scroll', list(SCROLL_ACTIONS))
            return
        if spec in DPI_ACTIONS:
            self._select(spec, 'DPI', list(DPI_ACTIONS))
            return
        if spec in PROFILE_ACTIONS:
            self._select(spec, 'Profile Switch', list(PROFILE_ACTIONS))
            return
        if spec in MEDIA_CODES:
            self._select(spec, 'Media Key', list(MEDIA_CODES))
            return
        # Keyboard shortcut: mod+mod+key, every part a known modifier/key -
        # same vocabulary check parse_button_function() itself uses.
        parts = spec.split('+')
        mods = [p for p in parts if p in HID_MODS]
        keys = [p for p in parts if p in HID_KEYS]
        if keys and len(mods) + len(keys) == len(parts):
            self._category_row.set_selected(self._CATEGORIES.index('Keyboard Shortcut'))
            self._sub_row.set_model(Gtk.StringList.new([k.capitalize() for k in sorted(HID_KEYS)]))
            self._sub_row.set_title('Key')
            self._sub_row.set_visible(True)
            self._mod_row.set_visible(True)
            try:
                self._sub_row.set_selected(sorted(HID_KEYS).index(keys[0]))
            except ValueError:
                pass
            for name, cb in self._mod_checks.items():
                cb.set_active(name in mods or (name == 'super' and 'gui' in mods))
            return
        # Unrecognised (raw hex fallback from describe_button(), or a type
        # this dialog doesn't model) - default to Mouse Click/Left rather
        # than guessing further.
        self._select('left', 'Mouse Click', list(MOUSE_ACTIONS))

    def _select(self, value, category, options):
        self._category_row.set_selected(self._CATEGORIES.index(category))
        self._sub_row.set_model(Gtk.StringList.new([o.capitalize() for o in options]))
        self._sub_row.set_title('Action')
        self._sub_row.set_visible(True)
        self._mod_row.set_visible(False)
        try:
            self._sub_row.set_selected(options.index(value))
        except ValueError:
            pass

    def _build_spec(self) -> str:
        cat = self._CATEGORIES[self._category_row.get_selected()]
        if cat == 'Disabled':
            return 'disabled'
        options = self._category_options(cat)
        idx = self._sub_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(options):
            raise ValueError('Choose an action')
        value = options[idx]
        if cat == 'Keyboard Shortcut':
            mods = [name for name, cb in self._mod_checks.items() if cb.get_active()]
            return '+'.join(mods + [value])
        return value

    def _on_set_clicked(self, _btn):
        try:
            spec = self._build_spec()
            if self._device is not None:
                t, a1, a2 = self._device.parse_button_function(spec)
            else:
                t, a1, a2 = parse_button_function(spec)
        except ValueError as e:
            self._error_label.set_label(str(e))
            self._error_label.set_visible(True)
            return
        self.close()
        self._on_applied(t, a1, a2)


class InputTestDialog(Adw.Window):
    """Input test dialog with a mouse diagram and event log."""

    _ZONES = {
        1: ('Left Click',    0.02, 0.02, 0.44, 0.35),
        2: ('Right Click',   0.54, 0.02, 0.44, 0.35),
        3: ('Wheel Click',   0.38, 0.06, 0.24, 0.20),
        8: ('Thumb Back',    0.00, 0.50, 0.30, 0.15),
        9: ('Thumb Forward', 0.00, 0.36, 0.30, 0.15),
    }
    _GTK_BTN_NAMES = {1: 'Left', 2: 'Middle', 3: 'Right',
                       8: 'Back (both sides)', 9: 'Forward (both sides)'}

    # User-provided top-down mouse artwork (single fill path, 800x800
    # source canvas) - drawn via _draw_svg_path() rather than hand-
    # transcribed into Cairo calls, since hand-copying ~50 curve segments
    # into Python literals is exactly the kind of thing that silently
    # introduces a typo'd control point. Parsed once at class-definition
    # time (see _ICON_SEGMENTS/_ICON_BBOX below), not per-frame.
    _ICON_PATH_D = (
        "M 499.50 455.52 C487.38,457.50 478.28,456.98 464.89,453.55 "
        "C458.21,451.84 443.77,445.39 438.99,441.99 L 436.00 439.86 "
        "L 436.00 425.31 C436.00,399.22 430.99,375.90 415.97,332.05 "
        "C410.98,317.50 407.04,304.45 407.20,303.05 "
        "C407.46,300.82 408.82,299.97 418.07,296.27 "
        "C453.93,281.91 494.45,279.18 532.60,288.55 "
        "C548.28,292.40 566.86,299.35 568.48,301.97 "
        "C568.82,302.52 564.16,317.93 558.12,336.23 "
        "C543.08,381.85 539.02,400.93 539.01,426.08 L 539.01 426.71 "
        "C539.00,434.75 539.00,438.52 537.25,441.08 "
        "C535.65,443.41 532.62,444.74 526.83,447.38 "
        "C517.15,451.79 510.82,453.68 499.50,455.52 "
        "ZM 570.52 292.96 C569.75,294.21 568.99,294.23 567.00,293.06 "
        "C564.01,291.30 541.30,283.84 533.15,281.93 "
        "C522.61,279.46 506.36,277.00 500.57,277.00 "
        "C498.13,277.00 494.98,276.56 493.57,276.02 "
        "C491.17,275.11 491.00,274.61 491.00,268.45 "
        "C491.00,262.51 491.27,261.63 493.67,259.67 "
        "C500.58,254.04 503.85,250.19 504.88,246.48 "
        "C506.21,241.70 506.33,211.12 505.04,205.58 "
        "C503.92,200.74 501.36,197.19 496.91,194.27 "
        "C494.40,192.62 492.96,191.71 492.13,190.41 "
        "C491.00,188.64 491.00,186.16 491.00,180.15 L 491.00 179.58 "
        "C491.00,166.32 491.04,166.29 502.78,167.91 "
        "C528.36,171.43 549.81,180.74 559.44,192.49 "
        "C570.50,205.99 575.77,228.77 574.69,258.50 "
        "C574.15,273.22 572.07,290.46 570.52,292.96 "
        "ZM 417.34 289.47 C405.40,294.41 405.37,294.41 404.63,292.48 "
        "C402.52,286.97 400.80,263.37 401.28,246.50 "
        "C401.86,225.84 404.20,214.47 410.32,202.43 "
        "C417.66,187.97 433.04,177.37 455.00,171.64 "
        "C467.62,168.34 481.82,166.42 483.23,167.83 "
        "C483.82,168.42 484.33,173.54 484.39,179.56 "
        "C484.49,189.64 484.37,190.32 482.18,191.86 "
        "C470.04,200.42 470.00,200.54 470.00,226.30 "
        "C470.00,250.69 470.18,251.31 478.98,257.50 "
        "C482.66,260.08 483.62,261.42 484.32,264.96 "
        "C484.79,267.33 484.90,270.74 484.56,272.53 "
        "C483.87,276.22 482.21,276.97 474.63,276.99 "
        "C461.10,277.02 432.24,283.31 417.34,289.47 "
        "ZM 555.06 427.34 L 554.65 427.77 "
        "C550.42,432.24 548.26,434.52 547.15,434.09 "
        "C546.00,433.64 546.00,430.23 546.00,423.27 L 546.00 422.63 "
        "C546.00,404.04 550.80,379.12 559.70,351.50 "
        "C565.22,334.36 566.76,332.00 572.47,332.00 "
        "C575.40,332.00 575.58,332.54 577.58,346.74 "
        "C582.20,379.64 574.63,406.71 555.06,427.34 "
        "ZM 429.90 424.46 C429.85,427.78 429.48,431.62 429.10,433.00 "
        "C428.42,435.41 428.14,435.24 420.95,427.94 "
        "C409.17,415.98 401.85,401.49 398.43,383.34 "
        "C396.51,373.14 396.95,348.86 399.24,338.50 "
        "C400.43,333.15 400.86,332.47 403.24,332.19 "
        "C408.06,331.62 409.58,333.44 413.37,344.22 "
        "C423.01,371.71 430.20,406.59 429.90,424.46 "
        "ZM 495.75 249.48 C490.47,255.09 483.56,254.83 478.51,248.83 "
        "L 475.86 245.68 L 476.18 225.26 L 476.50 204.84 L 480.24 201.42 "
        "C485.92,196.22 493.20,197.00 497.00,203.23 "
        "C498.81,206.21 498.99,208.27 499.00,226.26 L 499.00 246.03 "
        "ZM 406.00 324.18 C406.00,325.52 401.74,325.07 398.26,323.36 "
        "C393.76,321.14 391.31,316.68 388.59,305.82 "
        "C384.50,289.48 384.48,289.15 387.69,287.93 "
        "C389.23,287.34 391.91,287.14 393.66,287.46 "
        "C396.79,288.05 396.89,288.26 399.06,297.78 "
        "C400.27,303.13 402.33,311.07 403.63,315.43 "
        "C404.94,319.79 406.00,323.72 406.00,324.18 "
        "ZM 578.91 322.22 C577.56,323.23 574.79,324.32 572.76,324.65 "
        "L 569.07 325.25 L 570.12 321.88 "
        "C570.69,320.02 572.87,311.75 574.94,303.50 L 578.73 288.50 "
        "L 590.29 287.89 L 589.66 293.20 "
        "C588.13,306.22 583.46,318.82 578.91,322.22 "
        "ZM 394.68 280.22 C393.12,281.21 389.04,281.09 386.25,279.97 "
        "C384.08,279.10 384.00,278.61 384.02,266.28 "
        "C384.05,252.08 385.81,245.75 390.21,244.11 "
        "C393.37,242.93 393.96,244.23 394.02,252.50 "
        "C394.05,256.35 394.47,263.99 394.96,269.49 "
        "C395.62,276.89 395.55,279.67 394.68,280.22 "
        "ZM 590.24 279.35 C588.52,280.42 582.41,280.98 581.20,280.18 "
        "C580.15,279.48 580.12,275.99 581.07,261.41 "
        "C581.71,251.56 582.37,243.35 582.53,243.17 "
        "C583.31,242.30 587.31,245.47 589.05,248.31 "
        "C590.72,251.06 590.99,253.40 591.00,265.19 "
        "C591.00,272.72 590.66,279.09 590.24,279.35 Z"
    )

    # Approximate centroids (in the icon's own 800x800 source space) of
    # each interactive zone, eyeballed from the traced artwork's own
    # sub-shapes (left/right click paddles at the top, the small oval
    # between them for the wheel, the two side bars for thumb buttons).
    # Used to position highlight rings on _draw() clicks/scroll/DPI events
    # - the artwork itself doesn't carry this semantic info, so this is
    # our own mapping on top of it, not something derived from the SVG.
    # Centroids taken directly from each of the icon's own 10 subpaths
    # (computed with _svg_path_bbox() per-subpath, not eyeballed) so
    # every highlight actually lands on the shape it's meant to indicate.
    _ICON_ZONES = {
        1: (443, 230),        # left click paddle (subpath 2)
        3: (533, 230),        # right click paddle (subpath 1)
        2: (487, 226),        # wheel (subpath 5)
        'scroll_up': (487, 183),    # above the wheel, not on top of it
        'scroll_down': (487, 269),  # below the wheel
        'dpi': (487, 182),
        # GTK's back(8)/forward(9) codes don't say which physical side
        # was pressed - the OS can't tell, since both sides of a
        # symmetric thumb pair report the same code (see _GTK_BTN_NAMES'
        # "both sides" labels). So unlike the zones above, 8/9 aren't
        # looked up directly here - _draw() lights up every caps.buttons
        # thumb name whose *default* binding matches that direction
        # (thumb1/thumb3 = front = forward, thumb2/thumb4 = back, per
        # each driver's own comments). Forward sits on the small thumb-
        # click bar (subpaths 8/9); back sits lower, on the larger grip
        # panel beneath it (subpaths 4/3) - two visually distinct shapes
        # in the artwork, not one bar sharing both meanings.
        'thumb1': (390, 262),   # left, front/forward (subpath 8)
        'thumb2': (395, 306),   # left, back (subpath 6 - between the thumb
                                 # bar and the larger lower panel, not the
                                 # panel itself)
        'thumb3': (586, 262),   # right, front/forward (subpath 9)
        'thumb4': (580, 307),   # right, back (subpath 7, same as above)
    }

    # Parsed once at class-definition time, not per-frame - _draw() runs
    # on every pointer move over the diagram.
    _ICON_SEGMENTS = _parse_svg_path(_ICON_PATH_D)
    _ICON_BBOX = _svg_path_bbox(_ICON_SEGMENTS)

    def __init__(self, device: PulsarDevice | None = None, **kwargs):
        super().__init__(**kwargs, title='Input Test', default_width=380, default_height=520)
        self.set_modal(True)
        self._device = device
        self._buttons = device.capabilities.buttons if device else {}

        self._left_handed = False
        try:
            import subprocess
            val = subprocess.check_output(
                ['gsettings', 'get', 'org.gnome.desktop.peripherals.mouse', 'left-handed'],
                text=True).strip()
            self._left_handed = val == 'true'
        except Exception:
            pass

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(vbox)

        header = Adw.HeaderBar()
        vbox.append(header)

        hint = 'Left-handed mode detected — buttons un-swapped for display' \
               if self._left_handed else 'Click any mouse button in the area below'
        label = Gtk.Label(label=hint)
        label.set_margin_top(8)
        label.set_margin_bottom(4)
        vbox.append(label)

        self._active_btn = 0
        self._drawing = Gtk.DrawingArea()
        self._drawing.set_content_width(300)
        self._drawing.set_content_height(280)
        self._drawing.set_halign(Gtk.Align.CENTER)
        self._drawing.set_draw_func(self._draw)
        vbox.append(self._drawing)

        click = Gtk.GestureClick.new()
        click.set_button(0)
        click.connect('pressed', self._on_press)
        click.connect('released', self._on_release)
        self._drawing.add_controller(click)

        scroll_ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL |
            Gtk.EventControllerScrollFlags.HORIZONTAL)
        scroll_ctrl.connect('scroll', self._on_scroll)
        self._drawing.add_controller(scroll_ctrl)

        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_monospace(True)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_buf = self._log_view.get_buffer()

        scroll_win = Gtk.ScrolledWindow()
        scroll_win.set_vexpand(True)
        scroll_win.set_margin_start(12)
        scroll_win.set_margin_end(12)
        scroll_win.set_margin_top(8)
        scroll_win.set_margin_bottom(12)
        scroll_win.set_child(self._log_view)
        vbox.append(scroll_win)

        self._dpi_hide_seq = 0
        threading.Thread(target=self._dpi_listener, daemon=True).start()

    def _log(self, msg):
        end = self._log_buf.get_end_iter()
        self._log_buf.insert(end, msg + '\n')
        end = self._log_buf.get_end_iter()
        self._log_view.scroll_to_iter(end, 0, False, 0, 0)

    def _physical_btn(self, gtk_btn):
        if self._left_handed and gtk_btn in (1, 3):
            return 4 - gtk_btn
        return gtk_btn

    def _on_press(self, gesture, _n, x, y):
        gtk_btn = gesture.get_current_button()
        btn = self._physical_btn(gtk_btn)
        name = self._GTK_BTN_NAMES.get(btn, f'Button {btn}')
        self._active_btn = btn
        self._drawing.queue_draw()
        self._log(f'Press:   {name} (button {btn})')

    def _on_release(self, gesture, _n, x, y):
        gtk_btn = gesture.get_current_button()
        btn = self._physical_btn(gtk_btn)
        name = self._GTK_BTN_NAMES.get(btn, f'Button {btn}')
        self._active_btn = 0
        self._drawing.queue_draw()
        self._log(f'Release: {name} (button {btn})')

    def _on_scroll(self, ctrl, dx, dy):
        if dy < 0:
            self._active_btn = 'scroll_up'
            self._log('Scroll:  Up')
        elif dy > 0:
            self._active_btn = 'scroll_down'
            self._log('Scroll:  Down')
        if dx < 0:
            self._log('Scroll:  Left')
        elif dx > 0:
            self._log('Scroll:  Right')
        self._drawing.queue_draw()
        self._scroll_hide_seq = getattr(self, '_scroll_hide_seq', 0) + 1
        seq = self._scroll_hide_seq
        GLib.timeout_add(300, self._clear_scroll, seq)
        return True

    def _clear_scroll(self, seq):
        if seq == self._scroll_hide_seq:
            self._active_btn = 0
            self._drawing.queue_draw()
        return False

    def _dpi_listener(self):
        device = self._device
        if device is None:
            return
        path = device.find_hidraw()
        if not path:
            return
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            while True:
                data = os.read(fd, 256)
                if not data:
                    break
                event = device.parse_hidraw_event(data)
                if event and 'dpi' in event:
                    GLib.idle_add(self._on_dpi_event, event['dpi'], event['stage'])
        except OSError:
            pass
        finally:
            os.close(fd)

    def _on_dpi_event(self, dpi, stage):
        self._active_btn = 'dpi'
        self._drawing.queue_draw()
        self._log(f'DPI:     Stage {stage} → {dpi} DPI')
        self._dpi_hide_seq += 1
        seq = self._dpi_hide_seq
        GLib.timeout_add(500, self._clear_dpi, seq)

    def _clear_dpi(self, seq):
        if seq == self._dpi_hide_seq:
            self._active_btn = 0
            self._drawing.queue_draw()
        return False

    def _draw(self, area, cr, w, h):
        import math

        cr.set_source_rgb(0.15, 0.15, 0.17)
        cr.paint()

        mx, my, mw, mh = w * 0.15, h * 0.02, w * 0.7, h * 0.92
        bx0, by0, bx1, by1 = self._ICON_BBOX
        sx = mw / (bx1 - bx0)
        sy = mh / (by1 - by0)
        tx = mx - bx0 * sx
        ty = my - by0 * sy

        # Which zone(s) should glow. Most active states (including
        # 'scroll_up'/'scroll_down', which have their own positions above
        # and below the wheel rather than sharing its dot) map to exactly
        # one; GTK's back(8)/forward(9) can map to two (a 4-thumb-button
        # device like the X2A has a front+back pair on each side, and the
        # OS can't say which side was pressed - see _ICON_ZONES' comment)
        # or one (the FO1's thumb1/thumb2, both on the left).
        active = self._active_btn
        if active == 9:
            positions = [self._ICON_ZONES[n] for n in ('thumb1', 'thumb3')
                        if n in self._buttons]
        elif active == 8:
            positions = [self._ICON_ZONES[n] for n in ('thumb2', 'thumb4')
                        if n in self._buttons]
        elif active in self._ICON_ZONES:
            positions = [self._ICON_ZONES[active]]
        else:
            positions = []

        cr.set_source_rgb(0.55, 0.55, 0.58)
        _draw_svg_path(cr, self._ICON_SEGMENTS, tx, ty, sx, sy)
        cr.fill()

        # Glow drawn on top of the icon, not behind it - the left/right
        # click zones sit under solid opaque paddle shapes in the traced
        # artwork, so a glow drawn first would be completely covered
        # there (only visible over thin/outline areas like the wheel or
        # thumb bars). Translucent so the linework still shows through.
        r = 15 * min(sx, sy)
        for zx, zy in positions:
            gcx, gcy = zx * sx + tx, zy * sy + ty
            cr.set_source_rgba(0.3, 0.7, 1.0, 0.65)
            cr.arc(gcx, gcy, r, 0, 2 * math.pi)
            cr.fill()

            # Scroll direction arrow, drawn on top of the wheel's glow -
            # a plain wheel click and a scroll are otherwise the same dot.
            if active == 'scroll_up':
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(gcx, gcy - r * 0.5)
                cr.line_to(gcx - r * 0.45, gcy + r * 0.3)
                cr.line_to(gcx + r * 0.45, gcy + r * 0.3)
                cr.close_path()
                cr.fill()
            elif active == 'scroll_down':
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(gcx, gcy + r * 0.5)
                cr.line_to(gcx - r * 0.45, gcy - r * 0.3)
                cr.line_to(gcx + r * 0.45, gcy - r * 0.3)
                cr.close_path()
                cr.fill()


def main():
    app = PulsarMouseApp()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
