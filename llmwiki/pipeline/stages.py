"""S1 Notes, S2 Route and the combined fast path (S12 batch / S123 live).

Each function builds its prompt from budgeted sections, calls the pool (which handles rate limits and one
validation retry), lets the script fix the output, and writes facts by script.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.llm.pool import pool
from llmwiki.models import Email, Page, QuotedBlock
from llmwiki.pipeline import context as cx
from llmwiki.pipeline import pages as pg
from llmwiki.pipeline import parse, prompts
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.pipeline.templates import fuzzy_key
from llmwiki.pipeline.templates import keys as tpl_keys
from llmwiki.rag import search

log = logging.getLogger(__name__)


def _final_counter(max_rounds: int):
    state = {"n": 0}

    def is_final():
        state["n"] += 1
        return state["n"] > max_rounds
    return is_final


# ------------------------------------------------------------------ S1
def s1_sections(unit: cx.Unit, b: dict) -> list[Section]:
    return [
        Section("new", "Emails of this unit (new text)", [cx.email_block(i, e) for i, e in enumerate(unit.emails, 1)], b["new"], min_each=1500),
        Section("parent", "The email they reply to", [cx.parent_text(unit)] if cx.parent_text(unit) else [], b["parent"]),
        Section("unseen", "Quoted earlier mail that is not stored separately (context)", cx.unseen_text(cx.unseen_blocks(unit)), b["unseen"]),
        Section("outline", "Earlier in this thread (one line per email)", ["\n".join(cx.outline_text(unit))] if cx.outline_text(unit) else [], b["outline"]),
        Section("att", "Attachment extracts", cx.attachment_texts(unit), b["att"]),
    ]


def apply_notes(unit: cx.Unit, notes: dict):
    """Write the notes; then the script's own checks: automated mail (ingest/automated.py) rated 'routine' by the model is
    capped to pipeline.automated_cap, so system confirmations stop before routing."""
    from llmwiki.ingest import automated
    now = timezone.now()
    cap = get_settings().pipeline.automated_cap
    auto = set(automated.mark(unit.emails))
    for i, e in enumerate(unit.emails, 1):
        n = notes.get(i)
        if not n:
            continue
        imp = n["importance"]
        if e.id in auto and cap != "off" and imp == "routine":
            imp = cap
        ai = bool(n.get("ai"))
        Email.objects.filter(id=e.id).update(note=n["note"], kind=n["kind"], importance=imp, due=n["due"], ai_related=ai, noted_at=now, status="noted")
        e.note, e.kind, e.importance, e.due, e.ai_related, e.status = n["note"], n["kind"], imp, n["due"], ai, "noted"
    fps = list(QuotedBlock.objects.filter(email_id__in=unit.ids, linked__isnull=True).values_list("fingerprint", flat=True))
    # thread-wide: the same quoted text in later replies is never sent again
    QuotedBlock.objects.filter(email__thread_id=unit.thread_id, fingerprint__in=fps).update(sent_to_model=True)
    from llmwiki.ingest import skiprules
    for e in unit.emails:
        skiprules.audit_result(e)


def s1(unit: cx.Unit, job=None) -> dict:
    s = get_settings()
    system = prompts.s1_system()
    is_final = _final_counter(s.pool.validation_retries)

    def build(cap, scale):
        text, meta = assemble(s1_sections(unit, s.budgets.s1), cap, s.budgets.cut_order, system, scale)
        return system, f"TASK: s1\nEmails: {len(unit.emails)}\n\n" + text, meta

    res = pool().run_structured("s1", unit.key, build, lambda o: parse.parse_notes(o, len(unit.emails), s.pipeline.note_max_chars, is_final()), job=job)
    apply_notes(unit, res.obj["notes"])
    return {"notes": res.obj["notes"], "missing": res.obj["missing"], "calls": res.calls}


# ------------------------------------------------------------------ candidates
def candidates(unit: cx.Unit, query: str, k: int) -> tuple[dict, list[str], float]:
    """Return (label -> Page, rendered cards, best cosine). Thread memory pages are always included."""
    mem = cx.memory_pages(unit)
    found = search.candidate_pages(query, k) if query.strip() else []
    ids, hits = [], {}
    for p in mem:
        if p.id not in ids:
            ids.append(p.id)
    for d in found:
        if d["page_id"] not in ids:
            ids.append(d["page_id"])
        hits[d["page_id"]] = d["sections"]
    ids = ids[: max(k, len(mem))]
    pages = {p.id: p for p in Page.objects.filter(id__in=ids, archived=False).prefetch_related("sections")}
    labels, cards = {}, []
    for i, pid in enumerate([x for x in ids if x in pages], 1):
        lab = f"P{i}"
        labels[lab] = pages[pid]
        tag = " (this thread already fed this page)" if any(p.id == pid for p in mem) else ""
        cards.append(cx.page_card(lab, pages[pid], hits.get(pid)) + tag)
    best = max((d["cos"] for d in found), default=0.0)
    return labels, cards, best


# ------------------------------------------------------------------ apply a route
def new_page_threshold() -> float:
    s = get_settings()
    from llmwiki.rag.embed import get_embedder
    name = get_embedder().name
    return s.pipeline.new_page_threshold_by_model.get(name, s.pipeline.new_page_threshold)


_TITLE_STOP = {"and", "the", "for", "with", "from", "of", "to", "in", "on", "at", "a", "an", "new", "not", "yet"}


def title_overlap(a: str, b: str) -> float:
    """Share of the shorter title's content words that also occur in the other title."""
    import re as _re
    wa = {w for w in _re.findall(r"[\w가-힣]{3,}", (a or "").lower()) if w not in _TITLE_STOP}
    wb = {w for w in _re.findall(r"[\w가-힣]{3,}", (b or "").lower()) if w not in _TITLE_STOP}
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0


