"""Tags belong to pages. What keeps invented tags out lies in scripts:

- A term in the catalog (Tag table) may be used whenever a page is about it, named or not. An abstract process rarely appears
  word for word, and a mail about VL02N is about SAP without saying so. The model judges that.
- A new term must literally appear in the page's emails or wiki text (tags.evidence). That is the check a model cannot talk
  its way past. Terms that pass join the catalog, and every page that mentions them gets them from sweep(), without a model.
- tags.implies adds what follows for certain, by script: any t-code means SAP.

Three steps, cheapest first:
- sweep(): no model. Removes tags that are not in the catalog, adds catalog terms a page mentions and what they imply. Runs
  after every processing run.
- S4 in the pipeline: a first set for new pages. Pages start with few tags while the catalog is still empty.
- check_tags(): the wiki lint. Goes through the pages, asks again for those under tags.page_min_tags, grows the catalog, then
  sweeps everything. Sends page text only, so it may run on an external endpoint (stage `review`, PII removed); the words of a
  new term are checked here against the emails, which never leave.

T-codes keep their own route (found in t-code context, promoted by script, see ingest/store.py).
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.llm.pool import Halted, StageFailed, pool
from llmwiki.logs import event
from llmwiki.models import Attachment, Email, Page, Section as PageSection, Tag
from llmwiki.pipeline import prompts
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.util import clip_tokens, model_text

log = logging.getLogger(__name__)

NAMED = ("system", "site", "partner")
TYPES = ("system", "process", "work_type", "site", "partner")
_SEEDS = {"system": "systems", "process": "processes", "site": "sites", "partner": "partners", "work_type": "work_types"}


def _norm(v: str) -> str:
    return re.sub(r"[\s_-]+", " ", (v or "").casefold()).strip()


def term_re(term: str) -> re.Pattern:
    """The term as a whole word; space, - and _ count as the same. No end boundary after Hangul: particles attach to the word."""
    parts = [re.escape(x) for x in re.split(r"[\s_-]+", term.strip()) if x]
    tail = "" if re.search(r"[가-힣]$", term.strip()) else r"(?!\w)"
    return re.compile(r"(?<!\w)" + r"[\s_-]*".join(parts) + tail, re.I)


def seed_catalog() -> None:
    """Terms from the settings. A term someone rejected stays rejected."""
    t = get_settings().tags
    for ty, name in _SEEDS.items():
        for v in getattr(t, name):
            Tag.objects.get_or_create(type=ty, value=v, defaults={"state": "active", "why": "Settings › Tags"})
    for key, targets in t.implies.items():
        for target in targets:
            ty, _, v = target.partition(":")
            if ty in TYPES and v:
                Tag.objects.get_or_create(type=ty, value=v, defaults={"state": "active", "why": f"Settings › Tags: follows from {key}"})


class Catalog:
    def __init__(self):
        seed_catalog()
        self.by_type: dict[str, dict[str, Tag]] = {ty: {} for ty in TYPES}
        self.rejected: set[tuple[str, str]] = set()
        for t in Tag.objects.filter(type__in=TYPES):
            if t.state == "active":
                for name in [t.value] + list(t.aliases or []):
                    self.by_type[t.type].setdefault(_norm(name), t)
            elif t.state == "rejected":
                self.rejected.add((t.type, _norm(t.value)))

    def values(self, ty: str) -> list[str]:
        return sorted({t.value for t in self.by_type[ty].values()}, key=str.casefold)

    def find(self, ty: str, v: str) -> Tag | None:
        return self.by_type[ty].get(_norm(v))

    def add(self, t: Tag) -> None:
        self.by_type[t.type][_norm(t.value)] = t


class Sources:
    """The texts a page's tags must be proven against."""

    def __init__(self, page: Page):
        self.page = "\n".join([page.title] + [re.sub(r"\[e\d+\]", "", x) for x in PageSection.objects.filter(page=page).values_list("text", flat=True)])
        rows = list(Email.objects.filter(page_links__page=page).exclude(status__in=["duplicate", "skipped"]).values_list("id", "subject", "new_text", "from_addr"))
        self.emails = [f"{sj}\n{model_text(tx)}" for _, sj, tx, _ in rows]
        self.senders = [a for *_, a in rows]
        for x in Attachment.objects.filter(email_id__in=[r[0] for r in rows]).exclude(extract="").values_list("extract", flat=True):
            self.emails.append(x)
        self.all_mail = "\n".join(self.emails)

    def count(self, rx: re.Pattern) -> int:
        """How often it appears in the emails (the ranking of a page's tags)."""
        return len(rx.findall(self.all_mail))

    def proof(self, rx: re.Pattern) -> str:
        """'emails', 'page' or '': where the exact words stand."""
        if rx.search(self.all_mail):
            return "emails"
        return "page" if get_settings().tags.evidence == "emails_or_page" and rx.search(self.page) else ""

    def snippet(self, rx: re.Pattern) -> str:
        for x in self.emails + [self.page]:
            m = rx.search(x)
            if m:
                return re.sub(r"\s+", " ", x[max(0, m.start() - 60):m.end() + 60]).strip()
        return ""


