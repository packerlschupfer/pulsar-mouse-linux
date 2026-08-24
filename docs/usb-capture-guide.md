# USB Capture Guide

How to capture the USB HID traffic between Pulsar Fusion (the official
Windows tool) and your mouse, so we can add support for a new model.

Every driver in this project was built from these captures.

## What we need

One `.pcapng` file containing at least:

1. **A read of every setting** — open Pulsar Fusion, switch between tabs/profiles
2. **A write of a few settings** — change something (DPI, polling rate, LED),
   click Apply, then change it back

Label what you did and when (even rough timestamps help).

## Option A: Wireshark on Windows (simplest)

### 1. Install

- [Wireshark](https://www.wireshark.org/download.html) — check **Install USBPcap**
  during installation (it's an option in the installer)
- Reboot after installing USBPcap

### 2. Find your mouse's USB bus

1. Open Device Manager → Human Interface Devices
2. Find entries mentioning "Pulsar" — note the USB bus number
3. Or just try each USBPcap interface in Wireshark until you see traffic

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
