"""S3 Editor: rewrites the sections of one page that received new items, through the guards.

Flow per request (≤ budgets.s3_max_sections sections):
 1. draft  ← current sections + items + the source emails in full + notes + related sections on other pages (RAG)
 2. guards (guards.py): hard reject / warnings
 3. confirm round on warnings (or always/never per settings): the model sees ORIGINAL, DRAFT and a script diff
 4. accepted → new section version (history row), items marked written; rejected → history row (accepted=False),
    original kept, items stay pending ("not yet written in") and are retried next window (max 3 attempts).
Source markers: the model may write [n] (n = the prompt's email list); stored as [e<email id>]; unknown numbers dropped.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.llm.pool import Halted, StageFailed, pool
from llmwiki.logs import event
from llmwiki.models import Email, Item, OpenIssue, Page, Section
from llmwiki.pipeline import context as cx
from llmwiki.pipeline import guards, parse, prompts
from llmwiki.pipeline import pages as pg
from llmwiki.pipeline.budget import Section as PSec
from llmwiki.pipeline.budget import assemble
from llmwiki.pipeline.templates import describe, free, label_of, section_label
from llmwiki.rag import search
from llmwiki.util import first_sentence, slugify

log = logging.getLogger(__name__)
MARK = re.compile(r"\[(\d{1,3})\]")
STORED = re.compile(r"\[e(\d+)\]")


def to_prompt_markers(text: str, num_of: dict[int, int]) -> str:
    """Stored [e123] → the prompt's [n] if that email is in the list, else removed."""
    return STORED.sub(lambda m: f"[{num_of[int(m.group(1))]}]" if int(m.group(1)) in num_of else "", text or "")


def from_prompt_markers(text: str, id_of: dict[int, int]) -> tuple[str, list[int]]:
    used = []

    def rep(m):
        n = int(m.group(1))
        if n in id_of:
            used.append(id_of[n])
            return f"[e{id_of[n]}]"
        return ""
    out = MARK.sub(rep, text or "")
    if get_settings().guards.inline_markers == "off":
        out = STORED.sub("", out)
    return re.sub(r"\s+([.,;:])", r"\1", out).strip(), used


def _sources_for(items: list[Item]) -> list[Email]:
    ids = []
    for it in items:
        for x in it.emails:
            if x not in ids:
                ids.append(x)
    em = {e.id: e for e in Email.objects.filter(id__in=ids).prefetch_related("attachments")}
    return sorted([em[i] for i in ids if i in em], key=lambda e: e.sent_at)


def edit_page(page: Page, items: list[Item], job=None) -> dict:
    s = get_settings()
    if free():
        r = edit_whole(page, items, job)
        Page.objects.filter(id=page.id).update(updated_at=timezone.now())
        return {"page": page.title, "requests": [r]}
    from llmwiki.pipeline.templates import keys as tpl_keys
    tk = tpl_keys(page.level)
    by_key: dict[str, list[Item]] = {}
    for it in items:   # items routed while the structure was 'free' have no section key ("page")
        by_key.setdefault(it.section_key if it.section_key in tk else tk[0], []).append(it)
    keys = list(by_key)
    out = {"page": page.title, "requests": []}
    for k0 in range(0, len(keys), s.budgets.s3_max_sections):
        part = keys[k0:k0 + s.budgets.s3_max_sections]
        out["requests"].append(_edit_request(page, part, {k: by_key[k] for k in part}, job))
    Page.objects.filter(id=page.id).update(updated_at=timezone.now())
    return out


