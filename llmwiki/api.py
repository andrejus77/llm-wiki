"""JSON API for the portal (Django Ninja). OpenAPI docs: /api/docs.

Read endpoints never call the LLM. The only endpoints that cause LLM requests are POST /chat, POST /reports/generate
and POST /actions/* (which start background work through the scheduler's run_action).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, timedelta

from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Body, NinjaAPI

from llmwiki import departments as dp
from llmwiki.conf import GROUP_LABELS, PRESETS, Settings, get_settings, pinned_keys, save_settings
from llmwiki.ingest import skiprules
from llmwiki.logs import event
from llmwiki.models import json_has
from llmwiki.models import (Attachment, ChatMessage, Email, Event, Folder, Identifier, Item, Job, KV, LlmCall, OpenIssue, Page, PageEmail, QuotedBlock, Run, Section,
                            SectionHistory, SettingsVersion, SkipRule, StatusChange, Tag, Thread)
from llmwiki.pipeline.templates import LEVELS, STATUSES, TEMPLATES, free, label_of, section_label
from llmwiki.util import first_sentence, local_dt

log = logging.getLogger(__name__)
api = NinjaAPI(title="LLM Wiki API", version="1.0")  # no auth class: views are csrf-exempt; login is enforced by middleware in server mode


def _user(request) -> str:
    return request.user.get_username() if getattr(request, "user", None) and request.user.is_authenticated else ""


def _tz():
    return get_settings().sources.time_zone


def _window(days: int | None, dfrom: str | None, dto: str | None) -> tuple[str | None, str | None]:
    if dfrom or dto:
        return dfrom, dto
    if days:
        latest = Email.objects.aggregate(m=Max("day"))["m"] or date.today().isoformat()
        end = date.fromisoformat(latest) if latest else date.today()
        return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()
    return None, None


def _split(v: str | None) -> list[str]:
    return [x for x in (v or "").split(",") if x]


def person_name(addr: str) -> str:
    e = Email.objects.filter(from_addr=addr).exclude(from_name="").values_list("from_name", flat=True).first()
    return e or addr


# Columns no list needs. Lists never load a body: a row is a few hundred bytes, however long the mail is.
LIST_DEFER = ("new_text", "signature", "headers", "references", "source_ref", "thread_index", "conversation_id", "subject_norm", "subject_tpl", "fail_reason")
MAX_ROWS = 500

_ttl_cache: dict = {}
_ttl_lock = threading.Lock()


def _ttl(key, seconds: float, fn):
    """A small time-limited cache for values that many requests ask for and that may be slightly old."""
    now = time.monotonic()
    with _ttl_lock:
        hit = _ttl_cache.get(key)
        if hit and now - hit[0] < seconds:
            return hit[1]
    value = fn()
    with _ttl_lock:
        if len(_ttl_cache) > 64:
            _ttl_cache.clear()
        _ttl_cache[key] = (now, value)
    return value


def _is_admin(request) -> bool:
    """Admin mode: everybody on a local install, Django staff users on the server."""
    if get_settings().server.mode != "server":
        return True
    u = getattr(request, "user", None)
    return bool(u and u.is_authenticated and u.is_staff)


def _denied(request):
    return api.create_response(request, {"detail": "This needs an admin account."}, status=403)


def team_senders(days: int | None = None, depts: list[str] | None = None) -> list[dict]:
    """The people to filter by: department members only (departments.team_members / team_by_dept), most mail first."""
    def load():
        qs = Email.objects.exclude(status="duplicate")
        a, b = _window(days, None, None)
        if a:
            qs = qs.filter(day__gte=a, day__lte=b)
        rows = qs.values("from_addr").annotate(n=Count("id"), name=Max("from_name")).order_by("-n")
        return [{"addr": x["from_addr"], "name": x["name"] or x["from_addr"], "n": x["n"]} for x in rows if dp.is_member(x["from_addr"], depts)][:60]
    return _ttl(("team", days, tuple(depts or ()), json.dumps(get_settings().departments.team_members)), 15, load)


# ------------------------------------------------------------------ meta + health
@api.get("/health")
def health(request):
    from llmwiki.ingest.health import check_store
    return {"ok": True, "store": check_store(), "time": timezone.now().isoformat()}


def code_version() -> str:
    """The installed code version: llmwiki/BUILD.json (full package or patch), else MANIFEST.json, else 'dev'."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for p in (root / "llmwiki" / "BUILD.json", root / "MANIFEST.json"):
        try:
            return json.loads(p.read_text(encoding="utf-8"))["version"]
        except Exception:
            continue
    return "dev"


@api.get("/meta")
def meta(request):
    s = get_settings()
    from llmwiki.llm.pool import pool
    latest = _ttl("latest_day", 15, lambda: Email.objects.aggregate(m=Max("day"))["m"])
    tcodes = _ttl("tcodes", 60, lambda: sorted(set(Tag.objects.filter(type="tcode", state="active").values_list("value", flat=True))))
    return {
        "title": s.portal.title, "mode": s.server.mode, "user": _user(request), "admin": _is_admin(request), "default_window": s.portal.default_window_days,
        "wiki_view": s.portal.wiki_view, "latest_day": latest, "depts": s.sources.dept_addresses,
        "dept_info": __import__("llmwiki.reports.briefing", fromlist=["dept_list"]).dept_list(), "version": code_version(),
        "people": team_senders(), "team_list": dp.has_team_list(),
        "levels": LEVELS, "statuses": STATUSES, "templates": {k: [[a, b] for a, b, _ in v] for k, v in TEMPLATES.items()},
        "tag_values": {"tcode": tcodes, "system": s.tags.systems, "process": s.tags.processes, "work_type": s.tags.work_types},
        "schedule": {"live": s.schedule.live_enabled, "batch_time": s.schedule.batch_time, "paused_for_chat": pool().chat_active()},
        "chat": {"engine": s.chat.engine, "deep": s.chat.deep_enabled}, "stopped": pool().halted(),
    }


# ------------------------------------------------------------------ emails
def _email_row(e: Email, pages=None) -> dict:
    lt = local_dt(e.sent_at, _tz())
    return {"id": e.id, "day": e.day, "time": lt.strftime("%H:%M"), "dow": lt.strftime("%a"), "from": e.from_addr, "from_name": e.from_name or e.from_addr,
            "to": e.to, "cc": e.cc, "subject": e.subject, "note": e.note, "kind": e.kind, "importance": e.importance, "status": e.status,
            "skip_rule": e.skip_rule, "skip_action": e.skip_action, "thread_id": e.thread_id, "n_att": e.n_attachments, "has_table": e.has_table,
            "depts": e.depts, "lang": e.lang, "due": e.due, "ai": e.ai_related, "automated": e.automated, "parent_id": e.parent_id,
            "folder": e.folder_id, "pages": pages if pages is not None else []}


