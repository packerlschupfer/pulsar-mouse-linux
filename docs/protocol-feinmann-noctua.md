# Pulsar Feinmann F01 Noctua Edition — protocol notes

Decoded from three usbmon captures of Pulsar Fusion contributed by
[@Wyatt-Robinson](https://github.com/Wyatt-Robinson) in
[issue #12](https://github.com/packerlschupfer/pulsar-mouse-linux/issues/12), the first
with a matching screen recording. Every register below was confirmed either by
extracting the video frame at the timestamp of a write, or by writing it to the mouse
and reading it back.

Firmware in the captures: `5.09`. USB IDs `3710:7507` (cable) and `3710:5504` (dongle).

> **This is not the Feinmann FO1 protocol**, despite the name. `drivers/feinmann8k.py`
> speaks the Sonix 64-byte HID Feature Report protocol on interface 3; this mouse speaks
> the **Nordic 17-byte protocol on interface 1**, endpoint `0x82`, SET_REPORT
> `wValue=0x0208` — the same one as the X2 CrazyLight, with the same register map.
> A driver written from the FO1 gets silence back, which is exactly what happened to the
> first attempt.

Everything about the transport — framing, checksum, the command set, profiles, the
`0x0A` unprompted reports — is the X2 CrazyLight's, and
[protocol-x2-crazylight.md](protocol-x2-crazylight.md) documents it. This file records
only where this model differs.

## DPI lives at `0x1B00`, not `0x000C`

The single most expensive mistake in decoding this mouse, and worth stating plainly.

`0x000C` — the CrazyLight's DPI stage area — exists here too, and its first four entries
agreed with what Fusion displayed (`07`/`0f`/`1f`/`3f` read as 400/800/1600/3200 under
the CrazyLight's encoding at 50 DPI per step). Stages 5 and 6 did not: they held the
same bytes as stage 4 while Fusion showed 6400 and 12800. That disagreement was written
off as Fusion displaying its own defaults. It wasn't. It was the real table saying it
lived somewhere else.

Fusion reads and writes DPI **only** at `0x1B00`, six bytes per stage:

```
0x1B00 + 6*(stage-1):   [x_lo, x_hi, y_lo, y_hi, 0x00, checksum]
```

X and Y are separate little-endian 16-bit values holding **DPI − 1**, and the checksum
is the usual `0x55` minus the sum of the five bytes before it. Reading that region out
of the first capture gives all six stages matching Fusion exactly:

```
8f 01 8f 01 00 35 →   400     7f 0c 7f 0c 00 3f →  3200
1f 03 1f 03 00 11 →   800     ff 18 ff 18 00 27 →  6400
3f 06 3f 06 00 cb →  1600     ff 31 ff 31 00 f5 → 12800
```

A write takes effect on its own: Fusion sends one per slider position while dragging,
and nothing follows to commit it. The range is **50–42000 DPI at 1 DPI granularity** —
a drag produced values like 28393, and `0f a4` (41999, so 42000) at the top end.

`0x000C` is left alone by this driver. Whatever it is now, it is not what the sensor
reads.

## Fan, and low power

Two registers the CrazyLight has no use for:

| Address | Meaning | Evidence |
|---|---|---|
| `0x00E7` | Fan mode, `0` = off … `4` = maximum | written `04` then `00`; the video's FAN MODE slider reads "4" then "OFF" at those timestamps |
| `0x00D7` | Low power mode threshold, percent | stored `0x11` while the UI read 17%, then written `0x20` when it read 32% |

Both sit **above** the `0x00`–`0xBF` window the CrazyLight keeps a profile in, which
reads as device-wide and is not: setting the fan, switching profile and reading back
shows it change, so this model's per-profile window reaches further. Both take the
`per_profile_globals` contract — an explicit profile, or the active one when none is
given.

This edition has a **fan**, which is what the Noctua collaboration is about.

## No LEDs

The hardware has none, confirmed by the owner. Fusion shows a full LED panel anyway and
the firmware accepts writes to the LED registers — this edition shares firmware with its
lit siblings — so a capture will show LED effect, brightness and breathing speed being
written to `0x4C`/`0x4E`/`0x50` as usual. They do nothing. `has_led=False`, and the CLI
and GUI omit those controls rather than offering a brightness slider for LEDs that do
not exist.

## Lift-off distance: 0.7–1.7 mm

`ADDR_LOD_MM` (`0x0A`) holds a code, and `mm = (code + 6) / 10` — 0.1 mm per step. Two
points were pinned by video (`0x04` = 1.00 mm, `0x06` = 1.20 mm), and a capture of the
slider being dragged end to end used codes `0x01` through `0x0B` and nothing outside
them, which puts the range at **0.7–1.7 mm**.

The mouse will store a larger code — 1.8, 1.9 and 2.0 mm were written and read back
successfully during testing — but Fusion never offers them, so nothing says the sensor
acts on them. The driver stops at 1.7 mm: a lift-off distance that silently means
nothing is worse than one the UI doesn't list.

## Polling

125 Hz to 8 kHz, and unlike the X2 CrazyLight **8 kHz works over the cable as well as
the dongle**, so the wired driver is the dongle one with only the PID and name changed.

## Confirmed on hardware

@Wyatt-Robinson ran `tools/noctua-verify.py` on 2026-09-28, over the dongle: 14 of 14
checks passed and every byte was restored. That covers DPI (400, 1600, 28393 and 42000
written and read back), the fan, the low-power threshold, polling, debounce, angle snap,
ripple control, motion sync, turbo mode, auto sleep, and button remapping — and confirms
the profile count really is 4, which the driver had inherited from the CrazyLight
without evidence.

Writing only to profile 2 left the other three byte-identical. That check is what would
have caught the fan being treated as device-wide, and it is worth keeping in any future
verify script for this family.

Use `tools/noctua-verify.py`, **not** `tools/x2cl-verify.py`, on this mouse: the latter
assumes DPI lives at `0x000C`, so its restore would put back bytes that mean nothing.

## Not supported

Fusion offers **Enable X-Y**, separate X and Y DPI per stage. The table has room for it
— X and Y are already stored separately — but this driver always writes them equal,
since `set_dpi_stages()` takes one value per stage across every model.

The fan is also absent from profile export/import, along with the other settings this
family stores per profile but exposes through the profile-less accessors.
