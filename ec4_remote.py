"""EC4 remote commands (firmware 2.0+): live display text and setup/group reports.

Protocol taken from the DrivenByMoss Bitwig extension (LGPLv3), which drives the
EC4 display live: https://github.com/git-moss/DrivenByMoss
  src/main/java/de/mossgrabers/controller/faderfox/ec4/controller/EC4Display.java
  src/main/java/de/mossgrabers/controller/faderfox/ec4/controller/EC4ControlSurface.java

Every message is   F0 00 00 00 4E 2C 1B  <commands>  F7
and every command is 3 bytes: 0x4x, 0x20|high nibble, 0x10|low nibble.

  4E 22 1d             select display d (0 = encoder names page, 3 = 4x20 overlay)
  4A 2h 1l             set write position (0..63 on the names page, 0..79 on the overlay)
  4D 2h 1l             write one ASCII character, advancing the position
  4E 22 14 / 4E 22 15  show / hide the overlay
  F0 00 00 00 4E 20 10 F7   ask the EC4 for its state
The EC4 reports (and sends whenever you change them):
  4E 28 1s   current setup (0-based)      4E 24 1g   current group (0-based)
"""

from __future__ import annotations

HEADER = bytes([0xF0, 0x00, 0x00, 0x00, 0x4E, 0x2C, 0x1B])
REQUEST_INFO = bytes([0xF0, 0x00, 0x00, 0x00, 0x4E, 0x20, 0x10, 0xF7])

CMD_APP = 0x4E
APP_DISPLAY = 0x22
APP_GROUP = 0x24
APP_SETUP = 0x28

DISPLAY_NAMES = 0
DISPLAY_OVERLAY = 3
NAMES_LEN = 64      # 4 rows x 16 chars = 16 names x 4 chars
OVERLAY_LEN = 80    # 4 rows x 20 chars


def _nib(v: int) -> tuple[int, int]:
    return 0x20 | ((v >> 4) & 0x0F), 0x10 | (v & 0x0F)


def _ascii(text: str) -> bytes:
    return "".join(c if 32 <= ord(c) < 127 else " " for c in text).encode("ascii")


def write_text(display: int, offset: int, text: str) -> bytes:
    out = bytearray(HEADER)
    out += bytes([CMD_APP, APP_DISPLAY, 0x10 + display])
    out += bytes([0x4A, 0x20 + offset // 16, 0x10 + offset % 16])
    for b in _ascii(text):
        out += bytes([0x4D, *_nib(b)])
    out.append(0xF7)
    return bytes(out)


def names_page(names: list[str | None]) -> bytes:
    """Write 16 encoder names (4 chars each; None = blank) to the names page."""
    cells = [((n or "") + "    ")[:4] for n in (list(names) + [None] * 16)[:16]]
    return write_text(DISPLAY_NAMES, 0, "".join(cells))


def overlay_text(lines: list[str]) -> bytes:
    rows = [((l or "") + " " * 20)[:20] for l in (list(lines) + [""] * 4)[:4]]
    return write_text(DISPLAY_OVERLAY, 0, "".join(rows))


def overlay_show(visible: bool) -> bytes:
    return bytes([*HEADER, CMD_APP, APP_DISPLAY, 0x14 if visible else 0x15, 0xF7])


def parse_report(msg: bytes) -> dict | None:
    """Return {'setup': n, 'group': n} (either may be missing) for an EC4 state message."""
    if not msg.startswith(HEADER) or not msg.endswith(b"\xf7"):
        return None
    body = msg[len(HEADER):-1]
    out: dict = {}
    for i in range(0, len(body) - 2, 3):
        if body[i] != CMD_APP:
            break
        cmd, val = body[i + 1], body[i + 2]
        if cmd == APP_SETUP:
            out["setup"] = val - 0x10
        elif cmd == APP_GROUP:
            out["group"] = val - 0x10
    return out or None