def _email_qs(days=None, dfrom=None, dto=None, depts=None, senders=None, importance=None, status=None, q=None, att=False, tbl=False, thread=None, folder=None):
    s = get_settings()
    qs = Email.objects.exclude(status="duplicate")
    a, b = _window(days, dfrom, dto)
    if a:
        qs = qs.filter(day__gte=a)
    if b:
        qs = qs.filter(day__lte=b)
    if thread:
        qs = qs.filter(thread_id=thread)
    if folder == "inbox":      # filed nowhere
        qs = qs.filter(folder__isnull=True)
    elif folder and str(folder).isdigit():
        qs = qs.filter(folder_id=int(folder))
    for d in _split(depts):
        qs = json_has(qs, "depts", d)
    snd = _split(senders)
    if snd:
        internal = [x.lower() for x in s.sources.internal_domains]
        cond = Q()
        for x in snd:
            if x == "@ext":
                ex = Q()
                for dom in internal:
                    ex |= Q(from_addr__iendswith="@" + dom)
                cond |= ~ex
            elif x == "@auto":
                cond |= Q(status="skipped") | Q(importance="not_important")
            else:
                cond |= Q(from_addr=x)
        qs = qs.filter(cond)
    if importance == "important":
        qs = qs.filter(importance="important")
    elif importance == "ir":
        qs = qs.filter(importance__in=["important", "routine"])
    elif importance == "not":
        qs = qs.filter(Q(importance="not_important") | Q(status="skipped"))
    if status == "processed":
        qs = qs.exclude(status="skipped")
    elif status == "skipped":
        qs = qs.filter(status="skipped")
    if att:
        qs = qs.filter(n_attachments__gt=0)
    if tbl:
        qs = qs.filter(has_table=True)
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(note__icontains=q) | Q(new_text__icontains=q) | Q(from_name__icontains=q) | Q(from_addr__icontains=q))
    return qs


@api.get("/emails")
def emails(request, days: int = 0, dfrom: str = None, dto: str = None, depts: str = None, senders: str = None, importance: str = None,
           status: str = None, q: str = None, att: bool = False, tbl: bool = False, thread: int = None, folder: str = None, limit: int = 100, offset: int = 0):
    """One page of the list, newest first; the portal asks for the next page when it scrolls there."""
    limit = max(1, min(limit, MAX_ROWS))
    qs = _email_qs(days, dfrom, dto, depts, senders, importance, status, q, att, tbl, thread, folder).order_by("-sent_at")
    total = qs.count()
    rows = list(qs.defer(*LIST_DEFER)[offset:offset + limit])
    links = {}
    for eid, pid, title, role in PageEmail.objects.filter(email_id__in=[e.id for e in rows]).values_list("email_id", "page_id", "page__title", "role"):
        links.setdefault(eid, []).append({"id": pid, "title": title, "role": role})
    return {"total": total, "offset": offset, "rows": [_email_row(e, links.get(e.id, [])) for e in rows]}


@api.get("/emails/threads")
def email_threads(request, days: int = 0, dfrom: str = None, dto: str = None, depts: str = None, senders: str = None, importance: str = None,
                  status: str = None, q: str = None, att: bool = False, tbl: bool = False, folder: str = None, limit: int = 60, offset: int = 0):
    """One page of threads that have matching mail, newest activity first."""
    limit = max(1, min(limit, 200))
    qs = _email_qs(days, dfrom, dto, depts, senders, importance, status, q, att, tbl, None, folder)
    groups = qs.order_by().values("thread_id").annotate(n=Count("id"), last=Max("sent_at")).order_by("-last")
    total = groups.count()
    part = list(groups[offset:offset + limit])
    per: dict[int, list] = {}
    for row in qs.filter(thread_id__in=[g["thread_id"] for g in part]).order_by("-sent_at").values("thread_id", "id", "subject", "day", "kind", "note", "from_name", "from_addr"):
        per.setdefault(row["thread_id"], []).append(row)
    rows = []
    for g in part:
        es = per.get(g["thread_id"], [])
        if es:
            rows.append({"thread_id": g["thread_id"], "n": g["n"], "subject": es[0]["subject"], "day": es[0]["day"], "kind": es[0]["kind"], "note": es[0]["note"],
                         "senders": list(dict.fromkeys(x["from_name"] or x["from_addr"] for x in es))[:4]})
    return {"total": total, "offset": offset, "rows": rows}


@api.get("/emails/facets")
def email_facets(request, days: int = 0, depts: str = None):
    return {"senders": team_senders(days or None, _split(depts) or None), "team_list": dp.has_team_list()}


@api.post("/emails/bulk")
def email_bulk(request, payload: dict = Body(...)):
    """Admin: a person's own decision on selected emails. {ids, folder: id | null} moves them; {ids, skip: true | false}
    skips them or sends them back to the queue. Rules leave these emails alone from then on."""
    if not _is_admin(request):
        return _denied(request)
    ids = [int(x) for x in payload.get("ids") or []][:MAX_ROWS * 4]
    out = {"ids": len(ids)}
    if "folder" in payload:
        fid = payload["folder"]
        if fid is not None and not Folder.objects.filter(id=fid).exists():
            return api.create_response(request, {"detail": "No such folder."}, status=400)
        for k in range(0, len(ids), 500):
            Email.objects.filter(id__in=ids[k:k + 500]).update(folder_id=fid, folder_rule=None, folder_manual=True)
    if payload.get("skip") is True:
        out.update(skiprules.skip_emails(ids, f"manual: by {_user(request) or 'a user'}", None, manual=True))
    elif payload.get("skip") is False:
        out["restored"] = skiprules.restore_emails(ids, manual=True)
    return out


# ------------------------------------------------------------------ folders
@api.get("/folders")
def folders(request, days: int = 0, dfrom: str = None, dto: str = None):
    """The folder list with mail counts (one grouped query), for the side panel."""
    qs = _email_qs(days, dfrom, dto)
    counts = {}
    skipped = 0
    for fid, st, n in qs.order_by().values_list("folder_id", "status").annotate(n=Count("id")):
        if st == "skipped":
            skipped += n
        else:
            counts[fid] = counts.get(fid, 0) + n
    return {"folders": [{"id": f.id, "name": f.name, "n": counts.get(f.id, 0)} for f in Folder.objects.all()],
            "inbox": counts.get(None, 0), "all": sum(counts.values()), "skipped": skipped}


@api.post("/folders")
def folder_add(request, payload: dict = Body(...)):
    if not _is_admin(request):
        return _denied(request)
    name = str(payload.get("name") or "").strip()[:120]
    if not name:
        return api.create_response(request, {"detail": "A folder needs a name."}, status=400)
    if Folder.objects.filter(name__iexact=name).exists():
        return api.create_response(request, {"detail": f"There is already a folder called {name}."}, status=400)
    f = Folder.objects.create(name=name, position=(Folder.objects.aggregate(m=Max("position"))["m"] or 0) + 1)
    return {"id": f.id, "name": f.name}


