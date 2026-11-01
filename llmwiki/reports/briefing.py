"""Department reports. One report per department (sources.dept_addresses): every page belongs to exactly one
department (`Page.dept`, set by departments.assign), so no topic appears in two reports, and each report's agent texts
see only that department's topics.

Periods: a report covers the `reports.window_days` (7) full days before its date. A "daily" report exists for every
morning (texts written at reports.daily_time by the scheduler); the one on reports.weekly_weekday (Thursday, the day
before the CFO meeting) is the weekly report. Key: "d:YYYY-MM-DD" = the report date (window = the 7 days before it).

Only topics with team activity (a team member sent mail on it in the window, departments.is_team) are reported.
Almost everything is script-built; the agent writes only the lead and one text per headline story, from facts inside
the window only (notes of the window's emails, the facts they added, status changes, open issues), cached in `Report`
by input hash, with a script fallback.

Layout: 2 columns. Cards are paired by estimated height (≤ reports.pair_diff lines apart, within 3 ranks); the
shorter card may show one more key email, and a still-taller card's text is clamped ("Show all").
"""
from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.db.models import Q

from llmwiki import departments as dp
from llmwiki.conf import get_settings
from llmwiki.llm.pool import StageFailed, pool
from llmwiki.models import Email, Item, OpenIssue, Page, PageEmail, Report, StatusChange
from llmwiki.pipeline import prompts
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.util import first_sentence, local_dt, sha1

log = logging.getLogger(__name__)
KIND_RANK = {"decision": 0, "issue": 1, "request": 2, "done": 3, "info": 4, "": 5}
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _today() -> date:
    from llmwiki.scheduler import local_now
    return local_now().date()


KINDS = ("daily", "weekly", "monthly")


def periods(kind: str | None = None) -> list[dict]:
    """The reports that exist, newest first: daily (the last reports.keep_daily mornings), weekly (every
    reports.weekly_weekday) and monthly (calendar months). Keys: "d:YYYY-MM-DD" = the report date, its window the
    reports.window_days before it (a weekly report is the daily report of its weekday); "m:YYYY-MM" = the month.
    kind=None: every kind, each entry with its "kind" (a Thursday is listed as daily and as weekly)."""
    r = get_settings().reports
    days = Email.objects.exclude(status="duplicate").values_list("day", flat=True)
    if not days.exists():
        return []
    first, last = date.fromisoformat(min(days)), date.fromisoformat(max(days))
    end = max(_today(), last + timedelta(days=1))
    dates, d = [], first + timedelta(days=1)
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)
    dates = [d for d in dates if d - timedelta(days=r.window_days) <= last]
    out = []
    want = [kind] if kind else [k for k in KINDS if k in r.kinds]
    if "daily" in want:
        out += [{"key": f"d:{d.isoformat()}", "label": _label(d, "daily"), "kind": "daily", "weekly": d.weekday() == r.weekly_weekday}
                for d in reversed(dates[-r.keep_daily:])]
    if "weekly" in want:
        out += [{"key": f"d:{d.isoformat()}", "label": _label(d, "weekly"), "kind": "weekly", "weekly": True}
                for d in reversed([x for x in dates if x.weekday() == r.weekly_weekday])][: r.keep_weekly]
    if "monthly" in want:
        months, m = [], date(first.year, first.month, 1)
        while m <= end:
            months.append(m)
            m = (m + timedelta(days=32)).replace(day=1)
        out += [{"key": f"m:{m:%Y-%m}", "label": _month_label(m), "kind": "monthly", "weekly": False}
                for m in reversed(months)][: r.keep_monthly]
    return out


def _window(d: date) -> tuple[str, str]:
    n = get_settings().reports.window_days
    return (d - timedelta(days=n)).isoformat(), (d - timedelta(days=1)).isoformat()


def bounds(period: str) -> tuple[str, str]:
    """(first day, last day) of a period key."""
    if period.startswith("m:"):
        m = date.fromisoformat(period[2:] + "-01")
        nxt = (m + timedelta(days=32)).replace(day=1)
        return m.isoformat(), (nxt - timedelta(days=1)).isoformat()
    return _window(date.fromisoformat(period[2:]))


