"""Small ALSA sequencer wrapper for talking to the EC4.

ALSA seq is used directly (not rtmidi) because the setup dump is ~229 KB of
SysEx, which has to go out in fragments; ALSA seq allows that, and it also lets
the bridge share the EC4 with the RNBO runner (and a2jmidid) at the same time.
"""

from __future__ import annotations

import errno
import threading
import time
from typing import Callable

from ec4_remote import HEADER
from outqueue import OutQueue

ACK = HEADER + b"\xf7"  # F0 00 00 00 4E 2C 1B F7: the EC4's reply to a display message

from alsa_midi import (
    ControlChangeEvent,
    PortCaps,
    PortType,
    SequencerClient,
    SysExEvent,
)

CLIENT_NAME = "ec4bridge"


class EC4Midi:
    def __init__(self, match: str, on_cc: Callable[[int, int, int], None] | None = None,
                 on_sysex: Callable[[bytes], None] | None = None, log=print):
        self.match = match.lower()
        self.on_cc = on_cc
        self.on_sysex = on_sysex
        self.log = log
        self._in = SequencerClient(CLIENT_NAME + "-in")
        self._enlarge_input_buffer()
        self._out = SequencerClient(CLIENT_NAME + "-out")
        self._in_port = self._in.create_port("from EC4", caps=PortCaps.WRITE | PortCaps.SUBS_WRITE | PortCaps.NO_EXPORT,
                                             type=PortType.MIDI_GENERIC | PortType.APPLICATION)
        self._out_port = self._out.create_port("to EC4", caps=PortCaps.READ | PortCaps.SUBS_READ | PortCaps.NO_EXPORT,
                                               type=PortType.MIDI_GENERIC | PortType.APPLICATION)
        # NO_EXPORT: only this client may subscribe, so JACK's ALSA bridge (and with it the
        # RNBO runner) never sees these ports and our feedback CCs can't leak into a patch.
        self._out_lock = threading.Lock()
        # everything goes out through one background thread so reading encoders never waits
        self._queue = OutQueue(self._write_cc, self._write_sysex, on_error=log)
        self._device = None  # (client_id, port_id)
        self._sysex_buf = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, name="ec4-midi-in", daemon=True)
        self._thread.start()

    def _enlarge_input_buffer(self):
        """ALSA's default input queue is small; if Python is briefly busy (e.g. reading a large
        graph from the runner) knob messages overflow it and are lost ('No space left on device')."""
        try:
            pool = self._in.get_client_pool()
            pool.input_pool = max(pool.input_pool, 1000)
            self._in.set_client_pool(pool)
        except Exception as exc:  # pragma: no cover - depends on kernel limits
            self.log(f"could not enlarge MIDI input pool: {exc}")
        try:
            self._in.set_input_buffer_size(max(self._in.get_input_buffer_size(), 65536))
        except Exception as exc:  # pragma: no cover
            self.log(f"could not enlarge MIDI input buffer: {exc}")
        self._overruns = 0
        self._overrun_logged = 0.0
        self._received_cc = 0

    # ---- device discovery -------------------------------------------------
    def find_device(self):
        own = {self._in.client_id, self._out.client_id}
        for p in self._out.list_ports(output=True, input=True, include_midi_through=False):
            if p.client_id in own or (p.client_name or "").startswith(CLIENT_NAME):
                continue
            label = f"{p.client_name} {p.name}".lower()
            if self.match in label:
                return p
        return None

    @property
    def connected(self) -> bool:
        return self._device is not None

    def ensure_connected(self) -> bool:
        """Connect to the EC4 if present; detect unplugging. Returns True if a (re)connect happened."""
        dev = self.find_device()
        if dev is None:
            if self._device is not None:
                self.log("EC4 disconnected")
            self._device = None
            return False
        addr = (dev.client_id, dev.port_id)
        if addr == self._device:
            try:
                # a quick unplug/replug can keep the address but drops our subscriptions
                if self._out_port.list_subscribers() and self._in_port.list_subscribers():
                    return False
            except Exception:
                return False
            self.log("EC4 subscriptions lost, reconnecting")
        try:
            self._in_port.connect_from(addr)
        except Exception:
            pass  # already connected
        try:
            self._out_port.connect_to(addr)
        except Exception:
            pass
        self._device = addr
        self._queue.invalidate()  # whatever is on its screens now is unknown
        self.log(f"EC4 connected: {dev.client_name}:{dev.name} ({addr[0]}:{addr[1]})")
        return True

    # ---- output -----------------------------------------------------------
    # send_* only queue; the OutQueue thread writes (and drops superseded messages)
    def send_cc(self, channel: int, cc: int, value: int):
        if self._device:
            self._queue.cc(channel, cc, value)

    def send_ccs(self, msgs: list[tuple[int, int, int]], pause: float = 0.0):
        for ch, cc, v in msgs:
            self.send_cc(ch, cc, v)

    def send_sysex(self, data: bytes):
        """Queue one (short) SysEx message."""
        if self._device:
            self._queue.sysex(data)

    def flush(self, timeout: float = 5.0) -> bool:
        return self._queue.flush(timeout)

    # display text: only changes are sent, paced by the EC4's replies (see outqueue.py)
    def display_text(self, display: int, offset: int, text: str):
        if self._device:
            self._queue.set_text(display, offset, text)

    def overlay_visible(self, visible: bool):
        if self._device:
            self._queue.set_visible(visible)

    def take_stats(self) -> dict:
        """Messages written to / received from the EC4 since the last call."""
        s = dict(self._queue.stats)
        for k in self._queue.stats:
            self._queue.stats[k] = 0
        s["received_cc"], self._received_cc = self._received_cc, 0
        s["overruns"] = self._overruns
        return s

    def invalidate_display(self, display: int | None = None, visibility: bool = False):
        self._queue.invalidate(display, visibility)

    def _write_cc(self, channel: int, cc: int, value: int):
        if not self._device:
            return
        with self._out_lock:
            self._out.event_output(ControlChangeEvent(channel=channel, param=cc, value=value),
                                   port=self._out_port)
            self._out.drain_output()

    def _write_sysex(self, data: bytes):
        if not self._device:
            return
        with self._out_lock:
            for j in range(0, len(data), 256):
                self._out.event_output(SysExEvent(bytes(data[j:j + 256])), port=self._out_port)
            self._out.drain_output()

    def send_sysex_chunks(self, chunks: list[bytes], pause: float, progress=None):
        """Send one long SysEx message as consecutive fragments."""
        if not self._device:
            raise RuntimeError("EC4 not connected")
        self._queue.flush()  # nothing queued may end up inside the dump
        total = len(chunks)
        with self._out_lock:
            for i, chunk in enumerate(chunks):
                # ALSA splits/accepts sysex fragments; keep each event small
                for j in range(0, len(chunk), 256):
                    self._out.event_output(SysExEvent(bytes(chunk[j:j + 256])), port=self._out_port)
                self._out.drain_output()
                if pause:
                    time.sleep(pause)
                if progress and (i % 50 == 0 or i == total - 1):
                    progress(i + 1, total)

    # ---- input ------------------------------------------------------------
    def _reader(self):
        while not self._stop.is_set():
            try:
                # no timeout: alsa-midi's timeout path busy-polls; this is a daemon thread anyway
                ev = self._in.event_input()
            except Exception as exc:  # pragma: no cover - hardware path
                if getattr(exc, "errno", None) == errno.ENOSPC or "No space left" in str(exc):
                    # input queue overflowed: ALSA dropped some events; keep reading at once
                    self._overruns += 1
                    now = time.monotonic()
                    if now - self._overrun_logged > 10:
                        self._overrun_logged = now
                        self.log(f"MIDI input overflow ({self._overruns} so far): some EC4 messages were lost")
                    continue
                self.log(f"MIDI input error: {exc}")
                time.sleep(0.05)
                continue
            if ev is None:
                continue
            if isinstance(ev, ControlChangeEvent) and self.on_cc:
                self._received_cc += 1
                self.on_cc(ev.channel, ev.param, ev.value)
            elif isinstance(ev, SysExEvent):
                data = bytes(ev.data)
                if data[:1] == b"\xf0":
                    self._sysex_buf = bytearray()
                self._sysex_buf += data
                if data[-1:] == b"\xf7":
                    msg = bytes(self._sysex_buf)
                    self._sysex_buf = bytearray()
                    if msg == ACK:
                        self._queue.ack()  # the EC4's reply to each display message
                    if self.on_sysex:
                        self.on_sysex(msg)

    def close(self):
        self._queue.flush(2.0)  # let queued messages (e.g. a final 'hide pop-up') go out
        self._queue.close()
        self._stop.set()
        for c in (self._out,):  # the input client is left to die with the reader thread
            try:
                c.close()
            except Exception:
                pass
