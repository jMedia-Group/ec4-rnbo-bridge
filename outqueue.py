"""Outgoing MIDI queue for the EC4.

Everything sent to the EC4 goes through one background thread, so the thread that reads
encoder turns never waits for the EC4 (which can be slow while it redraws its display).
When messages pile up, superseded ones are dropped before sending:
  - control changes: only the newest value per (channel, cc) is kept;
  - display writes: only the newest write to the same display area is kept, and only the
    newest show/hide of the pop-up.
"""

from __future__ import annotations

import threading
from typing import Callable

import ec4_remote


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
                 write_sysex: Callable[[bytes], None], on_error=print):
        self._write_cc = write_cc
        self._write_sysex = write_sysex
        self._on_error = on_error
        self._items: list[tuple] = []
        self._cond = threading.Condition()
        self._busy = False
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="ec4-midi-out", daemon=True)
        self._thread.start()

    def cc(self, channel: int, cc: int, value: int):
        self._put(("cc", channel, cc, value))

    def sysex(self, data: bytes):
        self._put(("sysex", bytes(data)))

    def _put(self, item):
        with self._cond:
            self._items.append(item)
            self._cond.notify()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until everything queued has been written."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._items and not self._busy, timeout)

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def _run(self):
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._items or self._stop)
                if self._stop:
                    return
                batch, self._items = coalesce(self._items), []
                self._busy = True
            try:
                for item in batch:
                    if item[0] == "cc":
                        self._write_cc(item[1], item[2], item[3])
                    else:
                        self._write_sysex(item[1])
            except Exception as exc:  # pragma: no cover - hardware path
                self._on_error(f"MIDI output error: {exc}")
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()
