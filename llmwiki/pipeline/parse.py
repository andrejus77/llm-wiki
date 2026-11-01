"""Stage output fixers. Philosophy: the script repairs whatever it can; a retry is requested only when the output is
unusable (broken schema, a missing note for an email, an empty section). Each fixer returns (fixed_dict | None, errors).
`final=True` means this is the last allowed round: accept partial results instead of failing."""
from __future__ import annotations

import difflib
import re
from datetime import date

from llmwiki.pipeline.templates import free, fuzzy_key, keys

KINDS = {"decision", "issue", "request", "info", "done"}
KIND_ALIAS = {"question": "request", "action": "request", "task": "request", "problem": "issue", "incident": "issue", "fyi": "info",
              "update": "info", "information": "info", "resolved": "done", "completed": "done", "closed": "done", "decided": "decision"}
IMP_ALIAS = {"high": "important", "critical": "important", "medium": "routine", "normal": "routine", "low": "routine",
             "none": "not_important", "unimportant": "not_important", "not important": "not_important", "noise": "not_important", "spam": "not_important"}


def _int(x):
    try:
        return int(str(x).strip().strip("[]#").lstrip("Ee"))
    except Exception:
        return None


def fix_note(d: dict, max_chars: int) -> dict:
    note = re.sub(r"\s+", " ", str(d.get("note") or d.get("summary") or "")).strip()
    if len(note) > max_chars:
        cut = note[:max_chars]
        note = cut[: cut.rfind(" ")] + "…" if " " in cut else cut
    kind = str(d.get("kind") or "info").strip().lower()
    kind = kind if kind in KINDS else KIND_ALIAS.get(kind, "info")
    imp = str(d.get("importance") or "routine").strip().lower().replace("-", "_")
    imp = imp if imp in ("important", "routine", "not_important") else IMP_ALIAS.get(imp, "routine")
    due = d.get("due")
    due = str(due).strip() if due not in (None, "", "null", "None") else ""
    if due:
        try:
            date.fromisoformat(due[:10])
            due = due[:10]
        except ValueError:
            due = ""
    ai = d.get("ai", d.get("ai_related", False))
    ai = ai if isinstance(ai, bool) else str(ai).strip().lower() in ("true", "yes", "1", "y")
    return {"note": note, "kind": kind, "importance": imp, "due": due, "ai": ai}


def parse_notes(obj, n: int, max_chars: int, final: bool) -> tuple[dict | None, list[str]]:
    notes = obj.get("notes") if isinstance(obj, dict) else obj if isinstance(obj, list) else None
    if notes is None and isinstance(obj, dict) and ("note" in obj) and n == 1:
        notes = [dict(obj, email=1)]
    if not isinstance(notes, list):
        return None, ['the answer must have a "notes" list']
    out = {}
    for i, d in enumerate(notes):
        if not isinstance(d, dict):
            continue
        k = _int(d.get("email", d.get("ref", d.get("n", d.get("id", i + 1)))))
        if k is None or not 1 <= k <= n:
            continue
        f = fix_note(d, max_chars)
        if f["note"]:
            out[k] = f
    missing = [k for k in range(1, n + 1) if k not in out]
    if missing and not (final and out):
        return None, [f"missing a note for email(s) {', '.join(f'[{m}]' for m in missing)}: give exactly one note per email 1..{n}"]
    return {"notes": out, "missing": missing}, []


def _match_page(label, cand_labels: dict, cand_titles: dict) -> str | None:
    s = str(label or "").strip()
    m = re.match(r"^\[?p\s*(\d+)\]?$", s, re.I)
    if m and f"P{int(m.group(1))}" in cand_labels:
        return f"P{int(m.group(1))}"
    if s.isdigit() and f"P{int(s)}" in cand_labels:
        return f"P{int(s)}"
    if s.lower() in ("new", "new page", "none", ""):
        return None
    titles = {t.lower(): lab for lab, t in cand_titles.items()}
    mm = difflib.get_close_matches(s.lower(), list(titles), n=1, cutoff=0.75)
    return titles[mm[0]] if mm else None


