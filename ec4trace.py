"""Timeline of everything the bridge does for a few seconds, and where the delays are.

Start it on the running service with   sudo systemctl kill -s USR1 ec4bridge
then turn knobs the way that shows the problem. When it ends, a summary goes to the log and
the full timeline to trace-<date>.txt in the bridge's folder.

Each stage of a knob turn is timed separately, so the summary says which one is slow:
  EC4 -> bridge      gaps in what the EC4 sends while you're turning (and what the bridge
                     sent the EC4 just before each gap)
  bridge -> runner   how long a change waits in the bridge before it goes out over OSC
  runner             how long the runner takes to report a change back
  other              values the runner reports that the bridge didn't send (something else is
                     also changing the parameter), values sent back to the knob you're turning,
                     and slow requests to the runner's HTTP interface
"""

from __future__ import annotations

import threading
import time

# a pause between two messages from the same knob counts as a stall only if the value jumped
# by at least this much afterwards (you kept turning while nothing came through)
STALL_GAP = 0.08
STALL_JUMP = 3 / 127


class Trace:
    def __init__(self):
        self._lock = threading.Lock()
        self.until = 0.0
        self.t0 = 0.0
        self.clock_offset = 0.0  # wall clock minus monotonic, to print times you can match to logs
        self.events: list[tuple] = []

    @property
    def active(self) -> bool:
        return time.monotonic() < self.until

    def start(self, seconds: float):
        with self._lock:
            self.t0 = time.monotonic()
            self.clock_offset = time.time() - self.t0
            self.until = self.t0 + seconds
            self.events = []

    def add(self, kind: str, *fields):
        t = time.monotonic()
        if t < self.until:
            with self._lock:
                self.events.append((t, kind) + fields)

    def take(self) -> list[tuple]:
        with self._lock:
            ev, self.events = self.events, []
            self.until = 0.0
            return ev


def _ms(s: float) -> str:
    return f"{s * 1000:.0f} ms"


