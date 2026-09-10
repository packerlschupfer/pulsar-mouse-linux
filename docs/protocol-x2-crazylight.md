# Pulsar X2 CrazyLight (X2 CL) — wireless dongle protocol

Decoded from two USBPcap captures of Pulsar Fusion, each with a matching screen
recording, contributed by @iamtherobin in
[issue #7](https://github.com/packerlschupfer/pulsar-mouse-linux/issues/7).
Every register below was confirmed by extracting the video frame at the timestamp of
each write (the video/capture clock offset is calibrated from Wireshark's on-screen
packet counter).

Firmware in the capture: `X2 CL Wireless · Mouse v3.05 · Dongle v2.25 · DRV V1.31`.

> **This is not the Sonix 64-byte protocol** used by the X2A / Xlite v4 / Feinmann 8K
> drivers — which is why all three return garbage for this device. It is the
> **Nordic 17-byte protocol** this repo already implements in `drivers/nordic.py`
> for the X2A Wireless, even though the X2 CL ships under the Sonix VID `0x3710`
> rather than Nordic's `0x3554`. `drivers/x2_crazylight.py` therefore subclasses
> `PulsarNordic` and only overrides the model-specific layout.
>
> Everything below was decoded from the capture independently and then found to
> agree with `nordic.py`, so it doubles as confirmation of that driver's register
> map — which had been marked UNTESTED.

## Transport

| | |
|---|---|
| VID:PID | `3710:5406` (2.4 GHz dongle), `3710:3414` (wired) |
| Interface | **1** (report descriptor 266 bytes, IN endpoint `0x82`) |
| Report ID | `0x08` |
| Report size | **17 bytes** (report ID + 16) |
| Host → device | control `SET_REPORT`, `bmRequestType=0x21 bRequest=0x09 wValue=0x0208 wIndex=0x0001 wLength=0x0011` |
| Device → host | interrupt IN on endpoint `0x82`, 17 bytes |

Interface 1 has no OUT endpoint, so a plain `hidraw` `write()` degrades to exactly the
control `SET_REPORT` that Fusion issues — a hidraw-only driver is possible, no libusb
needed.

Interface 0 is the boot mouse (EP `0x81`), interface 2 is keyboard/consumer (EP `0x83`).

## Frame layout

```
byte  0   report ID, always 0x08
byte  1   command
byte  2   always 0x00 in the capture
byte  3   address high  (cmd 0x07 / 0x08)
byte  4   address low   (cmd 0x07 / 0x08)
byte  5   length        (cmd 0x07 / 0x08); response payload length otherwise
bytes 6-15 payload (10 bytes)
byte 16   checksum
```

### Checksum

```python
checksum = (0x55 - sum(report[0:16])) & 0xFF
```

The same `0x55 - x` complement is used *inside* the config blob: most single-byte
settings are stored as a `(value, 0x55 - value)` pair, and multi-byte records end with
`0x55 - sum(record)`. A write of a single setting is therefore always `len = 2`.

### Commands

| Cmd | Direction | Meaning |
|---|---|---|
| `0x01` | req/resp | handshake; request carries a 4-byte nonce, response returns 4 bytes + `57 03 05` |
| `0x02` | req/resp | enter config mode (payload `01`), echoed back |
| `0x03` | poll | link status — response payload `01`, then constant `54 ac 3a`. Fusion polls this before every operation |
| `0x04` | poll | battery — response byte 6 = percent (`0x5a` = 90 %), byte 7 = charging flag |
| `0x07` | write | write `len` bytes at address; `08 07 00 <ah> <al> 02 <val> <0x55-val> …` |
| `0x08` | read | read `len` bytes (max 10) at address; response echoes addr/len then returns data |
| `0x0a` | resp | **unsolicited** — always follows a profile switch with payload `04` (the profile count), but also arrives on its own mid-session with payload `0x40`. Never a reply to anything the host sent, so a command must match the reply's command byte rather than taking whatever lands next |
| `0x0e` | poll | active profile, 0-based |
| `0x0f` | write | set active profile, 0-based, `len = 1` |
| `0x12` | poll | firmware version — response `03 05` = mouse v3.05 |
| `0x15` | poll | 10 bytes, unknown |
| `0x17` | poll | 10 bytes, unknown |

## Config blob (address page `0x00`)

Full dump read by Fusion at startup, 10 bytes at a time:

```
0x00: 10 45 04 51 02 53 00 55 00 55
0x0a: 01 54 27 27 00 07 4f 4f 00 b7
0x14: 9f 9f 00 17 3f 3f 44 93 7f 7f
0x1e: 88 cf 37 37 22 c5 37 37 22 c5
0x28: 37 37 22 c5 ff 00 00 56 00 00
0x32: ff 56 00 ff 00 56 ff ff 00 57
0x3c: ff a3 00 b3 f2 0a ea 6f f2 0a
0x46: ea 6f f2 0a ea 6f 01 54 10 45
0x50: 03 52 00 55 ff 00 ff 57 00 55
0x5a: 80 d5 03 52 00 55 01 01 00 53
0x64: 01 02 00 52 01 04 00 50 01 08
0x6e: 00 4c 01 10 00 44 02 01 00 52
0x78: 02 02 00 51 02 03 00 50 07 00
0x82: 00 4e 08 04 00 49 04 0a 03 44
0x8c: 00 00 00 55 00 00 00 55 00 00
0x96: 00 55 00 00 00 55 00 00 00 55
0xa0: 01 ff 00 ff 07 09 46 00 55 00
0xaa: 55 01 54 06 4f 00 55 00 55 00
0xb4: 55 00 55 06 4f 00 55 0a 4b 00
0xbe: 55 00 55 00 55 00 00 80 3f 96
0xc8: 00 00 80 3f 96 00 00 80 3f 96
0xd2: 00 00 80 3f 96 00 55 01 54 00
0xdc: 00 00 55 00 55 e1 74 27 2e fc
0xe6: 59 7f d6 ff ff ff ff ff ff ff
0xf0: ff … ff (unused)
```

Pages `0x01`–`0x1b` were read too and are all `0xFF` (empty macro storage), except a
small block around `0x1b32`–`0x1b4b`.

### Register map

Addresses below were confirmed by correlating each `0x07` write in the capture with the
Fusion UI in the screen recording contributed alongside it.

| Addr | Setting | Encoding |
|---|---|---|
| `0x0000` | Polling rate | `0x01`=1000, `0x02`=500, `0x04`=250, `0x08`=125 Hz (period in ms); `0x10`=2 K, `0x20`=4 K, `0x40`=8 K. All seven confirmed |
| `0x0002` | DPI stage count | `0x04` in the capture |
| `0x0004` | Active DPI stage | 0-based (`0x02` = DPI 3) |
| `0x0006` | unknown | `0x00` |
| `0x0008` | unknown | `0x00` |
| `0x000a` | Lift-off distance | `0x01`=1 mm, `0x02`=2 mm, `0x03`=0.7 mm |
| `0x000c`–`0x002b` | 8 × DPI stage | 4 bytes each: `[x_lo, y_lo, mode/page, cksum]`. Six are usable — slots 7 and 8 repeat slot 6's defaults |
| `0x002c`–`0x004b` | 8 × DPI stage colour | 4 bytes each: `[R, G, B, cksum]` |
| `0x004c` | LED effect | `0x01`=steady, `0x02`=breathing |
| `0x004e` | LED brightness | 10 UI steps: `10 1e 3c 5a 80 96 b4 d2 e6 ff` |
| `0x0050` | LED breathing speed | UI 1…5, stored directly |
| `0x0052` | LED on/off | `0x00`=off, `0x01`=on (Fusion's "OFF" radio writes here, not to `0x004c`) |
| `0x0054` | LED colour | `[R, G, B, cksum]` |
| `0x0060`–`0x008b` | Button assignments | 4 bytes each: `[type, code, modifier, cksum]` |
| `0x00a9` | Debounce time | milliseconds; 0–20 swept in the second capture |
| `0x00ab` | Motion sync | `0`/`1` |
| `0x00ad` | Auto sleep | value × 10 s (`0x03` = 30 s, `0x06` = 1 min, `0x3c` = 10 min, `0xb4` = 30 min) |
| `0x00af` | Angle snapping | `0`/`1` |
| `0x00b1` | Ripple control | `0`/`1` |
| `0x00b5` | Turbo mode | `0`/`1` |
| `0x00b7` | Auto sleep (mirror) | Fusion writes the same value here as to `0x00ad` |
| `0x00c4`–`0x00d7` | 4 × float32 `1.0f` + cksum | mouse sensitivity multipliers |
| `0x00e1`–`0x00e8` | 8 bytes | looks like the pairing address / device serial |

### Profiles

The mouse holds **four onboard profiles**, and the memory map above is whichever one is
active — switching reloads all of it. Everything in the register table is therefore
per-profile, including the settings most drivers treat as device-wide: polling rate,
debounce, angle snapping, ripple control and motion sync all sit at `0x0000`–`0x00B1`,
inside that window. A live readout found profile 1 at 1 kHz and profile 4 at 8 kHz on
the same mouse.

Two consequences for a driver: the profile-less getters in `PulsarDevice` describe
whichever profile is loaded (hence the `per_profile_globals` capability flag), and
simply *reading* the device walks through every profile, so it must put the mouse back
on the one the user had selected before closing. Fusion switches with command `0x0F` (0-based
profile number, `len = 1`), gets an `0x0A` reply carrying the profile count (`04`), then
re-reads the entire map. Command `0x0E` reports the active profile.

Confirmed by watching `0x0000` change across switches — profile 1 was set to 1 kHz,
profile 3 to 4 kHz, profile 4 to 8 kHz — with the debounce and auto-sleep values
changing to match what the Fusion UI showed for each.

### DPI stage encoding

Fourteen (bytes → DPI) pairs across three captures, every one read off the matching
video:

```
27 27 00 →   400     3f 3f 44 →  3200     37 37 22 → 12800     17 17 66 → 24000
4f 4f 00 →   800     7f 7f 88 →  6400     77 77 22 → 16000     37 37 66 → 25600
9f 9f 00 →  1600     1f 1f cc →  8000     c7 c7 22 → 20000     77 77 33 → 32000
ef ef 00 →  2400     e7 e7 cc → 10000
```

A stage record is `[x_lo, y_lo, high, checksum]`. The `high` byte carries the same
nibble twice, one per axis. Rotate that nibble left by 2 — a self-inverse operation —
and it splits into **`[mode:2][page:2]`**:

```python
rot2 = lambda n: ((n << 2) | (n >> 2)) & 0x0F
high = rot2(raw[2] & 0x0F)
mode, page = high >> 2, high & 3
dpi  = (raw[0] + BASE[mode] + 256 * page) * STEP[mode]
```

`page` extends the 8-bit low byte to 10 bits. `mode` selects the DPI granularity, which
is how the range reaches 32000 without widening the field:

| mode | step | base | used for | encoded range |
|---|---|---|---|---|
| 0 | 10 | 1 | ≤ 10240 | 10 – 10240 |
| 2 | 50 | 201 | ≤ 25600 | 10050 – 35600 |
| 3 | 100 | 201 | above | 20100 – 122400 |

All fourteen samples decode exactly, and re-encode to the same bytes Fusion wrote.
Mode 1 has never been observed. The mode boundaries are Fusion's choice, not
representational limits — mode 2 could express 32000 but Fusion switches to mode 3
above 25600, so the driver follows the same thresholds.

For `mode = 0, page ≤ 3` this is identical to the single-mode formula `nordic.py`
already used, so the X2A Wireless is unaffected — devices that only ever use the finest
granularity declare just that one mode.

## Confirmed on hardware

@iamtherobin ran the driver against a real X2 CrazyLight on 2026-09-10: it reads
firmware, battery, all four profiles, DPI stages and colours, LOD, LED, buttons and the
per-profile tunables, and picks up changes made from Pulsar's own web configurator.
Two things that readout corrected:

- **Six DPI stages, not four.** Profiles configured with six read back correctly, and
  slots 5 and 6 carry real factory defaults (6400 orange, 12800 magenta `#F20AEA`) —
  which re-confirms the mode encoding, since slot 6's `37 37 22` is exactly the 12800
  that mode 2 predicts.
- **Nothing has been written to a device yet.** Reads are proven; writes are not.

Claiming interface 1 takes the device away from anything else using it — Pulsar's
WebHID configurator at `bbb.pulsar.gg` drops its connection while the CLI runs. That is
expected, and it reconnects afterwards.

## Wired mode

**Identical protocol.** The third capture caught both devices on one hub — `3710:3414`
(wired, `bcdDevice 0x0305`, matching the "Mouse v3.05" Fusion reports) and `3710:5406`
(the dongle) — and Fusion drives the wired PID with exactly the same commands on the
same interface: `wValue 0x0208`, `wIndex 1`, 17-byte reports, IN endpoint `0x82`. Same
register map, same four profiles, and the DPI sweep produced byte-for-byte the same
stage records as over the dongle, which independently confirms the encoding above on a
second PID.

One difference: **wired polling tops out at 1 kHz.** Fusion offers only 125/250/500/1K
there, against 125 Hz–8 K over the dongle. Battery reads still answer, but the mouse is
charging on the cable and Fusion shows a charging icon instead of a percentage.

`drivers/x2_crazylight_wired.py` is therefore the dongle driver with the PID and the
polling list narrowed.
