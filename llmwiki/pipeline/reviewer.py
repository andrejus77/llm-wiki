"""Reviewer (lint): checks changed pages for contradictions (also against similar sections on OTHER pages, via RAG),
relative dates and outdated statements; fixes go through the same guards. Also proposes merges of near-duplicate
pages (page similarity ≥ reviewer.dup_threshold) and flags over-long sections for a split."""
from __future__ import annotations

import logging
import re

import numpy as np
from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.llm.pool import StageFailed, pool
from llmwiki.logs import event
from llmwiki.models import ChunkVector, Item, Page, PageEmail
from llmwiki.pipeline import guards, prompts
from llmwiki.pipeline import pages as pg
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.pipeline.templates import free, fuzzy_key, section_label
from llmwiki.rag import search
from llmwiki.rag.embed import from_blob, get_embedder

log = logging.getLogger(__name__)


def review_page(page: Page) -> dict:
    s = get_settings()
    secs = {x.key: x for x in page.sections.all() if x.text.strip()}
    if not secs:
        return {"page": page.title, "skipped": "empty"}
    q = " ".join(x.text[:300] for x in secs.values())[:2000]
    rel = search.hybrid(q, {"collections": ["wiki"], "exclude_page_ids": [page.id]}, top_k=6, recency=False)
    system = prompts.review_system()
    stale = ", ".join(s.reviewer.stale_words)

    def build(cap, scale):
        parts = [f"### section: {k} — {section_label(page.level, k, x.title)}\n" + re.sub(r"\[e\d+\]", "", x.text) for k, x in secs.items()]
        ss = [Section("page", f"Page: {page.title} ({page.level}, status {page.status}) — sections", parts, s.budgets.s3["current"] + 6000),
              Section("related", "Similar sections on other pages", [f"{h.chunk.header}\n{h.chunk.text}" for h in rel], s.budgets.s3["related"]),
              Section("rules", "Also check", [f"Relative time words to replace with dates: {stale}. Today is {timezone.localdate().isoformat()}."], 200)]
        text, meta = assemble(ss, cap, s.budgets.cut_order, system, scale)
        return system, "TASK: review\n\n" + text, meta

    def parse(o):
        if isinstance(o, dict) and isinstance(o.get("findings"), list):
            return o, []
        return None, ['the answer must have a "findings" list (may be empty)']
    out = {"page": page.title, "fixed": [], "rejected": [], "flags": []}
    try:
        res = pool().run_structured("review", f"review:{page.id}", build, parse)
    except StageFailed as e:
        out["error"] = str(e)
        return out
    by_label = {section_label(page.level, k, x.title).lower(): k for k, x in secs.items()}
    for f in res.obj["findings"][:6]:
        name = str(f.get("section", "")).strip()
        k = name if name in secs else by_label.get(name.lower()) or fuzzy_key(page.level, name)
        new = guards.clean(str(f.get("text") or ""))
        if not k or k not in secs or not new:
            continue
        old = secs[k].text
        # keep the stored source markers the reviewer can't see
        g = guards.check(old, new, [], str(f.get("problem", "")))
        if g.hard:
            pg.record_rejected(page, k, new, g.rows, "reviewer: " + str(f.get("problem", ""))[:300], res.calls[-1])
            out["rejected"].append(k)
            continue
        markers = " ".join(dict.fromkeys(re.findall(r"\[e\d+\]", old)))
        pg.write_section(page, k, new + (" " + markers if markers and "[e" not in new else ""), secs[k].sources, g.rows,
                         "reviewer: " + str(f.get("problem", ""))[:500], res.calls[-1])
        out["fixed"].append({"section": k, "problem": str(f.get("problem", ""))[:300]})
        event("review", f"Reviewer fixed {page.title} › {section_label(page.level, k, secs[k].title)}: {str(f.get('problem', ''))[:160]}", page=page.id)
    limit = s.pages.section_words if free() else s.guards.max_words
    for k, x in secs.items():
        if guards.nwords(x.text) > limit:
            out["flags"].append(f"{section_label(page.level, k, x.title)} has {guards.nwords(x.text)} words (> {limit})")
    if free():
        info = pg.length_info(page)
        if info["words"] > info["budget"] * (1 + s.pages.tolerance):
            out["flags"].append(f"page has {info['words']} words, budget {info['budget']}: manage.py rewrite_pages --page {page.id}")
    Page.objects.filter(id=page.id).update(reviewed_at=timezone.now())
    return out