def new_or_existing(d: dict, unit: cx.Unit, summary: dict, shown_ids: set[int] | None = None):
    """The new-page rule, checked against a FRESH search under the creation lock.

    - If the best match was among the candidates the model saw and it still asked for a new page, the model's judgement
      wins (embedding cosines between different logistics topics are high, e.g. 0.95 for e5) — only a near-identical
      title is merged (create_page_locked).
    - If the best match was NOT shown to the model (created moments ago by a parallel unit, or missed by retrieval),
      reuse it when the cosine is above the model's threshold. This is what prevents duplicate pages from parallel units."""
    thr = new_page_threshold()
    probe = d["title"] + "\n" + " ".join(t for _, t, _ in d["items"])[:1200]
    shown_ids = shown_ids or set()
    with pg._create_lock:
        found = search.candidate_pages(probe, 3)
        best = next((f for f in found if f["page_id"] not in shown_ids), None)
        best_title = Page.objects.filter(id=best["page_id"]).values_list("title", flat=True).first() if best else ""
        # cosine alone can't separate "same topic" from "same company" (0.889 vs 0.885 measured), so the titles must overlap too
        if best and best["cos"] >= thr and title_overlap(d["title"], best_title) >= 0.5:
            page = Page.objects.get(id=best["page_id"])
            summary["merged_new"].append({"title": d["title"], "into": page.title, "cos": round(best["cos"], 3)})
            log.info("new page '%s' refused: '%s' has cosine %.3f ≥ %.2f", d["title"], page.title, best["cos"], thr)
            return page, False
        human = [e for e in unit.emails if not e.automated]
        size = {"emails": len(human), "days": len({e.day for e in human}), "people": len({e.from_addr for e in human})}
        page, created = pg.create_page_locked(d["title"], d["level"], d["why"] or f"routed from unit {unit.key}",
                                              start_level=pg.effective_level(d["level"], size))
        if not created:
            summary["merged_new"].append({"title": d["title"], "into": page.title, "cos": None})
            return page, False
        first = [(k, t, []) for k, t, _ in d["items"]]
        pg.provisional_overview(page, first)
        try:
            from llmwiki.rag.index import index_pages
            index_pages([page.id])   # visible to the next unit's routing immediately
        except Exception:
            log.exception("indexing the new page failed")
        return page, True


