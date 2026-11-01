"""Prompt assembly with per-section token budgets and a cut order.

Each request is a list of Sections (key, label, items). A section's budget is shared fairly between its items
(water-filling: small items keep everything, big ones share the rest). If the total is still over the cap, sections
are cut in `budgets.cut_order` down to their minimum. Every request records want / budget / sent per section, which
the portal shows on Agent › Pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from llmwiki.util import clip_tokens, est_tokens


@dataclass
class Section:
    key: str
    label: str
    items: list[str] = field(default_factory=list)   # one string per email/page/etc.
    budget: int = 0
    min_each: int = 0          # never cut an item below this (e.g. new emails keep 1.5k each)
    sep: str = "\n\n"
    title: bool = True         # print "## label" before it


def _fair(items: list[str], budget: int, min_each: int) -> list[str]:
    if not items:
        return []
    sizes = [est_tokens(x) for x in items]
    if sum(sizes) <= budget:
        return list(items)
    alloc = [0] * len(items)
    left = budget
    open_ = list(range(len(items)))
    while open_ and left > 0:
        share = left // len(open_)
        if share <= 0:
            break
        done = []
        for i in open_:
            give = min(sizes[i] - alloc[i], share)
            alloc[i] += give
            left -= give
            if alloc[i] >= sizes[i]:
                done.append(i)
        if not done:
            break
        open_ = [i for i in open_ if i not in done]
    return [x if alloc[i] >= sizes[i] else clip_tokens(x, max(alloc[i], min_each)) for i, x in enumerate(items)]


def assemble(sections: list[Section], cap: int, cut_order: list[str], system: str, scale: float = 1.0) -> tuple[str, list[dict]]:
    """Return (prompt_text, meta). `scale` < 1 shrinks every budget (used after a timeout)."""
    sys_tok = est_tokens(system)
    rendered, meta = {}, []
    for s in sections:
        want = sum(est_tokens(x) for x in s.items)
        b = int(s.budget * scale) if s.budget else want
        parts = _fair(s.items, b, int(s.min_each * scale))
        rendered[s.key] = parts
        meta.append({"key": s.key, "label": s.label, "want": want, "budget": b, "sent": sum(est_tokens(x) for x in parts), "cut_budget": 0, "cut_cap": 0})
    for m in meta:
        m["cut_budget"] = max(0, m["want"] - m["sent"])
    total = sys_tok + sum(m["sent"] for m in meta) + 50
    if total > cap:
        over = total - cap
        order = [k for k in cut_order if k in rendered] + [s.key for s in sections if s.key not in cut_order]
        for key in order:
            if over <= 0:
                break
            sec = next(s for s in sections if s.key == key)
            m = next(x for x in meta if x["key"] == key)
            floor = sec.min_each * len(rendered[key]) if sec.min_each else 0
            can = max(0, m["sent"] - floor)
            if can <= 0:
                continue
            target = max(floor, m["sent"] - over)
            parts = _fair(rendered[key], target, sec.min_each) if target > 0 else []
            new_sent = sum(est_tokens(x) for x in parts)
            m["cut_cap"] = m["sent"] - new_sent
            over -= m["cut_cap"]
            m["sent"] = new_sent
            rendered[key] = parts
    out = []
    for s in sections:
        parts = [p for p in rendered[s.key] if p]
        if not parts:
            continue
        out.append((f"## {s.label}\n" if s.title else "") + s.sep.join(parts))
    return "\n\n".join(out), meta


def over_cap(meta: list[dict], system: str, cap: int) -> bool:
    return est_tokens(system) + sum(m["sent"] for m in meta) > cap