@api.patch("/folders/{fid}")
def folder_edit(request, fid: int, payload: dict = Body(...)):
    if not _is_admin(request):
        return _denied(request)
    name = str(payload.get("name") or "").strip()[:120]
    if not name:
        return api.create_response(request, {"detail": "A folder needs a name."}, status=400)
    if Folder.objects.filter(name__iexact=name).exclude(id=fid).exists():
        return api.create_response(request, {"detail": f"There is already a folder called {name}."}, status=400)
    Folder.objects.filter(id=fid).update(name=name)
    return {"ok": True}


@api.delete("/folders/{fid}")
def folder_delete(request, fid: int):
    """The mail goes back to the inbox. Rules that only filed into this folder stop; rules that also skip keep skipping."""
    if not _is_admin(request):
        return _denied(request)
    SkipRule.objects.filter(folder_id=fid, action="keep").update(enabled=False)
    Email.objects.filter(folder_id=fid).update(folder_rule=None, folder_manual=False)
    Folder.objects.filter(id=fid).delete()
    return {"ok": True}


@api.get("/emails/week")
def email_week(request, start: str = None, depts: str = None, senders: str = None, importance: str = "ir", weekend: bool = False):
    if not start:
        latest = Email.objects.aggregate(m=Max("day"))["m"] or date.today().isoformat()
        d = date.fromisoformat(latest)
        start = (d - timedelta(days=d.weekday())).isoformat()
    a = date.fromisoformat(start)
    b = a + timedelta(days=6)
    qs = _email_qs(None, a.isoformat(), b.isoformat(), depts, senders, importance).order_by("sent_at")
    total = qs.count()
    rows = [_email_row(e) for e in qs.defer(*LIST_DEFER)[:600]]
    return {"start": a.isoformat(), "end": b.isoformat(), "rows": rows, "total": total, "prev": (a - timedelta(days=7)).isoformat(), "next": (a + timedelta(days=7)).isoformat()}


def _body(e: Email) -> dict:
    blocks = [{"idx": q.idx, "header": q.header, "text": q.text, "linked": q.linked_id, "match": q.match, "sent_to_model": q.sent_to_model}
              for q in e.quoted_blocks.all().order_by("idx")]
    atts = [{"id": a.id, "filename": a.filename, "size": a.size, "is_image": a.is_image, "inline": a.inline, "content_type": a.content_type,
             "extract": a.extract[:4000], "extract_error": a.extract_error} for a in e.attachments.all()]
    return {"new_text": e.new_text, "signature": e.signature, "quoted": blocks, "attachments": atts}


@api.get("/emails/{eid}")
def email_detail(request, eid: int):
    e = get_object_or_404(Email, id=eid)
    earlier = list(Email.objects.filter(thread_id=e.thread_id, sent_at__lt=e.sent_at).exclude(status="duplicate").order_by("-sent_at").prefetch_related("attachments")[:30])
    g = thread_graph(sorted(earlier + [e], key=lambda x: x.sent_at))
    anc, cur = set(), g["nodes"].get(f"e{e.id}", {}).get("parent")
    while cur and cur.startswith("e") and cur not in anc:
        anc.add(cur)
        cur = g["nodes"].get(cur, {}).get("parent")
    branch = {"parallel": g["nodes"].get(f"e{e.id}", {}).get("parallel"), "ghost": g["ghosts"].get(cur) if cur and cur.startswith("q") else None}
    later = Email.objects.filter(thread_id=e.thread_id, sent_at__gt=e.sent_at).exclude(status="duplicate").count()
    pages = [{"id": pe.page_id, "title": pe.page.title, "role": pe.role} for pe in PageEmail.objects.filter(email=e).select_related("page")]
    items = [{"page": it.page.title, "page_id": it.page_id, "section": "" if it.section_key == "page" else label_of(it.page.level, it.section_key), "text": it.text, "status": it.status}
             for it in json_has(Item.objects.all(), "emails", e.id).select_related("page")[:20]]
    unit_keys = sorted({j.unit for j in Job.objects.filter(Q(unit__startswith=f"td:{e.thread_id}:{e.day}") | Q(unit__startswith=f"dl:{e.thread_id}:"))})
    t = Thread.objects.filter(id=e.thread_id).first()
    return {**_email_row(e, pages), "headers": e.headers, "message_id": e.message_id, "sources": e.sources, "in_reply_to": e.in_reply_to,
            "body": _body(e), "thread": {"id": t.id, "subject": t.subject, "n": t.n_emails} if t else None,
            "earlier": [{**_email_row(x), "new_text": x.new_text[:6000], "attachments": [a.filename for a in x.attachments.all() if not a.inline],
                         "in_chain": f"e{x.id}" in anc} for x in earlier], "branch": branch,
            "later": later, "items": items, "units": unit_keys, "identifiers": [[i.type, i.value] for i in e.identifiers.all()],
            "parent_id": e.parent_id, "dup_of": e.dup_of_id, "fail_reason": e.fail_reason, "audit": e.audit, "rule_ref": e.rule_ref, "skip_manual": e.skip_manual,
            "subject_norm": e.subject_norm}


@api.get("/attachments/{aid}")
def attachment(request, aid: int, inline: bool = False):
    a = get_object_or_404(Attachment, id=aid)
    if not a.blob_id:
        return HttpResponse("not stored (too large)", status=404)
    resp = HttpResponse(a.blob.get(), content_type=a.content_type or "application/octet-stream")
    resp["Content-Disposition"] = f'{"inline" if inline or a.is_image else "attachment"}; filename="{a.filename}"'
    return resp