def _proof_of(tag_or_term, src: Sources) -> str:
    names = [tag_or_term] if isinstance(tag_or_term, str) else [tag_or_term.value] + list(tag_or_term.aliases or [])
    for name in names:
        hit = src.proof(term_re(name))
        if hit:
            return hit
    return ""


def _rank(tag: Tag, src: Sources) -> int:
    return sum(src.count(term_re(n)) for n in [tag.value] + list(tag.aliases or []))


def script_tags(page: Page, src: Sources) -> dict:
    """Facts from the headers and known codes: partner = the domain of outside senders, t-code = active codes in the text."""
    s = get_settings()
    internal = [d.lower() for d in s.sources.internal_domains]
    need = 1 if len(src.emails) < s.tags.page_min_emails else s.tags.page_min_emails
    per_domain: dict[str, int] = {}
    for a in src.senders:
        dom = a.split("@")[-1].lower()
        if dom and dom not in internal and "." in dom:
            per_domain[dom.split(".")[0]] = per_domain.get(dom.split(".")[0], 0) + 1
    codes, mentioned = [], False
    for code in Tag.objects.filter(type="tcode", state="active").values_list("value", flat=True):
        rx = re.compile(rf"(?<![\w/]){re.escape(code)}(?![\w])")
        n = sum(1 for x in src.emails if rx.search(x))
        if n >= need:
            codes.append((n, code))
        mentioned = mentioned or n > 0 or bool(rx.search(src.page))
    return {"partner": [d for d, n in sorted(per_domain.items(), key=lambda x: -x[1]) if n >= need][:s.tags.max_per_type],
            "tcode": [c for _, c in sorted(codes, reverse=True)][:s.tags.max_per_type], "any_code": mentioned}


def _imply(tags: dict, any_code: bool, cat: Catalog) -> None:
    """tags.implies, twice round so that a chain works. A rejected target is not in the catalog, so it is not added."""
    rules = get_settings().tags.implies
    for _ in range(2):
        for key, targets in rules.items():
            ty, _, v = key.partition(":")
            have = tags.get(ty) or []
            hit = bool(have) or (ty == "tcode" and any_code) if v == "*" else _norm(v) in {_norm(x) for x in have}
            if not hit:
                continue
            for target in targets:
                tty, _, tv = target.partition(":")
                t = cat.find(tty, tv) if tty in TYPES else None
                if t and t.value not in tags.setdefault(tty, []):
                    tags[tty].insert(0, t.value)


def sweep_page(page: Page, cat: Catalog) -> dict:
    """Bring a page's tags in line by script: off what is not in the catalog (never used, or rejected), on what it mentions and
    what follows from that. A catalog tag it already has stays, named or not: a model or a person judged that. No model."""
    s = get_settings().tags
    src = Sources(page)
    old = page.tags or {}
    new: dict[str, list[str]] = {}
    removed: list[str] = []
    scripted = script_tags(page, src)
    for ty in NAMED + ("process",):
        keep: list[str] = []
        for v in old.get(ty, []):
            t = cat.find(ty, v)
            if t:
                if t.value not in keep:
                    keep.append(t.value)
            elif v not in scripted.get(ty, []):
                removed.append(f"{ty}:{v}")
        found = [t for t in set(cat.by_type[ty].values()) if t.value not in keep and _proof_of(t, src)]
        found.sort(key=lambda t: (-_rank(t, src), t.value.casefold()))
        new[ty] = keep + [t.value for t in found]
    new["partner"] = list(dict.fromkeys(scripted["partner"] + new["partner"]))
    new["tcode"] = scripted["tcode"]
    new["work_type"] = [v for v in old.get("work_type", []) if v in s.work_types][:1]
    _imply(new, scripted["any_code"], cat)
    for ty in NAMED + ("process",):
        new[ty] = new[ty][:s.max_per_type]
    added = [f"{ty}:{v}" for ty, vs in new.items() for v in vs if v not in old.get(ty, [])]
    if {k: v for k, v in new.items() if v} != {k: v for k, v in old.items() if v}:
        Page.objects.filter(id=page.id).update(tags={k: v for k, v in new.items() if v})
    return {"added": added, "removed": removed}