def apply_route(unit: cx.Unit, route: dict, labels: dict, best_cos: float, window: str) -> dict:
    summary = {"pages": [], "items": 0, "new_pages": [], "merged_new": []}
    num2id = {i: e.id for i, e in enumerate(unit.emails, 1)}
    last_email = unit.emails[-1]
    human = [e for e in unit.emails if not e.automated]
    for d in route["pages"]:
        page, created = None, False
        if d["label"]:
            page = labels[d["label"]]
        else:
            if not human:   # automated mail alone never creates a page (it may still feed an existing one, e.g. an error report)
                summary.setdefault("refused_new", []).append({"title": d["title"], "why": "only automated mail"})
                log.info("new page '%s' refused: unit %s has only automated mail", d["title"], unit.key)
                continue
            page, created = new_or_existing(d, unit, summary, {p.id for p in labels.values()})
        # section keys were mapped for the level the model gave; remap if the page turned out to be another level
        pk = tpl_keys(page.level)
        items = [(fk if fk == "page" or fk in pk else (fuzzy_key(page.level, fk) or pk[0]), text, [num2id[n] for n in nums if n in num2id])
                 for fk, text, nums in d["items"]]
        linked = [e for e in unit.emails if any(e.id in it[2] for it in items)] or unit.emails
        pg.link_emails(page, linked, d["role"])
        n = pg.add_items(page, items, unit.key, window)
        if created:
            pg.provisional_overview(page, items)
            summary["new_pages"].append(page.title)
        if d["role"] == "primary":
            pg.set_status(page, d.get("status"), last_email)
            pg.open_issues(page, d.get("open_add"), d.get("open_close"), last_email)
        summary["pages"].append({"page": page.id, "title": page.title, "role": d["role"], "items": n, "created": created})
        summary["items"] += n
    now = timezone.now()
    Email.objects.filter(id__in=unit.ids, status="noted").update(status="processed", processed_at=now)
    return summary


# ------------------------------------------------------------------ S2
def s2(unit: cx.Unit, window: str, job=None) -> dict:
    s = get_settings()
    useful = [e for e in unit.emails if e.importance in ("important", "routine") and e.skip_action != "note_only"]
    if not useful:
        Email.objects.filter(id__in=unit.ids, status="noted").update(status="processed", processed_at=timezone.now())
        return {"skipped": "every note is not_important or note-only"}
    query = "\n".join(e.note for e in useful if e.note)
    labels, cards, best = candidates(unit, query, s.budgets.candidates_k)
    system = prompts.s2_system()
    mem = cx.memory_pages(unit)
    is_final = _final_counter(s.pool.validation_retries)

    def build(cap, scale):
        b = s.budgets.s2
        notes = "\n".join(f"[{i}] {e.kind} · {e.importance}{' · automated' if e.automated else ''} · {e.note}" for i, e in enumerate(unit.emails, 1))
        secs = [Section("emails", "Emails of this unit", [cx.email_block(i, e) for i, e in enumerate(unit.emails, 1)], b["emails"], min_each=1200),
                Section("notes", "Notes (from the note-taker)", [notes], 2000),
                Section("cand", "Candidate pages (choose from these)", cards or ["(no pages yet — every page you need is new)"], b["cand"]),
                Section("memory", "Pages this thread already fed", [", ".join(p.title for p in mem)] if mem else [], b["memory"])]
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        return system, f"TASK: s2\nEmails: {len(unit.emails)}\n\n" + text, meta

    res = pool().run_structured("s2", unit.key, build, lambda o: parse.parse_route(o, len(unit.emails), labels, s.pipeline.max_pages_per_unit, is_final()), job=job)
    summary = apply_route(unit, res.obj, labels, best, window)
    summary["calls"] = res.calls
    summary["candidates"] = {lab: p.title for lab, p in labels.items()}
    summary["best_cos"] = round(best, 3)
    return summary