def _edit_request(page: Page, sec_keys: list[str], items: dict[str, list[Item]], job=None) -> dict:
    s = get_settings()
    all_items = [it for k in sec_keys for it in items[k]]
    srcs = _sources_for(all_items)
    num_of = {e.id: i for i, e in enumerate(srcs, 1)}
    id_of = {i: eid for eid, i in num_of.items()}
    current = {sec.key: sec for sec in Section.objects.filter(page=page, key__in=sec_keys)}
    old_text = {k: (current[k].text if k in current else "") for k in sec_keys}
    unit = f"ed:{page.id}:{','.join(sec_keys)}"
    # related sections on other pages (RAG): consistent names, roles and definitions
    q = " ".join(it.text for it in all_items)[:1500]
    try:
        rel = search.hybrid(q, {"collections": ["wiki"], "exclude_page_ids": [page.id]}, top_k=5, recency=False)
    except Exception as e:
        log.warning("related-section search failed: %s", e)
        rel = []
    outline = "\n".join(f"- {label_of(page.level, sec.key)} ({sec.key}): " + (first_sentence(sec.text, 160) if sec.text else "(empty)")
                        for sec in page.sections.all())
    system = prompts.s3_system()
    is_final = {"n": 0}

    def build(cap, scale):
        b = s.budgets.s3
        secs = [
            PSec("outline", f"Page: {page.title} (level {page.level}, status {page.status}) — outline", [outline + "\n\nSection keys of this level:\n" + describe(page.level)], b["outline"]),
            PSec("current", "Sections to rewrite (current text)",
                 [f"### section: {k} — {label_of(page.level, k)}\n{to_prompt_markers(old_text[k], num_of) or '(empty)'}" for k in sec_keys], b["current"], min_each=2000),
            PSec("items", "New items to write in", [f"[{k}] " + it.text + " (emails " + ", ".join(f"[{num_of[x]}]" for x in it.emails if x in num_of) + ")"
                                                     for k in sec_keys for it in items[k]], b["items"]),
            PSec("sources", "Source emails (numbered)", [cx.email_block(i, e) + (f"\nNote: {e.note}" if e.note else "") for i, e in enumerate(srcs, 1)], b["sources"], min_each=800),
            PSec("related", "Related sections on other pages (for consistent names and roles — do not copy)", [f"{h.chunk.header}\n{h.chunk.text}" for h in rel], b["related"]),
        ]
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        asof = srcs[-1].day if srcs else ""
        return system, f"TASK: s3\nSections: {', '.join(sec_keys)}\nAs of: {asof} (date of the newest source email)\n\n" + text, meta

    def p(o):
        is_final["n"] += 1
        return parse.parse_sections(o, sec_keys, page.level, is_final["n"] > s.pool.validation_retries)

    result = {"sections": sec_keys, "accepted": [], "rejected": [], "confirm": False, "calls": []}
    try:
        res = pool().run_structured("s3", unit, build, p, job=job)
    except Halted:
        raise
    except StageFailed as e:
        _fail_items(all_items, str(e))
        result["error"] = str(e)
        result["calls"] = e.calls
        event("editor", f"Editor failed on {page.title}: {e}", level="warning", page=page.id)
        return result
    result["calls"] += res.calls
    drafts = {}
    checks = {}
    for k in sec_keys:
        d = res.obj["sections"].get(k)
        if not d:
            continue
        txt, used = from_prompt_markers(guards.clean(d["text"]), id_of)
        drafts[k] = {"text": txt, "reason": d["removed_reason"], "used": used}
        checks[k] = guards.check(old_text[k], txt, [it.text for it in items[k]], d["removed_reason"])
    need_confirm = [k for k in drafts if s.guards.confirm == "always" or (s.guards.confirm == "on_warning" and (checks[k].warn or checks[k].hard))]
    final = dict(drafts)
    if need_confirm:
        result["confirm"] = True
        conf = _confirm(page, need_confirm, old_text, drafts, checks, items, num_of, id_of, job)
        result["calls"] += conf.get("calls", [])
        for k, d in conf.get("sections", {}).items():
            # keep the round-1 draft as a rejected history row, judge the confirmed text
            pg.record_rejected(page, k, drafts[k]["text"], checks[k].rows, drafts[k]["reason"], res.calls[-1] if res.calls else None, 1, drafts[k]["used"])
            final[k] = d
            checks[k] = guards.check(old_text[k], d["text"], [it.text for it in items[k]], d["reason"])
    for k, d in final.items():
        g = checks[k]
        srcs_ids = sorted({x for it in items[k] for x in it.emails})
        if g.hard:
            pg.record_rejected(page, k, d["text"], g.rows, d["reason"], result["calls"][-1] if result["calls"] else None, 2 if result["confirm"] else 1, srcs_ids)
            _fail_items(items[k], "guards: " + "; ".join(r[0] for r in g.rows if not r[1] and not r[2]))
            result["rejected"].append({"section": k, "guards": g.rows})
            event("editor", f"Kept the original of {page.title} › {label_of(page.level, k)}: the rewrite failed the guards",
                  level="warning", page=page.id, section=k, guards=[r[0] for r in g.rows if not r[1]])
            continue
        v = pg.write_section(page, k, d["text"], srcs_ids + d["used"], g.rows, d["reason"], result["calls"][-1] if result["calls"] else None,
                             2 if k in need_confirm else 1)
        Item.objects.filter(id__in=[it.id for it in items[k]]).update(status="written", written_version=v)
        result["accepted"].append({"section": k, "version": v, "warnings": [r[0] for r in g.rows if not r[1]]})
    missing = [k for k in sec_keys if k not in final]
    for k in missing:
        _fail_items(items[k], "the editor returned no text for this section")
    return result


