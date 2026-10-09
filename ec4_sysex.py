"""Faderfox EC4 setup dump: parse, modify and rebuild.

Format reference: the open-source Faderfox editor (MIT licensed),
https://github.com/privatepublic-de/faderfox-editor  (doc/ and ec4-v2/ec4.js).
Only firmware 2.x dumps are supported.

A dump is one SysEx message:
    F0 00 00 00
    41 <dev id>  42 <type=03>  43 <fw hi>  44 <fw lo>
    for each 64-byte page:
        49 <addr hi> 4A <addr lo>  (4D <byte>) x 64  4B <crc hi> 4C <crc lo>  00 x 30
    4F <dev id>  F7
where every <byte> is sent as two bytes: 0x20|high nibble, 0x10|low nibble.
"""

from __future__ import annotations

from dataclasses import dataclass

DEVICE_ID_EC4 = 0x0B
MEMORY_OFFSET = 0x0B00
MEMORY_SIZE = 0xF500
PAGE = 64
PADDING = bytes(30)

ADDR_SETUP_NAMES = 0x1BC0 - MEMORY_OFFSET
ADDR_GROUP_NAMES = 0x1C00 - MEMORY_OFFSET
ADDR_SETUP_DATA = 0x2000 - MEMORY_OFFSET
GROUP_LEN = 192  # bytes per group in the setup data area

# encoder field offsets inside a group block (each field is 16 bytes, one per encoder)
F_TYPE_CHANNEL = 0
F_NUMBER = 16  # bit 7 = link, bits 0-6 = CC number
F_NUMBER_H = 32
F_LOWER = 48
F_UPPER = 64
F_MODE_SCALE = 80  # mode high nibble, display scale low nibble
F_LIMIT_MSB = 96  # upper msb high nibble, lower msb low nibble
F_PB_TYPE_CHANNEL = 112  # push button type high nibble, channel low nibble
F_NAMES = 128  # 16 x 4 chars

TYPE_CC_ABS = 2
TYPE_CC_14BIT = 4
PB_TYPE_OFF = 0
PB_TYPE_GROUP = 6  # push button selects a group; its channel field holds the group (0-based)

SCALE_OFF = 0
SCALE_100 = 2
SCALE_1000 = 3

# EC4 "Display" setting: config name -> code
DISPLAY_SCALES = {
    "off": 0, "127": 1, "100": 2, "1000": 3,
    "+-63": 4, "+-50": 5, "+-500": 6, "onoff": 7, "9999": 8,
}
# which displays fit each resolution (the EC4 uses a 0..127 value range for the
# small scales and the full 14-bit range for the large ones)
DISPLAYS_7BIT = ("off", "127", "100", "+-63", "+-50", "onoff")
DISPLAYS_14BIT = ("off", "1000", "+-500", "9999")
DEFAULT_DISPLAY = {"7bit": "100", "14bit": "1000"}

ENCODER_MODES = {
    "Div8": 0, "Div4": 1, "Div2": 2,
    "Acc0": 3, "Acc1": 4, "Acc2": 5, "Acc3": 6,
    "LSp2": 7, "LSp4": 8, "LSp6": 9,
}

LIVE_NAME_PLACEHOLDER = "----"  # encoder name the EC4 lets a host overwrite live
NAME_CHARS = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ./-")


class DumpError(Exception):
    pass


@dataclass
class Dump:
    memory: bytearray
    fw_hi: int
    fw_lo: int

    @property
    def version(self) -> float:
        return self.fw_hi + self.fw_lo / 10


def _nib(v: int) -> tuple[int, int]:
    return 0x20 | ((v >> 4) & 0x0F), 0x10 | (v & 0x0F)