def thread_graph(es: list[Email]) -> dict:
    """Who answers whom inside a thread, for drawing it: each email's parent (a stored email, or a 'ghost' = an earlier
    message that was never received but is quoted), and lanes — the first answer to a message continues its lane, every
    further answer to the same message opens a new lane (a parallel branch)."""
    ids = {e.id for e in es}
    firsts = {}
    for qb in QuotedBlock.objects.filter(email_id__in=list(ids), idx=0):
        firsts[qb.email_id] = qb
    parent: dict[int, str | None] = {}
    ghosts: dict[str, dict] = {}
    for e in es:
        p = None
        if e.parent_id in ids:
            p = f"e{e.parent_id}"
        else:
            qb = firsts.get(e.id)
            if qb and qb.linked_id in ids:
                p = f"e{qb.linked_id}"
            elif e.parallel_key or (qb and qb.fingerprint and qb.linked_id is None and e.parent_id is None):
                key = f"q{(e.parallel_key or qb.fingerprint)[:12]}"
                p = key
                if key not in ghosts:
                    hdr = (qb.header if qb else "").strip()
                    who = re.search(r"(?:From|Van|Von|보낸 사람)\s*:\s*\"?([^<\"\n]+?)\"?\s*(?:<|\n|$)", hdr) or re.search(r"On .+?,\s*([^<,\n]+?)\s*<", hdr)
                    when = re.search(r"(?:Sent|Date|Verzonden|Gesendet|보낸 날짜)\s*:\s*(?:\w+,\s*)?(\d{1,2}\s+\w+\s+\d{4}|\d{4}년\s*\d+월\s*\d+일)", hdr)
                    label = (f"{who.group(1).strip()}'s email" if who else "an earlier email") + (f" of {when.group(1)}" if when else "") + " (not received)"
                    ghosts[key] = {"key": key, "header": label, "raw_header": " · ".join(x.strip() for x in hdr.splitlines()[:2])[:200],
                                   "text": (qb.text if qb else "")[:400], "first_child": e.id}
        parent[e.id] = p
    # ghosts only matter when two or more emails answer the same unseen message
    kids: dict[str, list] = {}
    for eid, p in parent.items():
        if p:
            kids.setdefault(p, []).append(eid)
    for g in list(ghosts):
        if len(kids.get(g, [])) < 2:
            for eid in kids.get(g, []):
                parent[eid] = None
            ghosts.pop(g)
            kids.pop(g, None)
    order: list[str] = []
    for e in es:
        p = parent[e.id]
        if p in ghosts and p not in order:
            order.append(p)
        order.append(f"e{e.id}")
    lane: dict[str, int] = {}
    used: dict[str, int] = {}
    nxt = 0
    for n in order:
        p = parent.get(int(n[1:])) if n.startswith("e") else None
        if p is None or p not in lane:
            lane[n] = 0 if not lane else min(lane.get(order[0], 0), 0)
        elif used.get(p, 0) == 0:
            lane[n] = lane[p]
        else:
            nxt = max(max(lane.values()), nxt) + 1
            lane[n] = nxt
        if p:
            used[p] = used.get(p, 0) + 1
    by_id = {f"e{e.id}": e for e in es}
    nodes = {}
    for n in order:
        p = parent.get(int(n[1:])) if n.startswith("e") else None
        sib = kids.get(p, []) if p else []
        info = None
        if p and len(sib) > 1:
            pe = by_id.get(p)
            info = {"k": sib.index(int(n[1:])) + 1, "n": len(sib),
                    "to": (f"{pe.from_name or pe.from_addr} · {pe.day}" if pe else ghosts[p]["header"] or "an earlier message")}
        nodes[n] = {"lane": lane[n], "parent": p, "parallel": info,
                    "reply_to": (f"{by_id[p].from_name or by_id[p].from_addr}" if p in by_id else None)}
    return {"order": order, "nodes": nodes, "ghosts": ghosts, "lanes": max(lane.values(), default=0) + 1}


@api.get("/threads/{tid}")
def thread_detail(request, tid: int):
    t = get_object_or_404(Thread, id=tid)
    qs = Email.objects.filter(thread=t).exclude(status="duplicate")
    total = qs.count()
    es = list(qs.order_by("-sent_at")[:200])[::-1]   # a very long thread: its latest 200
    ids = [e.id for e in es]
    links = {}
    for eid, pid, title, role in PageEmail.objects.filter(email_id__in=ids).values_list("email_id", "page_id", "page__title", "role"):
        links.setdefault(eid, []).append({"id": pid, "title": title, "role": role})
    quoted: dict[int, list] = {}
    for eid, text in QuotedBlock.objects.filter(email_id__in=ids, linked__isnull=True).values_list("email_id", "text"):
        quoted.setdefault(eid, []).append(text[:1500])
    g = thread_graph(es)
    return {"id": t.id, "subject": t.subject, "participants": t.participants, "total": total, "graph": {"order": g["order"], "ghosts": g["ghosts"], "lanes": g["lanes"]},
            "emails": [{**_email_row(e, links.get(e.id, [])), **g["nodes"][f"e{e.id}"], "new_text": e.new_text[:4000], "more": len(e.new_text) > 4000,
                        "quoted": quoted.get(e.id, [])} for e in es]}


# ------------------------------------------------------------------ wiki
def _page_row(p: Page, secs: dict | None = None) -> dict:
    if secs is None:
        secs = {s.key: s for s in p.sections.all() if s.text}
    ov = secs.get("summary") or secs.get("overview") or secs.get("what")
    from llmwiki.departments import name_of
    return {"id": p.id, "slug": p.slug, "title": p.title, "level": p.level, "level_proposed": p.level_proposed or p.level, "status": p.status, "tags": p.tags or {},
            "dept": p.dept, "dept_name": name_of(p.dept) if p.dept else "", "dept_locked": p.dept_locked,
            "overview": first_sentence(re.sub(r"\[e\d+\]", "", ov.text), 260) if ov else "",
            "last_day": p.last_email_at.date().isoformat() if p.last_email_at else None}


@api.get("/pages")
def pages(request, days: int = 0, dfrom: str = None, dto: str = None, depts: str = None, people: str = None, levels: str = None,
          status: str = None, tag: str = None, system: str = None, process: str = None, work_type: str = None, q: str = None, limit: int = 120, offset: int = 0):
    """One page of the wiki list, most recently active first."""
    limit = max(1, min(limit, MAX_ROWS))
    a, b = _window(days, dfrom, dto)
    links = PageEmail.objects.filter(page__archived=False)
    if a:
        links = links.filter(email__day__gte=a)
    if b:
        links = links.filter(email__day__lte=b)
    if _split(depts):   # a page belongs to ONE department (Page.dept)
        links = links.filter(page__dept__in=[d.lower() for d in _split(depts)])
    ppl = _split(people)
    if ppl:
        links = links.filter(email__from_addr__in=ppl)
    agg: dict[int, dict] = {}
    for pid, name, addr, depts_, day in links.values_list("page_id", "email__from_name", "email__from_addr", "email__depts", "email__day"):
        d = agg.setdefault(pid, {"n": 0, "people": set(), "depts": set(), "last": ""})
        d["n"] += 1
        d["people"].add(name or addr)
        d["depts"].update(depts_)
        d["last"] = max(d["last"], day)
    qs = Page.objects.filter(id__in=list(agg), archived=False).defer("created_reason")
    if levels:
        qs = qs.filter(level__in=_split(levels))
    if status:
        qs = qs.filter(status__in=_split(status))
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(sections__text__icontains=q)).distinct()
    hits = []
    for p in qs:
        t = p.tags or {}
        if tag:
            ty, v = tag.split(":", 1)
            if v not in t.get(ty, []):
                continue
        if system and system not in t.get("system", []):
            continue
        if process and process not in t.get("process", []):
            continue
        if work_type and work_type not in t.get("work_type", []):
            continue
        hits.append(p)
    hits.sort(key=lambda p: (agg[p.id]["last"], agg[p.id]["n"]), reverse=True)
    part = hits[offset:offset + limit]
    tops: dict[int, dict] = {}    # only the sections the card shows, only for this page of the list
    for sec in Section.objects.filter(page_id__in=[p.id for p in part], key__in=("summary", "overview", "what")).exclude(text=""):
        tops.setdefault(sec.page_id, {})[sec.key] = sec
    rows = []
    for p in part:
        d = agg[p.id]
        rows.append({**_page_row(p, tops.get(p.id, {})), "emails": d["n"], "people": sorted(d["people"])[:8], "depts": sorted(d["depts"]), "last_day": d["last"]})
    return {"total_pages": Page.objects.filter(archived=False).count(), "total": len(hits), "offset": offset, "rows": rows}