def _confirm(page, keys_, old_text, drafts, checks, items, num_of, id_of, job) -> dict:
    s = get_settings()
    system = prompts.s3_confirm_system()
    unit = f"ed:{page.id}:{','.join(keys_)}:confirm"

    def build(cap, scale):
        parts = []
        for k in keys_:
            dsum = guards.diff_summary(old_text[k], drafts[k]["text"])
            notes = [r[0] for r in checks[k].rows if not r[1]]
            parts.append(f"### section: {k} — {label_of(page.level, k)}\nORIGINAL:\n{to_prompt_markers(old_text[k], num_of) or '(empty)'}\n\n"
                         f"DRAFT:\n{to_prompt_markers(drafts[k]['text'], num_of)}\n\nCHECKER: " + "; ".join(notes or ["no problems found, confirm or improve"])
                         + ("\nSentences of the original missing from the draft: " + " | ".join(dsum["removed_sentences"][:8]) if dsum["removed_sentences"] else "")
                         + (f"\nNames/numbers missing from the draft: {', '.join(dsum['removed_terms'][:15])}" if dsum["removed_terms"] else "")
                         + f"\nWords: {dsum['old_words']} → {dsum['new_words']}"
                         + "\nNEW ITEMS: " + " | ".join(it.text for it in items[k]))
        secs = [PSec("current", "Sections to confirm", parts, s.budgets.s3["current"] + s.budgets.s3["items"], min_each=3000)]
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        return system, f"TASK: s3c\nSections: {', '.join(keys_)}\n\n" + text, meta

    try:
        res = pool().run_structured("s3", unit, build, lambda o: parse.parse_sections(o, keys_, page.level, True), job=job, max_validation=0)
    except Halted:
        raise
    except StageFailed as e:
        log.warning("confirm round failed for %s: %s", page.title, e)
        return {"calls": e.calls}
    out = {}
    for k, d in res.obj["sections"].items():
        txt, used = from_prompt_markers(guards.clean(d["text"]), id_of)
        out[k] = {"text": txt, "reason": d["removed_reason"], "used": used}
    return {"sections": out, "calls": res.calls}


# ------------------------------------------------------------------ free structure: the whole page in one request
def _related(page: Page, q: str) -> list:
    try:
        return search.hybrid(q[:1500], {"collections": ["wiki"], "exclude_page_ids": [page.id]}, top_k=4, recency=False) if q.strip() else []
    except Exception as e:
        log.warning("related-section search failed: %s", e)
        return []


def _draft_from(obj: dict, id_of: dict[int, int]) -> tuple[guards.PageDraft, dict[str, list[int]], list[str]]:
    """Model output → a PageDraft with stored [e<id>] markers; the emails each part cites; sentences dropped as meta talk."""
    pats = get_settings().pages.drop_phrases
    used, dropped = {}, []

    def fix(key, text):
        t, u = from_prompt_markers(guards.clean(text), id_of)
        t, gone = guards.drop_meta(t, pats)
        dropped.extend(gone)
        used[key] = u
        return t
    summary = fix("summary", obj["summary"])
    secs = [(title, fix(title, text)) for title, text in obj["sections"]]
    return guards.PageDraft(summary, [(a, b) for a, b in secs if b.strip()], obj.get("removed", "")), used, dropped