def sweep(pages: list[Page] | None = None, dry_run: bool = False) -> dict:
    """Script only, for all pages unless given. Run after the catalog grew: older pages get the new terms too."""
    from django.db import transaction
    cat = Catalog()
    rows = pages if pages is not None else list(Page.objects.filter(archived=False))
    added, removed = [], []
    try:
        with transaction.atomic():
            for p in rows:
                r = sweep_page(p, cat)
                added += r["added"]
                removed += r["removed"]
            if dry_run:
                transaction.set_rollback(True)
    except Exception:
        log.exception("tag sweep failed")
        raise
    if (added or removed) and not dry_run:
        event("tags", f"Tag sweep over {len(rows)} pages: {len(added)} added, {len(removed)} not in the catalog removed"
              + (f" ({', '.join(sorted(set(removed))[:8])})" if removed else ""))
    return {"pages": len(rows), "added": len(added), "removed": len(removed), "removed_values": sorted(set(removed))[:50], "dry_run": dry_run}


def _take_answer(page: Page, d: dict, cat: Catalog, who: str) -> dict:
    """Apply one page's answer. A catalog term passes; a new term only with its exact words. What fails is reported, never stored."""
    s = get_settings().tags
    src = Sources(page)
    tags = dict(Page.objects.get(id=page.id).tags or {})
    took, refused = [], []
    for ty in TYPES:
        cur = list(tags.get(ty, []))
        for raw in d.get(ty) or []:
            v = re.sub(r"\s+", " ", str(raw)).strip()
            if not v:
                continue
            known = cat.find(ty, v)
            if ty == "work_type":
                if v.lower() in s.work_types:
                    cur = [v.lower()]
                    took.append(f"{ty}:{v.lower()}")
                else:
                    refused.append((ty, v, "not one of the work types"))
                continue
            if known:
                v = known.value
            else:
                if (ty, _norm(v)) in cat.rejected:
                    refused.append((ty, v, "rejected before"))
                    continue
                if len(v) > 60 or len(v.split()) > 6:
                    refused.append((ty, v, "too long to be a name"))
                    continue
                where = _proof_of(v, src)
                if not where:
                    refused.append((ty, v, "these exact words are not on this page or in its emails"))
                    continue
                t, _ = Tag.objects.get_or_create(type=ty, value=v[:100], defaults={"state": "active", "first_seen": timezone.now(), "last_seen": timezone.now(), "why": f"{who}: found in the {where}",
                                                                                  "contexts": [{"text": src.snippet(term_re(v))[:200]}]})
                cat.add(t)
                event("tags", f"New tag {ty}: {v} ({who}, found in the {where}, page “{page.title[:60]}”)", page=page.id)
            if v not in cur:
                cur.append(v)
                took.append(f"{ty}:{v}")
        tags[ty] = cur[: (1 if ty == "work_type" else s.max_per_type)]
    Page.objects.filter(id=page.id).update(tags={k: v for k, v in tags.items() if v}, tagged_at=timezone.now())
    for ty, v, why in refused:
        event("tags", f"Refused tag {ty}: {v} on “{page.title[:60]}”: {why} ({who})", level="warning", page=page.id)
    return {"took": took, "refused": refused}


def _cards(batch: dict[str, Page], per_page: int, hints: dict | None = None) -> list[str]:
    out = []
    for lab, p in batch.items():
        secs = "\n".join(re.sub(r"\[e\d+\]", "", x) for x in PageSection.objects.filter(page=p).order_by("position", "id").values_list("text", flat=True) if x.strip())
        have = ", ".join(f"{ty}: {v}" for ty, vs in (p.tags or {}).items() for v in vs) or "none"
        extra = f"\nNeeds more tags. Refused earlier: {hints[lab]}" if hints and hints.get(lab) else ""
        out.append(f"### {lab}: {p.title} ({p.level})\nCurrent tags: {have}{extra}\n{clip_tokens(secs, per_page)}")
    return out