def kind_of(period: str) -> str:
    if period.startswith("m:"):
        return "monthly"
    return "weekly" if date.fromisoformat(period[2:]).weekday() == get_settings().reports.weekly_weekday else "daily"


def _label(d: date, kind: str = "") -> str:
    a, b = (date.fromisoformat(x) for x in _window(d))
    rng = f"{a.day} {a:%b} – {b.day} {b:%b}"
    if kind:   # inside the period menu of one kind
        return f"{WD[d.weekday()]} {d.day} {d:%b} {d.year} · {rng}"
    k = "Weekly" if d.weekday() == get_settings().reports.weekly_weekday else "Daily"
    return f"{k} · {WD[d.weekday()]} {d.day} {d:%b} ({rng})"


def _month_label(m: date) -> str:
    t = _today()
    return f"{m:%B %Y}" + (" · so far" if (m.year, m.month) == (t.year, t.month) else "")


def period_label(period: str) -> str:
    if period.startswith("m:"):
        return "Monthly · " + _month_label(date.fromisoformat(period[2:] + "-01"))
    return _label(date.fromisoformat(period[2:]))


def _parse_period(period: str | None, ps: list[dict]) -> str:
    """A valid period key: the one asked for, else the newest listed."""
    if period and (period.startswith("d:") or period.startswith("m:")):
        try:
            bounds(period)
            return period
        except ValueError:
            pass
    return ps[0]["key"]


def _tags(p: Page) -> list[list[str]]:
    t = p.tags or {}
    out = []
    for ty, n in (("system", 2), ("process", 1), ("site", 1), ("partner", 1)):
        out += [[ty, v] for v in (t.get(ty) or [])[:n]]
    out += [["tcode", v] for v in (t.get("tcode") or [])[:3]]
    return out


def _fallback_story(es: list[Email]) -> str:
    people = len({e.from_addr for e in es})
    ranked = sorted(es, key=lambda e: (KIND_RANK.get(e.kind, 5), e.sent_at))
    return (f"{len(es)} email{'s' if len(es) != 1 else ''} from {people} {'person' if people == 1 else 'people'}. "
            + " ".join(e.note for e in ranked[:3] if e.note))


def dept_list() -> list[dict]:
    ai = dp.ai_dept()
    return [{"addr": a, "name": dp.name_of(a), "ai": a == ai} for a in dp.all_depts()]


def _scope(d0: str, d1: str, dept: str, area: str | None = None):
    """The report's material: (window emails of this department, {page: its window emails}, pages, left-out pages)."""
    ai = dp.ai_dept()
    emails = list(Email.objects.filter(day__gte=d0, day__lte=d1).exclude(status__in=["duplicate", "skipped"]).order_by("sent_at"))
    em_by_id = {e.id: e for e in emails}
    by_page: dict[int, list[Email]] = defaultdict(list)
    for pe in PageEmail.objects.filter(email_id__in=list(em_by_id), page__dept=dept, page__archived=False):
        by_page[pe.page_id].append(em_by_id[pe.email_id])
    pages = {p.id: p for p in Page.objects.filter(id__in=list(by_page))}
    if area:
        ty, v = area.split(":", 1)
        pages = {k: p for k, p in pages.items() if v in (p.tags or {}).get(ty, [])}
    left_out = []
    if get_settings().reports.require_team_activity:
        for pid in list(pages):
            if not any(dp.is_team(e.from_addr, dept) and not e.automated for e in by_page[pid]):
                left_out.append(pages.pop(pid))
    routed = {pe for pe in PageEmail.objects.filter(email_id__in=list(em_by_id)).values_list("email_id", flat=True)}
    mine = {e.id for pid in pages for e in by_page[pid]}
    for e in emails:   # unrouted mail of this department (AI-related unrouted mail counts for the AI department)
        if e.id in routed or e.id in mine:
            continue
        ed = [x.lower() for x in e.depts]
        if (ai and e.ai_related and dept == ai) or (dept in ed and not (ai and e.ai_related and dept != ai)):
            mine.add(e.id)
    return [em_by_id[i] for i in sorted(mine, key=lambda i: em_by_id[i].sent_at)], {k: by_page[k] for k in pages}, pages, left_out


