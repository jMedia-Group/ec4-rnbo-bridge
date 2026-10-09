#!/usr/bin/env python3
"""Faderfox EC4 <-> RNBO runner bridge.

Maps every visible parameter of every loaded RNBO instance to an EC4 encoder,
sends encoder moves to the runner over OSC, and sends value changes back to the
EC4 so its display and encoder positions stay in sync. It can also write the
parameter names into an EC4 setup (via a SysEx setup dump).

Subcommands:
  run              start the bridge (what the systemd service runs)
  list             print the current parameter -> encoder layout
  capture-backup   receive 'Send all setups' from the EC4 and save it
  make-syx         write an EC4 dump with the current layout and names
  send-layout      make the dump and send it to the EC4 (EC4 must be in Receive)
  test-display     check that the EC4 accepts live display text (firmware 2.0+)
  monitor          show what the EC4 sends as you turn encoders (timing, skipped steps)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.error

import ec4_remote
import ec4_sysex
from layout import Layout, Slot, build_layout, format_table
from rnbo import Param, fetch_tree, parse_params

log = logging.getLogger("ec4bridge")

DEFAULTS = {
    "runner_host": "127.0.0.1",
    "oscquery_port": 5678,
    "osc_port": 1234,
    "listen_port": 9123,
    "listen_ip": "auto",
    "midi_port": "EC4",
    "ec4_setup": 16,
    "setup_name": "RNBO",
    "resolution": "7bit",
    "encoder_mode": "Acc1",
    "display": "",
    "cc_base": 16,
    "poll_interval": 2.0,
    "feedback_holdoff_ms": 1000,
    "new_group_per_instance": True,
    "include": [],
    "exclude": [],
    "names": {},
    "group_names": {},
    "strip_prefixes": [],
    "name_case": "keep",
    "group_title_style": "instance",
    "hide_devices": [],
    "backup_syx": "ec4-backup.syx",
    "layout_syx": "ec4-layout.syx",
    "layout_txt": "layout.txt",
    "pause_file": "ec4bridge.pause",
    "sysex_page_pause_ms": 2,
    "live_names": True,
    "live_names_refresh": 0,
    "zero_unused": True,
    "value_popup": True,
    "value_popup_seconds": 1.5,
    "notify_graph_change": True,
    "notify_seconds": 2.5,
    "notify_group_change": True,
    "push_jumps_to_group": True,
    "device_list_key": "shift+16",
    "device_list_seconds": 8,
    "device_list_mode": "momentary",
    "device_list_page_seconds": 2,
}


def parse_list_key(key: str):
    """'shift+16' -> ('shift', 15); 'user1' -> ('user', 1); anything else -> None."""
    key = (key or "").strip().lower().replace(" ", "")
    if key.startswith("shift+") and key[6:].isdigit() and 1 <= int(key[6:]) <= 16:
        return ("shift", int(key[6:]) - 1)
    if key.startswith("user") and key[4:].isdigit() and 1 <= int(key[4:]) <= 4:
        return ("user", int(key[4:]))
    return None


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
    if path and os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        unknown = set(user) - set(DEFAULTS) - {"_comment"}
        if unknown:
            log.warning("unknown config keys ignored: %s", ", ".join(sorted(unknown)))
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    elif path:
        log.warning("config file %s not found, using defaults", path)
    for k in ("backup_syx", "layout_syx", "layout_txt", "pause_file"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(base, cfg[k])
    if not 1 <= int(cfg["ec4_setup"]) <= 16:
        raise SystemExit("ec4_setup must be 1..16")
    if cfg["resolution"] not in ("7bit", "14bit"):
        raise SystemExit("resolution must be '7bit' or '14bit'")
    disp = cfg["display"] = str(cfg["display"] or "").strip().lower()
    if disp:
        ok = ec4_sysex.DISPLAYS_14BIT if cfg["resolution"] == "14bit" else ec4_sysex.DISPLAYS_7BIT
        if disp not in ok:
            raise SystemExit(f"display '{disp}' can't be used with resolution {cfg['resolution']}; "
                             f"use one of: {', '.join(ok)}")
    from layout import NAME_CASES
    cfg["name_case"] = str(cfg["name_case"] or "keep").strip().lower()
    if cfg["name_case"] not in NAME_CASES:
        raise SystemExit("name_case must be one of " + ", ".join(NAME_CASES))
    if not isinstance(cfg["hide_devices"], list):
        raise SystemExit('hide_devices must be a list, e.g. ["reverb", "2"]')
    import re as _re
    for pat in cfg["hide_devices"]:
        if not str(pat).strip().isdigit():
            try:
                _re.compile(str(pat))
            except _re.error as exc:
                raise SystemExit(f"hide_devices entry {pat!r} is not a valid pattern: {exc}")
    key = cfg["device_list_key"] = str(cfg["device_list_key"] or "off").strip().lower().replace(" ", "")
    if parse_list_key(key) is None and key != "off":
        raise SystemExit('device_list_key must be "shift+1".."shift+16", "user1".."user4" or "off"')
    cfg["device_list_mode"] = str(cfg["device_list_mode"] or "momentary").strip().lower()
    if cfg["device_list_mode"] not in ("momentary", "toggle"):
        raise SystemExit('device_list_mode must be "momentary" or "toggle"')
    from layout import GROUP_TITLE_STYLES
    cfg["group_title_style"] = str(cfg["group_title_style"] or "instance").strip().lower()
    if cfg["group_title_style"] not in GROUP_TITLE_STYLES:
        raise SystemExit("group_title_style must be one of " + ", ".join(GROUP_TITLE_STYLES))
    if cfg["encoder_mode"] not in ec4_sysex.ENCODER_MODES:
        raise SystemExit("encoder_mode must be one of " + ", ".join(ec4_sysex.ENCODER_MODES))
    top = 31 if cfg["resolution"] == "14bit" else 127
    if not 0 <= int(cfg["cc_base"]) <= top - 15:
        raise SystemExit(f"cc_base must be 0..{top - 15} for {cfg['resolution']} mode")
    return cfg


def local_ip_for(host: str) -> str:
    if host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def fetch_params(cfg: dict) -> list[Param]:
    return parse_params(fetch_tree(cfg["runner_host"], cfg["oscquery_port"]))


class Bridge:
    """All the mapping logic. MIDI and OSC transports are injected so it can be tested."""

    def __init__(self, cfg: dict, midi=None, osc_send=None):
        self.cfg = cfg
        self.midi = midi
        self.osc_send = osc_send  # callable(address, float)
        self.lock = threading.RLock()
        self.layout = Layout([], [""] * 16, [])
        self._sig = None
        self.by_cc: dict[tuple[int, int], Slot] = {}
        self.by_addr: dict[str, Slot] = {}
        self.values: dict[str, float] = {}
        self.touched: dict[str, float] = {}
        self.sent: dict[str, float] = {}
        self.msb: dict[tuple[int, int], int] = {}
        self.cc_base = int(cfg["cc_base"])
        self.hi_res = cfg["resolution"] == "14bit"
        self.holdoff = cfg["feedback_holdoff_ms"] / 1000.0
        self.my_setup = int(cfg["ec4_setup"]) - 1
        self.cur_setup: int | None = None  # as reported by the EC4 (0-based); None = unknown
        self.cur_group: int | None = None
        self._overlay_timer: threading.Timer | None = None
        self.hold = False  # True while this process itself is transferring a dump
        self._list_page: int | None = None  # device list page on screen, None = closed
        self._list_until = 0.0
        self._list_held = False
        self._rotate_timer: threading.Timer | None = None
        self.by_raw: dict[str, Slot] = {}
        self._polled: dict[str, float] = {}  # normalized values seen in the last poll
        self.raw: dict[str, object] = {}  # latest exact raw value per parameter key
        self._popup_pending: tuple[Slot, object] | None = None
        self._popup_last = 0.0
        self._popup_timer: threading.Timer | None = None
        self._overlay_visible = False
        self._overlay_owner = None
        self._overlay_lines: list[str] = [""] * 4
        self._was_paused = False

    # ---- layout ------------------------------------------------------------
    def update_from_params(self, params: list[Param]) -> bool:
        """Apply a fresh parameter list. Returns True if the layout changed."""
        layout = build_layout(params, self.cfg)
        sig = layout.signature()
        with self.lock:
            changed = sig != self._sig
            if changed:
                self._sig = sig
                self.layout = layout
                self.by_cc = {(s.group, self.cc_base + s.encoder): s for s in layout.slots}
                self.by_addr = {s.param.address: s for s in layout.slots}
                self.by_raw = {s.param.raw_address: s for s in layout.slots}
                self.values = {s.param.key: s.param.normalized for s in layout.slots}
                self.raw = {s.param.key: s.param.value for s in layout.slots}
                self.msb.clear()
            else:
                # catch value changes the OSC listener might have missed. Only trust a polled
                # value once it's the same in two polls in a row: a snapshot taken while a knob
                # is moving can be older than what the EC4 already shows.
                now = time.monotonic()
                for s in layout.slots:
                    k = s.param.key
                    prev, self._polled[k] = self._polled.get(k), s.param.normalized
                    if s.param.value is not None and now - self.touched.get(k, 0) >= 1.0:
                        self.raw[k] = s.param.value
                    old = self.values.get(k)
                    if old is None or abs(old - s.param.normalized) < 1e-6:
                        continue
                    if prev is None or abs(prev - s.param.normalized) > 1e-6:
                        continue  # still changing (or first look): wait for the next poll
                    if now - self.touched.get(k, 0) < max(self.holdoff, 1.0) + float(self.cfg["poll_interval"]):
                        continue
                    self.values[k] = s.param.normalized
                    self._feedback(self.by_addr[s.param.address], s.param.normalized)
        if changed:
            self._on_layout_changed()
        return changed

    def _on_layout_changed(self):
        n = len(self.layout.slots)
        groups = len({s.group for s in self.layout.slots})
        log.info("layout: %d parameter(s) on %d group(s)", n, groups)
        table = format_table(self.layout)
        for line in table.splitlines():
            log.info("  %s", line)
        try:
            with open(self.cfg["layout_txt"], "w") as f:
                f.write(table + "\n")
        except OSError as exc:
            log.warning("could not write %s: %s", self.cfg["layout_txt"], exc)
        first = not getattr(self, "_seen_layout", False)
        self._seen_layout = True
        if os.path.exists(self.cfg["backup_syx"]):
            try:
                write_layout_syx(self.cfg, self.layout)
                if not self.cfg["live_names"]:
                    log.info("EC4 names changed: run 'ec4bridge.py send-layout' to put them on the display")
            except Exception as exc:  # keep running even if the backup is bad
                log.warning("could not build layout dump: %s", exc)
        self.resync()
        self.write_names()
        if not first and self.cfg["notify_graph_change"]:
            self.notify(self._graph_summary())

    # ---- EC4 setup/group state and live display --------------------------------
    def on_my_setup(self) -> bool:
        return self.cur_setup is None or self.cur_setup == self.my_setup

    def on_sysex(self, msg: bytes):
        rep = ec4_remote.parse_report(msg)
        if not rep:
            return
        if "pressed" in rep and "setup" not in rep and "group" not in rep:
            self._on_key(rep)
            return
        with self.lock:
            was_mine = self.on_my_setup() and self.cur_setup is not None
            old_group = self.cur_group
            self.cur_setup = rep.get("setup", self.cur_setup)
            self.cur_group = rep.get("group", self.cur_group)
            mine = self.on_my_setup()
        if mine and not was_mine:
            log.info("EC4 is on the RNBO setup (%d), group %s", self.my_setup + 1,
                     "?" if self.cur_group is None else self.cur_group + 1)
            self.resync()
            self.write_names()
        elif mine and self.cur_group != old_group:
            self.write_names()
            list_was_open = self._list_page is not None
            self._list_page = None
            if old_group is not None and self.cfg["notify_group_change"] and self.cur_group is not None:
                self.notify(self._group_summary(self.cur_group))
            elif list_was_open:
                self._hide_overlay()
        elif was_mine and not mine:
            log.info("EC4 switched to setup %d; pausing until it's back on setup %d",
                     self.cur_setup + 1, self.my_setup + 1)

    def paused(self) -> bool:
        """True while a setup dump is going to/from the EC4 (send-layout / capture-backup).

        Anything else sent to the EC4 during a dump (a value CC, a name update) lands inside
        the SysEx stream and the EC4 rejects the whole dump with 'receive error'.
        """
        if self.hold:
            return True
        try:
            return time.time() - os.path.getmtime(self.cfg["pause_file"]) < 600  # ignore stale files
        except (OSError, KeyError):
            return False

    def _send_sysex(self, data: bytes):
        if self.midi is not None and getattr(self.midi, "connected", True) and not self.paused():
            self.midi.send_sysex(data)

    def request_ec4_state(self):
        self._send_sysex(ec4_remote.REQUEST_INFO)

    def write_names(self):
        """Put the current group's parameter names on the EC4 display (firmware 2.0+)."""
        if not self.cfg["live_names"] or not self.on_my_setup():
            return
        with self.lock:
            g = self.cur_group if self.cur_group is not None else 0
            names = self.layout.encoder_names()[g] if 0 <= g < 16 else [None] * 16
        self._send_sysex(ec4_remote.names_page(names))

    def _graph_summary(self) -> list[str]:
        from layout import strip_prefixes
        prefixes = self.cfg.get("strip_prefixes") or []
        seen, lines = set(), []
        for s in self.layout.slots:
            if s.param.inst in seen:
                continue
            seen.add(s.param.inst)
            lines.append(f"{s.group + 1:>2} {strip_prefixes(s.param.inst_name, prefixes)}")
        if not lines:
            return ["RNBO graph loaded", "no parameters"]
        if len(lines) > 3:
            lines = lines[:2] + [f"   +{len(lines) - 2} more"]
        return ["RNBO graph loaded"] + lines

    def _group_summary(self, g: int) -> list[str]:
        """Overlay text for a group: its instance's full name (group names can't be written live)."""
        from layout import strip_prefixes
        prefixes = self.cfg.get("strip_prefixes") or []
        with self.lock:
            in_group = [s for s in self.layout.slots if s.group == g]
            if not in_group:
                return [f"Group {g + 1}", "(no parameters)"]
            inst = in_group[0].param.inst
            name = strip_prefixes(in_group[0].param.inst_name, prefixes)
            pages = sorted({s.group for s in self.layout.slots if s.param.inst == inst})
        lines = [f"Group {g + 1}", name[:20]]
        if len(pages) > 1:
            lines.append(f"page {pages.index(g) + 1} of {len(pages)}")
        return lines

    def notify(self, lines: list[str], seconds: float | None = None, owner=None):
        """Show a short message on the EC4's 4x20 overlay.

        owner identifies what's on the overlay (e.g. the value pop-up of one parameter), so a
        follow-up update can rewrite only the lines that changed.
        """
        if not self.on_my_setup() or self.midi is None:
            return
        self._send_sysex(ec4_remote.overlay_text(lines))
        self._send_sysex(ec4_remote.overlay_show(True))
        self._overlay_visible = True
        self._overlay_owner = owner
        self._overlay_lines = [((l or "") + " " * 20)[:20] for l in (list(lines) + [""] * 4)[:4]]
        self._arm_overlay_timer(float(self.cfg["notify_seconds"] if seconds is None else seconds))

    def _arm_overlay_timer(self, secs: float):
        if self._overlay_timer:
            self._overlay_timer.cancel()
        self._overlay_timer = threading.Timer(secs, self._overlay_timeout)
        self._overlay_timer.daemon = True
        self._overlay_timer.start()

    def _overlay_timeout(self):
        self._overlay_visible = False
        self._overlay_owner = None
        self._list_page = None
        self._list_held = False
        if self._rotate_timer:
            self._rotate_timer.cancel()
        self._send_sysex(ec4_remote.overlay_show(False))

    def _hide_overlay(self):
        if self._overlay_timer:
            self._overlay_timer.cancel()
        self._overlay_timeout()

    # ---- value pop-up while turning an encoder ---------------------------------
    @staticmethod
    def format_value(v) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        try:
            v = float(v)
        except (TypeError, ValueError):
            return str(v)
        av = abs(v)
        if v.is_integer() and av < 1e6:
            return str(int(v))
        if av >= 1000:
            return f"{v:.0f}"
        if av >= 100:
            return f"{v:.1f}"
        if av >= 10:
            return f"{v:.2f}"
        return f"{v:.3f}"

    def value_popup_lines(self, slot: Slot, raw) -> list[str]:
        from layout import strip_prefixes
        p = slot.param
        norm = min(1.0, max(0.0, self.values.get(p.key, p.normalized)))
        if raw is None:
            raw = self.raw.get(p.key)
        filled = round(norm * 15)
        bar = "#" * filled + "." * (15 - filled) + f"{round(norm * 100):>4}%"
        device = strip_prefixes(p.inst_name, self.cfg.get("strip_prefixes") or [])
        return [p.label[:20], self.format_value(raw)[:20], bar, device[:20]]

    def value_popup(self, slot: Slot, raw):
        """Full parameter name, value, bar and device on the overlay (throttled to ~12/s)."""
        if not self.cfg.get("value_popup", True) or self._list_held:
            return
        if not self.on_my_setup() or self.paused() or self.midi is None:
            return
        self._popup_pending = (slot, raw)
        wait = 0.08 - (time.monotonic() - self._popup_last)
        if wait <= 0:
            self._flush_popup()
        elif not (self._popup_timer and self._popup_timer.is_alive()):
            self._popup_timer = threading.Timer(wait, self._flush_popup)
            self._popup_timer.daemon = True
            self._popup_timer.start()

    def _flush_popup(self):
        pending, self._popup_pending = self._popup_pending, None
        if pending is None or self._list_held:
            return
        self._popup_last = time.monotonic()
        slot, raw = pending
        lines = self.value_popup_lines(slot, raw)
        secs = float(self.cfg["value_popup_seconds"])
        owner = ("value", slot.param.key)
        if self._overlay_visible and self._overlay_owner == owner and self.on_my_setup():
            # same knob still turning: rewrite only the value and bar lines (rows 2-3)
            rows = [((l or "") + " " * 20)[:20] for l in lines]
            if rows[1:3] != self._overlay_lines[1:3]:
                self._send_sysex(ec4_remote.write_text(ec4_remote.DISPLAY_OVERLAY, 20, rows[1] + rows[2]))
                self._send_sysex(ec4_remote.overlay_show(True))
                self._overlay_lines[1:3] = rows[1:3]
            self._arm_overlay_timer(secs)
        else:
            self.notify(lines, seconds=secs, owner=owner)

    # ---- device list (SHIFT + push encoder 16 by default) ----------------------
    def _is_list_key(self, rep: dict) -> bool:
        key = parse_list_key(self.cfg.get("device_list_key", ""))
        if key is None:
            return False
        kind, n = key
        return (kind == "shift" and rep.get("shift_key") == n) or (kind == "user" and rep.get("user_key") == n)

    def device_list_pages(self) -> list[list[str]]:
        """Overlay pages listing 'group device' entries, 8 per page (2 columns x 4 rows)."""
        from layout import strip_prefixes
        prefixes = self.cfg.get("strip_prefixes") or []
        with self.lock:
            slots = list(self.layout.slots)
        groups: dict[int, Slot] = {}
        for s in slots:
            groups.setdefault(s.group, s)
        pages_of: dict[int, list[int]] = {}
        for g, s in sorted(groups.items()):
            pages_of.setdefault(s.param.inst, []).append(g)
        entries = []
        for g, s in sorted(groups.items()):
            name = strip_prefixes(s.param.inst_name, prefixes)
            own = pages_of[s.param.inst]
            if len(own) > 1:
                name = name[:6] + str(own.index(g) + 1)
            entries.append(f"{g + 1:>2} {name[:7]:<7}")
        if not entries:
            return [["No devices loaded"]]
        pages = []
        for i in range(0, len(entries), 8):
            chunk = entries[i:i + 8]
            lines = ["".join(chunk[j:j + 2]) for j in range(0, len(chunk), 2)]
            pages.append(lines)
        return pages

    def _on_key(self, rep: dict):
        """Key events from the EC4: open/close the device list."""
        is_list_key = self._is_list_key(rep)
        momentary = self.cfg.get("device_list_mode", "momentary") == "momentary"
        if rep["pressed"]:
            if is_list_key and self.on_my_setup():
                if momentary:
                    self.hold_device_list()
                else:
                    self.toggle_device_list()
        elif momentary and self._list_held and (is_list_key or rep.get("shift")):
            # released the push button, or let go of SHIFT first
            self._hide_overlay()

    def hold_device_list(self):
        """Momentary: show the list while the key is held; pages flip by themselves."""
        self._list_held = True
        self._show_list_page(0)

    def _show_list_page(self, page: int):
        pages = self.device_list_pages()
        page %= len(pages)
        # the timeout is only a safety net in case the release never arrives
        self.notify(pages[page], seconds=float(self.cfg["device_list_seconds"]))
        self._list_page = page
        self._list_held = True
        if self._rotate_timer:
            self._rotate_timer.cancel()
        if len(pages) > 1:
            self._rotate_timer = threading.Timer(float(self.cfg["device_list_page_seconds"]),
                                                 self._rotate_list)
            self._rotate_timer.daemon = True
            self._rotate_timer.start()

    def _rotate_list(self):
        if self._list_held and self._list_page is not None:
            self._show_list_page(self._list_page + 1)

    def toggle_device_list(self):
        """First press shows the list, further presses page through it, then close it."""
        pages = self.device_list_pages()
        now = time.monotonic()
        if self._list_page is not None and now < self._list_until:
            nxt = self._list_page + 1
            if nxt >= len(pages):
                self._hide_overlay()
                return
        else:
            nxt = 0
        secs = float(self.cfg["device_list_seconds"])
        self.notify(pages[nxt], seconds=secs)
        self._list_page = nxt
        self._list_until = now + secs

    # ---- EC4 -> runner -----------------------------------------------------
    def on_cc(self, channel: int, cc: int, value: int):
        if not self.on_my_setup() or self.paused():
            return  # the EC4 is on one of your other setups
        if self.hi_res:
            if self.cc_base <= cc < self.cc_base + 16:
                self.msb[(channel, cc)] = value
                return
            if self.cc_base + 32 <= cc < self.cc_base + 48:
                msb_cc = cc - 32
                msb = self.msb.get((channel, msb_cc))
                if msb is None:
                    return
                self._set_from_ec4(channel, msb_cc, ((msb << 7) | value) / 16383.0)
            return
        if self.cc_base <= cc < self.cc_base + 16:
            self._set_from_ec4(channel, cc, value / 127.0)

    def _set_from_ec4(self, channel: int, cc: int, norm: float):
        with self.lock:
            slot = self.by_cc.get((channel, cc))
            if slot is None:
                return
            k = slot.param.key
            self.values[k] = norm
            self.touched[k] = time.monotonic()
            addr = slot.param.address
        if self.osc_send:
            self.osc_send(addr, float(norm))
        # show the name and value right away (a linear estimate); the runner's exact value
        # replaces it as soon as it comes back over OSC
        self.value_popup(slot, slot.param.approx_value(norm))

    # ---- runner -> EC4 -----------------------------------------------------
    def on_osc(self, address: str, *args):
        if not args:
            return
        raw_slot = self.by_raw.get(address)
        if raw_slot is not None:
            k = raw_slot.param.key
            self.raw[k] = args[0]
            if time.monotonic() - self.touched.get(k, 0) < self.cfg["value_popup_seconds"]:
                self.value_popup(raw_slot, args[0])  # exact value for the encoder being turned
            return
        if not address.endswith("/normalized"):
            return
        try:
            v = float(args[0])
        except (TypeError, ValueError):
            return
        with self.lock:
            slot = self.by_addr.get(address)
            if slot is None:
                return
            k = slot.param.key
            self.values[k] = v
            if time.monotonic() - self.touched.get(k, 0) < self.holdoff:
                return  # user is turning this encoder; don't fight it
            self._feedback(slot, v)

    def _cc_messages(self, slot: Slot, v: float) -> list[tuple[int, int, int]]:
        return self._encoder_ccs(slot.group, slot.encoder, v)

    def _encoder_ccs(self, group: int, encoder: int, v: float) -> list[tuple[int, int, int]]:
        v = min(1.0, max(0.0, v))
        cc = self.cc_base + encoder
        if self.hi_res:
            x = round(v * 16383)
            return [(group, cc, x >> 7), (group, cc + 32, x & 0x7F)]
        return [(group, cc, round(v * 127))]

    def _feedback(self, slot: Slot, v: float):
        if self.midi is None or not self.on_my_setup() or self.paused():
            return
        self.midi.send_ccs(self._cc_messages(slot, v), pause=0)

    def resync(self):
        """Send every current value to the EC4."""
        if (self.midi is None or not getattr(self.midi, "connected", True)
                or not self.on_my_setup() or self.paused()):
            return
        with self.lock:
            msgs = []
            for s in self.layout.slots:
                msgs += self._cc_messages(s, self.values.get(s.param.key, s.param.normalized))
            if self.cfg.get("zero_unused", True):
                # encoders with no parameter (e.g. left over from the previous graph) go to 0
                used = {(s.group, s.encoder) for s in self.layout.slots}
                for g in range(16):
                    for e in range(16):
                        if (g, e) not in used:
                            msgs += self._encoder_ccs(g, e, 0.0)
        self.midi.send_ccs(msgs)


