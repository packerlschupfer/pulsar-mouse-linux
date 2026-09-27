#!/usr/bin/env python3
"""Find out how the Feinmann F01 Noctua Edition wants a DPI stage written.

Written for issue #12.  @Wyatt-Robinson reports that the stage *count* and the
*selected* stage save correctly while the DPI values themselves do not — and
the capture that the driver was decoded from never once changed a DPI value,
so the write shape is the one thing in this driver with no evidence behind it.

Every write Fusion made in that capture was two bytes long, while a DPI stage
record is four (`[x, y, high, checksum]`) and the driver writes it in one go.
That is the leading suspicion, but it is only a suspicion, so this tries the
plausible shapes and reports which one the mouse actually keeps.

It writes to one DPI stage and puts the original bytes back before it exits,
including when a write fails part-way.  Nothing else is touched.

    PYTHONPATH=src python3 tools/noctua-dpi-probe.py
    PYTHONPATH=src python3 tools/noctua-dpi-probe.py --profile 2 --stage 6

Close Pulsar's web configurator and the pulsar-mouse GUI first — only one
program can hold the mouse at a time — and don't touch the mouse while it
runs, since its own DPI button changes the bytes being compared.
"""

import argparse
import sys

from pulsar_mouse import find_device
from pulsar_mouse.drivers.nordic import (
    ADDR_DPI_BASE, DPI_STAGE_SIZE, _dpi_to_raw, _raw_to_dpi,
)


def record_addr(stage: int) -> int:
    return ADDR_DPI_BASE + (stage - 1) * DPI_STAGE_SIZE


def read_record(device, stage: int) -> bytes:
    return device._mem_read_range(record_addr(stage), DPI_STAGE_SIZE)


def wanted_record(device, dpi: int) -> bytes:
    caps = device.capabilities
    raw = _dpi_to_raw(dpi, caps.dpi_step, device._DPI_MODES)
    return bytes(raw) + bytes([device._checksum(*raw)])


# ── The shapes worth trying ──────────────────────────────────────────────────
#
# Each takes the driver, the record's address and the four bytes to land there,
# and writes them however it likes.  The caller reads the record back.

def whole_record(device, addr, rec):
    """What the driver does today: one four-byte write."""
    device._mem_write({addr + i: rec[i] for i in range(4)})


def two_halves(device, addr, rec):
    """Two two-byte writes — the only length Fusion was ever seen using."""
    device._mem_write({addr: rec[0], addr + 1: rec[1]})
    device._mem_write({addr + 2: rec[2], addr + 3: rec[3]})


def byte_at_a_time(device, addr, rec):
    """Four one-byte writes, in case the length field is the problem."""
    for i in range(4):
        device._mem_write({addr + i: rec[i]})


def checksum_last(device, addr, rec):
    """Values first, checksum on its own — in case the mouse validates on the
    checksum byte landing and rejects a record it sees half-written."""
    device._mem_write({addr + i: rec[i] for i in range(3)})
    device._mem_write({addr + 3: rec[3]})


STRATEGIES = (
    ('one 4-byte write (what the driver does)', whole_record),
    ('two 2-byte writes', two_halves),
    ('four 1-byte writes', byte_at_a_time),
    ('3 bytes then the checksum', checksum_last),
)


def force(device, addr, stage, rec) -> bool:
    """Put `rec` at `addr` by whatever means the mouse accepts.

    Used to reset between strategies and to restore at the end, and it has to
    be shape-agnostic for the same reason this script exists: if resetting
    used one fixed shape and the mouse ignored that shape, the reset would
    silently do nothing.  Then every strategy after the first success would
    read back the value still sitting there and report a success of its own.
    """
    for _name, write in STRATEGIES:
        try:
            write(device, addr, rec)
        except Exception:
            continue
        if read_record(device, stage) == rec:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--profile', type=int, default=1)
    ap.add_argument('--stage', type=int, default=1,
                    help='which DPI stage to experiment on (default 1)')
    ap.add_argument('--dpi', type=int, default=1600,
                    help='DPI to write during the test (default 1600)')
    args = ap.parse_args()

    device = find_device()
    caps = device.capabilities
    if caps.fan_range is None:
        print(f"This probe is for the Noctua Edition; found {caps.name}.")
        return 2

    device.open()
    addr = record_addr(args.stage)
    try:
        device._ensure_profile(args.profile)
        original = read_record(device, args.stage)
        target = wanted_record(device, args.dpi)
        print(f"{caps.name}, profile {args.profile}, stage {args.stage} "
              f"at 0x{addr:04X}")
        print(f"  currently:   {original.hex(' ')}  "
              f"= {_raw_to_dpi(original[:3], caps.dpi_step, device._DPI_MODES)} DPI")
        print(f"  writing:     {target.hex(' ')}  = {args.dpi} DPI")
        if original == target:
            print("\nThat stage already holds the test value, so nothing would")
            print("change either way.  Re-run with a different --dpi.")
            return 2
        print()

        results = []
        for name, write in STRATEGIES:
            # Start from the original each time, so one strategy's success
            # can't make the next one look like it worked.
            if not force(device, addr, args.stage, original):
                print("  could not put the original value back between tests, "
                      "so the rest would be meaningless - stopping here.")
                break
            before = read_record(device, args.stage)
            try:
                write(device, addr, target)
                after = read_record(device, args.stage)
                took = after == target
            except Exception as e:
                after, took = f'error: {e}', False
            results.append((name, took, after))
            mark = 'STUCK  ' if took else '  -    '
            print(f"  {mark} {name}")
            print(f"          read back: "
                  f"{after.hex(' ') if isinstance(after, bytes) else after}"
                  f"{'' if took else f'   (wanted {target.hex(chr(32))}, was {before.hex(chr(32))})'}")

        print()
        winners = [n for n, took, _ in results if took]
        if winners:
            print("The mouse keeps the value when written as: "
                  + '; '.join(winners))
        else:
            print("None of these stuck, so the shape of the write isn't the")
            print("problem — the address or the encoding is.  Worth a capture")
            print("of Fusion changing one DPI value, which is the one thing")
            print("the original capture never did.")
        return 0
    finally:
        try:
            ok = force(device, addr, args.stage, original)
            back = read_record(device, args.stage)
            print(f"\nrestored stage {args.stage}: {back.hex(' ')}"
                  f"{'' if ok else '   RESTORE FAILED, set it back in Fusion'}")
        except Exception as e:      # noqa: BLE001 - report, never mask the original error
            print(f"\nrestore failed: {e}\n"
                  f"stage {args.stage} of profile {args.profile} should read "
                  f"{original.hex(' ')}", file=sys.stderr)
        device.close()


if __name__ == '__main__':
    sys.exit(main())