def _score(p: Page, es: list[Email], changed: bool) -> float:
    """How much a topic matters this window (script): important human mail, people, team members, decisions, status."""
    human = [e for e in es if not e.automated]
    return (3 * sum(1 for e in human if e.importance == "important") + sum(1 for e in human if e.importance == "routine")
            + 2 * len({e.from_addr for e in human}) + 2 * sum(1 for e in human if e.kind == "decision")
            + (5 if changed else 0) + (3 if p.status in ("At risk", "Blocked") else 0) + (2 if p.level == "project" else 0))


def _dots(es: list[Email], win_days: list[str]) -> list[dict]:
    """Per-story activity cells: one per day for short windows, one per week (Monday-based) for a month."""
    if len(win_days) <= 14:
        return [{"day": d, "n": sum(1 for e in es if e.day == d)} for d in win_days]
    weeks: dict[str, list[str]] = {}
    for d in win_days:
        x = date.fromisoformat(d)
        weeks.setdefault((x - timedelta(days=x.weekday())).isoformat(), []).append(d)
    return [{"day": ds[0], "to": ds[-1], "week": date.fromisoformat(ds[0]).isocalendar()[1], "n": sum(1 for e in es if e.day in ds)}
            for ds in weeks.values()]


def build(period: str | None = None, filters: dict | None = None) -> dict:
    s = get_settings()
    r, parts = s.reports, s.report_parts
    depts = dept_list()
    f = filters or {}
    kinds = [k for k in KINDS if k in r.kinds] or ["daily"]
    kind = f.get("kind") or (kind_of(period) if period else None) or (r.default_kind if r.default_kind in kinds else kinds[0])
    if period and kind == "weekly" and period.startswith("d:") and kind_of(period) != "weekly":
        kind = "daily"
    ps = periods(kind)
    if not ps or not depts:
        return {"empty": True, "periods": [], "depts": depts, "kinds": kinds, "kind": kind}
    period = _parse_period(period, ps)
    dept = (f.get("dept") or depts[0]["addr"]).lower()
    d0, d1 = bounds(period)
    emails, by_page, pages, left_out = _scope(d0, d1, dept, f.get("area"))
    if f.get("people"):
        emails = [e for e in emails if e.from_addr in f["people"]]
    changes = {}
    for sc in StatusChange.objects.filter(page_id__in=list(pages), at__date__gte=d0, at__date__lte=d1).order_by("at"):
        changes[sc.page_id] = f"{sc.old} → {sc.new}" if sc.old else sc.new
    ranked = sorted(pages, key=lambda pid: -_score(pages[pid], by_page[pid], pid in changes))
    top, rest = ranked[: r.stories], ranked[r.stories:]
    n_days = (date.fromisoformat(d1) - date.fromisoformat(d0)).days + 1
    win_days = [(date.fromisoformat(d0) + timedelta(days=k)).isoformat() for k in range(n_days)]
    cache = f"{dept}|{period[2:]}"
    stories = []
    for i, pid in enumerate(top):
        p = pages[pid]
        es = sorted(by_page[pid], key=lambda e: e.sent_at)
        txt, gen = _cached_story(cache, p, es, d0, d1) if parts.story_text else ("", False)
        keys = sorted([e for e in es if not e.automated] or es, key=lambda e: (KIND_RANK.get(e.kind, 5), -e.sent_at.timestamp()))
        due = sorted([e for e in Email.objects.filter(page_links__page=p).exclude(due="") if e.due >= d0], key=lambda e: e.due)
        dec = [e for e in es if e.kind == "decision" and not e.automated]
        stories.append({
            "rank": i + 1, "page_id": p.id, "slug": p.slug, "title": p.title, "level": p.level, "status": p.status, "change": changes.get(pid),
            "tags": _tags(p) if parts.story_tags else [], "text": txt, "text_source": "agent" if gen else "script",
            "decision": ({"id": dec[-1].id, "note": dec[-1].note, "day": dec[-1].day, "sender": dec[-1].from_name or dec[-1].from_addr}
                         if dec and parts.story_decision else None),
            "key_emails": [{"id": e.id, "kind": e.kind, "note": e.note or e.subject, "day": e.day, "sender": e.from_name or e.from_addr} for e in keys[:4]]
            if parts.story_key_emails else [],
            "n_key_available": len(keys) if parts.story_key_emails else 0,
            "n_emails": len(es), "people": sorted({e.from_name or e.from_addr for e in es if e.is_internal and not e.automated})[:6],
            "dots": _dots(es, win_days),
            "open_issues": OpenIssue.objects.filter(page=p, closed_at__isnull=True).count(),
            "next_deadline": ({"day": due[0].due, "text": due[0].note[:120]} if due else None),
        })
    rows = pair(stories) if r.layout == "2col" else [[{"i": i, "key_n": min(r.key_notes, len(st["key_emails"])), "clamp": 0, "h": 0}] for i, st in enumerate(stories)]
    lead, lead_gen = _cached_lead(cache, stories, emails, dept, d0, d1) if parts.lead else ("", False)
    decisions = [{"id": e.id, "note": e.note, "day": e.day, "sender": e.from_name or e.from_addr, "page": _first_page(e)} for e in emails if e.kind == "decision"]
    deadline_end = (date.fromisoformat(d1) + timedelta(days=14)).isoformat()
    dl_q = Email.objects.filter(due__gte=d0, due__lte=deadline_end).exclude(status__in=["duplicate", "skipped"])
    dl_q = dl_q.filter(Q(page_links__page__dept=dept) | Q(id__in=[e.id for e in emails])).distinct()
    deadlines = [{"id": e.id, "due": e.due, "note": e.note, "page": _first_page(e)} for e in dl_q.order_by("due")[:30]]
    waiting = _waiting(emails, d1, r.wait_days)
    attention = _attention(pages, d1, r.attn_days, emails)
    closed = OpenIssue.objects.filter(page_id__in=list(pages), closed_at__date__gte=d0, closed_at__date__lte=d1).count()
    new_pages = sum(1 for p in pages.values() if not PageEmail.objects.filter(page=p, email__day__lt=d0).exists())
    per_day = [{"day": d, "n": sum(1 for e in emails if e.day == d), "important": sum(1 for e in emails if e.day == d and e.importance == "important")}
               for d in win_days]
    need = (len(stories) if parts.story_text else 0) + (1 if parts.lead and stories else 0)
    done = (sum(1 for x in stories if x["text_source"] == "agent") if parts.story_text else 0) + (1 if lead_gen else 0)
    return {
        "period": period, "label": period_label(period), "kind": kind, "kinds": kinds, "weekly": kind == "weekly", "from": d0, "to": d1, "periods": ps,
        "in_progress": d1 >= _today().isoformat(), "dept": dept, "dept_name": dp.name_of(dept), "depts": depts,
        "parts": parts.model_dump(), "list_max_px": r.list_max_px, "daily_time": r.daily_time,
        "lead": lead, "lead_source": "agent" if lead_gen else "script",
        "kpis": {"emails": len(emails), "important": sum(1 for e in emails if e.importance == "important"), "threads": len({e.thread_id for e in emails}),
                 "pages_active": len(pages), "new_pages": new_pages, "decisions": len(decisions), "closed": closed,
                 "at_risk": sum(1 for p in pages.values() if p.status in ("At risk", "Blocked"))},
        "per_day": per_day,
        "left_out": [{"page_id": p.id, "title": p.title} for p in left_out],
        "stories": stories, "rows": rows, "decisions": decisions, "deadlines": deadlines, "waiting": waiting, "attention": attention,
        "rest": [{"page_id": pid, "slug": pages[pid].slug, "title": pages[pid].title, "status": pages[pid].status, "n": len(by_page[pid]),
                  "last_note": by_page[pid][-1].note} for pid in rest],
        "agent_texts": {"done": done, "total": need},
    }


