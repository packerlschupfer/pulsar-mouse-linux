--!nonstrict
-- Pulsar Mouse battery/charging status - [[widget]] (bar).
--
-- For anyone who only has the `pulsar-mouse` CLI installed, not the GUI/
-- tray - see desktop.luau's header for the read strategy (same here): reads
-- pulsar-mouse-gui's ~/.cache/pulsar-mouse/battery.json when it's fresh, or
-- falls back to a direct `pulsar-mouse --battery-json` call otherwise. A
-- CLI-only setup just always takes the fallback path, no special-casing
-- needed - the file will never exist, so every tick reads live from the
-- mouse instead.
--
-- Hides entirely (barWidget.setVisible(false)) for a wired mouse, which has
-- no battery to report - --battery-json returns {"wireless": false} for
-- those rather than an error, so this is a normal, not a failure, state.
--
-- Icon-only (no percentage label): battery level and charging state are
-- already in the hover tooltip, so a bar-row number would just be the same
-- fact twice. Just the Tabler "mouse-filled" glyph, tinted red by
-- statusColor() same as before, swapping to "mouse-off" for a genuine
-- error/no-mouse state.
--
-- Signal quality has no synchronous getter (see gui.py's own comment on
-- this) - it only arrives via an async hidraw event the GUI listens for, so
-- it's only ever available via the cached state file (when the GUI wrote
-- one after seeing an event, i.e. pulsar-mouse-gui/tray is running), never
-- from the CLI fallback read - the tooltip's signal line just doesn't
-- appear when only that path is available.
--
-- Click opens the "controls" panel (panel.luau) for quick DPI/polling rate
-- changes - that one always talks to the CLI directly, since neither of
-- those live in battery.json and writes can't be cached anyway.
--
--   barWidget.render(tree)      declarative UI tree
--   barWidget.isVertical()      branch on bar orientation
--   barWidget.setTooltip(text)  hover detail
--   barWidget.setVisible(bool)  hide entirely for a wired mouse
--   noctalia.readFile(path)     cheap path - reads the GUI's last reading
--   noctalia.runAsync(cmd, cb)  fallback path - direct CLI read
--   noctalia.json.decode(str)   both paths return JSON on stdout/in the file
--   noctalia.togglePanel(id)    open/close the controls panel on click

local STATE_PATH = "~/.cache/pulsar-mouse/battery.json"
local STALE_AFTER = 180 -- seconds - 3x the GUI's own 60s poll interval

local wireless = true -- assume true until a read says otherwise, so the
                       -- widget doesn't flash hidden then visible on load
local percent = nil
local charging = false
local signalPercent = nil
local errorText = nil
local checkingCli = false
local isVertical = false
local glyphSize = noctalia.getConfig("glyph_size")

local function statusGlyph(color)
  local name = (errorText ~= nil or percent == nil) and "mouse-off" or "mouse-filled"
  return ui.glyph({ name = name, size = glyphSize, color = color })
end

local function statusColor()
  if errorText ~= nil then
    return "error"
  end
  if percent ~= nil and percent <= 15 and not charging then
    return "error"
  end
  return "on_surface"
end

local function tooltipText()
  if errorText ~= nil then
    return errorText
  end
  if percent == nil then
    return noctalia.tr("ui.no-mouse")
  end
  local text = tostring(percent) .. "%"
  if charging then
    text = text .. " " .. noctalia.tr("ui.charging")
  end
  if signalPercent ~= nil then
    text = text .. "  ·  " .. noctalia.tr("ui.signal-label") .. " " .. tostring(signalPercent) .. "%"
  end
  return text
end

local function render()
  barWidget.setVisible(wireless)
  if not wireless then
    return
  end

  local glyph = statusGlyph(statusColor())
  barWidget.setTooltip(tooltipText())

  local container = isVertical and ui.column or ui.row
  barWidget.render(container({ align = "center" }, { glyph }))
end

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
    -- Not available via this path - see header comment.
    signalPercent = nil
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
  isVertical = barWidget.isVertical()
  noctalia.setUpdateInterval(30000)
  readState()
end

function onClick()
  noctalia.togglePanel("harveywuk/pulsar-mouse-battery:controls")
end