def _get_page(pid: str) -> Page:
    if str(pid).isdigit():
        return get_object_or_404(Page, id=int(pid))
    return get_object_or_404(Page, slug=pid)


@api.get("/pages/{pid}")
def page_detail(request, pid: str):
    p = _get_page(pid)
    secs = {s.key: s for s in p.sections.all()}
    pend = {}
    for it in Item.objects.filter(page=p, status__in=["pending", "failed"]):
        pend.setdefault(it.section_key, []).append({"id": it.id, "text": it.text, "emails": it.emails, "status": it.status, "attempts": it.attempts})
    em_ids = set()
    for s in secs.values():
        em_ids.update(s.sources or [])
        em_ids.update(int(x) for x in re.findall(r"\[e(\d+)\]", s.text))
    em = {e.id: e for e in Email.objects.filter(id__in=list(em_ids))}

    def src(eid):
        e = em.get(eid)
        return {"email_id": eid, "sender": (e.from_name or e.from_addr) if e else "?", "day": e.day if e else "", "subject": e.subject if e else ""}
    sections = []
    if free():   # the summary + the editor's own sections, in page order; items wait under the summary
        shown = [s for s in secs.values() if s.key == "summary" or s.text.strip()]
        shown.sort(key=lambda s: (s.key != "summary", s.position, s.id))
        rows = [(s.key, section_label(p.level, s.key, s.title), "") for s in shown] or [("summary", "Summary", "")]
        pend = {"summary": [x for v in pend.values() for x in v]}
    else:
        rows = TEMPLATES.get(p.level, TEMPLATES["topic"])
    for key, label, hint in rows:
        s = secs.get(key)
        sections.append({"key": key, "label": label, "hint": hint, "text": s.text if s else "", "version": s.version if s else 0,
                         "updated": s.updated_at.isoformat() if s and s.updated_at else None,
                         "sources": [src(x) for x in (s.sources if s else [])][:40], "pending": pend.get(key, []),
                         "rejected": SectionHistory.objects.filter(page=p, key=key, accepted=False).count()})
    links = list(PageEmail.objects.filter(page=p).select_related("email").defer(*[f"email__{x}" for x in LIST_DEFER]).order_by("-email__sent_at"))
    shown = links[:300]    # the timeline: its newest emails; the people and counts below use them all
    others = {}
    for eid, pid2, title in PageEmail.objects.filter(email_id__in=[l.email_id for l in shown]).exclude(page=p).values_list("email_id", "page_id", "page__title"):
        others.setdefault(eid, []).append({"id": pid2, "title": title})
    timeline = [{**_email_row(l.email), "role": l.role, "also": others.get(l.email_id, [])} for l in shown]
    internal = {d.lower() for d in get_settings().sources.internal_domains}
    ppl = {}
    for l in links:
        k = l.email.from_addr
        d = ppl.setdefault(k, {"addr": k, "name": l.email.from_name or k, "n": 0, "internal": k.split("@")[-1].lower() in internal})
        d["n"] += 1
    depts = {}
    for l in links:
        for d in l.email.depts:
            depts[d] = depts.get(d, 0) + 1
    threads = {}
    for l in links:
        threads.setdefault(l.email.thread_id, {"id": l.email.thread_id, "subject": l.email.subject, "n": 0})["n"] += 1
    atts = [{"id": i, "filename": fn, "email_id": eid, "day": day}
            for i, fn, eid, day in Attachment.objects.filter(email__page_links__page=p, inline=False).values_list("id", "filename", "email_id", "email__day")[:30]]
    related = []
    try:
        from llmwiki.pipeline.reviewer import page_vectors
        pv = _ttl("page_vectors", 300, page_vectors)
        if p.id in pv:
            sims = sorted(((float(pv[p.id] @ v), pid2) for pid2, v in pv.items() if pid2 != p.id), reverse=True)[:6]
            titles = dict(Page.objects.filter(id__in=[x[1] for x in sims], archived=False).values_list("id", "title"))
            related = [{"id": i, "title": titles[i], "sim": round(sim, 3)} for sim, i in sims if i in titles]
    except Exception:
        log.debug("related pages failed", exc_info=True)
    from llmwiki.pipeline.pages import length_info
    return {**_page_row(p), "created": p.created_at.isoformat(), "updated": p.updated_at.isoformat(), "created_reason": p.created_reason,
            "structure": "free" if free() else "template", "length": length_info(p), "sections": sections, "open_issues": [{"code": o.code, "text": o.text, "email_id": o.email_id, "opened": o.opened_at.date().isoformat(),
                                                   "closed": o.closed_at.date().isoformat() if o.closed_at else None} for o in OpenIssue.objects.filter(page=p).order_by("opened_at")],
            "status_changes": [{"old": c.old, "new": c.new, "at": c.at.date().isoformat(), "email_id": c.email_id} for c in StatusChange.objects.filter(page=p).order_by("at")],
            "timeline": timeline, "timeline_total": len(links), "people": sorted(ppl.values(), key=lambda x: -x["n"]), "depts": depts, "threads": list(threads.values()),
            "attachments": atts, "related": related, "merged_into": p.merged_into_id, "archived": p.archived}


@api.get("/pages/{pid}/history")
def page_history(request, pid: str, key: str = None):
    p = _get_page(pid)
    qs = SectionHistory.objects.filter(page=p)
    if key:
        qs = qs.filter(key=key)
    return [{"id": h.id, "key": h.key, "label": section_label(p.level, h.key, h.title), "version": h.version, "accepted": h.accepted, "round": h.round,
             "guards": h.guards, "reason": h.reason, "text": h.text, "sources": h.sources, "call_id": h.call_id, "at": h.created_at.isoformat()}
            for h in qs.order_by("-id")[:100]]


@api.patch("/pages/{pid}")
def page_edit(request, pid: str, payload: dict = Body(...)):
    """A person moves a page to another department ("" = back to automatic)."""
    from llmwiki.departments import all_depts, assign, name_of
    p = _get_page(pid)
    if "dept" in payload:
        d = str(payload["dept"] or "").lower()
        if d and d not in all_depts():
            return api.create_response(request, {"detail": f"unknown department {d}"}, status=400)
        if d:
            Page.objects.filter(id=p.id).update(dept=d, dept_locked=True)
            event("wiki", f"{p.title}: moved to {name_of(d)} by {_user(request) or 'a user'}", page=p.id)
        else:
            Page.objects.filter(id=p.id).update(dept_locked=False)
            p.refresh_from_db()
            assign(p)
    return {"ok": True}


