"""Tags belong to pages.
- Script tags (always): site / partner / t-code, aggregated from the page's source emails (≥ tags.page_min_emails).
- S4 tagger (P2, batched ≤ 20 pages): primary tags (system, process, work_type) from the fixed lists, and a yes/no
  for new t-code candidates.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.llm.pool import StageFailed, pool
from llmwiki.logs import event
from llmwiki.models import Email, Page, PageEmail, Tag
from llmwiki.pipeline import prompts
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.util import first_sentence

log = logging.getLogger(__name__)

SITES = ["Rotterdam", "Felixstowe", "Hamburg", "Antwerp", "Gdańsk", "Gdansk", "Venlo", "Tilburg", "Duisburg", "Le Havre", "Bremerhaven", "Incheon", "Busan", "Pyeongtaek"]


def script_tags(page: Page) -> dict:
    s = get_settings().tags
    eids = list(PageEmail.objects.filter(page=page).values_list("email_id", flat=True))
    emails = list(Email.objects.filter(id__in=eids).only("id", "new_text", "subject", "from_addr"))
    counts: dict[tuple, int] = {}
    active_codes = set(Tag.objects.filter(type="tcode", state="active").values_list("value", flat=True))
    internal = [d.lower() for d in get_settings().sources.internal_domains]
    for e in emails:
        text = f"{e.subject}\n{e.new_text}"
        found = set()
        for site in SITES:
            if re.search(rf"\b{re.escape(site)}\b", text, re.I):
                found.add(("site", site.replace("Gdansk", "Gdańsk").lower()))
        dom = e.from_addr.split("@")[-1].lower()
        if dom and dom not in internal and "." in dom:
            found.add(("partner", dom.split(".")[0]))
        for code in active_codes:
            if re.search(rf"(?<![\w/]){re.escape(code)}(?![\w])", text):
                found.add(("tcode", code))
        for k in found:
            counts[k] = counts.get(k, 0) + 1
    tags = {"site": [], "partner": [], "tcode": []}
    need = 1 if len(emails) < s.page_min_emails else s.page_min_emails
    for (ty, v), n in sorted(counts.items(), key=lambda x: -x[1]):
        if n >= need and len(tags[ty]) < 6:
            tags[ty].append(v)
    return tags


def refresh_script_tags(page: Page):
    t = dict(page.tags or {})
    t.update(script_tags(page))
    Page.objects.filter(id=page.id).update(tags=t)


def run_tagger(pages: list[Page]) -> dict:
    s = get_settings()
    for p in pages:
        try:
            refresh_script_tags(p)
        except Exception:
            log.exception("script tags failed for %s", p.id)
    if not s.pipeline.s4_enabled or not pages:
        return {"pages": len(pages), "s4": False}
    cands = list(Tag.objects.filter(type="tcode", state="candidate").order_by("-threads")[:15])
    done = 0
    for k in range(0, len(pages), 20):
        batch = [Page.objects.prefetch_related("sections").get(id=p.id) for p in pages[k:k + 20]]
        labels = {f"P{i}": p for i, p in enumerate(batch, 1)}
        system = prompts.s4_system()

        def build(cap, scale, batch=batch, labels=labels):
            cards = []
            for lab, p in labels.items():
                secs = {x.key: x.text for x in p.sections.all()}
                ov = secs.get("summary") or secs.get("overview") or secs.get("what") or ""
                cards.append(f"{lab}: {p.title} ({p.level}) — {first_sentence(ov, 300)}")
            tc = [f"{t.value}: …{(t.contexts or [{}])[0].get('text', '')[:160]}…" for t in cands] if k == 0 else []
            secs = [Section("pages", "Pages", cards, 12000), Section("tcodes", "T-code candidates (with context)", tc, 3000)]
            text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
            return system, "TASK: s4\n\n" + text, meta

        def parse(o, labels=labels):
            if not isinstance(o, dict) or not isinstance(o.get("pages"), list):
                return None, ['the answer must have a "pages" list']
            return o, []
        try:
            res = pool().run_structured("s4", f"tags:{batch[0].id}-{batch[-1].id}", build, parse)
        except StageFailed as e:
            log.warning("S4 tagger failed: %s", e)
            continue
        allowed = {"system": set(s.tags.systems), "process": set(s.tags.processes), "work_type": set(s.tags.work_types)}
        for d in res.obj.get("pages", []):
            lab = str(d.get("page", "")).upper().replace(" ", "")
            p = labels.get(lab)
            if not p:
                continue
            t = dict(Page.objects.get(id=p.id).tags or {})
            for ty in ("system", "process", "work_type"):
                vals = [str(v).strip().lower() for v in (d.get(ty) or []) if str(v).strip().lower() in allowed[ty]]
                t[ty] = vals[: (1 if ty == "work_type" else 2)]
            Page.objects.filter(id=p.id).update(tags=t, tagged_at=timezone.now())
            done += 1
        for d in res.obj.get("tcodes", []) or []:
            code = str(d.get("code", "")).upper()
            tg = next((c for c in cands if c.value == code), None)
            if tg:
                tg.state = "active" if d.get("is_tcode") else "rejected"
                tg.why = "confirmed by the S4 tagger" if d.get("is_tcode") else "S4 tagger: not a t-code"
                tg.save(update_fields=["state", "why"])
                event("tags", f"T-code candidate {code} → {tg.state} (S4)", code=code)
    return {"pages": len(pages), "s4": True, "tagged": done}
