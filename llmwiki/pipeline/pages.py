"""Page operations used by the stages. All writes that touch facts are done here, by script."""
from __future__ import annotations

import difflib
import logging
import re
import threading

from django.db import transaction
from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.logs import event
from llmwiki.models import Email, Item, OpenIssue, Page, PageEmail, Section, SectionHistory, StatusChange
from llmwiki.pipeline.templates import STATUSES, free, keys
from llmwiki.util import slugify

log = logging.getLogger(__name__)
_create_lock = threading.Lock()


def _norm_title(t: str) -> str:
    return re.sub(r"[^\w가-힣]+", " ", (t or "").lower()).strip()


def find_similar_title(title: str, threshold: float) -> Page | None:
    nt = _norm_title(title)
    best, score = None, 0.0
    for p in Page.objects.filter(archived=False).only("id", "title"):
        r = difflib.SequenceMatcher(None, nt, _norm_title(p.title)).ratio()
        if r > score:
            best, score = p, r
    return best if best and score >= threshold else None


def create_page(title: str, level: str, reason: str, start_level: str | None = None) -> tuple[Page, bool]:
    """Create a page unless a page with a near-identical title exists (then reuse it). Returns (page, created)."""
    with _create_lock:
        return create_page_locked(title, level, reason, start_level)


def create_page_locked(title: str, level: str, reason: str, start_level: str | None = None) -> tuple[Page, bool]:
    """Same as create_page; the caller holds _create_lock. `level` is the model's proposal (kept as level_proposed);
    `start_level` the script-checked level the page starts at (refresh_level promotes it as the page grows)."""
    level = level if level in ("task", "topic", "project") else "topic"
    proposed = level
    level = start_level if start_level in ("task", "topic", "project") else level
    title = re.sub(r"\s+", " ", (title or "Untitled").strip())[:300]
    same = find_similar_title(title, get_settings().pipeline.merge_new_titles)
    if same:
        return same, False
    base = slugify(title)
    slug, i = base, 2
    while Page.objects.filter(slug=slug).exists():
        slug, i = f"{base}-{i}", i + 1
    p = Page.objects.create(slug=slug, title=title, level=level, level_proposed=proposed, status="Open", created_reason=reason[:2000])
    for k in (["summary"] if free() else keys(level)):
        Section.objects.get_or_create(page=p, key=k)
    event("wiki", f"New page: {title} ({level}{'' if level == proposed else f', proposed {proposed}'})", page=p.id, reason=reason[:300])
    return p, True


def ensure_sections(p: Page):
    have = set(p.sections.values_list("key", flat=True))
    for k in (["summary"] if free() else keys(p.level)):
        if k not in have:
            Section.objects.create(page=p, key=k)


@transaction.atomic
def write_section(p: Page, key: str, text: str, sources: list[int], guards: list, reason: str = "", call_id=None, round_: int = 1,
                  title: str | None = None, position: int | None = None, replace_sources: bool = False) -> int:
    sec, _ = Section.objects.select_for_update().get_or_create(page=p, key=key)
    sec.version += 1
    sec.text = text
    sec.sources = sorted(set(sources)) if replace_sources else sorted(set(sec.sources or []) | set(sources))
    sec.updated_at = timezone.now()
    if title is not None:
        sec.title = title[:200]
    if position is not None:
        sec.position = position
    sec.save()
    SectionHistory.objects.create(page=p, key=key, title=sec.title, version=sec.version, text=text, accepted=True, round=round_, guards=guards,
                                  reason=reason, sources=sorted(set(sources)), call_id=call_id)
    Page.objects.filter(id=p.id).update(updated_at=timezone.now())
    return sec.version


def record_rejected(p: Page, key: str, text: str, guards: list, reason: str = "", call_id=None, round_: int = 1, sources=None, title: str = ""):
    if not get_settings().guards.keep_rejected:
        return
    cur = Section.objects.filter(page=p, key=key).values_list("version", flat=True).first() or 0
    SectionHistory.objects.create(page=p, key=key, title=title[:200], version=cur, text=text, accepted=False, round=round_, guards=guards, reason=reason,
                                  sources=sorted(set(sources or [])), call_id=call_id)