# ---- dump helpers ----------------------------------------------------------

def write_layout_syx(cfg: dict, layout: Layout) -> bytes:
    with open(cfg["backup_syx"], "rb") as f:
        dump = ec4_sysex.parse_dump(f.read())
    ec4_sysex.apply_layout(
        dump, int(cfg["ec4_setup"]) - 1, cfg["setup_name"], layout.group_names, layout.encoder_names(),
        cc_base=int(cfg["cc_base"]), resolution=cfg["resolution"], mode=cfg["encoder_mode"],
        display=cfg["display"] or None, live_names=bool(cfg["live_names"]),
        push_jumps=bool(cfg["push_jumps_to_group"]),
    )
    data = ec4_sysex.build_dump(dump)
    tmp = cfg["layout_syx"] + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cfg["layout_syx"])
    return data


def open_midi(cfg: dict, **kw):
    try:
        from midi_alsa import EC4Midi  # imported late so 'list' works without ALSA
        return EC4Midi(cfg["midi_port"], log=log.info, **kw)
    except Exception as exc:
        raise SystemExit(f"Could not open the ALSA MIDI sequencer: {exc}\n"
                         "Is this running on the Pi, and is the user in the 'audio' group?")


# ---- subcommands -------------------------------------------------------------

def cmd_run(cfg: dict, args, stop: threading.Event | None = None) -> int:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import BlockingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient

    client = SimpleUDPClient(cfg["runner_host"], int(cfg["osc_port"]))
    bridge = Bridge(cfg, osc_send=lambda a, v: client.send_message(a, v))
    midi = open_midi(cfg, on_cc=bridge.on_cc, on_sysex=bridge.on_sysex)
    bridge.midi = midi

    disp = Dispatcher()
    disp.set_default_handler(bridge.on_osc)
    server = BlockingOSCUDPServer(("0.0.0.0", int(cfg["listen_port"])), disp)
    threading.Thread(target=server.serve_forever, name="osc-in", daemon=True).start()

    ip = cfg["listen_ip"] if cfg["listen_ip"] != "auto" else local_ip_for(cfg["runner_host"])
    listener = f"{ip}:{cfg['listen_port']}"

    if stop is None:
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())

    runner_ok = False
    last_refresh = time.monotonic()
    log.info("bridge started; runner %s, EC4 match '%s', setup %s, %s",
             cfg["runner_host"], cfg["midi_port"], cfg["ec4_setup"], cfg["resolution"])
    while not stop.is_set():
        reconnected = False
        try:
            reconnected = midi.ensure_connected()
            if reconnected:
                bridge.cur_setup = bridge.cur_group = None
                bridge.request_ec4_state()  # the EC4 answers with its setup and group
        except Exception as exc:
            log.warning("MIDI port scan failed: %s", exc)
        try:
            params = fetch_params(cfg)
            if not runner_ok:
                log.info("runner reachable; registering OSC listener %s", listener)
                client.send_message("/rnbo/listeners/add", listener)
                runner_ok = True
            changed = bridge.update_from_params(params)
            if reconnected and not changed:
                bridge.resync()
                bridge.write_names()
            if bridge.paused():
                if not bridge._was_paused:
                    log.info("setup dump in progress; not sending anything to the EC4")
                bridge._was_paused = True
            elif bridge._was_paused:
                bridge._was_paused = False
                log.info("setup dump finished; resending values and names")
                bridge.cur_setup = bridge.cur_group = None
                bridge.request_ec4_state()
                bridge.resync()
                bridge.write_names()
            refresh = float(cfg["live_names_refresh"] or 0)
            if refresh and time.monotonic() - last_refresh >= refresh:
                last_refresh = time.monotonic()
                bridge.write_names()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if runner_ok:
                log.warning("runner not reachable: %s", exc)
            runner_ok = False
        stop.wait(float(cfg["poll_interval"]))

    log.info("stopping")
    try:
        client.send_message("/rnbo/listeners/del", listener)
    except OSError:
        pass
    server.server_close()
    midi.close()
    return 0


