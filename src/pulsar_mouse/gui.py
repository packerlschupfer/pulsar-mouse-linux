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
from pulsar_mouse.hid import describe_button

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
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connect('activate', self._on_activate)
        self._sni = None
        self._poll_items = {}
        self._tray_refresh_busy = False
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
        wins = self.get_windows()
        if wins:
            wins[0].present()
            return
        device = self._find_or_create_device()
        win = MainWindow(application=app, device=device)
        win.present()
        if device:
            self._build_tray(win, device)
        self.hold()

    # ── System-tray ──────────────────────────────────────────────────────────

    def _build_tray(self, win: 'MainWindow', device: PulsarDevice):
        caps = device.capabilities
        sni = _StatusNotifierItem('pulsar-mouse', 'input-mouse', caps.name)
        sni.set_on_activate(win.present)
        self._sni = sni

        root = Dbusmenu.Menuitem.new()

        item_open = Dbusmenu.Menuitem.new()
        item_open.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Open Settings')
        item_open.connect('item-activated', lambda _i, _t: win.present())
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
        self._conn_item = None
        if hasattr(device, 'find_hidraw'):
            conn_item = Dbusmenu.Menuitem.new()
            conn_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Connection Quality: —')
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

        item_auto = Dbusmenu.Menuitem.new()
        item_auto.property_set(Dbusmenu.MENUITEM_PROP_LABEL, 'Start on Login')
        item_auto.property_set(Dbusmenu.MENUITEM_PROP_TOGGLE_TYPE,
                               Dbusmenu.MENUITEM_TOGGLE_CHECK)
        item_auto.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                   Dbusmenu.MENUITEM_TOGGLE_STATE_CHECKED
                                   if self._autostart_enabled() else
                                   Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED)
        item_auto.connect('item-activated', lambda _i, _t: self._toggle_autostart(_i))
        root.child_append(item_auto)

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
        # Battery drains slowly - a periodic background refresh is enough,
        # no need for anything event-driven like the hidraw DPI listener.
        GLib.timeout_add_seconds(600, self._refresh_battery)
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
                    finally:
                        device.close()
                GLib.idle_add(self._set_battery_label, pwr)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()
        return True  # repeat

    def _set_battery_label(self, pwr):
        pct = pwr['battery_percent']
        charging = ' (charging)' if pwr['power_connected'] else ''
        self._battery_text = f'Battery: {pct}%{charging}'
        if self._battery_item is not None:
            self._battery_item.property_set(Dbusmenu.MENUITEM_PROP_LABEL, self._battery_text)
        self._update_tray_tooltip()

    def _set_conn_quality_label(self, pct):
        self._conn_text = f'Connection Quality: {pct}%  —  {_connection_quality_label(pct)}'
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

    def _toggle_autostart(self, menu_item):
        if self._autostart_enabled():
            os.remove(self._AUTOSTART_PATH)
            menu_item.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                       Dbusmenu.MENUITEM_TOGGLE_STATE_UNCHECKED)
        else:
            os.makedirs(os.path.dirname(self._AUTOSTART_PATH), exist_ok=True)
            with open(self._AUTOSTART_PATH, 'w') as f:
                f.write(self._AUTOSTART_CONTENT)
            menu_item.property_set_int(Dbusmenu.MENUITEM_PROP_TOGGLE_STATE,
                                       Dbusmenu.MENUITEM_TOGGLE_STATE_CHECKED)

    def _set_dpi(self, dpi_val: int):
        device = self._device

        def _write():
            if device is None:
                return
            try:
                with _USB_LOCK:
                    device.open()
                    info = device.get_dpi_stages(profile=1)
                    for i, (dx, _dy) in enumerate(info['stages']):
                        if dx == dpi_val:
                            device.set_active_dpi_stage(i + 1, profile=1)
                            break
                    else:
                        stages = [dx for dx, _dy in info['stages']]
                        stages[info['active'] - 1] = dpi_val
                        device.set_dpi_stages(stages, info['active'], profile=1)
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
                    device.set_polling_rate(hz)
                    device.close()
                GLib.idle_add(self._update_tray_label, None, hz)
            except Exception:
                pass
        threading.Thread(target=_write, daemon=True).start()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, device: PulsarDevice | None = None, **kwargs):
        super().__init__(**kwargs)
        self._device = device
        self._caps = device.capabilities if device else None
        self.set_title(self._caps.name if self._caps else 'Pulsar Mouse')
        self.set_default_size(560, 740)
        self.set_icon_name('input-mouse')
        self.set_hide_on_close(True)
        self._profile = 1
        self._building = False

        self._build_ui()
        GLib.idle_add(self._reload)
        GLib.idle_add(self._start_home_updates)

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
        self._nav_pages = [
            ('home', 'Home', 'go-home-symbolic'),
            ('performance', 'Performance', 'preferences-system-symbolic'),
            ('customize', 'Customize', 'preferences-desktop-symbolic'),
            ('power', 'Power', 'battery-good-symbolic'),
        ]
        for _name, label, icon in self._nav_pages:
            row = Adw.ActionRow()
            row.set_title(label)
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            nav_list.append(row)
        nav_list.connect('row-selected', self._on_nav_row_selected)
        sidebar_toolbar.set_content(nav_list)
        sidebar_page = Adw.NavigationPage.new(sidebar_toolbar, caps.name)
        split_view.set_sidebar(sidebar_page)

        # ── Content: header bar (Apply/Reload/profile - shared across all
        # tabs, not per-tab, since Apply always writes everything pending
        # regardless of which tab is showing) + a ViewStack switched by
        # the sidebar ─────────────────────────────────────────────────
        content_toolbar = Adw.ToolbarView()
        content_header = Adw.HeaderBar()

        if caps.num_profiles > 1:
            profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            profile_box.append(Gtk.Label(label='Profile:'))
            self._profile_combo = Gtk.DropDown.new_from_strings(
                [f'Profile {i}' for i in range(1, caps.num_profiles + 1)]
            )
            self._profile_combo.connect('notify::selected', self._on_profile_changed)
            profile_box.append(self._profile_combo)
            content_header.set_title_widget(profile_box)
        else:
            self._profile_combo = None

        reload_btn = Gtk.Button(icon_name='view-refresh-symbolic')
        reload_btn.set_tooltip_text('Reload from mouse')
        reload_btn.connect('clicked', lambda _: self._reload())
        content_header.pack_start(reload_btn)

        apply_btn = Gtk.Button(label='Apply')
        apply_btn.add_css_class('suggested-action')
        apply_btn.connect('clicked', lambda _: self._apply())
        content_header.pack_end(apply_btn)

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
        self._view_stack.add_named(self._build_power_page(), 'power')

        content_page = Adw.NavigationPage.new(content_toolbar, 'Settings')
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

    def _build_home_page(self):
        """Status/landing tab: device name, connection, battery, and
        wireless signal quality - populated live by _start_home_updates(),
        called once from __init__ after the window itself exists.
        """
        caps = self._caps
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.START)
        box.set_margin_top(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        icon = Gtk.Image.new_from_icon_name('input-mouse-symbolic')
        icon.set_pixel_size(96)
        icon.set_halign(Gtk.Align.CENTER)
        box.append(icon)

        title = Gtk.Label(label=caps.name)
        title.add_css_class('title-1')
        title.set_margin_top(12)
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        status_group = Adw.PreferencesGroup()
        status_group.set_margin_top(24)
        box.append(status_group)

        conn_row = Adw.ActionRow()
        conn_row.set_title('Connection')
        conn_row.set_subtitle('Wireless')
        conn_row.add_prefix(Gtk.Image.new_from_icon_name('network-wireless-symbolic'))
        connected_label = Gtk.Label(label='Connected')
        connected_label.add_css_class('success')
        conn_row.add_suffix(connected_label)
        status_group.add(conn_row)

        # Polling rate, kept in sync elsewhere: _populate_global() (on
        # reload) and _apply() (immediately on Apply, before the write even
        # finishes) - not event-driven, since polling rate only ever
        # changes via this app, not something the device pushes on its own.
        self._home_mode_row = Adw.ActionRow()
        self._home_mode_row.set_title('Wireless Mode')
        self._home_mode_row.set_subtitle('—')
        self._home_mode_row.add_prefix(
            Gtk.Image.new_from_icon_name('network-wireless-symbolic'))
        status_group.add(self._home_mode_row)

        device = self._device
        if hasattr(device, 'get_power'):
            self._home_battery_row = Adw.ActionRow()
            self._home_battery_row.set_title('Battery')
            self._home_battery_row.set_subtitle('—')
            self._home_battery_row.add_prefix(
                Gtk.Image.new_from_icon_name('battery-good-symbolic'))
            status_group.add(self._home_battery_row)
        else:
            self._home_battery_row = None

        # Always shown even though we can't know in advance whether this
        # model actually pushes signal-quality events - it just stays
        # "—" harmlessly if nothing ever arrives (see
        # _home_hidraw_listener()).
        self._home_signal_row = Adw.ActionRow()
        self._home_signal_row.set_title('Connection Quality')
        self._home_signal_row.set_subtitle('—')
        self._home_signal_row.add_prefix(
            Gtk.Image.new_from_icon_name('network-wireless-signal-good-symbolic'))
        status_group.add(self._home_signal_row)

        return box

    def _build_performance_page(self):
        caps = self._caps

        global_group = Adw.PreferencesGroup()
        global_group.set_title('Global Settings')
        global_group.set_description('Applies to all profiles')

        self._poll_row = Adw.ComboRow()
        self._poll_row.set_title('Polling Rate')
        self._poll_row.set_subtitle('Hz')
        self._poll_row.set_model(
            Gtk.StringList.new([f'{hz} Hz' for hz in caps.polling_rates])
        )
        global_group.add(self._poll_row)

        self._debounce_row = None
        if caps.has_debounce:
            lo, hi = caps.debounce_range
            row, self._debounce_row = self._make_slider_row(
                'Debounce', f'milliseconds ({lo} – {hi})', lo, hi, 1,
                format_value=lambda v: f'{int(v)} ms')
            global_group.add(row)

        self._angle_row = None
        if caps.has_angle_snap:
            self._angle_row = Adw.SwitchRow()
            self._angle_row.set_title('Angle Snap')
            self._angle_row.set_subtitle('Straightens cursor movement to horizontal/vertical lines')
            global_group.add(self._angle_row)

        self._ripple_row = None
        if caps.has_ripple_control:
            self._ripple_row = Adw.SwitchRow()
            self._ripple_row.set_title('Ripple Control')
            self._ripple_row.set_subtitle('Smooths out sensor jitter at low speeds')
            global_group.add(self._ripple_row)

        self._motion_row = None
        if caps.has_motion_sync:
            self._motion_row = Adw.SwitchRow()
            self._motion_row.set_title('Motion Sync')
            self._motion_row.set_subtitle('Synchronises sensor data with USB polling interval')
            global_group.add(self._motion_row)

        # LOD lives here (not on the Customize tab with the rest of
        # "Profile Settings") to match Fusion's own Performance tab, which
        # groups Lift-off Distance with DPI/polling rate rather than LED.
        tracking_group = Adw.PreferencesGroup()
        tracking_group.set_title('Tracking')

        self._lod_row = None
        if caps.lod_values:
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
                color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
                color_btn.set_rgba(Gdk.RGBA(red=1.0, green=1.0, blue=1.0, alpha=1.0))
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
        return self._wrap_page(*groups)

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
                'LED Brightness', '0% – 100%', 0, 100, 5,
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

        # Breath speed - self._breath_row is the Gtk.Scale (get_value/
        # set_value, as elsewhere), self._breath_row_container is the
        # wrapping Adw.ActionRow (title + slider together) - the visibility
        # toggle below needs to hide/show the whole row, not just the
        # slider inside it.
        self._breath_row = None
        self._breath_row_container = None
        if caps.has_led and caps.has_breath_speed:
            lo, hi = caps.breath_speed_range
            self._breath_row_container, self._breath_row = self._make_slider_row(
                'Breath Speed', '', lo, hi, 1,
                marks=[(lo, 'Slow'), (hi, 'Fast')])
            self._breath_row_container.set_visible(False)
            led_group.add(self._breath_row_container)

        btn_group = Adw.PreferencesGroup()
        btn_group.set_title('Button Bindings')
        btn_group.set_description('Use the CLI (--button) to remap buttons')

        self._btn_rows = {}
        for btn_name, btn_id in caps.buttons.items():
            row = Adw.ActionRow()
            label = caps.button_labels.get(btn_name, btn_name.capitalize())
            row.set_title(label)
            row.set_subtitle('–')
            btn_group.add(row)
            self._btn_rows[btn_id] = row

        actions_group = Adw.PreferencesGroup()

        test_row = Adw.ButtonRow()
        test_row.set_title('Test Input — click to test mouse buttons')
        test_row.connect('activated', self._on_test_clicked)
        actions_group.add(test_row)

        if caps.has_reset:
            reset_row = Adw.ButtonRow()
            reset_row.set_title('Reset to Factory Defaults')
            reset_row.add_css_class('destructive-action')
            reset_row.connect('activated', self._on_reset_clicked)
            actions_group.add(reset_row)

        groups = []
        if caps.has_led:
            groups.append(led_group)
        groups.append(btn_group)
        groups.append(actions_group)
        return self._wrap_page(*groups)

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
                    30, 900, 30,
                    format_value=lambda v: f'{int(v) // 60}:{int(v) % 60:02d}')
                power_group.add(row)

            if hasattr(device, 'set_low_power_threshold'):
                row, self._low_power_row = self._make_slider_row(
                    'Low Power Mode',
                    'Battery percentage that triggers low power mode (0 – 100)',
                    0, 100, 5)
                power_group.add(row)

            groups.append(power_group)

        return self._wrap_page(*groups)

    # ── Home tab live updates ───────────────────────────────────────────

    def _start_home_updates(self):
        """Independent of the tray's own polling in PulsarMouseApp (see
        _read_initial_state()/_hidraw_listener() there) - deliberately not
        shared, so this window stays self-contained regardless of whether
        a tray exists. hidraw supports multiple concurrent readers, so a
        second listener on the same device is safe, just a bit redundant.
        """
        device = self._device
        if device is None:
            return
        self._refresh_home_battery()
        threading.Thread(target=self._home_hidraw_listener, daemon=True).start()
        GLib.timeout_add_seconds(600, self._periodic_home_battery_refresh)

    def _periodic_home_battery_refresh(self):
        self._refresh_home_battery()
        return True

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
        charging = '  (charging)' if pwr['power_connected'] else ''
        self._home_battery_row.set_subtitle(f'{pct}%{charging}')

    def _home_hidraw_listener(self):
        device = self._device
        if device is None or not hasattr(device, 'find_hidraw'):
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
            while True:
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
        self._home_signal_row.set_subtitle(f'{pct}%  —  {_connection_quality_label(pct)}')

    def _make_slider_row(self, title, subtitle, lo, hi, step,
                         format_value=None, marks=None):
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
        replacement for it.
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

    def _on_profile_changed(self, combo, _param):
        if self._building:
            return
        self._profile = combo.get_selected() + 1
        GLib.idle_add(self._reload_profile)

    def _on_led_changed(self, combo, _param):
        if self._building:
            return
        caps = self._caps
        if caps and self._breath_row_container:
            effects = caps.led_effects
            selected = effects[combo.get_selected()] if combo.get_selected() < len(effects) else ''
            # The pulsing/breathing effect is always the last entry in
            # led_effects, by convention of every driver in this codebase
            # (['off', 'steady', <that one>]) - checked this way rather
            # than a hardcoded name since drivers differ on what to call
            # it (feinmann8k.py: 'pulse', base.py's shared default and
            # other drivers: 'breath').
            self._breath_row_container.set_visible(selected == effects[-1])

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
            s['lod'] = caps.lod_values[self._lod_row.get_selected()]
        if self._bright_row:
            lo, hi = caps.brightness_range
            pct = self._bright_row.get_value()
            s['brightness'] = round(lo + pct / 100 * (hi - lo))
        if self._led_row:
            s['led'] = caps.led_effects[self._led_row.get_selected()]
        if self._breath_row:
            s['breath'] = int(self._breath_row.get_value())
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

    def _do_reload(self):
        if not self._open_dev():
            return
        caps = self._caps
        device = self._device
        # Each field guarded independently via _read_field (same reasoning
        # as _do_reload_profile_inner() below) - a reload is 20+ individual
        # USB reads in quick succession, and a transient timeout on any one
        # of them (mouse's RF link briefly asleep) shouldn't blank out
        # every other already-successful field, including these.
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
        self._do_reload_profile_inner()
        self._close_dev()

    def _do_reload_profile(self):
        if not self._open_dev():
            return
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
        except (NotImplementedError, OSError):
            return None

    def _do_reload_profile_inner(self):
        caps = self._caps
        device = self._device
        p = self._profile
        try:
            lod = self._read_field(device.get_lod, p) if caps.lod_values else None
            brightness = self._read_field(device.get_brightness, p) if caps.has_led else None
            led = self._read_field(device.get_led_effect, p) if caps.has_led else None
            breath = (self._read_field(device.get_breath_speed, p)
                      if caps.has_led and caps.has_breath_speed else None)
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
                          lod, brightness, led, breath, dpi_info, colors, buttons)
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
                # See _on_led_changed()'s comment - the pulsing effect's
                # name varies per driver, so check by position not string.
                if s['led'] == caps.led_effects[-1] and 'breath' in s:
                    device.set_breath_speed(s['breath'], p)

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

    def _populate_profile(self, lod, brightness, led, breath, dpi_info, colors, buttons):
        caps = self._caps
        self._building = True
        if self._lod_row and lod is not None:
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
        if self._breath_row and breath is not None:
            self._breath_row.set_value(breath)
            if led and self._breath_row_container:
                self._breath_row_container.set_visible(led == caps.led_effects[-1])

        stages = dpi_info['stages']
        num    = dpi_info['count']
        active = dpi_info['active']
        self._stage_count_row.set_value(num)
        self._active_stage_row.set_selected(max(0, active - 1))
        for i, row in enumerate(self._dpi_rows):
            row.set_value(stages[i][0] if i < len(stages) else 800)
            row.set_sensitive(i < num)

        if self._color_buttons and colors:
            for i, btn in enumerate(self._color_buttons):
                rgb = colors[i] if i < len(colors) else None
                if rgb is not None:
                    r, g, b = rgb
                    btn.set_rgba(Gdk.RGBA(red=r / 255, green=g / 255, blue=b / 255, alpha=1.0))
                btn.set_sensitive(i < num)

        for btn_id, bind in buttons.items():
            if bind is not None and btn_id in self._btn_rows:
                t, a1, a2 = bind
                self._btn_rows[btn_id].set_subtitle(describe_button(t, a1, a2))
        self._building = False

    def _show_error(self, msg: str):
        self._banner.set_title(msg)
        self._banner.set_revealed(True)

    def _show_toast(self, msg: str):
        toast = Adw.Toast.new(msg)
        toast.set_timeout(3)
        self._toast_overlay.add_toast(toast)


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

    def __init__(self, device: PulsarDevice | None = None, **kwargs):
        super().__init__(**kwargs, title='Input Test', default_width=380, default_height=520)
        self.set_modal(True)
        self._device = device

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
        r = mw * 0.35
        cr.set_source_rgb(0.30, 0.30, 0.33)
        cr.new_path()
        cr.arc(mx + r, my + r, r, math.pi, 1.5 * math.pi)
        cr.arc(mx + mw - r, my + r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(mx + mw - r, my + mh - r, r, 0, 0.5 * math.pi)
        cr.arc(mx + r, my + mh - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()
        cr.fill()

        cr.set_source_rgb(0.20, 0.20, 0.22)
        cr.set_line_width(2)
        cr.move_to(w * 0.5, my)
        cr.line_to(w * 0.5, my + mh * 0.40)
        cr.stroke()

        ww, wh = mw * 0.12, mh * 0.12
        wx = w * 0.5 - ww / 2
        wy = my + mh * 0.08
        is_scroll = isinstance(self._active_btn, str) and self._active_btn.startswith('scroll')
        if is_scroll or self._active_btn == 2:
            cr.set_source_rgb(0.3, 0.7, 1.0)
        else:
            cr.set_source_rgb(0.45, 0.45, 0.50)
        self._rounded_rect(cr, wx, wy, ww, wh, ww * 0.3)
        cr.fill()

        if self._active_btn == 'scroll_up':
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(wx + ww / 2, wy + 3)
            cr.line_to(wx + ww / 2 - 4, wy + wh / 2)
            cr.line_to(wx + ww / 2 + 4, wy + wh / 2)
            cr.close_path()
            cr.fill()
        elif self._active_btn == 'scroll_down':
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(wx + ww / 2, wy + wh - 3)
            cr.line_to(wx + ww / 2 - 4, wy + wh / 2)
            cr.line_to(wx + ww / 2 + 4, wy + wh / 2)
            cr.close_path()
            cr.fill()

        thumb_buttons = [
            ('left',  0.38, 'FWD',  9),
            ('left',  0.52, 'BACK', 8),
            ('right', 0.38, 'FWD',  9),
            ('right', 0.52, 'BACK', 8),
        ]
        for side, ty, label, btn_id in thumb_buttons:
            if side == 'left':
                bx = mx - mw * 0.08
            else:
                bx = mx + mw * 0.90
            by = my + mh * ty
            bw = mw * 0.18
            bh = mh * 0.10
            if self._active_btn == btn_id:
                cr.set_source_rgb(0.3, 0.7, 1.0)
            else:
                cr.set_source_rgb(0.40, 0.40, 0.44)
            self._rounded_rect(cr, bx, by, bw, bh, 4)
            cr.fill()
            cr.set_source_rgb(0.9, 0.9, 0.9)
            cr.set_font_size(9)
            ext = cr.text_extents(label)
            cr.move_to(bx + bw / 2 - ext.width / 2, by + bh / 2 + ext.height / 2)
            cr.show_text(label)

        zones_on_body = {
            1: (mx, my, mw * 0.49, mh * 0.38),
            3: (mx + mw * 0.51, my, mw * 0.49, mh * 0.38),
        }
        if self._active_btn in zones_on_body:
            zx, zy, zw, zh = zones_on_body[self._active_btn]
            cr.set_source_rgba(0.3, 0.7, 1.0, 0.35)
            self._rounded_rect(cr, zx, zy, zw, zh, r if self._active_btn != 3 else 6)
            cr.fill()

        cr.set_source_rgb(0.85, 0.85, 0.85)
        cr.set_font_size(12)
        for label, lx_frac, ly_frac in [('L', 0.38, 0.22), ('R', 0.60, 0.22)]:
            ext = cr.text_extents(label)
            cr.move_to(w * lx_frac - ext.width / 2, my + mh * ly_frac)
            cr.show_text(label)

        dx, dy = w * 0.5, my + mh * 0.28
        dr = 7 if self._active_btn == 'dpi' else 5
        if self._active_btn == 'dpi':
            cr.set_source_rgb(0.3, 0.7, 1.0)
        else:
            cr.set_source_rgb(0.50, 0.50, 0.55)
        cr.arc(dx, dy, dr, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(0.9, 0.9, 0.9) if self._active_btn == 'dpi' else cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.set_font_size(8)
        ext = cr.text_extents('DPI')
        cr.move_to(dx - ext.width / 2, dy + dr + ext.height + 2)
        cr.show_text('DPI')

    @staticmethod
    def _rounded_rect(cr, x, y, w, h, r):
        import math
        r = min(r, w / 2, h / 2)
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
        cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()


def main():
    app = PulsarMouseApp()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
