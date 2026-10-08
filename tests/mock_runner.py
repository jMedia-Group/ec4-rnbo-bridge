"""A fake RNBO runner: serves an OSCQuery tree shaped like the real runner's
(see rnbo.oscquery.runner src/Instance.cpp) and records OSC it receives."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _leaf(path, value, typ="f", rng=None, access=3):
    n = {"FULL_PATH": path, "TYPE": typ, "VALUE": [value], "ACCESS": access}
    if rng:
        n["RANGE"] = rng
    return n


def make_param(inst, pid, index, value, lo=0.0, hi=1.0, display="", order=None, steps=0, enum=None):
    base = f"/rnbo/inst/{inst}/params/{pid}"
    norm = (value - lo) / (hi - lo) if enum is None else value / max(1, len(enum) - 1)
    contents = {
        "index": _leaf(base + "/index", index, "i", access=1),
        "display_name": _leaf(base + "/display_name", display, "s", access=1),
        "normalized": _leaf(base + "/normalized", norm, "f", [{"MIN": 0, "MAX": 1}]),
        "meta": _leaf(base + "/meta", "", "s"),
    }
    if order is not None:
        contents["display_order"] = _leaf(base + "/display_order", order, "i", access=1)
    if steps:
        contents["steps"] = _leaf(base + "/steps", steps, "i", access=1)
    if enum is not None:
        node = _leaf(base, enum[int(value)], "s", [{"VALS": enum}])
    else:
        node = _leaf(base, value, "f", [{"MIN": lo, "MAX": hi}])
    node["CONTENTS"] = contents
    return node


def insert(params_node, pid, node):
    parts = pid.split("/")
    cur = params_node
    for p in parts[:-1]:
        cur = cur.setdefault("CONTENTS", {}).setdefault(p, {"FULL_PATH": "", "CONTENTS": {}})
    cur.setdefault("CONTENTS", {})[parts[-1]] = node


def make_instance(inst, name, params, alias=""):
    root = {"FULL_PATH": f"/rnbo/inst/{inst}", "CONTENTS": {
        "name": _leaf(f"/rnbo/inst/{inst}/name", name, "s", access=1),
        "config": {"FULL_PATH": f"/rnbo/inst/{inst}/config", "CONTENTS": {
            "name_alias": _leaf(f"/rnbo/inst/{inst}/config/name_alias", alias, "s")}},
        "params": {"FULL_PATH": f"/rnbo/inst/{inst}/params", "CONTENTS": {}},
        "messages": {"FULL_PATH": f"/rnbo/inst/{inst}/messages", "CONTENTS": {}},
    }}
    for p in params:
        insert(root["CONTENTS"]["params"], p[0], make_param(inst, *p))
    return root


def default_tree():
    synth = [
        ("cutoff", 0, 1000.0, 20.0, 20000.0, "Cutoff"),
        ("resonance", 1, 0.5),
        ("env/attack", 2, 10.0, 0.0, 1000.0),
        ("env/release", 3, 200.0, 0.0, 5000.0),
        ("wave", 4, 1, 0, 1, "", None, 3, ["sine", "saw", "square"]),
        ("volume", 5, 0.8, 0.0, 1.0, "", 0),  # display_order 0 -> first
    ] + [(f"extra{i}", 6 + i, 0.25) for i in range(14)]  # 20 params -> spills into group 2
    delay = [("time", 0, 250.0, 0.0, 2000.0), ("feedback", 1, 0.4), ("mix", 2, 0.3)]
    return {"FULL_PATH": "/rnbo/inst", "CONTENTS": {
        "control": {"FULL_PATH": "/rnbo/inst/control", "CONTENTS": {}},
        "config": {"FULL_PATH": "/rnbo/inst/config", "CONTENTS": {}},
        "0": make_instance(0, "polysynth", synth),
        "1": make_instance(1, "pingpong", delay, alias="Delay"),
    }}


class MockRunner:
    def __init__(self, tree=None):
        self.tree = tree or default_tree()
        runner = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/") != "/rnbo/inst":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(runner.tree).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def close(self):
        self.http.shutdown()