def parse_dump(data: bytes) -> Dump:
    """Parse an 'all setups' dump sent by the EC4 (or saved by the editor)."""
    if len(data) < 8 or data[0] != 0xF0:
        raise DumpError("not a SysEx message")
    if data[1] or data[2] or data[3]:
        raise DumpError("not a Faderfox dump (manufacturer id is not 00 00 00)")
    mem = bytearray(MEMORY_SIZE)
    fw_hi = fw_lo = None
    ix = 4
    page = bytearray()
    addr = 0
    crc = 0
    crc_in = 0
    pages = 0
    end = len(data)
    while True:
        while ix < end and data[ix] == 0:
            ix += 1  # skip padding
        if ix + 3 > end:
            raise DumpError("dump is incomplete (no end marker)")
        cmd = data[ix]
        val = ((data[ix + 1] & 0x0F) << 4) | (data[ix + 2] & 0x0F)
        ix += 3
        if cmd == 0x41:
            if val != DEVICE_ID_EC4:
                raise DumpError(f"dump is for device id {val:#x}, not the EC4")
        elif cmd == 0x42:
            if val != 0x03:
                raise DumpError("dump is not an 'all setups' dump; use 'Send all setups' on the EC4")
        elif cmd == 0x43:
            fw_hi = val
        elif cmd == 0x44:
            fw_lo = val
        elif cmd == 0x49:
            addr = val << 8
        elif cmd == 0x4A:
            addr |= val
        elif cmd == 0x4D:
            page.append(val)
            crc += val
        elif cmd == 0x4B:
            crc_in = val << 8
        elif cmd == 0x4C:
            crc_in |= val
            if crc_in != (crc & 0xFFFF):
                raise DumpError(f"checksum error in page at {addr:#06x}")
            off = addr - MEMORY_OFFSET
            if off < 0 or off + len(page) > MEMORY_SIZE:
                raise DumpError(f"page address {addr:#06x} out of range")
            mem[off:off + len(page)] = page
            pages += 1
            page = bytearray()
            crc = 0
        elif cmd == 0x4F:
            break
        elif cmd == 0xF7:
            raise DumpError("dump ended without stop command")
        # unknown commands are ignored, like the official editor does
    if fw_hi is None:
        raise DumpError("dump has no firmware version")
    if pages * PAGE != MEMORY_SIZE:
        raise DumpError(f"dump has {pages} pages, expected {MEMORY_SIZE // PAGE}")
    dump = Dump(mem, fw_hi, fw_lo or 0)
    if fw_hi < 2:
        raise DumpError(f"dump is from firmware {dump.version:.1f}; firmware 2.x is required")
    return dump


def build_dump(dump: Dump) -> bytes:
    """Serialize memory into an 'all setups' dump the EC4 can receive."""
    out = bytearray([0xF0, 0x00, 0x00, 0x00])
    out += bytes([0x41, *_nib(DEVICE_ID_EC4), 0x42, *_nib(0x03),
                  0x43, *_nib(dump.fw_hi), 0x44, *_nib(dump.fw_lo)])
    mem = dump.memory
    for pos in range(0, MEMORY_SIZE, PAGE):
        addr = pos + MEMORY_OFFSET
        out += bytes([0x49, *_nib(addr >> 8), 0x4A, *_nib(addr & 0xFF)])
        crc = 0
        for b in mem[pos:pos + PAGE]:
            out += bytes([0x4D, *_nib(b)])
            crc += b
        crc &= 0xFFFF
        out += bytes([0x4B, *_nib(crc >> 8), 0x4C, *_nib(crc & 0xFF)])
        out += PADDING
    out += bytes([0x4F, *_nib(DEVICE_ID_EC4), 0xF7])
    return bytes(out)


def clean_name(s: str) -> str:
    s = "".join(c if c in NAME_CHARS else " " for c in s)
    return (s + "    ")[:4]


def _group_base(setup: int, group: int) -> int:
    return ADDR_SETUP_DATA + (setup * 16 + group) * GROUP_LEN


def read_names(dump: Dump, setup: int) -> dict:
    """Return {'setup': str, 'groups': [str]*16, 'encoders': [[str]*16]*16} for a setup."""
    m = dump.memory
    def s(addr):
        return bytes(m[addr:addr + 4]).decode("latin-1")
    return {
        "setup": s(ADDR_SETUP_NAMES + setup * 4),
        "groups": [s(ADDR_GROUP_NAMES + setup * 64 + g * 4) for g in range(16)],
        "encoders": [[s(_group_base(setup, g) + F_NAMES + e * 4) for e in range(16)] for g in range(16)],
    }


