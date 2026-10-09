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
and key events, followed by 4E 2E 11 (pressed) or 4E 2E 10 (released):
  4E 2A 1k   SHIFT + push button k (0-based)
  4E 26 1x   special key: 11 = SHIFT, 12..15 = user keys 1..4 (FUNC + encoder 1/5/9/13)
"""

from __future__ import annotations

HEADER = bytes([0xF0, 0x00, 0x00, 0x00, 0x4E, 0x2C, 0x1B])
REQUEST_INFO = bytes([0xF0, 0x00, 0x00, 0x00, 0x4E, 0x20, 0x10, 0xF7])

CMD_APP = 0x4E
APP_DISPLAY = 0x22
APP_GROUP = 0x24
APP_SETUP = 0x28
APP_EXT_KEY = 0x26
APP_SHIFTED_KEY = 0x2A
APP_KEY_STATE = 0x2E

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


def names_text(names: list[str | None]) -> str:
    """The names page as 64 characters (16 names x 4; None = blank)."""
    cells = [((n or "") + "    ")[:4] for n in (list(names) + [None] * 16)[:16]]
    return "".join(cells)


def names_page(names: list[str | None]) -> bytes:
    """Write 16 encoder names (4 chars each; None = blank) to the names page."""
    return write_text(DISPLAY_NAMES, 0, names_text(names))


def overlay_rows(lines: list[str]) -> str:
    """The overlay as 80 characters (4 rows x 20)."""
    return "".join(((l or "") + " " * 20)[:20] for l in (list(lines) + [""] * 4)[:4])


def overlay_text(lines: list[str]) -> bytes:
    return write_text(DISPLAY_OVERLAY, 0, overlay_rows(lines))


def diff_runs(old: str | None, new: str, merge_gap: int = 2) -> list[tuple[int, str]]:
    """Changed stretches of a screen as (offset, text). old=None means unknown: rewrite all.
    Stretches separated by at most merge_gap unchanged characters are joined (a new position
    command costs as much as a character)."""
    if old is None or len(old) != len(new):
        return [(0, new)] if new else []
    runs: list[list] = []
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b:
            continue
        if runs and i - (runs[-1][0] + len(runs[-1][1])) <= merge_gap:
            start = runs[-1][0]
            runs[-1][1] = new[start:i + 1]
        else:
            runs.append([i, b])
    return [(o, t) for o, t in runs]


def write_runs(display: int, runs: list[tuple[int, str]]) -> bytes:
    """One message writing several stretches of a display (like DrivenByMoss' diff writes)."""
    out = bytearray(HEADER)
    out += bytes([CMD_APP, APP_DISPLAY, 0x10 + display])
    for offset, text in runs:
        out += bytes([0x4A, 0x20 + offset // 16, 0x10 + offset % 16])
        for b in _ascii(text):
            out += bytes([0x4D, *_nib(b)])
    out.append(0xF7)
    return bytes(out)


def overlay_show(visible: bool) -> bytes:
    return bytes([*HEADER, CMD_APP, APP_DISPLAY, 0x14 if visible else 0x15, 0xF7])


def parse_report(msg: bytes) -> dict | None:
    """Parse an EC4 state/key message.

    Returns any of: 'setup', 'group' (0-based), 'shift_key' (0-based push button pressed with
    SHIFT), 'user_key' (1..4), 'shift' (True for the SHIFT key itself), 'pressed' (bool).
    """
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
        elif cmd == APP_SHIFTED_KEY:
            out["shift_key"] = val - 0x10
        elif cmd == APP_EXT_KEY:
            if val == 0x11:
                out["shift"] = True
            elif 0x12 <= val <= 0x15:
                out["user_key"] = val - 0x11
        elif cmd == APP_KEY_STATE:
            out["pressed"] = val == 0x11
    return out or None
