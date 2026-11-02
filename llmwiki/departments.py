"""Departments: one wiki, one report per department, and every page in exactly ONE department's report.

The department addresses (sources.dept_addresses) are separate teams of one bigger department. A page's department
(`Page.dept`) is decided by script after every run:
1. a person set it (`dept_locked`) → keep;
2. the AI department exists and ≥ departments.ai_page_min_share of the page's human emails are AI-related (the
   note-taker's `ai` flag) → the AI department, whatever address the mail was sent to;
3. else the department address that occurs on most of the page's emails (To/Cc), ties → the most recent email's.
Reports then take only their own pages, so no topic appears in two reports.

Team involvement: a topic is reported only if a team member SENT mail on it inside the window (`is_team`).
"""
from __future__ import annotations

import fnmatch
import logging
from collections import Counter

from llmwiki.conf import get_settings
from llmwiki.models import Email, Page

log = logging.getLogger(__name__)


def all_depts() -> list[str]:
    return [a.lower() for a in get_settings().sources.dept_addresses]


def name_of(addr: str) -> str:
    s = get_settings().departments
    names = {k.lower(): v for k, v in s.names.items()}
    return names.get((addr or "").lower()) or (addr or "").split("@")[0]


def ai_dept() -> str:
    a = (get_settings().departments.ai_address or "").lower()
    return a if a and a in all_depts() else ""


def assign(p: Page) -> str:
    if p.dept_locked and p.dept:
        return p.dept
    s = get_settings().departments
    es = list(Email.objects.filter(page_links__page=p).exclude(status__in=["duplicate", "skipped"]).order_by("sent_at")
              .values_list("depts", "ai_related", "automated"))
    human = [x for x in es if not x[2]] or es
    ai = ai_dept()
    dept = ""
    if ai and human and sum(1 for x in human if x[1]) / len(human) >= s.ai_page_min_share:
        dept = ai
    else:
        cnt = Counter(d.lower() for x in human for d in (x[0] or []) if d.lower() in all_depts() and d.lower() != ai)
        if not cnt:   # only mail to the AI address (but not judged AI-related) — or no department address at all
            cnt = Counter(d.lower() for x in human for d in (x[0] or []) if d.lower() in all_depts())
        if cnt:
            top = max(cnt.values())
            tied = [d for d, n in cnt.items() if n == top]
            if len(tied) == 1:
                dept = tied[0]
            else:
                for x in reversed(human):
                    hit = [d.lower() for d in (x[0] or []) if d.lower() in tied]
                    if hit:
                        dept = hit[0]
                        break
    if dept != p.dept:
        Page.objects.filter(id=p.id).update(dept=dept)
        if p.dept:
            from llmwiki.logs import event
            event("wiki", f"{p.title}: department {name_of(p.dept)} → {name_of(dept)}", page=p.id)
        p.dept = dept
    return dept


def team_patterns(dept: str | None = None) -> list[str]:
    s = get_settings().departments
    pats = list(s.team_members)
    if dept:
        pats += list({k.lower(): v for k, v in s.team_by_dept.items()}.get(dept.lower(), []))
    return [x.lower() for x in pats if x]


def is_team(addr: str, dept: str | None = None) -> bool:
    """A team member? Without a team list: any internal sender (that is not a department mailbox)."""
    a = (addr or "").lower()
    if a in all_depts():
        return False
    pats = team_patterns(dept)
    if pats:
        return any(fnmatch.fnmatch(a, p) for p in pats)
    return a.split("@")[-1] in [d.lower() for d in get_settings().sources.internal_domains]


def has_team_list() -> bool:
    s = get_settings().departments
    return bool(s.team_members or any(s.team_by_dept.values()))


def is_member(addr: str, depts: list[str] | None = None) -> bool:
    """A member of these departments (none given: of any department)? Same fallback as is_team without a team list."""
    s = get_settings().departments
    a = (addr or "").lower()
    if a in all_depts():
        return False
    if not has_team_list():
        return a.split("@")[-1] in [d.lower() for d in get_settings().sources.internal_domains]
    by = {k.lower(): v for k, v in s.team_by_dept.items()}
    wanted = [d.lower() for d in depts] if depts else list(by)
    pats = list(s.team_members) + [p for d in wanted for p in by.get(d, [])]
    return any(fnmatch.fnmatch(a, p.lower()) for p in pats if p)


def refresh_pages(page_ids: list[int] | None = None) -> dict:
    """Script checks after a run: page level (pages.refresh_level) and department (assign)."""
    from llmwiki.pipeline import pages as pg
    qs = Page.objects.filter(archived=False)
    if page_ids is not None:
        qs = qs.filter(id__in=page_ids)
    out = {"levels": Counter(), "depts": Counter(), "changed_level": 0}
    for p in qs:
        before = p.level
        try:
            lvl = pg.refresh_level(p)
            out["changed_level"] += lvl != before
            out["levels"][lvl] += 1
        except Exception:
            log.exception("level check failed for page %s", p.id)
        try:
            out["depts"][name_of(assign(p))] += 1
        except Exception:
            log.exception("department assignment failed for page %s", p.id)
    return {"levels": dict(out["levels"]), "depts": dict(out["depts"]), "changed_level": out["changed_level"]}