@api.post("/pages/{pid}/rollback")
def page_rollback(request, pid: str, payload: dict = Body(...)):
    from llmwiki.pipeline.pages import rollback
    p = _get_page(pid)
    v = rollback(p, payload["key"], int(payload["version"]), _user(request))
    from llmwiki.rag.index import index_pages
    index_pages([p.id])
    return {"ok": True, "version": v}


# ------------------------------------------------------------------ search
@api.get("/search")
def search(request, q: str, collections: str = None, k: int = 12):
    from llmwiki.rag.search import hybrid
    ids = [{"type": i.type, "value": i.value, "email": _email_row(i.email)} for i in Identifier.objects.filter(value__iexact=q.strip()).select_related("email")[:10]]
    hits = hybrid(q, {"collections": _split(collections)} if collections else None, top_k=k)
    out = []
    for h in hits:
        d = h.to_dict(500)
        if h.chunk.page_id:
            d["page_title"] = Page.objects.filter(id=h.chunk.page_id).values_list("title", flat=True).first()
        out.append(d)
    return {"q": q, "identifiers": ids, "hits": out}


# ------------------------------------------------------------------ chat
@api.post("/chat")
def chat_start(request, payload: dict = Body(...)):
    from llmwiki.chat import service
    q = str(payload.get("question") or "").strip()
    if not q:
        return api.create_response(request, {"detail": "empty question"}, status=400)
    m = service.start(q, payload.get("mode") or "fast", payload.get("view") or {}, payload.get("session_id"), _user(request))
    return {"id": m.id, "session_id": m.session_id}


@api.get("/chat/{mid}")
def chat_get(request, mid: int):
    m = get_object_or_404(ChatMessage, id=mid)
    return {"id": m.id, "session_id": m.session_id, "question": m.question, "mode": m.mode, "status": m.status, "steps": m.steps, "answer": m.answer,
            "sources": m.sources, "confidence": m.confidence, "error": m.error, "feedback": m.feedback,
            "seconds": round((m.finished_at - m.created_at).total_seconds(), 1) if m.finished_at else None}


@api.post("/chat/{mid}/feedback")
def chat_feedback(request, mid: int, payload: dict = Body(...)):
    ChatMessage.objects.filter(id=mid).update(feedback=int(payload.get("value", 0)))
    return {"ok": True}


# ------------------------------------------------------------------ reports
@api.get("/reports/briefing")
def briefing(request, period: str = None, dept: str = None, people: str = None, area: str = None, kind: str = None):
    from llmwiki.reports.briefing import build
    return build(period, {"dept": dept, "people": _split(people), "area": area, "kind": kind if kind in ("daily", "weekly", "monthly") else None})


@api.get("/reports/week")
def report_week(request, period: str = None, dept: str = None):
    from llmwiki.reports.briefing import topic_week
    return topic_week(period, dept)


@api.post("/reports/generate")
def briefing_generate(request, payload: dict = Body(...)):
    from llmwiki.scheduler import run_action
    started = run_action("report", period=payload.get("period"), dept=payload.get("dept"))
    return {"started": started}


# ------------------------------------------------------------------ agent: logs, pipeline, pool, rules, tags
@api.get("/agent/logs")
def agent_logs(request, kind: str = None, issues: bool = False, limit: int = 15):
    from llmwiki.ingest.health import check_store
    from llmwiki.llm.pool import pool
    from llmwiki.scheduler import running_actions
    s = get_settings()
    runs = Run.objects.order_by("-id")
    if kind:
        runs = runs.filter(kind=kind)
    out = []
    for r in runs[:min(limit, 50)]:
        evs = list(r.events.order_by("id")[:60])
        bad = any(e.level in ("warning", "error") for e in evs) or r.status == "failed"
        if issues and not bad:
            continue
        jobs = dict(r.jobs.values_list("stage").annotate(n=Count("id")))
        jobs_failed = r.jobs.filter(status="failed").count()
        out.append({"id": r.id, "kind": r.kind, "status": r.status, "started": r.started_at.isoformat(), "finished": r.finished_at.isoformat() if r.finished_at else None,
                    "stats": r.stats, "note": r.note[:2000], "jobs": jobs, "jobs_failed": jobs_failed,
                    "events": [{"ts": e.ts.isoformat(), "kind": e.kind, "level": e.level, "message": e.message, "data": e.data} for e in evs]})
    loose = [{"ts": e.ts.isoformat(), "kind": e.kind, "level": e.level, "message": e.message} for e in Event.objects.filter(run__isnull=True).order_by("-id")[:40]]
    calls = LlmCall.objects.filter(created_at__gte=timezone.now() - timedelta(days=1))
    total_units = Job.objects.filter(stage__in=["s1", "s12", "s123"], created_at__gte=timezone.now() - timedelta(days=1)).count()
    retried = LlmCall.objects.filter(created_at__gte=timezone.now() - timedelta(days=1), round__gt=1).values("unit").distinct().count()
    fast_fail = Job.objects.filter(stage__in=["s12", "s123"], status="failed", created_at__gte=timezone.now() - timedelta(days=1)).count()
    from llmwiki.pipeline.runner import _run_lock
    return {"runs": out, "events": loose, "health": _ttl("health", 30, check_store), "running": running_actions(), "processing": _run_lock.locked(), "stopped": pool().halted(),
            "schedule": {"batch_time": s.schedule.batch_time, "batch_enabled": s.schedule.batch_enabled, "live": s.schedule.live_enabled,
                         "live_tick": s.schedule.live_tick_minutes, "live_lint": s.schedule.live_lint_minutes, "last_batch": KV.get("last_batch_day"),
                         "live_failures": KV.get("live_failures", 0), "paused_for_chat": pool().chat_active()},
            "rates_24h": {"calls": calls.count(), "units": total_units, "units_retried": retried, "fast_path_fallbacks": fast_fail,
                          "rejected": calls.filter(status="rejected").count(), "timeouts": calls.filter(status="timeout").count(),
                          "invalid": calls.filter(status="invalid").count(), "blocked": calls.filter(status="blocked").count()}}


@api.get("/agent/units")
def agent_units(request, limit: int = 200, q: str = None):
    qs = Job.objects.order_by("-id")
    if q:
        qs = qs.filter(unit__icontains=q)
    limit = min(limit, MAX_ROWS)
    seen, out = set(), []
    for unit, stage, status, at in qs.values_list("unit", "stage", "status", "created_at")[: limit * 4]:
        if unit in seen:
            continue
        seen.add(unit)
        out.append({"unit": unit, "stage": stage, "status": status, "at": at.isoformat()})
        if len(out) >= limit:
            break
    return out