def page_vectors() -> dict[int, np.ndarray]:
    emb = get_embedder()
    rows = ChunkVector.objects.filter(model=emb.name, chunk__collection="wiki").values_list("chunk__page_id", "vector")
    acc: dict[int, list] = {}
    for pid, v in rows:
        acc.setdefault(pid, []).append(from_blob(v))
    out = {}
    for pid, vs in acc.items():
        m = np.mean(vs, axis=0)
        out[pid] = m / (np.linalg.norm(m) or 1)
    return out


def duplicates(threshold: float) -> list[tuple[int, int, float]]:
    pv = page_vectors()
    ids = [i for i in pv if Page.objects.filter(id=i, archived=False).exists()]
    pairs = []
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            sim = float(pv[ids[a]] @ pv[ids[b]])
            if sim >= threshold:
                pairs.append((ids[a], ids[b], sim))
    return sorted(pairs, key=lambda x: -x[2])


def merge_pages(keep: Page, gone: Page):
    """Move links and turn the merged page's text into pending items on the kept page (the editor merges them)."""
    for pe in PageEmail.objects.filter(page=gone):
        PageEmail.objects.get_or_create(page=keep, email=pe.email, defaults={"role": pe.role})
    for x in gone.sections.all():
        if x.text.strip():
            k = "page" if free() else fuzzy_key(keep.level, x.key) or fuzzy_key(keep.level, "overview") or "overview"
            Item.objects.create(page=keep, section_key=k, text=f"(merged from '{gone.title}') " + re.sub(r"\[e\d+\]", "", x.text)[:3000],
                                emails=x.sources, unit_key=f"merge:{gone.id}", window="merge")
    Item.objects.filter(page=gone, status="pending").update(page=keep)
    gone.archived = True
    gone.merged_into = keep
    gone.save(update_fields=["archived", "merged_into"])
    event("review", f"Merged page '{gone.title}' into '{keep.title}'", keep=keep.id, gone=gone.id)


def run_review(pages: list[Page] | None = None) -> dict:
    s = get_settings()
    if pages is None:
        pages = list(Page.objects.filter(archived=False).order_by("-updated_at")[: s.reviewer.max_pages])
    results = []
    for p in pages[: s.reviewer.max_pages]:
        try:
            results.append(review_page(p))
        except Exception as e:
            log.exception("review of %s failed", p.id)
            results.append({"page": p.title, "error": str(e)})
    tags = {}
    try:
        from llmwiki.pipeline.tagger import check_tags
        tags = check_tags()
    except Exception as e:
        log.warning("the tag check failed: %s", e)
    dups = []
    try:
        for a, b, sim in duplicates(s.reviewer.dup_threshold)[:10]:
            pa, pb = Page.objects.get(id=a), Page.objects.get(id=b)
            dups.append({"a": pa.title, "b": pb.title, "sim": round(sim, 3)})
            if s.reviewer.auto_apply:
                na, nb = PageEmail.objects.filter(page=pa).count(), PageEmail.objects.filter(page=pb).count()
                merge_pages(pa, pb) if na >= nb else merge_pages(pb, pa)
            else:
                event("review", f"Possible duplicate pages: '{pa.title}' and '{pb.title}' (similarity {sim:.2f}) — logged only", a=a, b=b)
    except Exception as e:
        log.warning("duplicate check failed: %s", e)
    return {"pages": results, "duplicates": dups, "tags": tags}
