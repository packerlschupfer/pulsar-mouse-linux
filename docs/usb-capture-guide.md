# USB Capture Guide

How to capture the USB HID traffic between Pulsar Fusion (the official
Windows tool) and your mouse, so we can add support for a new model.

Every driver in this project was built from these captures.

## What we need

**Two files: a `.pcapng` capture and a screen recording of Fusion taken at the same
time.** The capture alone gives us a list of writes — addresses and values with nothing
to say what they mean. The video is what identifies them: we line up each write with
the video frame at that moment and read straight off the UI which setting changed and
to what. Settings that would otherwise stay guesses become certain, and a value we
guess wrong gets written to someone's mouse.

The capture should contain:

1. **A read of every setting** — open Pulsar Fusion, switch between tabs and profiles
2. **A sweep of each setting** — move every control through *all* of its positions, not
   just one. A full sweep of a slider pins down how the value is encoded; a single
   sample usually doesn't

Any screen recorder works (OBS, Xbox Game Bar, ShareX). See
[Recording your screen alongside](#recording-your-screen-alongside) for the two details
that make a recording usable — it takes no extra effort if you know them up front.

If you genuinely can't record, send the capture anyway and label what you did and
roughly when; it's slower to decode but still useful.

## Option A: Wireshark on Windows (simplest)

### 1. Install

- [Wireshark](https://www.wireshark.org/download.html) — check **Install USBPcap**
  during installation (it's an option in the installer)
- Reboot after installing USBPcap

### 2. Find your mouse's USB bus

**This is the step that most often goes wrong.** USBPcap captures one *root hub* at a
time, not one device — if you pick the wrong hub you get a large capture full of
webcam, audio and card-reader traffic and none of your mouse, no matter what filter
you set. A capture that "shows nothing" is almost always the wrong hub rather than a
bad filter, so do not disable the filter and re-capture; change the interface.

The reliable way is to let USBPcap list the devices on each hub:

```
"C:\Program Files\USBPcap\USBPcapCMD.exe"
```

It prints something like:

```
1 \\.\USBPcap1
  \??\USB#ROOT_HUB30#4&2f9a0ee3&0&0#  Root Hub
    [Port 3] Pulsar Gaming Mouse
2 \\.\USBPcap2
  \??\USB#ROOT_HUB30#5&1a2b3c4d&0&0#  Root Hub
    [Port 1] USB Camera
```

Pick the `USBPcapN` that lists your mouse — here `USBPcap1`. Plug the mouse (or the
2.4 GHz dongle) **directly into a motherboard port**, not through a hub or monitor, and
re-run the command if you move it, since that can change the hub.

If the mouse isn't listed under any hub, close Pulsar Fusion, unplug and replug the
device, and run the command again.

#### Sanity-check before the long capture

Start the capture on your chosen interface, open Fusion, and confirm packets are
arriving with this display filter:

```
usb.transfer_type == 0x02
```

If that stays empty while Fusion is clearly talking to the mouse, you are on the wrong
hub — stop and try the next `USBPcapN`. Ten seconds here saves re-recording everything.

### 3. Capture

1. Open Wireshark
2. Select the **USBPcap** interface for the bus your mouse is on
3. Click **Start capturing**
4. Open **Pulsar Fusion** — let it read all settings
5. Switch through each tab and profile in Fusion
6. Change a few settings (DPI, polling rate, LED effect) and hit Apply
7. Change them back and Apply again
8. **Stop** the capture in Wireshark
9. Save as `.pcapng` (File → Save As)

### 4. Filter (optional, helps us)

These are **display** filters — they only change what Wireshark shows you, and the
saved file still holds everything. So an empty view means the wrong interface, never a
reason to capture unfiltered.

Display filter to see only HID feature reports:

```
usb.transfer_type == 0x02 && usb.endpoint_address.direction == 1
```

Or to see both directions (host→device and device→host):

```
usb.transfer_type == 0x02
```

## Option B: Windows VM with USB passthrough (Linux host)

If you don't have a Windows install, you can run Pulsar Fusion in a VM
with the mouse passed through, and capture on the Linux side — no
Wireshark needed on Windows.

### Using virt-manager / QEMU

```bash
# 1. Install a Windows VM (virt-manager makes this easy)
# 2. Pass through the mouse USB device:
#    virt-manager → VM → Add Hardware → USB Host Device → select Pulsar mouse
#
# 3. Load the usbmon kernel module
sudo modprobe usbmon

# 4. Find which usbmon bus the mouse is on
# Look for your Pulsar VID:PID (e.g. 3710:5406)
lsusb | grep 3710
# Bus 005 Device 002: ID 3710:5406 Pulsar ...
# → use usbmon5

# 5. Start capturing (as root)
sudo tshark -i usbmon5 -w capture.pcapng

# 6. In the Windows VM: open Pulsar Fusion, change settings
# 7. Stop tshark (Ctrl+C)
```

### Using Proxmox

1. Pass the USB device to a Windows VM (Datacenter → VM → Hardware → Add → USB Device)
2. SSH to the Proxmox host
3. Same `modprobe usbmon` + `tshark` steps as above

## Option C: Quick protocol test (no capture needed)

If your mouse uses the same Sonix chipset (`VID 0x3710`) and shows up on
**Interface 3** with **64-byte reports**, it likely speaks the same protocol
as an existing driver. You can test without captures:

```bash
# Check USB descriptors
lsusb -v -d 3710:YOUR_PID 2>/dev/null | grep -A5 'Interface Descriptor'

# If you see bInterfaceNumber 3 and wMaxPacketSize 64, try adding
# your PID to an existing driver's vid_pid_pairs and see if it reads:
sudo PYTHONPATH=src python3 -m pulsar_mouse.cli
```

Only test **reading** until we confirm the protocol matches. Do not write
settings to a device with an unverified driver.

## Recording your screen alongside

Record the whole Fusion window, and keep Wireshark visible behind or beside it.

Two things make the recording usable:

- **Keep the Wireshark packet counter visible.** It is how the video and capture clocks
  get aligned, to about a tenth of a second.
- **Pause a beat on each change.** Move one control, wait a second, move the next. Fast
  drags produce a burst of writes that can't be told apart.

- **Sweep, don't sample.** Every position of every control. This is the single biggest
  difference between a capture we can half-decode and one we can finish.

Don't worry about trimming or narrating the video — we only ever look at single frames
at specific timestamps.

## Sending us the capture

Attach the `.pcapng` file to a GitHub issue. If it's large (>25MB),
upload to a file sharing service and link it.

We also have `tools/parse-capture.py` that extracts the HID feature
reports from a pcapng and annotates the known fields:

```bash
python3 tools/parse-capture.py capture.pcapng
```

## What happens next

We'll decode the packet structure from your capture and either:
- Confirm it matches an existing driver (just needs a new VID:PID entry)
- Build a new driver if the register layout differs

The plugin architecture means adding a new model is typically a single
file — see [Adding a new driver](../README.md#adding-a-new-driver).
