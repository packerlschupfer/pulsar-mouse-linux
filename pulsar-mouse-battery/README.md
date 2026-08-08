# Pulsar Mouse Battery

Bar and desktop widgets showing battery percentage and charging status for a Pulsar gaming mouse (via [pulsar-mouse-linux](https://github.com/harveywuk/pulsar-mouse-linux)), plus a quick-controls panel for DPI stage and polling rate. Since desktop widgets are shared with Noctalia's lock screen, the desktop widget shows up in both places once added. Works with just the `pulsar-mouse` CLI installed - the GUI/tray isn't required, it's just more efficient if it's running (see Usage).

## Plugin

| Field | Value |
| --- | --- |
| ID | `harveywuk/pulsar-mouse-battery` |
| Entries | Bar widget: `bar`; panel: `controls`; desktop widget: `battery` |

## Usage

### Requirements

- [pulsar-mouse-linux](https://github.com/harveywuk/pulsar-mouse-linux) installed, with `pulsar-mouse` on `PATH`
- `pulsar-mouse-gui` running is optional but recommended (autostart) - see below

### Bar Widget and Controls Panel

Add the `bar` bar widget to your bar. Shows a battery glyph (and percentage, on horizontal bars) with a tooltip for the full status; the glyph turns red under 15% uncharged. If `pulsar-mouse-gui` isn't installed or running at all, this widget still works, it just always reads a fresh value directly from the mouse instead of the GUI's cached one (see Data source below).

Click the bar widget to open the `controls` panel: buttons for each configured DPI stage on profile 1 (switches which one is active, doesn't change their values), and a dropdown for polling rate. Both write straight through the `pulsar-mouse` CLI immediately - unlike the battery reading, DPI/polling rate always need a live, authoritative read/write, so this panel doesn't use the cached state file at all.

You can also open the panel over IPC:

```sh
noctalia msg panel-toggle harveywuk/pulsar-mouse-battery:controls
```

### Desktop Widget

Add the `battery` desktop widget from Noctalia's desktop-widget editor (it's then also available for the lock-screen widget editor).

### Data source

The `bar` and `battery` entries read the battery reading `pulsar-mouse-gui` already writes on its periodic poll (`~/.cache/pulsar-mouse/battery.json`) rather than polling the mouse themselves, so they don't double up on USB traffic. If that file is missing or older than 3 minutes - the GUI isn't installed, isn't running, or was just closed - each falls back to its own direct `pulsar-mouse --battery-json` call instead, so both still work standalone, just with their own USB round-trip each tick.

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
