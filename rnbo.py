"""Read loaded instances and parameters from the RNBO runner's OSCQuery tree."""

from __future__ import annotations

import json
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

    @property
    def key(self) -> str:
        return f"{self.inst}/{self.pid}"

    @property
    def label(self) -> str:
        return self.display_name or self.pid.split("/")[-1]


def fetch_tree(host: str, port: int, path: str = "/rnbo/inst", timeout: float = 2.0) -> dict:
    url = f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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
                    ))
                elif cnode.get("CONTENTS"):
                    walk(cnode, path)  # subpatcher folder

        if params_root:
            walk(params_root, [])
        # RNBO display order first (params without one go last), then parameter index
        found.sort(key=lambda p: (p.display_order is None, p.display_order or 0, p.index))
        out.extend(found)
    return out