def apply_layout(dump: Dump, setup: int, setup_name: str, group_names: list[str],
                 encoder_names: list[list[str | None]], *, cc_base: int, resolution: str,
                 mode: str, display: str | None = None, live_names: bool = False,
                 push_jumps: bool = False) -> None:
    """Program one setup (0-based) with the bridge's fixed MIDI scheme.

    Encoder e in group g sends CC (cc_base + e) on MIDI channel g+1, absolute mode.
    encoder_names[g][e] is a 4-char name, or None for an unused encoder (display off).
    Push buttons are switched off. Other setups are left untouched.
    display is the EC4 value display (see DISPLAY_SCALES); None = default for the resolution.
    live_names=True stores '----' as every encoder name and turns the value display on for every
    encoder: the EC4 only accepts names written live over SysEx on encoders named '----'
    (EC4 manual V03), and which encoders are in use changes with every graph.
    push_jumps=True makes encoder N's push button jump to group N (the EC4's own "Grp" type);
    otherwise push buttons are off.
    """
    if not 0 <= setup < 16:
        raise ValueError("setup must be 0..15")
    if resolution not in ("7bit", "14bit"):
        raise ValueError("resolution must be '7bit' or '14bit'")
    if resolution == "14bit" and cc_base + 15 > 31:
        raise ValueError("14-bit mode needs cc_base <= 16 (CC numbers 0..31)")
    if cc_base + 15 > 127:
        raise ValueError("cc_base too large")
    mode_code = ENCODER_MODES[mode]
    display = display or DEFAULT_DISPLAY[resolution]
    allowed = DISPLAYS_14BIT if resolution == "14bit" else DISPLAYS_7BIT
    if display not in allowed:
        raise ValueError(f"display '{display}' can't be used in {resolution} mode; use one of: {', '.join(allowed)}")
    display_code = DISPLAY_SCALES[display]
    m = dump.memory

    a = ADDR_SETUP_NAMES + setup * 4
    m[a:a + 4] = clean_name(setup_name).encode("latin-1")

    for g in range(16):
        a = ADDR_GROUP_NAMES + setup * 64 + g * 4
        m[a:a + 4] = clean_name(group_names[g] if g < len(group_names) else "").encode("latin-1")
        base = _group_base(setup, g)
        for e in range(16):
            name = encoder_names[g][e] if g < len(encoder_names) and e < len(encoder_names[g]) else None
            used = name is not None
            if resolution == "14bit":
                etype, scale = TYPE_CC_14BIT, display_code
                lower, upper, msbs = 0x00, 0xFF, 0xF0  # 0 .. 4095 (= 16383, full range)
            else:
                etype, scale = TYPE_CC_ABS, display_code
                lower, upper, msbs = 0, 127, 0x00
            if not used and not live_names:
                scale = SCALE_OFF
            m[base + F_TYPE_CHANNEL + e] = (etype << 4) | g
            m[base + F_NUMBER + e] = (cc_base + e) & 0x7F  # link bit cleared
            m[base + F_NUMBER_H + e] = 0
            m[base + F_LOWER + e] = lower
            m[base + F_UPPER + e] = upper
            m[base + F_MODE_SCALE + e] = (mode_code << 4) | scale
            m[base + F_LIMIT_MSB + e] = msbs
            if push_jumps:
                m[base + F_PB_TYPE_CHANNEL + e] = (PB_TYPE_GROUP << 4) | e
            else:
                m[base + F_PB_TYPE_CHANNEL + e] = (PB_TYPE_OFF << 4) | g
            a = base + F_NAMES + e * 4
            stored = LIVE_NAME_PLACEHOLDER if live_names else (name or "")
            m[a:a + 4] = clean_name(stored).encode("latin-1")


def split_for_sending(data: bytes) -> list[bytes]:
    """Split a dump into chunks that end after each page's padding, for paced sending."""
    chunks = []
    start = 0
    marker = PADDING
    i = data.find(marker, start)
    while i != -1:
        end = i + len(marker)
        chunks.append(data[start:end])
        start = end
        i = data.find(marker, start)
    if start < len(data):
        chunks.append(data[start:])
    return chunks
