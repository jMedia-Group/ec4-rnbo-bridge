"""Assign RNBO parameters to EC4 groups/encoders and make 4-character names."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rnbo import Param

VOWELS = set("aeiouAEIOU")
SUFFIX_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _squeeze(word: str, n: int) -> str:
    """Shorten one word to n chars: keep its first two letters, then drop
    vowels and doubled letters from the rest ('cutoff' -> 'cutf', 'resonance' -> 'resn')."""
    if len(word) <= n:
        return word
    head, tail = word[:2], word[2:]
    rest = "".join(c for c in tail if c not in VOWELS)
    s = head + rest
    s = re.sub(r"(.)\1+", r"\1", s)
    if len(s) < n:  # dropped too much: fall back to plain truncation
        s = word
    return s[:n]


def _words(name: str) -> list[str]:
    return re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)


NAME_CASES = ("keep", "title", "upper", "lower")
GROUP_TITLE_STYLES = ("instance", "number", "blank")


def _cap(s: str, case: str) -> str:
    return s[:1].upper() + s[1:] if case == "title" else s


def apply_case(s: str, case: str) -> str:
    if case == "upper":
        return s.upper()
    if case == "lower":
        return s.lower()
    return s


def abbreviate(name: str, n: int = 4, case: str = "keep") -> str:
    """Shorten a name to n chars. case: 'keep' (as in RNBO), 'title' (capitalize each
    word), 'upper' or 'lower'."""
    return apply_case(_abbreviate(name, n, case), case)


def _abbreviate(name: str, n: int, case: str) -> str:
    words = _words(name)
    if not words:
        return (re.sub(r"[^0-9A-Za-z./-]", "", name) or "?")[:n]
    if len(words) == 1:
        return _cap(_squeeze(words[0], n), case)
    if len(words) >= n:
        return "".join(_cap(w[0], case) for w in words[:n])
    # share n chars across the words, earlier words get the remainder
    k = len(words)
    sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
    # give unused room from short words (e.g. digits '2') to the first word
    parts = []
    spare = 0
    for w, size in zip(words, sizes):
        if len(w) < size:
            spare += size - len(w)
    for i, (w, size) in enumerate(zip(words, sizes)):
        if i == 0:
            size += spare
        p = w if w.isdigit() else _squeeze(w, size)
        parts.append(_cap(p[:size], case))
    return "".join(parts)[:n]


def _dedupe(names: list[str | None]) -> list[str | None]:
    seen: set[str] = set()
    out = []
    for nm in names:
        if nm is None:
            out.append(None)
            continue
        cand = nm
        i = 0
        while cand.lower() in seen and i < len(SUFFIX_CHARS):
            cand = nm[:3] + SUFFIX_CHARS[i]
            i += 1
        seen.add(cand.lower())
        out.append(cand)
    return out


@dataclass
class Slot:
    group: int   # 0..15  (EC4 group, also MIDI channel)
    encoder: int  # 0..15
    param: Param
    short: str   # 4-char display name


@dataclass
class Layout:
    slots: list[Slot]
    group_names: list[str]
    skipped: list[Param]  # parameters that did not fit in 256 slots
    hidden: list[str] = field(default_factory=list)  # devices left off the EC4 ("0 polysynth")

    def signature(self) -> tuple:
        return tuple((s.group, s.encoder, s.param.key, s.short) for s in self.slots) + tuple(self.group_names)

    def encoder_names(self) -> list[list[str | None]]:
        grid: list[list[str | None]] = [[None] * 16 for _ in range(16)]
        for s in self.slots:
            grid[s.group][s.encoder] = s.short
        return grid


def strip_prefixes(name: str, prefixes: list[str]) -> str:
    """Remove the first matching prefix (case-insensitive), e.g. 'j.reverb' -> 'reverb'."""
    for pre in prefixes:
        if pre and name.lower().startswith(pre.lower()) and len(name) > len(pre):
            return name[len(pre):]
    return name


def device_hidden(p: Param, patterns: list, prefixes: list[str]) -> bool:
    """True if the parameter's device (RNBO instance) matches hide_devices.

    An entry of only digits is an instance number ("2" = /rnbo/inst/2); anything else is a
    case-insensitive pattern searched in the device name, with and without strip_prefixes
    ("reverb" hides "j.reverb" and "big_reverb"; "^mixer$" hides only "mixer").
    """
    names = {p.inst_name, strip_prefixes(p.inst_name, prefixes)}
    for pat in patterns:
        pat = str(pat).strip()
        if not pat:
            continue
        if pat.isdigit():
            if int(pat) == p.inst:
                return True
        elif any(re.search(pat, n, re.IGNORECASE) for n in names):
            return True
    return False


def _matches(patterns: list[str], p: Param) -> bool:
    return any(re.search(pat, p.key) or re.search(pat, p.label) for pat in patterns)


def build_layout(params: list[Param], cfg: dict) -> Layout:
    include = cfg.get("include") or []
    exclude = cfg.get("exclude") or []
    name_overrides: dict = cfg.get("names") or {}
    group_overrides: dict = cfg.get("group_names") or {}
    new_group_per_instance = cfg.get("new_group_per_instance", True)
    prefixes = cfg.get("strip_prefixes") or []
    case = cfg.get("name_case") or "keep"

    hide = cfg.get("hide_devices") or []
    hidden: list[str] = []
    visible: list[Param] = []
    for p in params:
        if hide and device_hidden(p, hide, prefixes):
            label = f"{p.inst} {p.inst_name}"
            if label not in hidden:
                hidden.append(label)
        else:
            visible.append(p)
    chosen = [p for p in visible if (not include or _matches(include, p)) and not _matches(exclude, p)]

    slots: list[Slot] = []
    skipped: list[Param] = []
    group_owner: list[tuple[int, str] | None] = [None] * 16  # (inst, inst_name) of first param in group
    g, e = 0, 0
    last_inst = None
    for p in chosen:
        if last_inst is not None and p.inst != last_inst and new_group_per_instance and e != 0:
            g, e = g + 1, 0
        last_inst = p.inst
        if g >= 16:
            skipped.append(p)
            continue
        if group_owner[g] is None:
            group_owner[g] = (p.inst, p.inst_name)
        short = name_overrides.get(p.key) or name_overrides.get(p.pid) or abbreviate(strip_prefixes(p.label, prefixes), case=case)
        slots.append(Slot(g, e, p, short[:4]))
        e += 1
        if e == 16:
            g, e = g + 1, 0

    # make names unique inside each group
    for grp in range(16):
        in_group = [s for s in slots if s.group == grp]
        for s, nm in zip(in_group, _dedupe([s.short for s in in_group])):
            s.short = nm

    # group names: instance name, plus a page number when an instance spans several groups
    group_names = [""] * 16
    pages: dict[int, int] = {}
    span: dict[int, int] = {}
    for owner in group_owner:
        if owner:
            span[owner[0]] = span.get(owner[0], 0) + 1
    for grp, owner in enumerate(group_owner):
        if not owner:
            continue
        inst, iname = owner
        iname = strip_prefixes(iname, prefixes)
        pages[inst] = pages.get(inst, 0) + 1
        ov = group_overrides.get(str(inst))
        if span[inst] > 1:
            base = (ov or abbreviate(iname, 3, case))[:3]
            group_names[grp] = base + (str(pages[inst]) if pages[inst] < 10 else SUFFIX_CHARS[pages[inst] - 2])
        else:
            group_names[grp] = (ov or abbreviate(iname, case=case))[:4]
    style = cfg.get("group_title_style") or "instance"
    if style == "number":
        # fixed titles that stay true whatever graph is loaded (all 16 groups, used or not)
        group_names = [f"G{g + 1:02d}" for g in range(16)]
    elif style == "blank":
        group_names = [""] * 16
    return Layout(slots, group_names, skipped, hidden)


def format_table(layout: Layout) -> str:
    lines = []
    cur = None
    for s in layout.slots:
        if s.group != cur:
            cur = s.group
            lines.append(f"\nGroup {s.group + 1:>2} [{layout.group_names[s.group]:<4}]  (MIDI ch {s.group + 1})")
        p = s.param
        lines.append(f"  enc {s.encoder + 1:>2}  {s.short:<4}  inst {p.inst}  {p.pid:<28} {p.label}")
    if layout.skipped:
        lines.append(f"\n{len(layout.skipped)} parameter(s) did not fit (more than 256):")
        lines += [f"  {p.key}" for p in layout.skipped]
    if layout.hidden:
        lines.append("\nHidden devices (hide_devices): " + ", ".join(layout.hidden))
    if not layout.slots:
        lines.append("No parameters found (is a patcher loaded in the runner?)" if not layout.hidden
                     else "No parameters left after hiding devices.")
    return "\n".join(lines).lstrip("\n")
