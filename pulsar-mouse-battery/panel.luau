--!nonstrict
-- Pulsar Mouse quick controls - [[panel]]. Opened from the bar widget.
--
-- Unlike the battery read path (bar.luau/desktop.luau), this always talks
-- to the mouse directly via the CLI - none of DPI/polling/LED state lives
-- in the cached battery.json, and writes obviously can't be cached at all.
--
-- Sliders (DPI stage, polling rate, brightness, breath speed) commit on
-- onDragEnd, not onChange - onChange only updates the live label as you
-- drag, so a drag gesture doesn't fire a USB write per pixel/step crossed.
-- DPI and polling rate are index-based sliders over their configured/
-- supported values (not a raw DPI/Hz range), so every position lands on
-- an actual valid value - equivalent to a slider with a notch per option.
--
-- Root is a ui.scroll, not a ui.column - the panel surface already insets
-- its own content (Style::panelPadding on the host side), so an outer
-- `padding` prop here would double up on that, and scrolling is a safety
-- net against the fixed panel height (declared in plugin.toml) not fitting
-- every combination of rows (the speed slider alone changes content height
-- depending on which LED effect is selected).
--
-- No native tabs control exists (checked noctalia.d.luau) - "Sensor" and
-- "Lighting" are two ui.button()s whose variant swaps between primary/
-- outline based on activeTab, same segmented-button pattern the DPI/polling
-- stage buttons used before they became sliders. Each section is just
-- conditionally included in the rendered tree rather than actually hidden.
--
--   panel.render(tree)          declarative UI tree
--   panel.close()                close the panel surface
--   noctalia.runAsync(cmd, cb)  CLI read/write
--   noctalia.json.decode(str)   status comes back as JSON on stdout

local profile = 1
local pollingRate = nil
local pollingRates = {}
local dpiActive = nil
local dpiStages = {}
local dpiPreviewIndex = nil -- live drag preview, separate from the committed dpiActive
local pollPreviewIndex = nil
local ledEffects = {}
local ledEffect = nil
local brightnessPct = nil
local brightnessPreview = nil
local breathSpeed = nil
local breathSpeedPreview = nil
local wireless = false
local debounce = nil -- {value, min, max}
local debouncePreview = nil
local angleSnap = nil
local rippleControl = nil
local motionSync = nil
local lod = nil -- {value, min, max, step}
local lodPreview = nil
local powerSaving = nil -- {value, min, max}
local powerSavingPreview = nil
local lowPower = nil -- {value, min, max}
local lowPowerPreview = nil
local errorText = nil
local busy = false
local activeTab = "sensor" -- "sensor" | "lighting" | "power"