# ------------------------------------------------------------------ the page's length budget (free structure)
def summary_text(p: Page) -> str:
    """The page's one-paragraph description: the summary (free structure) or the Overview / What it is section."""
    secs = {s.key: s.text for s in p.sections.all()}
    return next((secs[k] for k in ("summary", "overview", "what") if secs.get(k, "").strip()), "")


def source_words(p: Page) -> int:
    """Words people wrote in the page's emails: new text only (no quoted history), no automated mail."""
    texts = Email.objects.filter(page_links__page=p, automated=False).exclude(status__in=["duplicate", "skipped"]).values_list("new_text", flat=True)
    return sum(len(re.findall(r"\S+", t or "")) for t in texts)


def page_words(p: Page) -> int:
    return sum(len(re.findall(r"\S+", re.sub(r"\[e\d+\]", "", s.text or ""))) for s in p.sections.all())


def length_info(p: Page) -> dict:
    sw = source_words(p)
    return {"words": page_words(p), "budget": get_settings().pages.budget(sw), "source_words": sw}


def rollback(p: Page, key: str, version: int, user: str = "") -> int:
    h = SectionHistory.objects.filter(page=p, key=key, version=version, accepted=True).order_by("-id").first()
    if not h:
        raise ValueError("no such version")
    v = write_section(p, key, h.text, h.sources, [["rollback", True, False]], reason=f"rollback to v{version} by {user or 'user'}")
    event("wiki", f"Rolled back {p.title} › {key} to v{version} (saved as v{v})", page=p.id)
    return v


def link_emails(p: Page, emails: list[Email], role: str):
    for e in emails:
        pe, created = PageEmail.objects.get_or_create(page=p, email=e, defaults={"role": role})
        if not created and role == "primary" and pe.role != "primary":
            pe.role = "primary"
            pe.save(update_fields=["role"])
    last = max((e.sent_at for e in emails), default=None)
    if last and (not p.last_email_at or last > p.last_email_at):
        Page.objects.filter(id=p.id).update(last_email_at=last)


def add_items(p: Page, items: list[tuple[str, str, list[int]]], unit_key: str, window: str) -> int:
    n = 0
    for key, text, eids in items:
        if text and text.strip():
            Item.objects.create(page=p, section_key=key, text=text.strip()[:3000], emails=eids, unit_key=unit_key, window=window)
            n += 1
    return n


def set_status(p: Page, status: str | None, email: Email | None):
    if not status:
        return
    st = next((s for s in STATUSES if s.lower() == status.strip().lower()), None)
    if not st or st == p.status:
        return
    StatusChange.objects.create(page=p, old=p.status, new=st, email=email, at=email.sent_at if email else timezone.now())
    p.status = st
    p.save(update_fields=["status"])


def open_issues(p: Page, add: list[str], close: list[str], email: Email | None):
    for txt in add or []:
        txt = (txt or "").strip()
        if not txt:
            continue
        if OpenIssue.objects.filter(page=p, closed_at__isnull=True, text__iexact=txt).exists():
            continue
        n = OpenIssue.objects.filter(page=p).count() + 1
        OpenIssue.objects.create(page=p, code=f"o-{n}", text=txt[:1000], email=email, opened_at=email.sent_at if email else timezone.now())
    for code in close or []:
        c = str(code).strip().lower()
        if not c.startswith("o-"):
            continue
        OpenIssue.objects.filter(page=p, code=c, closed_at__isnull=True).update(closed_at=email.sent_at if email else timezone.now(), closed_by=email)


# ------------------------------------------------------------------ level: the model proposes, the script checks the size
LEVEL_ORDER = ["task", "topic", "project"]
# where a section's text goes when the page changes level (old key → new key); unknown keys go to the first section
MOVE = {"task": {"overview": "what", "background": "what", "goals": "what", "decisions": "status", "people": "status", "open": "status",
                 "plan": "status", "risks": "status", "systems": "how"},
        "topic": {"what": "overview", "status": "open", "goals": "overview", "plan": "background", "risks": "open", "systems": "how"},
        "project": {"what": "overview", "how": "systems", "status": "risks", "background": "overview", "open": "risks"}}


