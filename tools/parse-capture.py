#!/usr/bin/env python3
"""Parse a Wireshark .pcapng and extract Pulsar HID Feature Reports.

Reads raw pcapng files using the block format directly (no external
dependencies). Extracts 64-byte HID payloads and annotates the known
Sonix protocol fields.

Usage:
    python3 tools/parse-capture.py capture.pcapng
    python3 tools/parse-capture.py capture.pcapng --vid 3710 --pid 5406
    python3 tools/parse-capture.py capture.pcapng --raw
"""

import argparse
import struct
import sys


# ── Sonix protocol field names ───────────────────────────────────────────────

DIRECTIONS = {0x00: 'CMD', 0x01: 'RSP'}

CATEGORIES = {
    0x01: 'poll',
    0x03: 'led',
    0x04: 'debounce',
    0x05: 'dpi',
    0x06: 'button',
    0x07: 'feature',
}

def describe_register(cat, reg):
    is_read = bool(reg & 0x80)
    base = reg & 0x7F
    rw = 'RD' if is_read else 'WR'
    return f'{rw} 0x{base:02x}'


def checksum_ok(data):
    if len(data) < 64:
        return False
    csum = struct.unpack_from('<H', data, 62)[0]
    return csum == (sum(data[:62]) & 0xFFFF)


def format_packet(data, raw=False):
    if len(data) < 64:
        return f'  [{len(data)} bytes — too short]'

    direction = DIRECTIONS.get(data[0], f'0x{data[0]:02x}')
    cat = data[1]
    reg = data[2]
    sub = data[3]
    profile = data[6]
    cat_name = CATEGORIES.get(cat, f'0x{cat:02x}')
    reg_desc = describe_register(cat, reg)
    csum = 'OK' if checksum_ok(data) else 'BAD'

    header = (f'  {direction:3s}  cat={cat_name:<8s}  {reg_desc}  '
              f'sub=0x{sub:02x}  profile={profile}  csum={csum}')

    payload = data[7:62]
    nonzero = [(i, b) for i, b in enumerate(payload) if b != 0]

    if raw or len(nonzero) > 16:
        hex_line = ' '.join(f'{b:02x}' for b in data)
        return f'{header}\n       {hex_line}'

    if not nonzero:
        return f'{header}  payload=empty'

    parts = [f'[{i+7}]=0x{b:02x}' for i, b in nonzero]
    return f'{header}  {" ".join(parts)}'


# ── pcapng parser (minimal, no dependencies) ─────────────────────────────────

def read_pcapng_blocks(f):
    """Yield (block_type, block_body) from a pcapng file."""
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        btype, blen = struct.unpack('<II', hdr)
        if blen < 12:
            return
        body = f.read(blen - 12)
        _trailing = f.read(4)
        yield btype, body


def parse_pcapng(path):
    """Extract USB HID payloads from a pcapng file.

    Yields (packet_num, timestamp_us, direction_hint, payload_bytes).
    direction_hint is 'OUT' (host→device) or 'IN' (device→host).
    """
    pkt_num = 0
    ts_resol = 1_000_000  # default: microseconds
    if_resols = {}

    with open(path, 'rb') as f:
        for btype, body in read_pcapng_blocks(f):
            # Section Header Block
            if btype == 0x0A0D0D0A:
                continue

            # Interface Description Block
            if btype == 0x00000001:
                if len(body) >= 4:
                    link_type = struct.unpack_from('<H', body, 0)[0]
                    if_id = len(if_resols)
                    # parse options for if_tsresol
                    resol = 1_000_000
                    opt_off = 8
                    while opt_off + 4 <= len(body):
                        oc, ol = struct.unpack_from('<HH', body, opt_off)
                        opt_off += 4
                        if oc == 0:
                            break
                        if oc == 9 and ol >= 1:  # if_tsresol
                            tsresol_byte = body[opt_off]
                            if tsresol_byte & 0x80:
                                resol = 2 ** (tsresol_byte & 0x7F)
                            else:
                                resol = 10 ** tsresol_byte
                        opt_off += ol
                        if opt_off % 4:
                            opt_off += 4 - (opt_off % 4)
                    if_resols[if_id] = resol
                continue

            # Enhanced Packet Block
            if btype == 0x00000006:
                if len(body) < 20:
                    continue
                if_id, ts_hi, ts_lo, cap_len, orig_len = struct.unpack_from(
                    '<IIIIII', body, 0)[:5]
                # fix: unpack 5 fields from 20 bytes
                if_id = struct.unpack_from('<I', body, 0)[0]
                ts_hi = struct.unpack_from('<I', body, 4)[0]
                ts_lo = struct.unpack_from('<I', body, 8)[0]
                cap_len = struct.unpack_from('<I', body, 12)[0]
                orig_len = struct.unpack_from('<I', body, 16)[0]

                pkt_data = body[20:20 + cap_len]
                pkt_num += 1

                resol = if_resols.get(if_id, 1_000_000)
                ts_raw = (ts_hi << 32) | ts_lo
                ts_us = ts_raw * 1_000_000 // resol if resol != 1_000_000 else ts_raw

                # USB pseudo-header: look for HID data
                # URB header is typically 27 bytes (Linux usbmon) or 28 bytes
                # The endpoint byte tells us direction
                if len(pkt_data) < 28:
                    continue

                # Try to find a 64-byte HID payload
                # USB transfer type 0x02 = control, endpoint direction in byte
                # Look for 64-byte chunks that match our protocol
                for offset in range(0, len(pkt_data) - 63):
                    candidate = pkt_data[offset:offset + 64]
                    # Quick check: first byte is 0x00 (CMD) or 0x01 (RSP),
                    # and checksum matches
                    if candidate[0] in (0x00, 0x01) and checksum_ok(candidate):
                        direction = 'OUT' if candidate[0] == 0x00 else 'IN'
                        yield pkt_num, ts_us, direction, candidate
                        break


def main():
    parser = argparse.ArgumentParser(
        description='Parse Pulsar HID Feature Reports from a pcapng capture')
    parser.add_argument('pcapng', help='Path to .pcapng file')
    parser.add_argument('--raw', action='store_true',
                        help='Show full hex dump of every packet')
    parser.add_argument('--vid', default='3710',
                        help='USB Vendor ID to filter (default: 3710)')
    parser.add_argument('--pid', default=None,
                        help='USB Product ID to filter (optional)')
    args = parser.parse_args()

    count = 0
    first_ts = None
    for pkt_num, ts_us, direction, data in parse_pcapng(args.pcapng):
        if first_ts is None:
            first_ts = ts_us
        rel_s = (ts_us - first_ts) / 1_000_000
        count += 1
        print(f'#{pkt_num:4d}  +{rel_s:8.3f}s  {direction}')
        print(format_packet(data, raw=args.raw))
        print()

    if count == 0:
        print('No Pulsar HID Feature Reports found in this capture.')
        print('Make sure you captured on the correct USB bus and the mouse')
        print('was communicating with Pulsar Fusion during the capture.')
        sys.exit(1)
    else:
        print(f'--- {count} packets extracted ---')


if __name__ == '__main__':
    main()
