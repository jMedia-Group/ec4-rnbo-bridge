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
    "feedback_holdoff_ms": 250,
    "new_group_per_instance": True,
    "include": [],
    "exclude": [],
    "names": {},
    "group_names": {},
    "strip_prefixes": [],
    "name_case": "keep",
    "backup_syx": "ec4-backup.syx",
    "layout_syx": "ec4-layout.syx",
    "layout_txt": "layout.txt",
    "sysex_page_pause_ms": 2,
}


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
    for k in ("backup_syx", "layout_syx", "layout_txt"):
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
                self.values = {s.param.key: s.param.normalized for s in layout.slots}
                self.msb.clear()
            else:
                # catch value changes the OSC listener might have missed
                now = time.monotonic()
                for s in layout.slots:
                    k = s.param.key
                    old = self.values.get(k)
                    if old is None or abs(old - s.param.normalized) < 1e-6:
                        continue
                    if now - self.touched.get(k, 0) < max(self.holdoff, 1.0):
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
        if os.path.exists(self.cfg["backup_syx"]):
            try:
                write_layout_syx(self.cfg, self.layout)
                log.info("EC4 names changed: run 'ec4bridge.py send-layout' to put them on the display")
            except Exception as exc:  # keep running even if the backup is bad
                log.warning("could not build layout dump: %s", exc)
        self.resync()

    # ---- EC4 -> runner -----------------------------------------------------
    def on_cc(self, channel: int, cc: int, value: int):
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

    # ---- runner -> EC4 -----------------------------------------------------
    def on_osc(self, address: str, *args):
        if not args or not address.endswith("/normalized"):
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
        v = min(1.0, max(0.0, v))
        cc = self.cc_base + slot.encoder
        if self.hi_res:
            x = round(v * 16383)
            return [(slot.group, cc, x >> 7), (slot.group, cc + 32, x & 0x7F)]
        return [(slot.group, cc, round(v * 127))]

    def _feedback(self, slot: Slot, v: float):
        if self.midi is None:
            return
        self.midi.send_ccs(self._cc_messages(slot, v), pause=0)

    def resync(self):
        """Send every current value to the EC4."""
        if self.midi is None or not getattr(self.midi, "connected", True):
            return
        with self.lock:
            msgs = []
            for s in self.layout.slots:
                msgs += self._cc_messages(s, self.values.get(s.param.key, s.param.normalized))
        self.midi.send_ccs(msgs)


# ---- dump helpers ----------------------------------------------------------

def write_layout_syx(cfg: dict, layout: Layout) -> bytes:
    with open(cfg["backup_syx"], "rb") as f:
        dump = ec4_sysex.parse_dump(f.read())
    ec4_sysex.apply_layout(
        dump, int(cfg["ec4_setup"]) - 1, cfg["setup_name"], layout.group_names, layout.encoder_names(),
        cc_base=int(cfg["cc_base"]), resolution=cfg["resolution"], mode=cfg["encoder_mode"],
        display=cfg["display"] or None,
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
    midi = open_midi(cfg, on_cc=bridge.on_cc)
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
    log.info("bridge started; runner %s, EC4 match '%s', setup %s, %s",
             cfg["runner_host"], cfg["midi_port"], cfg["ec4_setup"], cfg["resolution"])
    while not stop.is_set():
        reconnected = False
        try:
            reconnected = midi.ensure_connected()
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
    if not args.yes:
        try:
            input("Press Enter when the EC4 is waiting to receive (Ctrl-C to cancel) ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 1
    midi = open_midi(cfg)
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
        bridge = Bridge(cfg, midi=midi)
        bridge.update_from_params(fetch_params(cfg))  # also sends all current values
        print("Sent current parameter values to the EC4.")
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
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s" if sys.stderr.isatty() else "%(levelname)s %(message)s")
    cfg = load_config(args.config)
    return {
        "run": cmd_run, "list": cmd_list, "capture-backup": cmd_capture_backup,
        "make-syx": cmd_make_syx, "send-layout": cmd_send_layout,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
