# Pulsar Mouse Battery

Bar and desktop widgets showing battery percentage and charging status for a Pulsar gaming mouse (via [pulsar-mouse-linux](https://github.com/harveywuk/pulsar-mouse-linux)). Since desktop widgets are shared with Noctalia's lock screen, the desktop widget shows up in both places once added. Works with just the `pulsar-mouse` CLI installed - the GUI/tray isn't required, it's just more efficient if it's running (see Usage).

## Plugin

| Field | Value |
| --- | --- |
| ID | `harveywuk/pulsar-mouse-battery` |
| Entries | Bar widget: `bar`; desktop widget: `battery` |

## Usage

### Requirements

- [pulsar-mouse-linux](https://github.com/harveywuk/pulsar-mouse-linux) installed, with `pulsar-mouse` on `PATH`
- `pulsar-mouse-gui` running is optional but recommended (autostart) - see below

### Bar Widget

Add the `bar` bar widget to your bar. Shows a battery glyph (and percentage, on horizontal bars) with a tooltip for the full status; a hover glyph turns red under 15% uncharged. If `pulsar-mouse-gui` isn't installed or running at all, this widget still works, it just always reads a fresh value directly from the mouse instead of the GUI's cached one (see below).

### Desktop Widget

Add the `battery` desktop widget from Noctalia's desktop-widget editor (it's then also available for the lock-screen widget editor).

### Data source

Both widgets read the battery reading `pulsar-mouse-gui` already writes on its periodic poll (`~/.cache/pulsar-mouse/battery.json`) rather than polling the mouse themselves, so they don't double up on USB traffic. If that file is missing or older than 3 minutes - the GUI isn't installed, isn't running, or was just closed - each widget falls back to its own direct `pulsar-mouse --battery-json` call instead, so both still work standalone, just with their own USB round-trip each tick.

## Settings

### Desktop Widget

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `color` | `color` | `primary` | Accent color for the percentage text and progress bar. |
| `show_progress` | `bool` | `true` | Shows or hides the battery-level progress bar. |