def analyze(events: list[tuple], params: dict[str, tuple[str, int]] | None = None,
            clock_offset: float | None = None) -> list[str]:
    """Summary lines. params: {osc address: (label, steps)}. clock_offset: wall clock minus
    monotonic time; when given, times also show the time of day (to match the runner's log)."""
    params = params or {}
    if not events:
        return ["no events recorded (did you turn any knobs?)"]
    t0 = events[0][0]
    def rel(t: float) -> str:
        s = f"{t - t0:6.2f}s"
        if clock_offset is not None:
            s += time.strftime(" (%H:%M:%S)", time.localtime(t + clock_offset))
        return s
    out: list[str] = []

    sets = [e for e in events if e[1] == "set"]           # (t, set, key, address, norm)
    ins = [e for e in events if e[1] == "in"]             # (t, in, ch, cc, value)
    skips = [e for e in events if e[1] == "skip"]         # (t, skip, reason, ch, cc, value)
    outs = [e for e in events if e[1] == "osc_out"]       # (t, osc_out, address, value)
    replies = [e for e in events if e[1] == "osc_in"]     # (t, osc_in, address, value)
    to_ec4 = [e for e in events if e[1] in ("to_ec4_cc", "to_ec4_sysex")]
    http = [e for e in events if e[1] == "http"]          # (t_end, http, what, seconds)

    out.append(f"{len(ins)} knob messages from the EC4, {len(sets)} parameter changes, "
               f"{len(outs)} sent to the runner, {len(replies)} reported back, "
               f"{len(to_ec4)} messages to the EC4")
    if not sets:
        out.append("no knob turns on mapped encoders were recorded")

    # ---- EC4 -> bridge: stalls while turning --------------------------------------------
    by_key: dict[str, list[tuple]] = {}
    for e in sets:
        by_key.setdefault(e[2], []).append(e)
    stalls = []
    for key, evs in by_key.items():
        for a, b in zip(evs, evs[1:]):
            gap = b[0] - a[0]
            if STALL_GAP <= gap < 1.5 and abs(b[4] - a[4]) >= STALL_JUMP:
                stalls.append((a[0], gap, abs(b[4] - a[4]), key))
    stalls.sort()
    if stalls:
        worst = max(s[1] for s in stalls)
        out.append(f"EC4 -> bridge: {len(stalls)} stall(s) while turning (nothing arrived, then the "
                   f"value jumped); longest {_ms(worst)}")
        for t, gap, jump, key in sorted(stalls, key=lambda s: -s[1])[:6]:
            before = [e for e in events if t - 0.3 <= e[0] <= t + gap and e[1] in
                      ("to_ec4_cc", "to_ec4_sysex", "http", "port_scan")]
            what = {}
            for e in before:
                what[e[1]] = what.get(e[1], 0) + 1
            ctx = ", ".join(f"{k} x{n}" for k, n in what.items()) or "nothing else happening"
            out.append(f"  at {rel(t)}: {key} silent for {_ms(gap)}, jumped {jump * 127:.0f} steps "
                       f"(around it: {ctx})")
    elif sets:
        out.append("EC4 -> bridge: smooth (no stalls while turning)")
    reasons: dict[str, int] = {}
    for e in skips:
        reasons[e[2]] = reasons.get(e[2], 0) + 1
    if reasons:
        out.append("knob messages ignored: " + ", ".join(f"{n} ({r})" for r, n in reasons.items()))

    # ---- bridge -> runner: wait in the pacer --------------------------------------------
    waits = []
    outs_by_addr: dict[str, list[tuple]] = {}
    for e in outs:
        outs_by_addr.setdefault(e[2], []).append(e)
    for e in sets:
        nxt = next((o for o in outs_by_addr.get(e[3], []) if o[0] >= e[0] - 1e-6), None)
        if nxt is not None:
            waits.append(nxt[0] - e[0])
    if waits:
        out.append(f"bridge -> runner: changes waited avg {_ms(sum(waits) / len(waits))}, "
                   f"longest {_ms(max(waits))} before going out")

    # ---- runner: time until it reports a change back -------------------------------------
    rep_by_addr: dict[str, list[tuple]] = {}
    for e in replies:
        rep_by_addr.setdefault(e[2], []).append(e)
    lats, missing, slow = [], 0, []
    for o in outs:
        _, steps = params.get(o[2], ("", 0))
        tol = max(1e-3, 0.5 / (steps - 1) + 1e-3) if steps and steps > 1 else 1e-3
        r = next((r for r in rep_by_addr.get(o[2], []) if r[0] >= o[0] and abs(r[3] - o[3]) <= tol), None)
        if r is None:
            missing += 1
            continue
        lat = r[0] - o[0]
        lats.append(lat)
        if lat > 0.05:
            slow.append((o[0], lat, o[2]))
    if lats:
        out.append(f"runner: reported changes back after avg {_ms(sum(lats) / len(lats))}, "
                   f"slowest {_ms(max(lats))}; {missing} of {len(outs)} never reported back "
                   f"(normal for a few during fast turns)")
        for t, lat, addr in sorted(slow, key=lambda s: -s[1])[:5]:
            out.append(f"  at {rel(t)}: {params.get(addr, (addr,))[0] or addr} took {_ms(lat)}")
    elif outs:
        out.append(f"runner: none of the {len(outs)} changes were reported back")

    # ---- values the bridge didn't send ----------------------------------------------------
    foreign = []
    for r in replies:
        sent = [o for o in outs_by_addr.get(r[2], []) if r[0] - 2.0 <= o[0] <= r[0]]
        if not sent:
            continue  # not a parameter you were turning
        _, steps = params.get(r[2], ("", 0))
        tol = max(0.03, 0.5 / (steps - 1) + 0.01) if steps and steps > 1 else 0.03
        if min(abs(o[3] - r[3]) for o in sent) > tol:
            foreign.append(r)
    if foreign:
        out.append(f"other: {len(foreign)} value(s) reported by the runner that the bridge never "
                   f"sent, while you were turning: something else is also changing these parameters "
                   f"(e.g. MIDI mapping in the patch, or the runner receiving the EC4 directly)")
        for r in foreign[:5]:
            out.append(f"  at {rel(r[0])}: {params.get(r[2], (r[2],))[0] or r[2]} = {r[3]:.3f}")

    # ---- values sent back to a knob that's being turned ------------------------------------
    fb = [e for e in events if e[1] == "feedback"]
    fighting = [e for e in fb if any(0 <= e[0] - s[0] < 1.0 and s[2] == e[2] for s in sets)]
    if fighting:
        out.append(f"other: {len(fighting)} value(s) sent back to a knob within 1 s of turning it")

    # ---- HTTP requests to the runner ------------------------------------------------------
    if http:
        worst = max(http, key=lambda e: e[3])
        out.append(f"runner HTTP: {len(http)} request batch(es), slowest {_ms(worst[3])} ({worst[2]})")
    scans = [e for e in events if e[1] == "port_scan"]
    if scans and max(e[3] for e in scans) > 0.05:
        out.append(f"MIDI port scan: slowest {_ms(max(e[3] for e in scans))}")
    return out


def format_events(events: list[tuple], params: dict[str, tuple[str, int]] | None = None) -> str:
    params = params or {}
    if not events:
        return ""
    t0 = events[0][0]
    lines = []
    for e in events:
        t, kind, *f = e
        if kind in ("osc_out", "osc_in"):
            f = [params.get(f[0], (f[0],))[0] or f[0], f"{f[1]:.4f}"] + f[2:]
        elif kind in ("to_ec4_sysex", "from_ec4_sysex"):
            f = [f[0].hex(" ")[:60] + (" ..." if len(f[0]) > 20 else "")]
        elif kind in ("http", "port_scan"):
            f = [f[0], _ms(f[1])]
        elif kind == "set":
            f = [f[0], f"{f[2]:.4f}"]
        lines.append(f"{t - t0:8.3f}  {kind:<13} " + "  ".join(str(x) for x in f))
    return "\n".join(lines)
