--!nonstrict
-- Pulsar Mouse quick controls - [[panel]]. Opened from the bar widget.
--
-- Unlike the battery read path (bar.luau/desktop.luau), this always talks
-- to the mouse directly via the CLI - DPI/polling rate need a live,
-- authoritative read when the panel opens (the cached battery.json file
-- doesn't carry them), and writes obviously can't be cached at all.
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
local errorText = nil
local busy = false

-- onOpen renders once immediately (a loading placeholder) before the async
-- CLI read below has a result - dpiStages/pollingRates are still {} at that
-- point, so ui.select/button rows are skipped entirely rather than built
-- from an empty options list, which risks failing to render at all.
local function render()
  local rows = {}

  if errorText ~= nil then
    table.insert(rows, ui.label({ text = errorText, color = "error" }))
  end

  local loaded = #dpiStages > 0 or #pollingRates > 0

  if not loaded and errorText == nil then
    table.insert(rows, ui.label({ text = noctalia.tr("ui.loading"), color = "outline" }))
    panel.render(ui.column({ gap = 10, padding = 16 }, rows))
    return
  end

  if #dpiStages > 0 then
    table.insert(rows, ui.label({ text = noctalia.tr("ui.dpi-label"), fontSize = 13, color = "on_surface_variant" }))

    local dpiButtons = {}
    for i, dpi in ipairs(dpiStages) do
      table.insert(dpiButtons, ui.button({
        text = tostring(dpi),
        variant = (i == dpiActive) and "primary" or "outline",
        enabled = not busy,
        onClick = function()
          local target = i
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
    table.insert(rows, ui.row({ gap = 6, wrap = true }, dpiButtons))
  end

  if #pollingRates > 0 then
    table.insert(rows, ui.label({ text = noctalia.tr("ui.polling-label"), fontSize = 13, color = "on_surface_variant" }))

    local pollOptions = {}
    local pollSelectedIndex = 0
    for i, hz in ipairs(pollingRates) do
      table.insert(pollOptions, `{hz} Hz`)
      if hz == pollingRate then
        pollSelectedIndex = i - 1 -- ui.select is 0-indexed
      end
    end
    table.insert(rows, ui.select({
      key = "polling-rate",
      options = pollOptions,
      selectedIndex = pollSelectedIndex,
      enabled = not busy,
      onChange = "onPollChange",
    }))
  end

  panel.render(ui.column({ gap = 10, padding = 16 }, rows))
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
      pollingRate = decoded.polling_rate
      pollingRates = decoded.polling_rates or {}
      dpiActive = decoded.dpi.active
      dpiStages = decoded.dpi.stages or {}
      render()
    end,
    10000
  )
end

function onOpen(_context)
  render() -- immediate placeholder while the read below is in flight
  readStatus()
end

function onPollChange(index, label)
  local optionIndex = tonumber(index)
  if optionIndex == nil or pollingRates[optionIndex + 1] == nil then
    return
  end
  local hz = pollingRates[optionIndex + 1]
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
end
