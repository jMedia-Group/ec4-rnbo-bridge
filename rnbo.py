"""Read loaded instances and parameters from the RNBO runner's OSCQuery tree."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Param:
    inst: int            # instance index (/rnbo/inst/<inst>)
    inst_name: str       # name alias if set, else patcher name
    pid: str             # parameter id, may contain '/' for subpatcher params
    index: int           # RNBO parameter index
    display_name: str
    display_order: int | None
    steps: int
    enum_values: list[str] | None
    normalized: float
    address: str         # OSC address of the normalized value
    value: float | str | None = None  # current raw value (enum: its label)
    min: float | None = None
    max: float | None = None
    unit: str = ""  # RNBO @unit (e.g. "Hz"), or "unit" in the parameter's meta

    @property
    def key(self) -> str:
        return f"{self.inst}/{self.pid}"

    @property
    def label(self) -> str:
        return self.display_name or self.pid.split("/")[-1]

    @property
    def raw_address(self) -> str:
        """OSC address of the raw (unnormalized) value."""
        return self.address[: -len("/normalized")] if self.address.endswith("/normalized") else self.address

    def approx_value(self, normalized: float):
        """Raw value for a normalized one, until the runner reports the exact value
        (linear guess; RNBO parameters can have a non-linear curve)."""
        if self.enum_values:
            n = len(self.enum_values)
            return self.enum_values[min(n - 1, max(0, round(normalized * (n - 1))))]
        if self.min is None or self.max is None:
            return None
        v = self.min + normalized * (self.max - self.min)
        if self.steps and self.steps > 1:
            step = (self.max - self.min) / (self.steps - 1)
            v = self.min + round((v - self.min) / step) * step
        return v


def fetch_tree(host: str, port: int, path: str = "/rnbo/inst", timeout: float = 2.0) -> dict:
    url = f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


SET_NAME_PATH = "/rnbo/inst/control/sets/current/name"


def instance_names(tree: dict) -> dict[int, str]:
    """{instance index: patcher name} for every instance in a /rnbo/inst tree."""
    insts = tree.get("CONTENTS") or {}
    return {int(k): str(_value(_child(insts[k], "name"), "") or "") for k in insts if k.isdigit()}


def fetch_value(host: str, port: int, path: str, timeout: float = 1.0):
    """Value of one small node, or None if it doesn't exist."""
    try:
        return _value(fetch_tree(host, port, path, timeout))
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:  # no such node (servers differ in the exact code)
            return None
        raise


def fetch_graph(host: str, port: int, known: dict[int, str] | None = None,
                pause: float = 0.02, timeout: float = 5.0) -> dict:
    """The parts of /rnbo/inst the bridge needs, read in small pieces.

    Reading all of /rnbo/inst in one request makes the runner build the whole state of every
    instance (presets, data, ports, ...) at once; with a big graph that takes seconds, during
    which it doesn't apply incoming parameter changes. Reading each instance's name and
    parameters separately, with a short pause in between, lets it keep up.
    """
    tree: dict = {"FULL_PATH": "/rnbo/inst", "CONTENTS": {}}
    top = max(known, default=-1) if known else -1
    misses, i = 0, 0
    while misses < 8 or i <= top + 2:  # instance indexes can have gaps
        name = fetch_value(host, port, f"/rnbo/inst/{i}/name")
        if name is None:
            misses += 1
            i += 1
            continue
        misses = 0
        alias = fetch_value(host, port, f"/rnbo/inst/{i}/config/name_alias") or ""
        try:
            params = fetch_tree(host, port, f"/rnbo/inst/{i}/params", timeout)
        except urllib.error.HTTPError as exc:
            if not 400 <= exc.code < 500:
                raise
            params = {"CONTENTS": {}}
        tree["CONTENTS"][str(i)] = {"FULL_PATH": f"/rnbo/inst/{i}", "CONTENTS": {
            "name": {"VALUE": [name]},
            "config": {"CONTENTS": {"name_alias": {"VALUE": [alias]}}},
            "params": params,
        }}
        if pause:
            time.sleep(pause)
        i += 1
    return tree