def topic_week(period: str | None = None, dept: str | None = None) -> dict:
    """Which of this department's topics were active on which day and hour of the report window (script, no agent):
    one row per (topic, email) with the receive time; the portal groups them into a days × hours grid."""
    ps = periods()
    depts = dept_list()
    if not ps or not depts:
        return {"days": [], "rows": []}
    period = _parse_period(period, ps)
    dept = (dept or depts[0]["addr"]).lower()
    d0, d1 = bounds(period)
    tzname = get_settings().sources.time_zone
    rows = []
    for pe in (PageEmail.objects.filter(page__dept=dept, page__archived=False, email__day__gte=d0, email__day__lte=d1)
               .exclude(email__status__in=["duplicate", "skipped"]).select_related("page", "email").order_by("email__sent_at")):
        e = pe.email
        rows.append({"page_id": pe.page_id, "title": pe.page.title, "status": pe.page.status, "level": pe.page.level, "day": e.day,
                     "hour": local_dt(e.sent_at, tzname).hour, "email_id": e.id, "sender": e.from_name or e.from_addr,
                     "automated": e.automated, "important": e.importance == "important"})
    n = (date.fromisoformat(d1) - date.fromisoformat(d0)).days + 1
    days = [(date.fromisoformat(d0) + timedelta(days=k)).isoformat() for k in range(n)]
    return {"period": period, "kind": kind_of(period), "dept": dept, "days": days, "rows": rows}


