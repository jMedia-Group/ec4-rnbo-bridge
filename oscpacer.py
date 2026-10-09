"""Limit how often each parameter is sent to the RNBO runner.

A fast turn on the EC4 produces a message every 2-3 ms. Sending each one to the runner as
an OSC message makes it apply hundreds of changes a second per parameter. The pacer sends at
most one message per parameter every `interval` seconds: the first change goes out at once,
later ones within the interval are merged and the newest value goes out when it's due. The
final value of a turn is never lost.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class OscPacer:
    def __init__(self, send: Callable[[str, float], None], interval: float):
        self._send = send
        self.interval = max(0.0, interval)
        self._last: dict[str, float] = {}
        self._pending: dict[str, tuple[str, float]] = {}
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="osc-pacer", daemon=True)
        self._thread.start()

    def submit(self, key: str, address: str, value: float):
        now = time.monotonic()
        with self._cond:
            if self.interval <= 0 or (key not in self._pending
                                       and now - self._last.get(key, -1e9) >= self.interval):
                self._last[key] = now
                send_now = True
            else:
                self._pending[key] = (address, value)  # newest value replaces older ones
                self._cond.notify()
                send_now = False
        if send_now:
            self._send(address, value)

    def flush(self, timeout: float = 2.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._pending, timeout)

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def _run(self):
        while True:
            due: list[tuple[str, float]] = []
            with self._cond:
                while True:
                    if self._stop:
                        return
                    now = time.monotonic()
                    ready = [k for k in self._pending if now - self._last.get(k, -1e9) >= self.interval]
                    if ready:
                        for k in ready:
                            due.append(self._pending.pop(k))
                            self._last[k] = now
                        self._cond.notify_all()
                        break
                    if self._pending:
                        wait = min(self._last[k] + self.interval for k in self._pending) - now
                        self._cond.wait(max(0.0005, wait))
                    else:
                        self._cond.wait()
            for address, value in due:
                try:
                    self._send(address, value)
                except OSError:
                    pass
