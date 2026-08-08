# Pulsar Mouse Battery

Bar and desktop widgets showing battery percentage, charging status, and signal strength for a Pulsar gaming mouse (via [pulsar-mouse-linux](https://github.com/harveywuk/pulsar-mouse-linux)), plus a quick-controls panel for DPI stage, polling rate, and LED (effect/brightness/speed). Since desktop widgets are shared with Noctalia's lock screen, the desktop widget shows up in both places once added. Works with just the `pulsar-mouse` CLI installed - the GUI/tray isn't required, it's just more efficient if it's running (see Data source).

Battery/signal only apply to wireless mice - on a wired one, the bar widget hides itself entirely and the desktop widget shows a plain "Wired" placeholder instead. The controls panel (DPI/polling/LED) works for any mouse.

## Plugin

| Field | Value |
| --- | --- |
| ID | `harveywuk/pulsar-mouse-battery` |
| Entries | Bar widget: `bar`; panel: `controls`; desktop widget: `battery` |

## Usage

### Requirements

- [pulsar-mouse-linux](https://github.com/harveywuk/pulsar-mouse-linux) installed, with `pulsar-mouse` on `PATH`
- `pulsar-mouse-gui` running is optional but recommended (autostart) - see Data source

### Updating

`noctalia msg plugins update <source>` only re-syncs files from a path/git source - it does not re-register a plugin's entries (bar widget, panel, etc). If an update adds or changes an entry, do a full disable/enable cycle afterward to pick it up:

```sh
noctalia msg plugins update pulsar-mouse   # or whatever you named the source
noctalia msg plugins disable harveywuk/pulsar-mouse-battery
noctalia msg plugins enable harveywuk/pulsar-mouse-battery
```

### Bar Widget

Add the `bar` bar widget to your bar. Shows a battery glyph (and percentage, on horizontal bars) with a tooltip for the full status, including signal strength when available; the glyph turns red under 15% uncharged. Hides itself entirely on a wired mouse (no battery to show). If `pulsar-mouse-gui` isn't installed or running at all, this widget still works, it just always reads a fresh value directly from the mouse instead of the GUI's cached one (see Data source).

### Controls Panel

Click the bar widget to open the `controls` panel, split into two tabs (Noctalia has no native tabs control - these are just two segmented buttons swapping which section renders):

**Sensor**
- **DPI** - a slider stepping through profile 1's configured DPI stages (not a raw DPI range - every position lands on an actual configured value, like a notch per stage)
- **Polling Rate** - a slider the same way, stepping through the device's supported rates

**Lighting**
- **LED Effect** - a dropdown (off/steady/pulse, or whatever the device supports)
- **Brightness** - a 0-100% slider
- **Speed** - a 0-100 slider for the LED effect's speed, shown only when the current effect actually has one (e.g. pulse/breath, not steady)

All of these write straight through the `pulsar-mouse` CLI immediately on release (sliders commit on drag-end, not live per pixel) - unlike the battery reading, none of this lives in the cached state file, so the panel always talks to the mouse directly.

You can also open the panel over IPC:

```sh
noctalia msg panel-toggle harveywuk/pulsar-mouse-battery:controls
```

### Desktop Widget

Add the `battery` desktop widget from Noctalia's desktop-widget editor (it's then also available for the lock-screen widget editor).

### Data source

The `bar` and `battery` entries read the battery/signal reading `pulsar-mouse-gui` already writes on its periodic poll (`~/.cache/pulsar-mouse/battery.json`) rather than polling the mouse themselves, so they don't double up on USB traffic. If that file is missing or older than 3 minutes - the GUI isn't installed, isn't running, or was just closed - each falls back to its own direct `pulsar-mouse --battery-json` call instead, so both still work standalone, just with their own USB round-trip each tick and no signal reading (signal strength has no synchronous getter - it only ever arrives via an async event the GUI listens for, so it's only available through the cached file).

## Settings

### Bar Widget

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `show_percent` | `bool` | `true` | Turn off to rely on just the glyph (horizontal bars normally also show the percentage next to it). |
| `glyph_size` | `int` | `16` | Glyph size, 10-32. |

### Desktop Widget

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `color` | `color` | `primary` | Accent color for the percentage text and progress bar. |
| `show_progress` | `bool` | `true` | Shows or hides the battery-level progress bar. |
| `show_percent` | `bool` | `true` | Turn off to rely on just the glyph and progress bar. |
| `glyph_size` | `int` | `28` | Glyph size, 16-64. |