def _first_page(e: Email):
    pe = PageEmail.objects.filter(email=e).select_related("page").order_by("-role").first()
    return {"id": pe.page_id, "title": pe.page.title} if pe else None


def _waiting(emails, d1, wait_days):
    out = []
    end = date.fromisoformat(d1)
    for e in emails:
        if e.kind != "request":
            continue
        later = Email.objects.filter(thread_id=e.thread_id, sent_at__gt=e.sent_at).exclude(from_addr=e.from_addr).exists()
        if not later and (end - date.fromisoformat(e.day)).days >= wait_days:
            out.append({"id": e.id, "note": e.note, "day": e.day, "to": [a.split("@")[0] for a in e.to[:3]], "sender": e.from_name or e.from_addr})
    return out[:15]


def _attention(pages, d1, attn_days, emails):
    out = []
    cut = (date.fromisoformat(d1) - timedelta(days=attn_days)).isoformat()
    for p in pages.values():
        if p.status in ("At risk", "Blocked"):
            out.append({"text": f"{p.title} is {p.status.lower()}", "page_id": p.id})
    for o in OpenIssue.objects.filter(page__in=list(pages.values()), closed_at__isnull=True, opened_at__date__lte=cut).select_related("page")[:10]:
        out.append({"text": f"{o.page.title}: {o.text[:100]} (open since {o.opened_at:%d %b})", "page_id": o.page_id})
    failed = Item.objects.filter(status="failed", page__in=list(pages.values())).values_list("page_id", flat=True).distinct()
    for pid in failed:
        out.append({"text": f"{pages[pid].title}: new facts not yet written into the page", "page_id": pid})
    by_thread = Counter(e.thread_id for e in emails if not e.page_links.exists() and e.importance == "important")
    for tid, n in by_thread.most_common(3):
        if n >= 3:
            e = next(x for x in emails if x.thread_id == tid)
            out.append({"text": f"Busy thread without a page: {e.subject[:80]} ({n} emails)", "email_id": e.id})
    return out[:12]