def parse_route(obj, n: int, cands: dict, max_pages: int, final: bool) -> tuple[dict | None, list[str]]:
    """cands: label -> Page. Result: {"pages": [{"label": "P1"|None, "title", "level", "why", "role", "items": [(key, text, [nums])], "status", "open_add", "open_close"}]}"""
    pages = obj.get("pages") if isinstance(obj, dict) else obj if isinstance(obj, list) else None
    if pages is None and isinstance(obj, dict) and obj.get("page"):
        pages = [obj]
    if pages is None:
        return None, ['the answer must have a "pages" list (it may be empty)']
    titles = {lab: p.title for lab, p in cands.items()}
    free_structure = free()
    out, dropped = [], []
    for d in pages:
        if not isinstance(d, dict):
            continue
        lab = _match_page(d.get("page") or d.get("label") or d.get("id"), cands, titles)
        is_new = lab is None
        if is_new:
            title = str(d.get("title") or "").strip()
            if not title:
                # an unknown label with no title: try it as a title
                maybe = str(d.get("page") or "").strip()
                if maybe and not re.match(r"^\[?p\s*\d+\]?$|^new$", maybe, re.I):
                    title = maybe
            if not title:
                dropped.append(str(d.get("page")))
                continue
            level = str(d.get("level") or "topic").lower()
            level = level if level in ("task", "topic", "project") else "topic"
        else:
            level = cands[lab].level
            title = cands[lab].title
        items = []
        for it in d.get("items") or []:
            if isinstance(it, str):
                it = {"text": it}
            if not isinstance(it, dict):
                continue
            text = str(it.get("text") or it.get("fact") or "").strip()
            if not text:
                continue
            key = "page" if free_structure else fuzzy_key(level, str(it.get("section") or "")) or keys(level)[0]
            nums = [x for x in (_int(v) for v in (it.get("emails") or it.get("refs") or [])) if x and 1 <= x <= n] or list(range(1, n + 1))
            items.append((key, text, nums))
        role = "primary" if str(d.get("role") or "").lower() == "primary" else "secondary"
        out.append({"label": lab, "title": title, "level": level, "why": str(d.get("why") or "")[:500], "role": role, "items": items,
                    "status": d.get("status"), "open_add": [str(x) for x in (d.get("open_add") or []) if x][:10],
                    "open_close": [str(x) for x in (d.get("open_close") or []) if x][:10]})
    if out and not any(p["role"] == "primary" for p in out):
        out[0]["role"] = "primary"
    seen, uniq = set(), []
    for p in out:  # one entry per page
        k = p["label"] or ("new:" + p["title"].lower())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    prim = [p for p in uniq if p["role"] == "primary"][:1]
    rest = [p for p in uniq if p not in prim][: max_pages - 1]
    for p in rest:
        p["role"] = "secondary"
    return {"pages": prim + rest, "dropped": dropped}, []


def parse_page(obj, final: bool) -> tuple[dict | None, list[str]]:
    """Whole-page rewrite (free structure): {"summary", "sections": [{"title", "text"}], "removed"}. Sections with the same
    title are joined; a section titled Summary/Overview stands in for a missing summary."""
    if not isinstance(obj, dict):
        return None, ['the answer must be a JSON object with "summary" and "sections"']
    raw = obj.get("sections")
    if raw is None:
        raw = []
    if isinstance(raw, dict):   # {"Title": "text", ...}
        raw = [{"title": k, "text": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return None, ['"sections" must be a list of {"title": "...", "text": "..."}']
    secs: dict[str, list] = {}
    for d in raw:
        if not isinstance(d, dict):
            continue
        title = re.sub(r"\s+", " ", str(d.get("title") or d.get("section") or d.get("heading") or "")).strip().strip("#*: ")[:120]
        text = str(d.get("text") or d.get("content") or "").strip()
        if not text:
            continue
        title = title or "Details"
        k = title.lower()
        if k in secs:
            secs[k][1] += "\n\n" + text
        else:
            secs[k] = [title, text]
    summary = str(obj.get("summary") or obj.get("overview") or "").strip()
    if not summary:
        for k in ("summary", "overview"):
            if k in secs:
                summary = secs.pop(k)[1]
                break
    if not summary:
        return None, ['"summary" is empty: write 2–4 sentences on what this is, where it stands and what is next']
    return {"summary": summary, "sections": [tuple(v) for v in secs.values()], "removed": str(obj.get("removed") or obj.get("removed_reason") or "").strip()}, []


def parse_sections(obj, allowed: list[str], level: str, final: bool) -> tuple[dict | None, list[str]]:
    secs = obj.get("sections") if isinstance(obj, dict) else obj if isinstance(obj, list) else None
    if secs is None and isinstance(obj, dict) and "text" in obj and len(allowed) == 1:
        secs = [dict(obj, section=allowed[0])]
    if not isinstance(secs, list):
        return None, ['the answer must have a "sections" list']
    out, errs = {}, []
    for d in secs:
        if not isinstance(d, dict):
            continue
        k = fuzzy_key(level, str(d.get("section") or ""))
        if k not in allowed:
            if len(allowed) == 1 and len(secs) == 1:
                k = allowed[0]
            else:
                continue
        text = str(d.get("text") or "").strip()
        out[k] = {"text": text, "removed_reason": str(d.get("removed_reason") or "").strip()}
    empty = [k for k in allowed if k in out and not out[k]["text"]]
    missing = [k for k in allowed if k not in out]
    if empty:
        errs.append("these sections came back empty: " + ", ".join(empty) + " — return the full text of each section")
    if missing and not final:
        errs.append("missing sections: " + ", ".join(missing))
    if errs and not (final and any(v["text"] for v in out.values())):
        return None, errs
    return {"sections": {k: v for k, v in out.items() if v["text"]}}, []