def _page_text(secs: list[Section], level: str, num_of: dict[int, int]) -> str:
    return "\n\n".join(f"### {section_label(level, x.key, x.title)}\n{to_prompt_markers(x.text, num_of)}" for x in secs) or "(empty — a new page)"


def edit_whole(page: Page, items: list[Item], job=None, compact: bool = False) -> dict:
    """Rewrite the whole page (summary + the editor's own sections) with the new items, held to the page's length budget.
    compact=True also works without items: the page is rewritten to the budget (manage.py rewrite_pages)."""
    s = get_settings()
    srcs = _sources_for(items)
    if compact and not srcs:
        srcs = list(Email.objects.filter(page_links__page=page, automated=False).exclude(status__in=["duplicate", "skipped"])
                    .order_by("-sent_at").prefetch_related("attachments")[:10])[::-1]
    num_of = {e.id: i for i, e in enumerate(srcs, 1)}
    id_of = {i: eid for eid, i in num_of.items()}
    cur = [x for x in page.sections.all().order_by("position", "id") if x.text.strip()]
    if cur and cur[0].key != "summary":
        cur = sorted(cur, key=lambda x: (x.key != "summary", x.key not in ("overview", "what"), x.position, x.id))
    old_words = sum(guards.nwords(x.text) for x in cur)
    info = pg.length_info(page)
    budget = info["budget"]
    page_emails = list(Email.objects.filter(page_links__page=page).exclude(status__in=["duplicate", "skipped"]).order_by("sent_at"))
    lines = "\n".join(f"- {e.day} {e.from_name or e.from_addr}{' (automated)' if e.automated else ''} · {e.kind}: {e.note}" for e in page_emails if e.note)
    opens = [f"{o.code}: {o.text}" for o in OpenIssue.objects.filter(page=page, closed_at__isnull=True).order_by("opened_at")]
    rel = _related(page, " ".join(it.text for it in items) or page.title + " " + pg.summary_text(page))
    system = prompts.page_system(budget)
    unit = f"ed:{page.id}:page"
    asof = max((e.day for e in page_emails), default="")
    state = {"n": 0}

    def build(cap, scale):
        b = s.budgets.s3
        secs = [
            PSec("current", f"Current page: {page.title} ({page.level}, status {page.status}) — {old_words} words now; budget {budget} words "
                            f"(the mail behind it has {info['source_words']} words)", [_page_text(cur, page.level, num_of)], b["current"], min_each=3000),
            PSec("items", "New facts to write in", [it.text + " (emails " + ", ".join(f"[{num_of[x]}]" for x in it.emails if x in num_of) + ")"
                                                    for it in items], b["items"]),
            PSec("sources", "Source emails (numbered)", [cx.email_block(i, e) + (f"\nNote: {e.note}" if e.note else "") for i, e in enumerate(srcs, 1)],
                 b["sources"], min_each=800),
            PSec("outline", "Every email on this page, one line each (oldest first) — for the summary and the current state", [lines] if lines else [], b["outline"]),
            PSec("open", "Open issues (the page lists them already — do not repeat them in the text)", ["\n".join(opens)] if opens else [], 1500),
            PSec("related", "Related sections on other pages (for consistent names and roles — do not copy)", [f"{h.chunk.header}\n{h.chunk.text}" for h in rel], b["related"]),
        ]
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        mode = "Mode: compact — no new facts; rewrite the page within the budget, each fact once.\n" if compact and not items else ""
        return system, f"TASK: page\n{mode}As of: {asof} (date of the newest email on the page)\n\n" + text, meta

    def p(o):
        state["n"] += 1
        return parse.parse_page(o, state["n"] > s.pool.validation_retries)

    result = {"mode": "page", "accepted": [], "rejected": [], "confirm": False, "calls": [], "budget": budget, "words_before": old_words}
    try:
        res = pool().run_structured("s3", unit, build, p, job=job, schema="page")
    except Halted:
        raise
    except StageFailed as e:
        _fail_items(items, str(e))
        result.update(error=str(e), calls=e.calls)
        event("editor", f"Editor failed on {page.title}: {e}", level="warning", page=page.id)
        return result
    result["calls"] += res.calls
    texts = [it.text for it in items]
    draft, used, dropped = _draft_from(res.obj, id_of)
    g = guards.check_page(old_words, draft, texts, budget)
    if s.guards.confirm == "always" or (s.guards.confirm == "on_warning" and (g.warn or g.hard)):
        result["confirm"] = True
        conf = _confirm_page(page, cur, draft, g, items, num_of, id_of, budget, job)
        result["calls"] += conf.get("calls", [])
        if conf.get("obj"):
            pg.record_rejected(page, "summary", draft.render(), g.rows, "first draft (before the confirm round)", res.calls[-1] if res.calls else None, 1,
                               title="(whole-page draft)")
            d2, u2, dr2 = _draft_from(conf["obj"], id_of)
            draft, used, dropped = d2, u2, dropped + dr2
    g = guards.check_page(old_words, draft, texts, budget, final=True)
    call_id = result["calls"][-1] if result["calls"] else None
    rnd = 2 if result["confirm"] else 1
    result.update(words_after=draft.words(), dropped=dropped[:10])
    if g.hard:
        pg.record_rejected(page, "summary", draft.render(), g.rows, draft.removed, call_id, rnd, sorted({x for it in items for x in it.emails}),
                           title="(whole-page draft)")
        _fail_items(items, "guards: " + "; ".join(r[0] for r in g.rows if not r[1] and not r[2]))
        result["rejected"].append({"section": "page", "guards": g.rows})
        event("editor", f"Kept the original of {page.title}: the rewrite failed the guards", level="warning", page=page.id,
              guards=[r[0] for r in g.rows if not r[1]])
        return result
    v = _store_page(page, cur, draft, used, items, g.rows, call_id, rnd)
    Item.objects.filter(id__in=[it.id for it in items]).update(status="written", written_version=v)
    result["accepted"].append({"section": "page", "version": v, "warnings": [r[0] for r in g.rows if not r[1]]})
    if dropped:
        log.info("%s: removed %d meta sentence(s): %s", page.title, len(dropped), " | ".join(dropped[:3]))
    return result