# ------------------------------------------------------------------ agent texts (cached)
def _story_input(p: Page, es: list[Email], d0: str, d1: str) -> str:
    """Only what happened inside the window: the window's notes, the facts those emails added to the page, status
    changes and open issues of the window. The overview's first sentence is labelled as background."""
    from llmwiki.pipeline.pages import summary_text
    ov = summary_text(p)
    ids = {e.id for e in es}
    notes = "\n".join(f"- {e.day} {e.from_name or e.from_addr} ({e.kind}, {e.importance}{', automated' if e.automated else ''}): {e.note}"
                      for e in es if e.note)
    facts = [it.text for it in Item.objects.filter(page=p).order_by("id") if set(it.emails or []) & ids][:20]
    sc = [f"{c.at.date().isoformat()}: {c.old} → {c.new}" for c in StatusChange.objects.filter(page=p, at__date__gte=d0, at__date__lte=d1).order_by("at")]
    opened = [f"{o.opened_at.date().isoformat()}: {o.text}" for o in OpenIssue.objects.filter(page=p, opened_at__date__gte=d0, opened_at__date__lte=d1)]
    closed = [f"{o.closed_at.date().isoformat()}: {o.text}" for o in OpenIssue.objects.filter(page=p, closed_at__date__gte=d0, closed_at__date__lte=d1)]
    return (f"Window: {d0} to {d1}\nTopic: {p.title} ({p.level}, status now {p.status})\n"
            f"Background (context only, not news): {first_sentence(ov, 300) if ov else '-'}\n\n"
            f"In the window:\nEmails (notes):\n{notes or '-'}\n"
            + ("Facts these emails added to the page:\n" + "\n".join(f"- {t[:500]}" for t in facts) + "\n" if facts else "")
            + ("Status changes: " + "; ".join(sc) + "\n" if sc else "")
            + ("Opened issues: " + "; ".join(opened) + "\n" if opened else "")
            + ("Closed issues: " + "; ".join(closed) + "\n" if closed else ""))


def _cached_story(cache: str, p: Page, es: list[Email], d0: str, d1: str) -> tuple[str, bool]:
    h = sha1(_story_input(p, es, d0, d1))[:40]
    rep = Report.objects.filter(part=f"story:{p.id}", input_hash=h).order_by("-id").first()   # same input → same text, any report
    return (rep.text, rep.generated) if rep else (_fallback_story(es), False)


def _lead_input(stories, emails, dept: str, d0: str, d1: str) -> str:
    return (f"Team: {dp.name_of(dept)}. Window: {d0} to {d1}.\nThis team's stories:\n"
            + "\n".join(f"- {s['title']} ({s['status']}{', ' + s['change'] if s['change'] else ''}): {first_sentence(s['text'], 300)}" for s in stories)
            + f"\nTotals: {len(emails)} emails, {sum(1 for e in emails if e.importance == 'important')} important.")


def _cached_lead(cache: str, stories, emails, dept: str, d0: str, d1: str) -> tuple[str, bool]:
    h = sha1(_lead_input(stories, emails, dept, d0, d1))[:40]
    rep = Report.objects.filter(part=f"lead:{dept}", input_hash=h).order_by("-id").first()
    if rep:
        return rep.text, rep.generated
    if not stories:
        return f"No topics with team activity between {d0} and {d1}.", False
    return f"{len(emails)} emails on {len(stories)} main topics. " + " ".join(f"{s['title']}: {s['status']}." for s in stories[:4]), False


def _gen(stage_part: str, system: str, user: str, key: str, lo: int, hi: int) -> str | None:
    def build(cap, scale):
        text, meta = assemble([Section("in", "Facts", [user], get_settings().budgets.reports["story"])], cap, [], system, scale)
        return system, f"TASK: {stage_part}\n\n" + text, meta

    def parse(o):
        t = str((o or {}).get(key) or "").strip() if isinstance(o, dict) else ""
        if len(t) < lo:
            return None, [f'"{key}" is {len(t)} characters; it must be {lo}–{hi} characters']
        if len(t) > hi * 1.15:
            return None, [f'"{key}" is {len(t)} characters; shorten it to at most {hi}']
        return t[: int(hi * 1.15)], []
    try:
        return pool().run_structured("report", f"report:{stage_part}", build, parse, schema="lead" if key == "lead" else "story").obj
    except StageFailed as e:
        log.warning("report text failed (%s), using the script fallback", e)
        return None


