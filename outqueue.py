"""Outgoing MIDI queue for the EC4.

Everything sent to the EC4 goes through one background thread, so the thread that reads
encoder turns never waits for the EC4.

Display text is the expensive part: the EC4 takes ~80 ms to process each display message
(it answers each one with F0 00 00 00 4E 2C 1B F7, seen with `ec4bridge.py monitor`), and while it's busy it also delays sending
encoder data. So display text is not queued message by message. Instead the queue keeps the
*desired* contents of each screen and sends one message at a time with only the characters
that changed (like DrivenByMoss does), waiting for the EC4's reply (or a short timeout) before
sending the next. However fast the bridge updates a pop-up, the EC4 only ever gets as much as
it can handle, and always the latest state.

Control changes and other SysEx are still coalesced: only the newest value per (channel, cc).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import ec4_remote

SCREEN_SIZE = {ec4_remote.DISPLAY_NAMES: ec4_remote.NAMES_LEN,
               ec4_remote.DISPLAY_OVERLAY: ec4_remote.OVERLAY_LEN}


def sysex_key(data: bytes):
    """Coalescing key for EC4 remote SysEx, or None if every copy must be sent."""
    h = ec4_remote.HEADER
    if not data.startswith(h) or len(data) < len(h) + 4:
        return None
    body = data[len(h):]
    if body[0] == ec4_remote.CMD_APP and body[1] == ec4_remote.APP_DISPLAY:
        if body[2] in (0x14, 0x15):
            return ("overlay-visible",)
        if len(body) >= 6 and body[3] == 0x4A:
            # display + start position + length identify the area being written
            return ("text", body[2], body[4], body[5], len(data))
    return None


def coalesce(items: list[tuple]) -> list[tuple]:
    """Keep the last of each superseded message, in the order of those last copies."""
    last: dict = {}
    for i, item in enumerate(items):
        if item[0] == "cc":
            key = ("cc", item[1], item[2])
        else:
            key = sysex_key(item[1])
            if key is None:
                key = ("unique", i)
        last[key] = i
    keep = sorted(last.values())
    return [items[i] for i in keep]


class OutQueue:
    def __init__(self, write_cc: Callable[[int, int, int], None],
                 write_sysex: Callable[[bytes], None], on_error=print, ack_timeout: float = 0.15):
        self._write_cc = write_cc
        self._write_sysex = write_sysex
        self._on_error = on_error
        self.ack_timeout = ack_timeout
        self._items: list[tuple] = []
        self._cond = threading.Condition()
        self._busy = False
        self._stop = False
        # display state: what each screen should show, and what the EC4 has (None = unknown)
        self._desired: dict[int, list[str] | None] = {d: None for d in SCREEN_SIZE}
        self._shadow: dict[int, str | None] = {d: None for d in SCREEN_SIZE}
        self._vis_desired: bool | None = None
        self._vis_sent: bool | None = None
        self._await_until = 0.0  # waiting for the EC4's reply to a display message until then
        self._hold_until = 0.0   # no display messages until then (a knob is being turned)
        self.display_messages = 0  # for tests / diagnostics
        self.stats = {"cc": 0, "sysex": 0, "display": 0}  # messages written (diagnostics)
        self._thread = threading.Thread(target=self._run, name="ec4-midi-out", daemon=True)
        self._thread.start()

    # ---- producers ---------------------------------------------------------
    def cc(self, channel: int, cc: int, value: int):
        self._put(("cc", channel, cc, value))

    def sysex(self, data: bytes):
        self._put(("sysex", bytes(data)))

    def set_text(self, display: int, offset: int, text: str):
        """Desired contents of part of a screen; only changes are sent, when the EC4 is ready."""
        size = SCREEN_SIZE[display]
        with self._cond:
            buf = self._desired[display]
            if buf is None:
                base = self._shadow[display] or " " * size
                buf = self._desired[display] = list(base)
            for i, ch in enumerate(text):
                if 0 <= offset + i < size:
                    buf[offset + i] = ch if 32 <= ord(ch) < 127 else " "
            self._cond.notify()

    def set_visible(self, visible: bool):
        with self._cond:
            self._vis_desired = bool(visible)
            self._cond.notify()

    def invalidate(self, display: int | None = None, visibility: bool = False):
        """Forget what the EC4 shows (it redrew a screen itself), so the next update is complete."""
        with self._cond:
            for d in (SCREEN_SIZE if display is None else [display]):
                self._shadow[d] = None
            if visibility or display is None:
                self._vis_sent = None
            self._cond.notify()

    def hold_display(self, seconds: float):
        """A knob is moving: send nothing to the EC4 for `seconds` (it stops reading its
        encoders while it processes incoming data, especially drawing). Called on every turn,
        so it lasts until the knob rests; queued values are coalesced and go out afterwards."""
        with self._cond:
            self._hold_until = max(self._hold_until, time.monotonic() + seconds)
            self._cond.notify()

    def ack(self):
        """The EC4 answered (it does after each display message): ready for the next one."""
        with self._cond:
            self._await_until = 0.0
            self._cond.notify()

    def _put(self, item):
        with self._cond:
            self._items.append(item)
            self._cond.notify()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued (including display updates) has been written."""
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._items and not self._busy and not self._display_pending(), timeout)

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    # ---- display diffing (call with the lock held) -------------------------
    def _display_pending(self) -> bool:
        for d, buf in self._desired.items():
            if buf is not None and "".join(buf) != self._shadow[d]:
                return True
        return self._vis_desired is not None and self._vis_desired != self._vis_sent

    def _next_display_message(self) -> bytes | None:
        for d in (ec4_remote.DISPLAY_NAMES, ec4_remote.DISPLAY_OVERLAY):
            buf = self._desired[d]
            if buf is None:
                continue
            new = "".join(buf)
            runs = ec4_remote.diff_runs(self._shadow[d], new)
            if runs:
                self._shadow[d] = new
                return ec4_remote.write_runs(d, runs)
        if self._vis_desired is not None and self._vis_desired != self._vis_sent:
            self._vis_sent = self._vis_desired
            return ec4_remote.overlay_show(self._vis_desired)
        return None

    # ---- worker ------------------------------------------------------------
    def _run(self):
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    now = time.monotonic()
                    held = now < self._hold_until
                    disp_at = max(self._await_until, self._hold_until)
                    pending = self._display_pending()
                    if (self._items and not held) or (pending and now >= disp_at):
                        break
                    deadlines = []
                    if self._items and held:
                        deadlines.append(self._hold_until)
                    if pending:
                        deadlines.append(disp_at)
                    self._cond.wait(max(0.0, min(deadlines) - now) if deadlines else None)
                if time.monotonic() >= self._hold_until:
                    batch, self._items = coalesce(self._items), []
                else:
                    batch = []
                disp = None
                if time.monotonic() >= max(self._await_until, self._hold_until):
                    disp = self._next_display_message()
                    if disp is not None:
                        self._await_until = time.monotonic() + self.ack_timeout
                        self.display_messages += 1
                self._busy = True
            try:
                for item in batch:
                    if item[0] == "cc":
                        self._write_cc(item[1], item[2], item[3])
                        self.stats["cc"] += 1
                    else:
                        self._write_sysex(item[1])
                        self.stats["sysex"] += 1
                if disp is not None:
                    self._write_sysex(disp)
                    self.stats["display"] += 1
            except Exception as exc:  # pragma: no cover - hardware path
                self._on_error(f"MIDI output error: {exc}")
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()