def _confirm_page(page, cur, draft: guards.PageDraft, g, items, num_of, id_of, budget, job) -> dict:
    s = get_settings()
    system = prompts.page_confirm_system()
    unit = f"ed:{page.id}:page:confirm"
    old = "\n\n".join(x.text for x in cur)
    dsum = guards.diff_summary(old, draft.render())
    rep = guards.repeats(draft.parts(), s.pages.repeat_similarity) if s.pages.repeat_check else []
    notes = [r[0] for r in g.rows if not r[1]]

    def build(cap, scale):
        part = (f"ORIGINAL:\n{_page_text(cur, page.level, num_of)}\n\nDRAFT:\n{to_prompt_markers(draft.render(), num_of)}\n\n"
                f"CHECKER: " + "; ".join(notes or ["no problems found, confirm or improve"])
                + f"\nLimits: whole page ≤ {budget} words (draft {draft.words()}); summary ≤ {s.pages.summary_words}; ≤ {s.pages.max_sections} sections "
                  f"of ≤ {s.pages.section_words} words"
                + ("\nRepeated (say each once): " + " | ".join(f"'{a[1][:100]}' ~ '{a[3][:100]}'" for a in rep[:6]) if rep else "")
                + (f"\nNames/numbers of the original missing from the draft (keep them if still true): {', '.join(dsum['removed_terms'][:15])}"
                   if dsum["removed_terms"] else "")
                + "\nNEW FACTS: " + (" | ".join(it.text for it in items) or "(none)"))
        secs = [PSec("current", "Page to confirm", [part], s.budgets.s3["current"] + s.budgets.s3["items"], min_each=4000)]
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        return system, "TASK: pagec\n\n" + text, meta

    try:
        res = pool().run_structured("s3", unit, build, lambda o: parse.parse_page(o, True), job=job, max_validation=0, schema="page")
    except Halted:
        raise
    except StageFailed as e:
        log.warning("confirm round failed for %s: %s", page.title, e)
        return {"calls": e.calls}
    return {"obj": res.obj, "calls": res.calls}