def size_of(p: Page) -> dict:
    """Script facts for the level check: human (non-automated) emails, active days, people who wrote them."""
    es = list(Email.objects.filter(page_links__page=p, automated=False).exclude(status__in=["duplicate", "skipped"])
              .values_list("day", "from_addr"))
    depts = {a.lower() for a in get_settings().sources.dept_addresses}
    return {"emails": len(es), "days": len({d for d, _ in es}), "people": len({a for _, a in es if a.lower() not in depts})}


def allowed_level(size: dict) -> str:
    rules = get_settings().pipeline.level_min
    for lvl in ("project", "topic"):
        r = rules.get(lvl) or {}
        if all(size.get(k, 0) >= v for k, v in r.items()):
            return lvl
    return "task"


def effective_level(proposed: str, size: dict) -> str:
    proposed = proposed if proposed in LEVEL_ORDER else "topic"
    return min(proposed, allowed_level(size), key=LEVEL_ORDER.index)


def change_level(p: Page, new: str, why: str = "") -> None:
    """Switch a page to another level's template without losing text: sections that don't exist in the new template are
    appended to their counterpart (MOVE) and emptied; pending items are re-keyed. Every move is a section version."""
    old = p.level
    if new == old or new not in LEVEL_ORDER:
        return
    if free():   # the editor's own sections don't depend on the level
        Page.objects.filter(id=p.id).update(level=new)
        p.level = new
        event("wiki", f"{p.title}: {old} → {new}" + (f" ({why})" if why else ""), page=p.id)
        return
    new_keys = keys(new)
    with transaction.atomic():
        Page.objects.filter(id=p.id).update(level=new)
        p.level = new
        ensure_sections(p)
        for sec in Section.objects.filter(page=p).exclude(key__in=new_keys):
            if not sec.text.strip():
                continue
            target = MOVE[new].get(sec.key) or new_keys[0]
            tsec = Section.objects.get(page=p, key=target)
            merged = (tsec.text.rstrip() + "\n\n" + sec.text.strip()).strip() if tsec.text.strip() else sec.text.strip()
            write_section(p, target, merged, list(sec.sources or []), [["level change", True, False]], reason=f"level {old} → {new}: text moved from '{sec.key}'")
            write_section(p, sec.key, "", [], [["level change", True, False]], reason=f"level {old} → {new}: moved to '{target}'")
        for it in Item.objects.filter(page=p, status__in=["pending", "failed"]).exclude(section_key__in=new_keys):
            it.section_key = MOVE[new].get(it.section_key) or new_keys[0]
            it.save(update_fields=["section_key"])
    event("wiki", f"{p.title}: {old} → {new}" + (f" ({why})" if why else ""), page=p.id)


def refresh_level(p: Page) -> str:
    proposed = p.level_proposed or p.level
    if not p.level_proposed:
        Page.objects.filter(id=p.id).update(level_proposed=proposed)
        p.level_proposed = proposed
    size = size_of(p)
    eff = effective_level(proposed, size)
    if eff != p.level:
        change_level(p, eff, f"{size['emails']} human emails, {size['days']} days, {size['people']} people; proposed {proposed}")
    return eff


def provisional_overview(p: Page, items: list[tuple[str, str, list[int]]]):
    """A brand-new page gets its first items as a provisional Overview, so routing in the same window can find it.
    The editor rewrites it properly."""
    first = "summary" if free() else "overview" if "overview" in keys(p.level) else "what"
    sec, _ = Section.objects.get_or_create(page=p, key=first)
    if sec and not sec.text.strip():
        txt = " ".join(t for _, t, _ in items)[:1500]
        if txt:
            sec.text = txt
            sec.save(update_fields=["text"])
