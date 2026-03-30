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
from gi.repository import Gtk, Adw, GLib, Gio, Dbusmenu

from pulsar_mouse import find_device, scan_devices, __version__
from pulsar_mouse.base import PulsarDevice, DeviceCapabilities
from pulsar_mouse.drivers import discover_all
from pulsar_mouse.hid import describe_button

APP_ID = 'io.github.packerlschupfer.PulsarMouse'

# Serialises all USB open/close operations so the tray and window don't collide.
_USB_LOCK = threading.Lock()

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
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg type="s"/></signal>
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
        return False

    def _read_initial_state(self):
        device = self._device

        def _read():
            if device is None:
                return
            try:
                with _USB_LOCK:
                    device.open()
                    hz = device.get_polling_rate()
                    dpi_info = device.get_dpi_stages(profile=1)
                    device.close()
                dpi = dpi_info['stages'][dpi_info['active'] - 1][0]
                GLib.idle_add(self._update_tray_label, dpi, hz, True)
            except Exception:
                pass
        threading.Thread(target=_read, daemon=True).start()

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
        try:
            while True:
                data = os.read(fd, 256)
                if not data:
                    break
                event = device.parse_hidraw_event(data)
                if event:
                    GLib.idle_add(self._update_tray_label, event['dpi'], None)
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

    def _build_ui(self):
        caps = self._caps
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # ── Header bar ───────────────────────────────────────────────────
        header = Adw.HeaderBar()

        # Profile selector (hidden if only 1 profile)
        if caps and caps.num_profiles > 1:
            profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            profile_box.append(Gtk.Label(label='Profile:'))
            self._profile_combo = Gtk.DropDown.new_from_strings(
                [f'Profile {i}' for i in range(1, caps.num_profiles + 1)]
            )
            self._profile_combo.connect('notify::selected', self._on_profile_changed)
            profile_box.append(self._profile_combo)
            header.set_title_widget(profile_box)
        else:
            self._profile_combo = None

        reload_btn = Gtk.Button(icon_name='view-refresh-symbolic')
        reload_btn.set_tooltip_text('Reload from mouse')
        reload_btn.connect('clicked', lambda _: self._reload())
        header.pack_start(reload_btn)

        apply_btn = Gtk.Button(label='Apply')
        apply_btn.add_css_class('suggested-action')
        apply_btn.connect('clicked', lambda _: self._apply())
        header.pack_end(apply_btn)

        toolbar_view.add_top_bar(header)

        # ── Toast overlay ────────────────────────────────────────────────
        toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(toast_overlay)
        self._toast_overlay = toast_overlay

        # ── Scrollable content ───────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        toast_overlay.set_child(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)
        scroll.set_child(clamp)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(page_box)

        # Error banner
        self._banner = Adw.Banner()
        self._banner.set_revealed(False)
        page_box.append(self._banner)

        if not caps:
            self._banner.set_title('No supported mouse found')
            self._banner.set_revealed(True)
            return

        # ── Global settings ──────────────────────────────────────────────
        global_group = Adw.PreferencesGroup()
        global_group.set_title('Global Settings')
        global_group.set_description('Applies to all profiles')
        page_box.append(global_group)

        # Polling rate
        self._poll_row = Adw.ComboRow()
        self._poll_row.set_title('Polling Rate')
        self._poll_row.set_subtitle('Hz')
        self._poll_row.set_model(
            Gtk.StringList.new([f'{hz} Hz' for hz in caps.polling_rates])
        )
        global_group.add(self._poll_row)

        # Debounce
        self._debounce_row = None
        if caps.has_debounce:
            lo, hi = caps.debounce_range
            self._debounce_row = Adw.SpinRow.new_with_range(lo, hi, 1)
            self._debounce_row.set_title('Debounce')
            self._debounce_row.set_subtitle(f'milliseconds ({lo} – {hi})')
            global_group.add(self._debounce_row)

        # Angle snap
        self._angle_row = None
        if caps.has_angle_snap:
            self._angle_row = Adw.SwitchRow()
            self._angle_row.set_title('Angle Snap')
            self._angle_row.set_subtitle('Straightens cursor movement to horizontal/vertical lines')
            global_group.add(self._angle_row)

        # Ripple control
        self._ripple_row = None
        if caps.has_ripple_control:
            self._ripple_row = Adw.SwitchRow()
            self._ripple_row.set_title('Ripple Control')
            self._ripple_row.set_subtitle('Smooths out sensor jitter at low speeds')
            global_group.add(self._ripple_row)

        # Motion sync
        self._motion_row = None
        if caps.has_motion_sync:
            self._motion_row = Adw.SwitchRow()
            self._motion_row.set_title('Motion Sync')
            self._motion_row.set_subtitle('Synchronises sensor data with USB polling interval')
            global_group.add(self._motion_row)

        # ── Per-profile settings ─────────────────────────────────────────
        profile_group = Adw.PreferencesGroup()
        profile_group.set_title('Profile Settings')
        page_box.append(profile_group)

        # LOD
        self._lod_row = None
        if caps.lod_values:
            self._lod_row = Adw.ComboRow()
            self._lod_row.set_title('Lift-off Distance')
            self._lod_row.set_subtitle('Height at which tracking stops when lifting the mouse')
            self._lod_row.set_model(
                Gtk.StringList.new([f'{v} mm' for v in caps.lod_values])
            )
            profile_group.add(self._lod_row)

        # LED brightness
        self._bright_row = None
        if caps.has_led:
            lo, hi = caps.brightness_range
            self._bright_row = Adw.SpinRow.new_with_range(lo, hi, 5)
            self._bright_row.set_title('LED Brightness')
            self._bright_row.set_subtitle(f'{lo} – {hi}')
            profile_group.add(self._bright_row)

        # LED effect
        self._led_row = None
        if caps.has_led and caps.led_effects:
            self._led_row = Adw.ComboRow()
            self._led_row.set_title('LED Effect')
            self._led_row.set_model(
                Gtk.StringList.new([e.capitalize() for e in caps.led_effects])
            )
            self._led_row.connect('notify::selected', self._on_led_changed)
            profile_group.add(self._led_row)

        # Breath speed
        self._breath_row = None
        if caps.has_led and caps.has_breath_speed:
            lo, hi = caps.breath_speed_range
            self._breath_row = Adw.SpinRow.new_with_range(lo, hi, 1)
            self._breath_row.set_title('Breath Speed')
            self._breath_row.set_subtitle(f'{lo} – {hi}')
            self._breath_row.set_visible(False)
            profile_group.add(self._breath_row)

        # ── DPI stages ───────────────────────────────────────────────────
        dpi_group = Adw.PreferencesGroup()
        dpi_group.set_title('DPI Stages')
        page_box.append(dpi_group)

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
        for i in range(1, caps.max_dpi_stages + 1):
            row = Adw.SpinRow.new_with_range(caps.dpi_min, caps.dpi_max, caps.dpi_step)
            row.set_title(f'Stage {i}')
            dpi_group.add(row)
            self._dpi_rows.append(row)

        # ── Button bindings (read-only display) ──────────────────────────
        btn_group = Adw.PreferencesGroup()
        btn_group.set_title('Button Bindings')
        btn_group.set_description('Use the CLI (--button) to remap buttons')
        page_box.append(btn_group)

        self._btn_rows = {}
        for btn_name, btn_id in caps.buttons.items():
            row = Adw.ActionRow()
            label = caps.button_labels.get(btn_name, btn_name.capitalize())
            row.set_title(label)
            row.set_subtitle('–')
            btn_group.add(row)
            self._btn_rows[btn_id] = row

        # ── Reset ────────────────────────────────────────────────────────
        if caps.has_reset:
            reset_group = Adw.PreferencesGroup()
            page_box.append(reset_group)

            reset_row = Adw.ButtonRow()
            reset_row.set_title('Reset to Factory Defaults')
            reset_row.add_css_class('destructive-action')
            reset_row.connect('activated', self._on_reset_clicked)
            reset_group.add(reset_row)

        # ── Test Input ───────────────────────────────────────────────────
        test_group = Adw.PreferencesGroup()
        page_box.append(test_group)

        test_row = Adw.ButtonRow()
        test_row.set_title('Test Input — click to test mouse buttons')
        test_row.connect('activated', self._on_test_clicked)
        test_group.add(test_row)

        # ── OS / Desktop Settings ────────────────────────────────────────
        os_group = Adw.PreferencesGroup()
        os_group.set_title('Desktop Mouse Settings')
        os_group.set_description('System-wide GNOME settings (affects all mice)')
        page_box.append(os_group)

        self._accel_row = Adw.ComboRow()
        self._accel_row.set_title('Acceleration Profile')
        self._accel_row.set_subtitle('Use "Flat" for raw input (recommended for gaming)')
        self._accel_row.set_model(Gtk.StringList.new(['Flat (raw)', 'Adaptive (default)']))
        self._accel_row.connect('notify::selected', self._on_accel_changed)
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

        self._load_os_settings()

    def _load_os_settings(self):
        try:
            import subprocess
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

    def _on_accel_changed(self, combo, _param):
        pass  # applied via button

    def _on_os_apply(self, _row):
        accel = 'flat' if self._accel_row.get_selected() == 0 else 'adaptive'
        speed = self._speed_row.get_value()
        import subprocess
        subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.mouse',
                        'accel-profile', accel])
        subprocess.run(['gsettings', 'set', 'org.gnome.desktop.peripherals.mouse',
                        'speed', str(speed)])

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
        if caps and self._breath_row:
            effects = caps.led_effects
            selected = effects[combo.get_selected()] if combo.get_selected() < len(effects) else ''
            self._breath_row.set_visible(selected == 'breath')

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
        if self._debounce_row:
            s['debounce'] = int(self._debounce_row.get_value())
        if self._angle_row:
            s['angle'] = self._angle_row.get_active()
        if self._ripple_row:
            s['ripple'] = self._ripple_row.get_active()
        if self._motion_row:
            s['motion'] = self._motion_row.get_active()
        if self._lod_row:
            s['lod'] = caps.lod_values[self._lod_row.get_selected()]
        if self._bright_row:
            s['brightness'] = int(self._bright_row.get_value())
        if self._led_row:
            s['led'] = caps.led_effects[self._led_row.get_selected()]
        if self._breath_row:
            s['breath'] = int(self._breath_row.get_value())
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
        try:
            poll_hz = device.get_polling_rate()
            debounce = device.get_debounce() if caps.has_debounce else None
            angle = device.get_angle_snap() if caps.has_angle_snap else None
            ripple = device.get_ripple_control() if caps.has_ripple_control else None
            motion = device.get_motion_sync() if caps.has_motion_sync else None
            GLib.idle_add(self._populate_global, poll_hz, debounce, angle, ripple, motion)
        except Exception as e:
            GLib.idle_add(self._show_error, f'Read error (global): {e}')
        self._do_reload_profile_inner()
        self._close_dev()

    def _do_reload_profile(self):
        if not self._open_dev():
            return
        self._do_reload_profile_inner()
        self._close_dev()

    def _do_reload_profile_inner(self):
        caps = self._caps
        device = self._device
        p = self._profile
        try:
            lod = device.get_lod(p) if caps.lod_values else None
            brightness = device.get_brightness(p) if caps.has_led else None
            led = device.get_led_effect(p) if caps.has_led else None
            breath = device.get_breath_speed(p) if caps.has_led and caps.has_breath_speed else None
            dpi_info = device.get_dpi_stages(p)
            buttons = {bid: device.get_button(bid, p)
                       for bid in caps.buttons.values()}
            GLib.idle_add(self._populate_profile,
                          lod, brightness, led, breath, dpi_info, buttons)
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

            p = s['profile']
            if 'lod' in s:
                device.set_lod(s['lod'], p)
            if 'brightness' in s:
                device.set_brightness(s['brightness'], p)
            if 'led' in s:
                device.set_led_effect(s['led'], p)
                if s['led'] == 'breath' and 'breath' in s:
                    device.set_breath_speed(s['breath'], p)

            stages = s['dpi_values'][:s['num_stages']]
            device.set_dpi_stages(stages, s['active'], p)

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

    def _populate_global(self, poll_hz, debounce, angle, ripple, motion):
        caps = self._caps
        self._building = True
        # Find index of polling rate
        try:
            poll_idx = caps.polling_rates.index(poll_hz)
        except ValueError:
            poll_idx = len(caps.polling_rates) - 1
        self._poll_row.set_selected(poll_idx)
        if self._debounce_row and debounce is not None:
            self._debounce_row.set_value(debounce)
        if self._angle_row and angle is not None:
            self._angle_row.set_active(angle)
        if self._ripple_row and ripple is not None:
            self._ripple_row.set_active(ripple)
        if self._motion_row and motion is not None:
            self._motion_row.set_active(motion)
        self._building = False

    def _populate_profile(self, lod, brightness, led, breath, dpi_info, buttons):
        caps = self._caps
        self._building = True
        if self._lod_row and lod is not None:
            try:
                lod_idx = caps.lod_values.index(lod)
            except ValueError:
                lod_idx = 0
            self._lod_row.set_selected(lod_idx)
        if self._bright_row and brightness is not None:
            self._bright_row.set_value(brightness)
        if self._led_row and led is not None:
            try:
                led_idx = caps.led_effects.index(led)
            except ValueError:
                led_idx = 1
            self._led_row.set_selected(led_idx)
        if self._breath_row and breath is not None:
            self._breath_row.set_value(breath)
            if led:
                self._breath_row.set_visible(led == 'breath')

        stages = dpi_info['stages']
        num    = dpi_info['count']
        active = dpi_info['active']
        self._stage_count_row.set_value(num)
        self._active_stage_row.set_selected(max(0, active - 1))
        for i, row in enumerate(self._dpi_rows):
            row.set_value(stages[i][0] if i < len(stages) else 800)
            row.set_sensitive(i < num)

        for btn_id, (t, a1, a2) in buttons.items():
            if btn_id in self._btn_rows:
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
                if event:
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
