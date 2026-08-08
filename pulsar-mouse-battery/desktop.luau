--!nonstrict
-- Pulsar Mouse battery/charging status - [[desktop_widget]].
--
-- Data source: ~/.cache/pulsar-mouse/battery.json, written by
-- pulsar-mouse-gui's tray/Home page poll (every 60s while it's running) -
-- reading that costs nothing extra on the mouse's wireless link. If that
-- file is missing or older than STALE_AFTER (the GUI/tray isn't running),
-- falls back to a direct `pulsar-mouse --battery-json` read instead, so the
-- widget still works standalone, just with its own USB round-trip.
--
-- For a wired mouse (no battery to report), --battery-json returns
-- {"wireless": false} rather than an error - renders a minimal "Wired"
-- placeholder instead of battery info. Unlike bar.luau, there's no
-- desktopWidget.setVisible() to hide entirely, so this is the closest
-- available equivalent.
--
-- Signal quality has no synchronous getter (see gui.py's own comment on
-- this) - it only arrives via an async hidraw event the GUI listens for, so
-- it's only ever available via the cached state file, never the CLI
-- fallback - the signal row just doesn't appear when only that path is
-- available (e.g. GUI not running).
--
--   noctalia.readFile(path)     cheap path - reads the GUI's last reading
--   noctalia.runAsync(cmd, cb)  fallback path - direct CLI read
--   noctalia.json.decode(str)   both paths return JSON on stdout/in the file
--   desktopWidget.render(tree)  declarative UI tree

local STATE_PATH = "~/.cache/pulsar-mouse/battery.json"
local STALE_AFTER = 180 -- seconds - 3x the GUI's own 60s poll interval

local color = noctalia.getConfig("color")
local showProgress = noctalia.getConfig("show_progress")
local showPercent = noctalia.getConfig("show_percent")
local glyphSize = noctalia.getConfig("glyph_size")

local wireless = true -- assume true until a read says otherwise
local percent = nil
local charging = false
local signalPercent = nil
local errorText = nil
local checkingCli = false

local function statusGlyph()
  local name = (errorText ~= nil or percent == nil) and "mouse-off" or "mouse-filled"
  return ui.glyph({ name = name, size = glyphSize, color = statusColor() })
end

local function statusColor()
  if errorText ~= nil then
    return "error"
  end
  if percent ~= nil and percent <= 15 and not charging then
    return "error"
  end
  return color
end

local function render()
  if not wireless then
    desktopWidget.render(ui.column({ gap = 6, align = "center" }, {
      ui.glyph({ name = "plug", size = glyphSize, color = "outline" }),
      ui.label({ text = noctalia.tr("ui.wired"), fontSize = 12, color = "outline" }),
    }))
    return
  end

  local rows = {
    statusGlyph(),
  }

  if errorText ~= nil then
    -- Diagnostic, not decorative - stays visible even with show_percent off,
    -- since otherwise a broken setup would just silently show a bare glyph.
    table.insert(rows, ui.label({ text = errorText, fontSize = 12, color = "error" }))
  elseif showPercent and percent == nil then
    table.insert(rows, ui.label({ text = "--", fontSize = 20, color = "outline" }))
  elseif showPercent then
    local text = tostring(percent) .. "%"
    if charging then
      text = text .. " " .. noctalia.tr("ui.charging")
    end
    table.insert(rows, ui.label({ text = text, fontSize = 18, fontWeight = "bold", color = statusColor() }))
  end

  if showProgress and percent ~= nil then
    table.insert(rows, ui.progress({
      progress = percent / 100,
      fill = statusColor(),
      width = 120,
      height = 6,
      radius = 3,
    }))
  end

  if signalPercent ~= nil then
    table.insert(rows, ui.label({
      text = `{noctalia.tr("ui.signal-label")} {signalPercent}%`,
      fontSize = 11,
      color = "on_surface_variant",
    }))
  end

  desktopWidget.render(ui.column({ gap = 6, align = "center" }, rows))
end

-- Fallback: no fresh state file, so ask the driver directly. Only one of
-- these in flight at a time - update() ticks every 30s and a CLI round-trip
-- can occasionally take longer than that if the mouse's RF link is asleep.
local function readViaCli()
  if checkingCli then
    return
  end
  if not noctalia.commandExists("pulsar-mouse") then
    errorText = noctalia.tr("ui.not-installed")
    render()
    return
  end
  checkingCli = true
  noctalia.runAsync("pulsar-mouse --battery-json", function(result)
    checkingCli = false
    if type(result) ~= "table" or result.timedOut or result.exitCode ~= 0 then
      errorText = noctalia.tr("ui.no-mouse")
      render()
      return
    end
    local decoded = noctalia.json.decode(result.stdout or "")
    if type(decoded) ~= "table" then
      errorText = noctalia.tr("ui.no-mouse")
      render()
      return
    end
    if decoded.wireless == false then
      wireless = false
      errorText = nil
      render()
      return
    end
    if decoded.battery_percent == nil then
      errorText = noctalia.tr("ui.no-mouse")
      render()
      return
    end
    wireless = true
    errorText = nil
    percent = decoded.battery_percent
    charging = decoded.power_connected == true
    signalPercent = nil -- not available via this path - see header comment
    render()
  end, 10000)
end

local function readState()
  local contents = noctalia.readFile(STATE_PATH)
  if type(contents) ~= "string" or contents == "" then
    readViaCli()
    return
  end

  local decoded = noctalia.json.decode(contents)
  if type(decoded) ~= "table" or decoded.battery_percent == nil then
    readViaCli()
    return
  end

  local age = os.time() - (decoded.updated_at or 0)
  if age > STALE_AFTER then
    readViaCli()
    return
  end

  wireless = true
  errorText = nil
  percent = decoded.battery_percent
  charging = decoded.power_connected == true
  signalPercent = decoded.signal_percent
  render()
end

function update()
  noctalia.setUpdateInterval(30000)
  readState()
end

render()