-- Matches the GUI's own convention (see gui.py) for when breath/pulse speed
-- is relevant: the device's last LED effect in its list, whatever it's
-- actually named ("pulse" here, "breath" on other drivers) - not a literal
-- string match.
local function breathSpeedRelevant()
  return breathSpeed ~= nil and ledEffect == ledEffects[#ledEffects]
end

local function pollingRateIndex()
  for i, hz in ipairs(pollingRates) do
    if hz == pollingRate then
      return i
    end
  end
  return 1
end

-- Generic value slider (label + slider) for the four new global/per-profile
-- settings below - DPI/polling/brightness/breath speed above predate this
-- and stay as they are (index-based lookups for the first two, already
-- working) rather than being retrofitted for the sake of it. get/setPreview
-- hold the live-drag value separately from the committed one, same
-- preview-then-commit-on-drag-end split as everywhere else in this file.
local function sliderRow(labelText, formatValue, min, max, step, get, getPreview, setPreview, buildCmd, onSuccess)
  local shown = getPreview() or get()
  return {
    ui.label({
      text = `{labelText} — {formatValue(shown)}`,
      fontSize = 13,
      color = "on_surface_variant",
    }),
    ui.slider({
      min = min,
      max = max,
      step = step,
      value = shown,
      enabled = not busy,
      onChange = function(value)
        setPreview(tonumber(value) or get())
        render()
      end,
      onDragEnd = function(value)
        local target = tonumber(value) or get()
        setPreview(nil)
        if target == get() then
          render()
          return
        end
        busy = true
        render()
        noctalia.runAsync(buildCmd(target), function(result)
          busy = false
          if type(result) == "table" and result.exitCode == 0 then
            onSuccess(target)
            errorText = nil
          else
            errorText = noctalia.tr("ui.write-failed")
          end
          render()
        end, 10000)
      end,
    }),
  }
end

local function toggleRow(labelText, checked, buildCmd, onSuccess)
  return ui.row({ gap = 8, align = "center" }, {
    ui.toggle({
      checked = checked,
      enabled = not busy,
      onChange = function(value)
        local target = value == "true"
        if target == checked then
          return
        end
        busy = true
        render()
        noctalia.runAsync(buildCmd(target), function(result)
          busy = false
          if type(result) == "table" and result.exitCode == 0 then
            onSuccess(target)
            errorText = nil
          else
            errorText = noctalia.tr("ui.write-failed")
          end
          render()
        end, 10000)
      end,
    }),
    ui.label({ text = labelText, flexGrow = 1 }),
  })
end

local function render()
  local rows = {}

  if errorText ~= nil then
    table.insert(rows, ui.label({ text = errorText, color = "error" }))
  end

  local loaded = #dpiStages > 0 or #pollingRates > 0 or #ledEffects > 0

  if not loaded and errorText == nil then
    table.insert(rows, ui.label({ text = noctalia.tr("ui.loading"), color = "outline" }))
    panel.render(ui.scroll({ flexGrow = 1, gap = 10 }, rows))
    return
  end

  -- No native tabs control - a row of segmented buttons standing in for one,
  -- same variant-swap pattern as the DPI/polling stage buttons used to use
  -- before they became sliders. Power only applies to wireless mice (power
  -- saving timeout, low-battery threshold) - hidden entirely on a wired one.
  local function tabButton(tab, labelKey)
    return ui.button({
      text = noctalia.tr(labelKey),
      variant = (activeTab == tab) and "primary" or "outline",
      flexGrow = 1,
      onClick = function()
        activeTab = tab
        render()
      end,
    })
  end
  local tabButtons = { tabButton("sensor", "ui.tab-sensor"), tabButton("lighting", "ui.tab-lighting") }
  if wireless then
    table.insert(tabButtons, tabButton("power", "ui.tab-power"))
  end
  table.insert(rows, ui.row({ gap = 6 }, tabButtons))

  if activeTab == "sensor" and #dpiStages > 0 then
    local shownIndex = dpiPreviewIndex or dpiActive
    local shownDpi = dpiStages[shownIndex]
    table.insert(rows, ui.label({
      text = `{noctalia.tr("ui.dpi-label")} — {shownDpi}`,
      fontSize = 13,
      color = "on_surface_variant",
    }))
    table.insert(rows, ui.slider({
      min = 1,
      max = #dpiStages,
      step = 1,
      value = shownIndex,
      enabled = not busy,
      onChange = function(value)
        dpiPreviewIndex = math.floor(tonumber(value) or dpiActive)
        render()
      end,
      onDragEnd = function(value)
        local target = math.floor(tonumber(value) or dpiActive)
        dpiPreviewIndex = nil
        if target == dpiActive then
          render()
          return
        end
        busy = true
        render()
        noctalia.runAsync(
          `pulsar-mouse --profile {profile} --active-stage {target}`,
          function(result)
            busy = false
            if type(result) == "table" and result.exitCode == 0 then
              dpiActive = target
              errorText = nil
            else
              errorText = noctalia.tr("ui.write-failed")
            end
            render()
          end,
          10000
        )
      end,
    }))
  end

  if activeTab == "sensor" and #pollingRates > 0 then
    local shownPollIndex = pollPreviewIndex or pollingRateIndex()
    local shownHz = pollingRates[shownPollIndex]
    table.insert(rows, ui.label({
      text = `{noctalia.tr("ui.polling-label")} — {shownHz} Hz`,
      fontSize = 13,
      color = "on_surface_variant",
    }))
    table.insert(rows, ui.slider({
      min = 1,
      max = #pollingRates,
      step = 1,
      value = shownPollIndex,
      enabled = not busy,
      onChange = function(value)
        pollPreviewIndex = math.floor(tonumber(value) or pollingRateIndex())
        render()
      end,
      onDragEnd = function(value)
        local targetIndex = math.floor(tonumber(value) or pollingRateIndex())
        pollPreviewIndex = nil
        local hz = pollingRates[targetIndex]
        if hz == nil or hz == pollingRate then
          render()
          return
        end
        busy = true
        render()
        noctalia.runAsync(`pulsar-mouse --poll {hz}`, function(result)
          busy = false
          if type(result) == "table" and result.exitCode == 0 then
            pollingRate = hz
            errorText = nil
          else
            errorText = noctalia.tr("ui.write-failed")
          end
          render()
        end, 10000)
      end,
    }))
  end

  if activeTab == "sensor" and debounce ~= nil then
    for _, node in ipairs(sliderRow(
      noctalia.tr("ui.debounce-label"),
      function(v) return `{math.floor(v)}ms` end,
      debounce.min, debounce.max, 1,
      function() return debounce.value end,
      function() return debouncePreview end,
      function(v) debouncePreview = v end,
      function(v) return `pulsar-mouse --debounce {math.floor(v)}` end,
      function(v) debounce.value = math.floor(v) end
    )) do
      table.insert(rows, node)
    end
  end

  if activeTab == "sensor" and angleSnap ~= nil then
    table.insert(rows, toggleRow(
      noctalia.tr("ui.angle-snap-label"),
      angleSnap,
      function(v) return `pulsar-mouse --angle-snap {v and "on" or "off"}` end,
      function(v) angleSnap = v end
    ))
  end

  if activeTab == "sensor" and rippleControl ~= nil then
    table.insert(rows, toggleRow(
      noctalia.tr("ui.ripple-label"),
      rippleControl,
      function(v) return `pulsar-mouse --ripple {v and "on" or "off"}` end,
      function(v) rippleControl = v end
    ))
  end

  if activeTab == "sensor" and motionSync ~= nil then
    table.insert(rows, toggleRow(
      noctalia.tr("ui.motion-sync-label"),
      motionSync,
      function(v) return `pulsar-mouse --motion-sync {v and "on" or "off"}` end,
      function(v) motionSync = v end
    ))
  end

  if activeTab == "sensor" and lod ~= nil then
    for _, node in ipairs(sliderRow(
      noctalia.tr("ui.lod-label"),
      function(v) return `{string.format("%.1f", v)}mm` end,
      lod.min, lod.max, lod.step,
      function() return lod.value end,
      function() return lodPreview end,
      function(v) lodPreview = v end,
      function(v) return `pulsar-mouse --profile {profile} --lod {string.format("%.1f", v)}` end,
      function(v) lod.value = v end
    )) do
      table.insert(rows, node)
    end
  end

  if activeTab == "power" and powerSaving ~= nil then
    for _, node in ipairs(sliderRow(
      noctalia.tr("ui.power-saving-label"),
      function(v) return string.format("%d:%02d", math.floor(v / 60), math.floor(v) % 60) end,
      powerSaving.min, powerSaving.max, 30,
      function() return powerSaving.value end,
      function() return powerSavingPreview end,
      function(v) powerSavingPreview = v end,
      function(v) return `pulsar-mouse --power-saving {math.floor(v)}` end,
      function(v) powerSaving.value = math.floor(v) end
    )) do
      table.insert(rows, node)
    end
  end

  if activeTab == "power" and lowPower ~= nil then
    for _, node in ipairs(sliderRow(
      noctalia.tr("ui.low-power-label"),
      function(v) return `{math.floor(v)}%` end,
      lowPower.min, lowPower.max, 5,
      function() return lowPower.value end,
      function() return lowPowerPreview end,
      function(v) lowPowerPreview = v end,
      function(v) return `pulsar-mouse --low-power {math.floor(v)}` end,
      function(v) lowPower.value = math.floor(v) end
    )) do
      table.insert(rows, node)
    end
  end

  if activeTab == "lighting" and #ledEffects > 0 then
    table.insert(rows, ui.label({ text = noctalia.tr("ui.led-label"), fontSize = 13, color = "on_surface_variant" }))

    local effectOptions = {}
    local effectSelectedIndex = 0
    for i, effect in ipairs(ledEffects) do
      table.insert(effectOptions, effect)
      if effect == ledEffect then
        effectSelectedIndex = i - 1
      end
    end
    table.insert(rows, ui.select({
      key = "led-effect",
      options = effectOptions,
      selectedIndex = effectSelectedIndex,
      enabled = not busy,
      onChange = "onEffectChange",
    }))

    local shownBrightness = brightnessPreview or brightnessPct
    table.insert(rows, ui.label({
      text = `{noctalia.tr("ui.brightness-label")} — {shownBrightness}%`,
      fontSize = 13,
      color = "on_surface_variant",
    }))
    table.insert(rows, ui.slider({
      min = 0,
      max = 100,
      step = 1,
      value = shownBrightness,
      enabled = not busy,
      onChange = function(value)
        brightnessPreview = math.floor(tonumber(value) or brightnessPct)
        render()
      end,
      onDragEnd = function(value)
        local target = math.floor(tonumber(value) or brightnessPct)
        brightnessPreview = nil
        if target == brightnessPct then
          render()
          return
        end
        busy = true
        render()
        noctalia.runAsync(
          `pulsar-mouse --profile {profile} --brightness-percent {target}`,
          function(result)
            busy = false
            if type(result) == "table" and result.exitCode == 0 then
              brightnessPct = target
              errorText = nil
            else
              errorText = noctalia.tr("ui.write-failed")
            end
            render()
          end,
          10000
        )
      end,
    }))

    if breathSpeedRelevant() then
      local shownSpeed = breathSpeedPreview or breathSpeed
      table.insert(rows, ui.label({
        text = `{noctalia.tr("ui.speed-label")} — {shownSpeed}`,
        fontSize = 13,
        color = "on_surface_variant",
      }))
      table.insert(rows, ui.slider({
        min = 0,
        max = 100,
        step = 1,
        value = shownSpeed,
        enabled = not busy,
        onChange = function(value)
          breathSpeedPreview = math.floor(tonumber(value) or breathSpeed)
          render()
        end,
        onDragEnd = function(value)
          local target = math.floor(tonumber(value) or breathSpeed)
          breathSpeedPreview = nil
          if target == breathSpeed then
            render()
            return
          end
          busy = true
          render()
          noctalia.runAsync(
            `pulsar-mouse --profile {profile} --breath-speed {target}`,
            function(result)
              busy = false
              if type(result) == "table" and result.exitCode == 0 then
                breathSpeed = target
                errorText = nil
              else
                errorText = noctalia.tr("ui.write-failed")
              end
              render()
            end,
            10000
          )
        end,
      }))
    end
  end

  panel.render(ui.scroll({ flexGrow = 1, gap = 10 }, rows))
end

local function readStatus()
  if not noctalia.commandExists("pulsar-mouse") then
    errorText = noctalia.tr("ui.not-installed")
    render()
    return
  end
  noctalia.runAsync(
    `pulsar-mouse --status-json --profile {profile}`,
    function(result)
      if type(result) ~= "table" or result.timedOut or result.exitCode ~= 0 then
        errorText = noctalia.tr("ui.no-mouse")
        render()
        return
      end
      local decoded = noctalia.json.decode(result.stdout or "")
      if type(decoded) ~= "table" or decoded.polling_rate == nil then
        errorText = noctalia.tr("ui.no-mouse")
        render()
        return
      end
      errorText = nil
      wireless = decoded.wireless == true
      pollingRate = decoded.polling_rate
      pollingRates = decoded.polling_rates or {}
      dpiActive = decoded.dpi.active
      dpiStages = decoded.dpi.stages or {}
      if type(decoded.debounce) == "table" then
        debounce = decoded.debounce
      end
      if type(decoded.angle_snap) == "boolean" then
        angleSnap = decoded.angle_snap
      end
      if type(decoded.ripple_control) == "boolean" then
        rippleControl = decoded.ripple_control
      end
      if type(decoded.motion_sync) == "boolean" then
        motionSync = decoded.motion_sync
      end
      if type(decoded.lod) == "table" and decoded.lod.step ~= nil then
        -- Only the continuous-slider shape (lod_step set) is handled here -
        -- a driver with a discrete lod_values list instead (no "step") isn't
        -- supported by this panel, same as the DPI/polling stage sliders
        -- assume their own value lists are always present and non-empty.
        lod = decoded.lod
      end
      if type(decoded.power_saving) == "table" then
        powerSaving = decoded.power_saving
      end
      if type(decoded.low_power) == "table" then
        lowPower = decoded.low_power
      end
      if type(decoded.led) == "table" then
        ledEffects = decoded.led.effects or {}
        ledEffect = decoded.led.effect
        brightnessPct = decoded.led.brightness_percent
        breathSpeed = decoded.led.breath_speed
      end
      render()
    end,
    10000
  )
end

function onOpen(_context)
  render() -- immediate placeholder while the read below is in flight
  readStatus()
end

function onEffectChange(index, label)
  local optionIndex = tonumber(index)
  if optionIndex == nil or ledEffects[optionIndex + 1] == nil then
    return
  end
  local effect = ledEffects[optionIndex + 1]
  busy = true
  render()
  noctalia.runAsync(`pulsar-mouse --profile {profile} --led {effect}`, function(result)
    busy = false
    if type(result) == "table" and result.exitCode == 0 then
      ledEffect = effect
      errorText = nil
    else
      errorText = noctalia.tr("ui.write-failed")
    end
    render()
  end, 10000)
end