def cmd_list(cfg: dict, args) -> int:
    try:
        params = fetch_params(cfg)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not reach the RNBO runner at {cfg['runner_host']}:{cfg['oscquery_port']}: {exc}")
        return 1
    lay = build_layout(params, cfg)
    print(format_table(lay))
    if args.json:
        out = [{"group": s.group + 1, "encoder": s.encoder + 1, "midi_channel": s.group + 1,
                "cc": int(cfg["cc_base"]) + s.encoder, "name": s.short, "instance": s.param.inst,
                "param": s.param.pid, "label": s.param.label, "address": s.param.address,
                "normalized": s.param.normalized} for s in lay.slots]
        print(json.dumps(out, indent=2))
    return 0


class pause_bridge:
    """Context manager: tell a running bridge service to stay quiet during a dump."""

    def __init__(self, cfg: dict, settle: float = 0.5):
        self.path = cfg["pause_file"]
        self.settle = settle

    def __enter__(self):
        try:
            with open(self.path, "w") as f:
                f.write(str(os.getpid()))
        except OSError as exc:
            print(f"Warning: could not pause the bridge service ({exc}).")
            print("If it's running, stop it first: sudo systemctl stop ec4bridge")
        time.sleep(self.settle)  # let anything already on its way to the EC4 go out first
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