def _unit_emails(unit: str) -> list[Email]:
    m = re.match(r"^td:(\d+):(\d{4}-\d{2}-\d{2})$", unit)
    if m:
        return list(Email.objects.filter(thread_id=int(m.group(1)), day=m.group(2)).exclude(status="duplicate").order_by("sent_at"))
    m = re.match(r"^dl:(\d+):(\d+)-(\d+)$", unit)
    if m:
        return list(Email.objects.filter(thread_id=int(m.group(1)), id__gte=int(m.group(2)), id__lte=int(m.group(3))).order_by("sent_at"))
    return []


@api.get("/agent/unit")
def agent_unit(request, unit: str):
    jobs = list(Job.objects.filter(unit=unit).order_by("id"))
    calls = list(LlmCall.objects.filter(Q(unit=unit) | Q(job__in=jobs)).defer("system", "prompt").order_by("-id")[:100])[::-1]
    es = _unit_emails(unit)
    pages = []
    if unit.startswith("page:"):
        pages = [int(unit.split(":")[1])]
    else:
        pages = list(PageEmail.objects.filter(email__in=es).values_list("page_id", flat=True).distinct())
    ed_calls = list(LlmCall.objects.filter(stage="s3", unit__regex=r"^ed:(%s):" % "|".join(str(p) for p in pages) if pages else r"^$").defer("system", "prompt").order_by("id")[:20])
    return {"unit": unit, "emails": [_email_row(e) for e in es],
            "jobs": [{"id": j.id, "stage": j.stage, "status": j.status, "attempts": j.attempts, "error": j.error[:2000], "result": j.result,
                      "started": j.started_at.isoformat() if j.started_at else None, "finished": j.finished_at.isoformat() if j.finished_at else None} for j in jobs],
            "calls": [_call(c, full=False) for c in calls], "editor_calls": [_call(c, full=False) for c in ed_calls],
            "pages": [{"id": p.id, "title": p.title} for p in Page.objects.filter(id__in=pages)]}


def _call(c: LlmCall, full=True, brief=False) -> dict:
    """brief: only the row for a table (the query defers the texts)."""
    d = {"id": c.id, "stage": c.stage, "unit": c.unit, "endpoint": c.endpoint, "status": c.status, "http_code": c.http_code, "attempt": c.attempt,
         "round": c.round, "queue_wait_ms": c.queue_wait_ms, "latency_ms": c.latency_ms, "prompt_tokens": c.prompt_tokens,
         "errors": c.errors, "redactions": c.redactions, "at": c.created_at.isoformat()}
    if brief:
        return d | {"sections": [], "parsed": None, "response": ""}
    d.update({"sections": c.sections, "parsed": c.parsed})
    if full:
        d.update({"system": c.system, "prompt": c.prompt, "response": c.response})
    else:
        d.update({"response": c.response[:3000]})
    return d


@api.get("/agent/calls/{cid}")
def agent_call(request, cid: int):
    return _call(get_object_or_404(LlmCall, id=cid))


@api.get("/agent/pool")
def agent_pool(request):
    from llmwiki.llm.pool import pool
    from llmwiki.pipeline.runner import eta
    hour = timezone.now() - timedelta(hours=1)
    recent = LlmCall.objects.order_by("-id").defer("system", "prompt", "response", "parsed", "sections")[:40]
    return {"endpoints": pool().status(), "eta": eta(), "paused_for_chat": pool().chat_active(),
            "queue": {"emails_waiting": Email.objects.filter(status__in=["new", "noted", "failed"]).count(),
                      "items_pending": Item.objects.filter(status="pending").count(),
                      "jobs_running": dict(Job.objects.filter(status="running").values_list("stage").annotate(n=Count("id")))},
            "last_hour": dict(LlmCall.objects.filter(created_at__gte=hour).values_list("status").annotate(n=Count("id"))),
            "recent": [_call(c, brief=True) for c in recent], "refusals": refusals()}


def refusals(days: int = 14, limit: int = 40) -> dict:
    """Requests an endpoint refused (status 'blocked'): counts per day and endpoint, and the latest ones with what
    happened next (the failover call for the same unit and stage)."""
    since = timezone.now() - timedelta(days=days)
    qs = LlmCall.objects.filter(status="blocked", created_at__gte=since)
    by_day: dict[str, dict[str, int]] = {}
    for at, ep in qs.values_list("created_at", "endpoint"):
        d = timezone.localtime(at).date().isoformat()
        by_day.setdefault(d, {}).setdefault(ep, 0)
        by_day[d][ep] += 1
    sent = dict(LlmCall.objects.filter(created_at__gte=since).values_list("endpoint").annotate(n=Count("id")))
    rows = []
    for c in qs.order_by("-id").defer("system", "prompt", "response", "parsed", "sections")[:limit]:
        nxt = LlmCall.objects.filter(unit=c.unit, stage=c.stage, id__gt=c.id).order_by("id").values("endpoint", "status").first()
        rows.append({"id": c.id, "at": c.created_at.isoformat(), "endpoint": c.endpoint, "stage": c.stage, "unit": c.unit, "http_code": c.http_code,
                     "error": (c.errors or [""])[0][:400], "prompt_tokens": c.prompt_tokens, "redactions": c.redactions,
                     "then": nxt})
    totals = dict(qs.values_list("endpoint").annotate(n=Count("id")))
    return {"days": days, "by_day": dict(sorted(by_day.items())), "totals": totals,
            "rate": {ep: round(n / max(1, sent.get(ep, n)), 3) for ep, n in totals.items()}, "recent": rows}


@api.get("/agent/refusals")
def agent_refusals(request, days: int = 14):
    return refusals(days)


@api.post("/agent/resume")
def agent_resume(request):
    """The Resume button: send requests to the stopped endpoints again (it does not start a run)."""
    from llmwiki.llm.pool import pool
    return {"resumed": pool().resume(_user(request) or "a user")}


@api.post("/agent/stop")
def agent_stop(request):
    """The Stop button: the current run starts nothing new; requests already running finish."""
    from llmwiki.pipeline import runner
    if not runner._run_lock.locked():
        return {"stopped": False}
    runner.stop(f"stopped by {_user(request) or 'a user'}")
    return {"stopped": True}


def _rule_row(r: SkipRule) -> dict:
    return {"id": r.id, "kind": r.kind, "sender": r.sender, "subject": r.subject, "recipient": r.recipient, "template": r.template, "action": r.action,
            "folder": r.folder_id, "folder_name": r.folder.name if r.folder_id else "", "enabled": r.enabled, "include_replies": r.include_replies,
            "hits": r.hits, "why": r.why, "updated": r.updated_at.isoformat()}