def graph_signature(host: str, port: int, known: dict[int, str]) -> tuple:
    """Cheap fingerprint of what's loaded: the current set's name and the patcher name of each
    known instance plus the next two indexes. A handful of tiny requests, instead of the whole
    tree (which can take the runner seconds to produce with a big graph)."""
    top = max(known, default=-1)
    indexes = sorted(set(known) | {top + 1, top + 2})
    names = tuple((i, fetch_value(host, port, f"/rnbo/inst/{i}/name")) for i in indexes)
    return (fetch_value(host, port, SET_NAME_PATH), names)


def _value(node: dict | None, default=None):
    if not node:
        return default
    v = node.get("VALUE")
    if isinstance(v, list):
        return v[0] if v else default
    return default if v is None else v


def _child(node: dict | None, *names: str) -> dict | None:
    for n in names:
        if not node:
            return None
        node = (node.get("CONTENTS") or {}).get(n)
    return node


def _is_param(node: dict) -> bool:
    c = node.get("CONTENTS") or {}
    return "normalized" in c and "index" in c


def parse_params(tree: dict) -> list[Param]:
    """Turn the JSON from /rnbo/inst into a flat, ordered parameter list."""
    out: list[Param] = []
    insts = tree.get("CONTENTS") or {}
    for key in sorted((k for k in insts if k.isdigit()), key=int):
        inst = insts[key]
        name = _value(_child(inst, "config", "name_alias"), "") or _value(_child(inst, "name"), "") or f"inst{key}"
        params_root = _child(inst, "params")
        found: list[Param] = []

        def walk(node: dict, prefix: list[str]):
            for cname, cnode in (node.get("CONTENTS") or {}).items():
                path = prefix + [cname]
                if _is_param(cnode):
                    norm = _child(cnode, "normalized")
                    rng = cnode.get("RANGE") or []
                    enum_vals = None
                    if rng and isinstance(rng[0], dict) and "VALS" in rng[0]:
                        enum_vals = [str(v) for v in rng[0]["VALS"]]
                    order = _value(_child(cnode, "display_order"))
                    lo = hi = None
                    if rng and isinstance(rng[0], dict) and "MIN" in rng[0] and "MAX" in rng[0]:
                        try:
                            lo, hi = float(rng[0]["MIN"]), float(rng[0]["MAX"])
                        except (TypeError, ValueError):
                            lo = hi = None
                    raw = _value(cnode)
                    unit = str(_value(_child(cnode, "unit"), "") or "")
                    if not unit:
                        meta = _value(_child(cnode, "meta"), "")
                        if isinstance(meta, str) and meta.strip().startswith("{"):
                            try:
                                m = json.loads(meta)
                                if isinstance(m, dict) and isinstance(m.get("unit"), str):
                                    unit = m["unit"]
                            except ValueError:
                                pass
                    found.append(Param(
                        inst=int(key),
                        inst_name=str(name),
                        pid="/".join(path),
                        index=int(_value(_child(cnode, "index"), 0)),
                        display_name=str(_value(_child(cnode, "display_name"), "") or ""),
                        display_order=int(order) if isinstance(order, (int, float)) else None,
                        steps=int(_value(_child(cnode, "steps"), 0) or 0),
                        enum_values=enum_vals,
                        normalized=float(_value(norm, 0.0) or 0.0),
                        address=norm.get("FULL_PATH") or f"/rnbo/inst/{key}/params/{'/'.join(path)}/normalized",
                        value=raw if isinstance(raw, (int, float, str)) else None,
                        min=lo,
                        max=hi,
                        unit=unit.strip(),
                    ))
                elif cnode.get("CONTENTS"):
                    walk(cnode, path)  # subpatcher folder

        if params_root:
            walk(params_root, [])
        # RNBO display order first (params without one go last), then parameter index
        found.sort(key=lambda p: (p.display_order is None, p.display_order or 0, p.index))
        out.extend(found)
    return out