def _ask(pages: list[Page], stage: str, task: str, who: str, cat: Catalog, min_tags: int, codes=None, hints: dict | None = None) -> dict:
    s = get_settings()
    labels = {f"P{i}": p for i, p in enumerate(pages, 1)}
    system = prompts.tags_system({ty: cat.values(ty) for ty in TYPES}, s.tags.work_types, min_tags)

    def build(cap, scale):
        secs = [Section("pages", "Pages", _cards(labels, max(400, 16000 // max(1, len(labels))), hints), 16000)]
        if codes:
            secs.append(Section("tcodes", "T-code candidates (with context)", [f"{t.value}: …{(t.contexts or [{}])[0].get('text', '')[:160]}…" for t in codes], 3000))
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        return system, f"TASK: {task}\n\n{text}", meta

    def parse(o):
        if not isinstance(o, dict) or not isinstance(o.get("pages"), list):
            return None, ['the answer must have a "pages" list']
        return o, []
    res = pool().run_structured(stage, f"tags:{pages[0].id}-{pages[-1].id}", build, parse, schema="tags")
    took = refused = 0
    per_page: dict[int, list] = {}
    for d in res.obj.get("pages", []):
        p = labels.get(str(d.get("page", "")).upper().replace(" ", ""))
        if p:
            r = _take_answer(p, d, cat, who)
            took += len(r["took"])
            refused += len(r["refused"])
            per_page[p.id] = r["refused"]
    for d in res.obj.get("tcodes", []) or []:
        code = str(d.get("code", "")).upper()
        tg = next((c for c in codes or [] if c.value == code), None)
        if tg:
            tg.state = "active" if d.get("is_tcode") else "rejected"
            tg.why = "confirmed by the S4 tagger" if d.get("is_tcode") else "S4 tagger: not a t-code"
            tg.save(update_fields=["state", "why"])
            event("tags", f"T-code candidate {code} → {tg.state} (S4)", code=code)
    return {"took": took, "refused": refused, "per_page": per_page}


def run_tagger(pages: list[Page]) -> dict:
    """After a processing run: tags that follow from the catalog, and a model's first set for new pages."""
    s = get_settings()
    cat = Catalog()
    for p in pages:
        try:
            sweep_page(p, cat)
        except Exception:
            log.exception("script tags failed for %s", p.id)
    if not s.pipeline.s4_enabled or not pages:
        return {"pages": len(pages), "s4": False}
    codes = list(Tag.objects.filter(type="tcode", state="candidate").order_by("-threads")[:15])
    done = 0
    for k in range(0, len(pages), s.tags.check_batch):
        batch = [Page.objects.get(id=p.id) for p in pages[k:k + s.tags.check_batch]]
        try:
            done += _ask(batch, "s4", "s4", "S4", cat, 0, codes if k == 0 else None)["took"]
        except Halted:
            raise
        except StageFailed as e:
            log.warning("S4 tagger failed: %s", e)
    return {"pages": len(pages), "s4": True, "tagged": done}


def check_tags(limit: int | None = None, everything: bool = False) -> dict:
    """The wiki lint for tags. Pages with the fewest tags first; pages still below the minimum get one more try with what
    was refused. Ends with a sweep, so that terms found on one page reach all the others."""
    s = get_settings()
    cat = Catalog()
    want = s.tags.page_min_tags
    rows = list(Page.objects.filter(archived=False))
    n_tags = lambda p: sum(len(v) for k, v in (p.tags or {}).items() if k != "tcode")  # noqa: E731
    rows.sort(key=n_tags)
    if not everything:     # a page that really has few tags is not asked about again until it changes
        rows = [p for p in rows if n_tags(p) < max(want, 1) and (not p.tags_checked_at or p.updated_at > p.tags_checked_at)]
    rows = rows[: limit or s.reviewer.max_pages]
    out = {"pages": len(rows), "took": 0, "refused": 0, "failed": 0}
    again: list[tuple[Page, list]] = []
    for k in range(0, len(rows), s.tags.check_batch):
        batch = [Page.objects.get(id=p.id) for p in rows[k:k + s.tags.check_batch]]
        try:
            r = _ask(batch, "review", "taglint", "tag check", cat, want)
        except Halted:
            raise
        except StageFailed as e:
            log.warning("tag check failed: %s", e)
            out["failed"] += len(batch)
            rows = [p for p in rows if p not in batch]    # asked again next time
            continue
        out["took"] += r["took"]
        out["refused"] += r["refused"]
        for p in batch:
            fresh = Page.objects.get(id=p.id)
            if n_tags(fresh) < want:
                again.append((fresh, r["per_page"].get(p.id, [])))
    for k in range(0, len(again), s.tags.check_batch):
        part = again[k:k + s.tags.check_batch]
        batch = [p for p, _ in part]
        hints = {f"P{i}": "; ".join(f"{v} ({why})" for _, v, why in ref[:6]) or "nothing" for i, (_, ref) in enumerate(part, 1)}
        try:
            r = _ask(batch, "review", "taglint", "tag check", cat, want, hints=hints)
            out["took"] += r["took"]
            out["refused"] += r["refused"]
        except Halted:
            raise
        except StageFailed as e:
            log.warning("tag check, second try, failed: %s", e)
    Page.objects.filter(id__in=[p.id for p in rows]).update(tags_checked_at=timezone.now())
    sw = sweep()
    out.update(swept_added=sw["added"], swept_removed=sw["removed"])
    fresh = list(Page.objects.filter(archived=False))
    out["below_minimum"] = sum(1 for p in fresh if n_tags(p) < want)
    out["catalog"] = Tag.objects.filter(state="active").exclude(type="tcode").count()
    event("tags", f"Tag check: {out['pages']} pages, {out['took']} tags added, {out['refused']} refused, {out['swept_added']} more from the catalog; catalog {out['catalog']} terms; {out['below_minimum']} pages below {want}")
    return out