def _match_key(title: str, existing: dict[str, Section], taken: set[str], level: str) -> str:
    """Keep a section's key (and so its history) when the editor keeps or slightly renames its title."""
    import difflib
    low = title.lower()
    slug = (slugify(title) or "section")[:50]
    free_keys = [k for k in existing if k != "summary" and k not in taken]
    for k in free_keys:
        if k == slug or section_label(level, k, existing[k].title).lower() == low:
            return k
    labels = {section_label(level, k, existing[k].title).lower(): k for k in free_keys if existing[k].text.strip()}
    m = difflib.get_close_matches(low, list(labels), n=1, cutoff=0.6)
    if m:
        return labels[m[0]]
    key, i = slug, 2
    while key in taken or (key in existing and existing[key].text.strip()) or key == "summary":
        key, i = f"{slug[:46]}-{i}", i + 1
    return key


def _store_page(page: Page, cur: list[Section], d: guards.PageDraft, used: dict[str, list[int]], items: list[Item], rows, call_id, rnd) -> int:
    existing = {x.key: x for x in page.sections.all()}
    taken, plan = {"summary"}, [("summary", "", d.summary, used.get("summary", []), 0)]
    for i, (title, text) in enumerate(d.sections, 1):
        k = _match_key(title, existing, taken, page.level)
        taken.add(k)
        plan.append((k, title, text, used.get(title, []), i))
    cited = {x for _, _, _, u, _ in plan for x in u}
    uncited = sorted({x for it in items for x in it.emails} - cited)
    reason = d.removed or ""
    version = 0
    for k, title, text, u, pos in plan:
        old = existing.get(k)
        srcs = u + (uncited if k == "summary" else [])
        if old and old.text == text and old.title == title:
            if old.position != pos or srcs:
                Section.objects.filter(id=old.id).update(position=pos, sources=sorted(set(old.sources or []) | set(srcs)))
            version = max(version, old.version)
            continue
        version = max(version, pg.write_section(page, k, text, srcs, rows, reason, call_id, rnd, title=title, position=pos))
    for k, x in existing.items():
        if k not in taken and x.text.strip():
            pg.write_section(page, k, "", [], rows, "not in the rewritten page" + (f" ({reason[:300]})" if reason else ""), call_id, rnd, position=99)
    return version


def rewrite_page(page: Page, job=None) -> dict:
    """Rewrite one page to the free structure and its length budget (also writes its pending items)."""
    items = list(Item.objects.filter(page=page, status="pending").order_by("created_at"))
    r = edit_whole(page, items, job, compact=True)
    done = {"needs_rewrite": False} if r.get("accepted") else {}
    Page.objects.filter(id=page.id).update(updated_at=timezone.now(), **done)
    return r


def _fail_items(items: list[Item], why: str):
    for it in items:
        it.attempts += 1
        it.status = "failed" if it.attempts >= 3 else "pending"
        it.save(update_fields=["attempts", "status"])
    log.info("items %s not written: %s", [it.id for it in items], why)


def apply_direct_edit(page: Page, sections: dict, unit, call_id) -> list:
    """S123 (live fast path): sections rewritten in the same request, through the same guards."""
    out = []
    num_of = {e.id: i for i, e in enumerate(unit.emails, 1)}
    id_of = {i: eid for eid, i in num_of.items()}
    pend = list(Item.objects.filter(page=page, status="pending", unit_key=unit.key))
    for k, d in sections.items():
        old = Section.objects.filter(page=page, key=k).values_list("text", flat=True).first() or ""
        txt, used = from_prompt_markers(guards.clean(d["text"]), id_of)
        its = [it for it in pend if it.section_key == k]
        g = guards.check(old, txt, [it.text for it in its], d["removed_reason"])
        if g.hard or g.warn:
            # no confirm round inside the fast path: leave the items to the regular editor
            pg.record_rejected(page, k, txt, g.rows, d["removed_reason"], call_id, 1, unit.ids)
            out.append({"section": k, "accepted": False})
            continue
        v = pg.write_section(page, k, txt, unit.ids, g.rows, d["removed_reason"], call_id)
        Item.objects.filter(id__in=[it.id for it in its]).update(status="written", written_version=v)
        out.append({"section": k, "accepted": True, "version": v})
    return out
