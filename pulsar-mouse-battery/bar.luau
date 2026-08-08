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
--   barWidget.render(tree)      declarative UI tree
--   barWidget.isVertical()      branch on bar orientation
--   barWidget.setTooltip(text)  hover detail
--   noctalia.readFile(path)     cheap path - reads the GUI's last reading
--   noctalia.runAsync(cmd, cb)  fallback path - direct CLI read
--   noctalia.json.decode(str)   both paths return JSON on stdout/in the file

local STATE_PATH = "~/.cache/pulsar-mouse/battery.json"
local STALE_AFTER = 180 -- seconds - 3x the GUI's own 60s poll interval

local percent = nil
local charging = false
local errorText = nil
local checkingCli = false
local isVertical = false

local function batteryGlyph()
  if charging then
    return "battery-charging"
  end
  if percent == nil then
    return "battery-off"
  end
  if percent >= 75 then
    return "battery-4"
  elseif percent >= 50 then
    return "battery-3"
  elseif percent >= 25 then
    return "battery-2"
  end
  return "battery-1"
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
  if charging then
    return tostring(percent) .. "% " .. noctalia.tr("ui.charging")
  end
  return tostring(percent) .. "%"
end

local function render()
  local color = statusColor()
  local glyph = ui.glyph({ name = batteryGlyph(), color = color })

  barWidget.setTooltip(tooltipText())

  if isVertical or percent == nil then
    barWidget.render(ui.column({ align = "center" }, { glyph }))
    return
  end

  local text = tostring(percent) .. "%"
  barWidget.render(ui.row({ gap = 6, align = "center" }, {
    glyph,
    ui.label({ text = text, fontWeight = "bold", color = color }),
  }))
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
    if type(decoded) ~= "table" or decoded.battery_percent == nil then
      errorText = noctalia.tr("ui.no-mouse")
      render()
      return
    end
    errorText = nil
    percent = decoded.battery_percent
    charging = decoded.power_connected == true
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

  errorText = nil
  percent = decoded.battery_percent
  charging = decoded.power_connected == true
  render()
end

function update()
  isVertical = barWidget.isVertical()
  noctalia.setUpdateInterval(30000)
  readState()
end