def generate_texts(period: str | None = None, dept: str | None = None) -> dict:
    """Write the missing agent texts of one department's report (or of every department): each request separate and fed
    only with that department's topics; failures keep the script fallback."""
    if dept is None:
        out = {}
        for d in dept_list():
            try:
                out[d["addr"]] = generate_texts(period, d["addr"])["generated"]
            except Exception:
                log.exception("report texts for %s failed", d["addr"])
        return {"period": period, "generated": out}
    r, parts = get_settings().reports, get_settings().report_parts
    data = build(period, {"dept": dept, "kind": kind_of(period) if period else None})
    if data.get("empty"):
        return {"period": period, "dept": dept, "generated": 0}
    d0, d1, cache = data["from"], data["to"], f"{dept}|{data['period'][2:]}"
    emails, by_page, pages, _ = _scope(d0, d1, dept)
    made = 0
    for st in data.get("stories", []):
        if st["text_source"] == "agent" or not parts.story_text:   # switched off in Report parts: no request
            continue
        p = pages[st["page_id"]]
        es = sorted(by_page[p.id], key=lambda e: e.sent_at)
        inp = _story_input(p, es, d0, d1)
        t = _gen("story", prompts.report_story_system(r.story_min, r.story_max), inp, "text", r.story_min, r.story_max)
        Report.objects.update_or_create(period=cache, part=f"story:{p.id}", input_hash=sha1(inp)[:40],
                                        defaults={"text": t or _fallback_story(es), "generated": bool(t)})
        made += bool(t)
    data = build(data["period"], {"dept": dept, "kind": data["kind"]})
    if parts.lead and data["lead_source"] != "agent" and data["stories"]:
        inp = _lead_input(data["stories"], emails, dept, d0, d1)
        t = _gen("lead", prompts.report_lead_system(r.lead_min, r.lead_max), inp, "lead", r.lead_min, r.lead_max)
        if t:
            Report.objects.update_or_create(period=cache, part=f"lead:{dept}", input_hash=sha1(inp)[:40], defaults={"text": t, "generated": True})
            made += 1
    return {"period": data["period"], "dept": dept, "generated": made}


# ------------------------------------------------------------------ 2-column pairing
LINE = 64


def est_lines(st: dict, key_n: int, clamp: int = 0) -> int:
    title = math.ceil(len(st["title"]) / 34)
    tl = math.ceil(len(st["text"]) / LINE)
    keys = sum(min(2, math.ceil(len(k["note"]) / 52)) + 0.6 for k in st["key_emails"][:key_n])
    return round(title + 1.4 + math.ceil(len(st["tags"]) / 4) + 1 + (min(tl, clamp) if clamp else tl) + 1 + keys + 2)


def pair(stories: list[dict]) -> list[list[dict]]:
    r = get_settings().reports
    items = [{"i": i, "key_n": min(r.key_notes, len(s["key_emails"])), "clamp": 0} for i, s in enumerate(stories)]
    for it in items:
        it["h"] = est_lines(stories[it["i"]], it["key_n"])
    left, rows = list(items), []
    while left:
        a = left.pop(0)
        if not left:
            rows.append([a])
            break
        win = left[:3]
        pick = next((x for x in win if abs(x["h"] - a["h"]) <= r.pair_diff), None) or min(win, key=lambda x: abs(x["h"] - a["h"]))
        left.remove(pick)
        lo, hi = (a, pick) if a["h"] <= pick["h"] else (pick, a)
        slo, shi = stories[lo["i"]], stories[hi["i"]]
        while hi["h"] - lo["h"] > 1 and lo["key_n"] < min(4, len(slo["key_emails"])):
            lo["key_n"] += 1
            lo["h"] = est_lines(slo, lo["key_n"])
        while hi["h"] - lo["h"] > 1 and hi["key_n"] > 2:
            hi["key_n"] -= 1
            hi["h"] = est_lines(shi, hi["key_n"])
        if hi["h"] - lo["h"] > r.pair_diff:
            tl = math.ceil(len(shi["text"]) / LINE)
            c = max(5, tl - (hi["h"] - lo["h"] - r.pair_diff))
            if c < tl:
                hi["clamp"] = c
                hi["h"] = est_lines(shi, hi["key_n"], c)
        rows.append([a, pick])
    return rows