def _capture(cfg: dict, timeout: float) -> bytes | None:
    q: queue.Queue[bytes] = queue.Queue()
    midi = open_midi(cfg, on_sysex=q.put)
    try:
        midi.ensure_connected()
        if not midi.connected:
            print(f"No MIDI port matching '{cfg['midi_port']}' found. Is the EC4 plugged in?")
            return None
        print("On the EC4: open the 'Send' menu, select 'Send all setups' and keep the\n"
              "encoder pressed until the transfer starts.\n"
              f"Waiting up to {int(timeout)} s ...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = q.get(timeout=1)
            except queue.Empty:
                continue
            if len(msg) < 1000:
                continue  # not a setup dump
            return msg
        print("Timed out waiting for the dump.")
        return None
    finally:
        midi.close()


def cmd_capture_backup(cfg: dict, args) -> int:
    with pause_bridge(cfg):
        msg = _capture(cfg, args.timeout)
    if msg is None:
        return 1
    try:
        dump = ec4_sysex.parse_dump(msg)
    except ec4_sysex.DumpError as exc:
        print(f"Received {len(msg)} bytes but it is not a valid dump: {exc}")
        return 1
    path = cfg["backup_syx"]
    if os.path.exists(path):
        keep = path + time.strftime(".%Y%m%d-%H%M%S")
        shutil.copy2(path, keep)
        print(f"Previous backup kept as {keep}")
    with open(path, "wb") as f:
        f.write(msg)
    print(f"Saved EC4 backup (firmware {dump.version:.1f}, {len(msg)} bytes) to {path}")
    return 0


def _make(cfg: dict) -> tuple[bytes, Layout] | None:
    if not os.path.exists(cfg["backup_syx"]):
        print(f"No EC4 backup at {cfg['backup_syx']}. Run 'capture-backup' first.")
        return None
    try:
        params = fetch_params(cfg)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not reach the RNBO runner: {exc}")
        return None
    lay = build_layout(params, cfg)
    data = write_layout_syx(cfg, lay)
    return data, lay


def cmd_make_syx(cfg: dict, args) -> int:
    made = _make(cfg)
    if not made:
        return 1
    data, lay = made
    print(format_table(lay))
    print(f"\nWrote {cfg['layout_syx']} ({len(data)} bytes) for EC4 setup {cfg['ec4_setup']}.")
    return 0


def cmd_send_layout(cfg: dict, args) -> int:
    if args.fresh_backup:
        print("Step 1: capturing a fresh backup so your other setups are preserved.")
        if cmd_capture_backup(cfg, args) != 0:
            return 1
    made = _make(cfg)
    if not made:
        return 1
    data, lay = made
    print(format_table(lay))
    age = time.time() - os.path.getmtime(cfg["backup_syx"])
    print(f"\nThis overwrites ALL 16 setups on the EC4: setup {cfg['ec4_setup']} gets the RNBO layout,"
          f"\nthe others are restored from the backup taken {age / 3600:.1f} h ago.")
    print("On the EC4: open the 'Receive' menu; the display shows 'Work in progress'.")
    with pause_bridge(cfg):  # a running service must not talk to the EC4 during the dump
        return _send_layout(cfg, args, data)


def _send_layout(cfg: dict, args, data: bytes) -> int:
    if not args.yes:
        try:
            input("Press Enter when the EC4 is waiting to receive (Ctrl-C to cancel) ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 1
    bridge = Bridge(cfg)
    bridge.hold = True  # stay silent until the dump is out
    midi = open_midi(cfg, on_sysex=bridge.on_sysex)
    bridge.midi = midi
    try:
        midi.ensure_connected()
        if not midi.connected:
            print(f"No MIDI port matching '{cfg['midi_port']}' found.")
            return 1
        chunks = ec4_sysex.split_for_sending(data)
        pause = cfg["sysex_page_pause_ms"] / 1000.0

        def progress(i, n):
            print(f"\r  sending {i}/{n} pages", end="", flush=True)

        midi.send_sysex_chunks(chunks, pause, progress)
        print("\nDone. The EC4 shows the progress and returns to normal when finished.")
        time.sleep(3)
        bridge.hold = False
        bridge.cfg = dict(cfg, pause_file="")  # our own pause file is still there; ignore it
        bridge.request_ec4_state()  # so names only go to the screen if the RNBO setup is showing
        time.sleep(1)
        bridge.update_from_params(fetch_params(cfg))  # sends all current values and live names
        if cfg["live_names"]:
            print("Sent current values and names to the EC4. Encoder names are stored as '----'")
            print("so the bridge can write them live; select the RNBO setup to see them.")
        else:
            print("Sent current parameter values to the EC4.")
    finally:
        midi.close()
    return 0


def cmd_monitor(cfg: dict, args) -> int:
    """Print what the EC4 sends, with timing, to see whether values arrive late or skip."""
    cc_base = int(cfg["cc_base"])
    last: dict[tuple[int, int], tuple[float, int]] = {}
    t0 = time.monotonic()

    def on_cc(ch, cc, val):
        now = time.monotonic()
        prev = last.get((ch, cc))
        last[(ch, cc)] = (now, val)
        enc = cc - cc_base + 1 if cc_base <= cc < cc_base + 16 else None
        where = f"group {ch + 1:>2} enc {enc:>2}" if enc else f"ch {ch + 1:>2} cc {cc:>3}"
        if prev:
            dt = (now - prev[0]) * 1000
            step = val - prev[1]
            flag = "  <-- skipped" if abs(step) > 1 and dt < 300 else ""
            print(f"{now - t0:8.3f}s  {where}  value {val:>3}  step {step:+3d}  {dt:6.1f} ms{flag}", flush=True)
        else:
            print(f"{now - t0:8.3f}s  {where}  value {val:>3}", flush=True)

    midi = open_midi(cfg, on_cc=on_cc)
    try:
        midi.ensure_connected()
        if not midi.connected:
            print(f"No MIDI port matching '{cfg['midi_port']}' found.")
            return 1
        print("Turn encoders slowly, then quickly. Ctrl-C to stop.")
        print("'step' is the change since the previous message; 'ms' the time since it.")
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        midi.close()
    return 0


def cmd_test_display(cfg: dict, args) -> int:
    """Check that the EC4 answers state requests and shows live display text."""
    q: queue.Queue[dict] = queue.Queue()

    def on_sysex(msg):
        rep = ec4_remote.parse_report(msg)
        if rep:
            q.put(rep)

    def ask(timeout=2.0) -> dict | None:
        while not q.empty():
            q.get_nowait()
        midi.send_sysex(ec4_remote.REQUEST_INFO)
        state: dict = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state.update(q.get(timeout=0.2))
            except queue.Empty:
                if state:
                    break
        return state or None

    def fmt(st):
        return (f"setup {st['setup'] + 1 if 'setup' in st else '?'}, "
                f"group {st['group'] + 1 if 'group' in st else '?'}")

    midi = open_midi(cfg, on_sysex=on_sysex)
    try:
        midi.ensure_connected()
        if not midi.connected:
            print(f"No MIDI port matching '{cfg['midi_port']}' found.")
            return 1
        print("1) Asking the EC4 which setup and group it is on ...")
        before = ask()
        if not before:
            print("   No answer. Remote commands need EC4 firmware 2.0 or newer; live names won't work.")
            return 1
        print(f"   EC4 reports {fmt(before)}  -> state reports work")

        print("2) Writing a test message on the EC4 display for 4 seconds ...")
        midi.send_sysex(ec4_remote.overlay_text(["ec4bridge", "live display test", "", "can you read this?"]))
        midi.send_sysex(ec4_remote.overlay_show(True))
        time.sleep(4)
        midi.send_sysex(ec4_remote.overlay_show(False))
        print("   If you saw the text, live display text works.")

        print("3) Writing test names (T01 ... T16) on the encoder names for 4 seconds ...")
        midi.send_sysex(ec4_remote.names_page([f"T{e + 1:02d}" for e in range(16)]))
        time.sleep(4)
        names: list = [None] * 16
        try:  # put the real names back for the group that's showing
            lay = build_layout(fetch_params(cfg), cfg)
            names = lay.encoder_names()[before.get("group", 0)]
        except Exception:
            pass
        midi.send_sysex(ec4_remote.names_page(names))
        print("   T01..T16 appeared  -> live names work.")
        print("   Old names stayed   -> the encoder names stored in this setup aren't '----'.")
        print("                         Run 'send-layout' once (with live_names on) to fix that.")
        if before.get("setup") != int(cfg["ec4_setup"]) - 1:
            print(f"   Note: the EC4 is on setup {before.get('setup', -1) + 1}; the bridge writes names only"
                  f" while it's on setup {cfg['ec4_setup']} (the RNBO setup).")
    finally:
        midi.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the bridge")
    p = sub.add_parser("list", help="print the parameter layout")
    p.add_argument("--json", action="store_true", help="also print the layout as JSON")
    p = sub.add_parser("capture-backup", help="save the EC4's setups")
    p.add_argument("--timeout", type=float, default=120)
    sub.add_parser("make-syx", help="write the layout dump file")
    p = sub.add_parser("send-layout", help="send names/layout to the EC4")
    p.add_argument("--yes", action="store_true", help="don't wait for Enter")
    p.add_argument("--fresh-backup", action="store_true", help="capture a new backup first")
    p.add_argument("--timeout", type=float, default=120)
    sub.add_parser("test-display", help="check live display text on the EC4")
    sub.add_parser("monitor", help="show what the EC4 sends, with timing")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s" if sys.stderr.isatty() else "%(levelname)s %(message)s")
    cfg = load_config(args.config)
    return {
        "run": cmd_run, "list": cmd_list, "capture-backup": cmd_capture_backup,
        "make-syx": cmd_make_syx, "send-layout": cmd_send_layout, "test-display": cmd_test_display, "monitor": cmd_monitor,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