def _rule_fields(payload: dict) -> dict:
    return {"sender": str(payload.get("sender") or "").strip()[:300], "subject": str(payload.get("subject") or "").strip()[:500],
            "recipient": str(payload.get("recipient") or "").strip()[:300], "action": payload.get("action") or "skip",
            "folder_id": payload.get("folder") or None, "include_replies": bool(payload.get("include_replies", False))}


@api.get("/agent/rules")
def rules(request):
    s = get_settings().skip
    return {"builtin": {"auto_submitted": s.auto_submitted, "bulk": s.bulk, "auto_reply": s.auto_reply, "rules_skip_replies": s.rules_skip_replies,
                        "noreply_senders": s.noreply_senders, "learned_enabled": s.learned_enabled, "learn_min": s.learn_min,
                        "learn_days": s.learn_days, "audit_every": s.audit_every},
            "rules": [_rule_row(r) for r in SkipRule.objects.select_related("folder").order_by("kind", "-hits")],
            "admin": _is_admin(request),
            "skipped_30d": Email.objects.filter(status="skipped", sent_at__gte=timezone.now() - timedelta(days=30)).count()}


@api.post("/agent/rules/preview")
def rule_preview(request, payload: dict = Body(...)):
    """What a rule would do to the stored mail. Changes nothing, so the portal can ask while someone types."""
    errors = [e for e in skiprules.validate(payload) if "folder" not in e]
    if errors:
        return {"errors": errors}
    return skiprules.preview(SkipRule(kind="static", **_rule_fields(payload))) | {"errors": []}


@api.post("/agent/rules/apply_all")
def rules_apply_all(request):
    """Run every enabled rule on the stored mail."""
    if not _is_admin(request):
        return _denied(request)
    return skiprules.apply_all()


@api.post("/agent/rules")
def rule_add(request, payload: dict = Body(...)):
    """{sender, subject, recipient, action, folder, include_replies, apply}. apply (default on) also runs it on the stored mail."""
    if not _is_admin(request):
        return _denied(request)
    errors = skiprules.validate(payload)
    if errors:
        return api.create_response(request, {"detail": " ".join(errors)}, status=400)
    r = SkipRule.objects.create(kind="static", why=str(payload.get("why") or "")[:500] or f"added by {_user(request) or 'user'}", **_rule_fields(payload))
    return {"id": r.id, "applied": skiprules.apply_rule(r) if payload.get("apply", True) else None}


@api.patch("/agent/rules/{rid}")
def rule_edit(request, rid: int, payload: dict = Body(...)):
    """Changing a rule first undoes what it did, then applies the new version (apply: false = new mail only)."""
    if not _is_admin(request):
        return _denied(request)
    r = get_object_or_404(SkipRule, id=rid)
    merged = {**_rule_row(r), **payload}
    if r.kind == "static" and (errors := skiprules.validate(merged)):
        return api.create_response(request, {"detail": " ".join(errors)}, status=400)
    if "folder" in payload:
        r.folder_id = payload["folder"] or None
    for k in ("enabled", "action", "sender", "subject", "recipient", "include_replies", "why"):
        if k in payload:
            setattr(r, k, payload[k])
    r.save()
    undone = skiprules.revert_rule(r.id)
    return {"ok": True, "undone": undone, "applied": skiprules.apply_rule(r) if payload.get("apply", True) else None}


@api.delete("/agent/rules/{rid}")
def rule_delete(request, rid: int):
    """Removing a rule also undoes it: the mail it skipped goes back in the queue."""
    if not _is_admin(request):
        return _denied(request)
    SkipRule.objects.filter(id=rid).delete()
    return {"ok": True, "undone": skiprules.revert_rule(rid)}


@api.get("/agent/tags")
def tags(request):
    pages = list(Page.objects.filter(archived=False).values_list("tags", flat=True))
    usage: dict[tuple, int] = {}
    for t in pages:
        for ty, vals in (t or {}).items():
            for v in vals or []:
                usage[(ty, v)] = usage.get((ty, v), 0) + 1
    s = get_settings().tags
    lists = {"system": s.systems, "process": s.processes, "work_type": s.work_types}
    rows = [{"type": ty, "value": v, "state": "active", "pages": usage.get((ty, v), 0), "threads": None, "why": "fixed list (Settings › Tags)"}
            for ty, vals in lists.items() for v in vals]
    for ty in ("site", "partner"):
        for (t2, v), n in usage.items():
            if t2 == ty:
                rows.append({"type": ty, "value": v, "state": "active", "pages": n, "threads": None, "why": "script: from the page's emails"})
    for t in Tag.objects.all().order_by("type", "value"):
        rows.append({"type": t.type, "value": t.value, "state": t.state, "pages": usage.get((t.type, t.value), 0), "threads": t.threads, "why": t.why,
                     "contexts": [c.get("text") for c in (t.contexts or [])][:3]})
    log_ = [{"ts": e.ts.isoformat(), "message": e.message, "level": e.level} for e in Event.objects.filter(kind="tags").order_by("-id")[:40]]
    return {"rows": rows, "log": log_}


# ------------------------------------------------------------------ settings
@api.get("/settings")
def settings_get(request):
    s = get_settings()
    hist = [{"id": h.id, "user": h.user, "note": h.note, "at": h.created_at.isoformat()} for h in SettingsVersion.objects.order_by("-id")[:15]]
    return {"schema": Settings.model_json_schema(), "values": json.loads(s.model_dump_json()), "defaults": json.loads(Settings().model_dump_json()),
            "groups": GROUP_LABELS, "presets": {k: {"label": v["label"], "cap": v["cap"]} for k, v in PRESETS.items()}, "history": hist,
            "pinned": pinned_keys()}


@api.put("/settings")
def settings_put(request, payload: dict = Body(...)):
    try:
        s = Settings.model_validate(payload.get("values") or payload)
    except Exception as e:
        return api.create_response(request, {"detail": str(e)[:3000]}, status=400)
    save_settings(s, user=_user(request), note=payload.get("note", "saved in the portal"))
    from llmwiki.llm.pool import pool
    pool()._refresh()
    return {"ok": True}


@api.post("/settings/preset")
def settings_preset(request, payload: dict = Body(...)):
    s = get_settings().model_copy(deep=True)
    s.budgets = s.budgets.apply_preset(payload["name"])
    save_settings(s, user=_user(request), note=f"preset {payload['name']}")
    return {"ok": True}


# ------------------------------------------------------------------ actions
@api.post("/actions/{name}")
def action(request, name: str, payload: dict = Body(default={})):
    from llmwiki.scheduler import run_action
    try:
        started = run_action(name, **(payload or {}))
    except ValueError as e:
        return api.create_response(request, {"detail": str(e)}, status=400)
    return {"started": started}


@api.get("/eval")
def eval_results(request):
    from django.conf import settings as dj
    d = dj.BASE_DIR / "eval" / "results"
    files = sorted(d.glob("*.json")) if d.exists() else []
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else {}