# ------------------------------------------------------------------ fast path S12 / S123 (and the single note-taker fallback)
def fast(unit: cx.Unit, window: str, edit: bool, job=None) -> dict:
    s = get_settings()
    system = prompts.s12_system(edit)
    e0 = unit.emails[0]
    query = (e0.subject + "\n" + " ".join(e0.new_text.split()[:300])) if len(unit.emails) == 1 else "\n".join(e.subject + " " + " ".join(e.new_text.split()[:80]) for e in unit.emails)
    labels, cards, best = candidates(unit, query, s.budgets.candidates_k)
    mem = cx.memory_pages(unit)
    cur_secs = []
    if edit and labels:
        p1 = labels["P1"]
        hits = search.hybrid(query, {"collections": ["wiki"], "page_ids": [p1.id]}, top_k=4, recency=False)
        keys_hit = []
        for h in hits:
            k = h.chunk.ref.split(":")[-1]
            if k not in keys_hit:
                keys_hit.append(k)
        for k in keys_hit[:2]:
            sec = p1.sections.filter(key=k).first()
            if sec:
                cur_secs.append((k, sec.text))
    is_final = _final_counter(s.pool.validation_retries)
    budget_total = s.budgets.fast["s123" if edit else "s12"]

    def build(cap, scale):
        b1, b2 = s.budgets.s1, s.budgets.s2
        secs = s1_sections(unit, b1)
        secs += [Section("cand", "Candidate pages (choose from these)", cards or ["(no pages yet — every page you need is new)"], b2["cand"]),
                 Section("memory", "Pages this thread already fed", [", ".join(p.title for p in mem)] if mem else [], b2["memory"])]
        if cur_secs:
            secs.append(Section("current", "Current sections of P1 (rewrite these if your items belong there)",
                                [f"### section: {k}\n{t or '(empty)'}" for k, t in cur_secs], s.budgets.s3["current"]))
        text, meta = assemble(secs, min(cap, budget_total), s.budgets.cut_order, system, scale)
        return system, f"TASK: {'s123' if edit else 's12'}\nEmails: {len(unit.emails)}\n\n" + text, meta

    def p(o):
        fin = is_final()
        notes, errs = parse.parse_notes(o, len(unit.emails), s.pipeline.note_max_chars, fin)
        if notes is None:
            return None, errs
        route, errs2 = parse.parse_route(o if isinstance(o, dict) and "pages" in o else {"pages": []}, len(unit.emails), labels, s.pipeline.max_pages_per_unit, fin)
        if route is None:
            return None, errs2
        secs = None
        if edit and cur_secs and isinstance(o, dict) and o.get("sections"):
            secs, _ = parse.parse_sections(o, [k for k, _ in cur_secs], labels["P1"].level, True)
        return {"notes": notes, "route": route, "sections": secs}, []

    stage = "s123" if edit else "s12"
    res = pool().run_structured(stage, unit.key, build, p, job=job)
    apply_notes(unit, res.obj["notes"]["notes"])
    useful = [e for e in unit.emails if e.importance in ("important", "routine") and e.skip_action != "note_only"]
    if not useful:
        Email.objects.filter(id__in=unit.ids, status="noted").update(status="processed", processed_at=timezone.now())
        return {"notes": res.obj["notes"]["notes"], "skipped_route": True, "calls": res.calls}
    summary = apply_route(unit, res.obj["route"], labels, best, window)
    summary["notes"] = res.obj["notes"]["notes"]
    summary["calls"] = res.calls
    summary["candidates"] = {lab: pp.title for lab, pp in labels.items()}
    if edit and res.obj.get("sections") and labels:
        from llmwiki.pipeline.editor import apply_direct_edit
        summary["edits"] = apply_direct_edit(labels["P1"], res.obj["sections"]["sections"], unit, res.calls[-1] if res.calls else None)
    return summary
