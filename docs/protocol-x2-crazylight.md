# Pulsar X2 CrazyLight (X2 CL) — wireless dongle protocol

Decoded from a USBPcap capture of Pulsar Fusion contributed by @iamtherobin in
[issue #7](https://github.com/packerlschupfer/pulsar-mouse-linux/issues/7).

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
| VID:PID | `3710:5406` (2.4 GHz dongle) |
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
| `0x0e` | poll | response payload `01` (unknown) |
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
| `0x0000` | Polling rate | `0x01`=1000, `0x02`=500, `0x04`=250, `0x08`=125 Hz (period in ms); `0x10`=2 K, `0x20`=4 K, `0x40`=8 K. **Confirmed: 125, 2 K, 4 K, 8 K.** 250/500/1 K inferred |
| `0x0002` | DPI stage count | `0x04` in the capture |
| `0x0004` | Active DPI stage | 0-based (`0x02` = DPI 3) |
| `0x0006` | unknown | `0x00` |
| `0x0008` | unknown | `0x00` |
| `0x000a` | Lift-off distance | `0x01`=1 mm, `0x02`=2 mm, `0x03`=0.7 mm |
| `0x000c`–`0x002b` | 8 × DPI stage | 4 bytes each: `[x_lo, y_lo, hi, cksum]` |
| `0x002c`–`0x004b` | 8 × DPI stage colour | 4 bytes each: `[R, G, B, cksum]` |
| `0x004c` | LED effect | `0x01`=steady, `0x02`=breathing |
| `0x004e` | LED brightness | `0x10`…`0xff`, 10 UI steps (step 1 = `0x10`, step 6 = `0x96`) |
| `0x0050` | LED breathing speed | UI 1…5, stored directly |
| `0x0052` | LED on/off | `0x00`=off, `0x01`=on (Fusion's "OFF" radio writes here, not to `0x004c`) |
| `0x0054` | LED colour | `[R, G, B, cksum]` |
| `0x0060`–`0x008b` | Button assignments | 4 bytes each: `[type, code, modifier, cksum]` |
| `0x00a9` | Debounce time | milliseconds (0, 3, 20 observed) |
| `0x00ab` | Motion sync | `0`/`1` |
| `0x00ad` | Auto sleep | value × 10 s (`0x06` = 1 min, `0x1e` = 5 min) |
| `0x00af` | Angle snapping | `0`/`1` |
| `0x00b1` | Ripple control | `0`/`1` |
| `0x00b5` | Turbo mode | `0`/`1` |
| `0x00b7` | Auto sleep (mirror) | Fusion writes the same value here as to `0x00ad` |
| `0x00c4`–`0x00d7` | 4 × float32 `1.0f` + cksum | mouse sensitivity multipliers |
| `0x00e1`–`0x00e8` | 8 bytes | looks like the pairing address / device serial |

### DPI stage encoding

The four configured stages in the capture were 400 / 800 / 1600 / 3200 DPI:

```
stage 1   27 27 00 07  →  (0x27+1) × 10                =   400
stage 2   4f 4f 00 b7  →  (0x4f+1) × 10                =   800
stage 3   9f 9f 00 17  →  (0x9f+1) × 10                =  1600
stage 4   3f 3f 44 93  →  (0x3f+1) × 10 + 1 × 2560     =  3200
stage 5   7f 7f 88 cf  →  (0x7f+1) × 10 + 2 × 2560     =  6400   (unused default)
stage 6-8 37 37 22 c5  →  (0x37+1) × 10                =   560   (filler)
```

This is exactly the encoding `nordic.py` already implements, with the step changed
from 50 to 10 DPI:

```python
dpi      = (raw[0] + 1) * step + ((raw[2] >> 2) & 0x03) * 256 * step
raw[2]   = (overflow << 2) | (overflow << 6)
```

All five values round-trip byte-for-byte against the capture. Because the overflow
field is 2 bits wide, this encoding tops out at `1024 × step` = **10240 DPI**, which
is where the driver sets `dpi_max`. The sensor is specified higher, so the firmware
most likely widens the field somewhere above 6400 — the capture never went there, so
**a follow-up capture setting a DPI above 6400 is still needed** before raising the
ceiling.

The remaining gap is the polling-rate table: 125 Hz, 2 K, 4 K and 8 K were observed
directly; 250 / 500 / 1 K are carried over from `nordic.py` (andrewrabert's tool) and
have not been seen on this model.

## Wired mode

The wired PID `3710:3414` was not captured and is unmapped.
