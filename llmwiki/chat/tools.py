"""Chat tools and the numbered source list.

Every piece of evidence the agent sees gets a number [n] (pre-retrieval, then each tool result). The model cites
those numbers; the script maps them back to pages/emails. Unknown numbers are dropped.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta

from llmwiki.models import Email, Identifier, Page, Thread
from llmwiki.pipeline.templates import section_label
from llmwiki.rag.search import hybrid
from llmwiki.util import clip_tokens

TOOLS_DOC = """Tools (call at most one per round):
- search_wiki {"query": "...", "days": 0}            wiki page sections (the main source)
- search_notes {"query": "...", "days": 30}          one-line notes about individual emails
- search_emails {"query": "...", "days": 30}         email text, tables and attachment extracts
- read_page {"page": "page title"}                   all sections of one page
- get_email {"n": 3}                                 the full email behind source [3] (or {"id": 123})
- get_thread {"n": 3}                                all notes of the thread of source [3]
- lookup_identifier {"value": "4500123456"}          exact search for PO / delivery / invoice / container numbers
- list_pages {"status": "At risk", "days": 7}        pages with recent activity (status optional)"""


@dataclass
class Source:
    n: int
    kind: str          # wiki | note | email | attachment | page | thread
    ref: str
    title: str
    text: str
    page_id: int | None = None
    email_id: int | None = None
    day: str = ""

    def render(self, chars: int = 1600) -> str:
        return f"[{self.n}] {self.kind} · {self.title}{(' · ' + self.day) if self.day else ''}\n{self.text[:chars]}"

    def public(self) -> dict:
        return {"n": self.n, "kind": self.kind, "ref": self.ref, "title": self.title, "page_id": self.page_id, "email_id": self.email_id,
                "day": self.day, "snippet": self.text[:300]}


@dataclass
class Sources:
    items: list[Source] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)   # deep-mode researchers add in parallel

    def add(self, kind, ref, title, text, page_id=None, email_id=None, day="") -> Source:
        with self.lock:
            for s in self.items:
                if s.ref == ref and s.text[:200] == text[:200]:
                    return s
            s = Source(len(self.items) + 1, kind, ref, title, text, page_id, email_id, day)
            self.items.append(s)
            return s

    def get(self, n) -> Source | None:
        try:
            n = int(n)
        except Exception:
            return None
        return self.items[n - 1] if 1 <= n <= len(self.items) else None


KIND = {"wiki": "wiki", "notes": "note", "emails": "email", "attachments": "attachment"}


def add_hits(src: Sources, hits) -> list[Source]:
    out = []
    for h in hits:
        c = h.chunk
        kind = KIND.get(c.collection, c.collection)
        title = c.header
        out.append(src.add(kind, c.ref, title, c.text, page_id=c.page_id, email_id=c.email_id, day=c.day))
    return out


def _days(args) -> dict:
    try:
        d = int(args.get("days") or 0)
    except Exception:
        d = 0
    return {"day_from": (date.today() - timedelta(days=d)).isoformat()} if d > 0 else {}


def _find_page(name: str) -> Page | None:
    name = (name or "").strip()
    if not name:
        return None
    p = Page.objects.filter(title__iexact=name, archived=False).first() or Page.objects.filter(slug=name).first()
    if p:
        return p
    p = Page.objects.filter(title__icontains=name[:60], archived=False).first()
    if p:
        return p
    hits = hybrid(name, {"collections": ["wiki"]}, top_k=1)
    return Page.objects.filter(id=hits[0].chunk.page_id).first() if hits else None


def run_tool(name: str, args: dict, src: Sources, top_k: int, tool_tokens: int) -> tuple[str, list[Source]]:
    """Execute a tool, add its evidence to the source list, return (text for the model, new sources)."""
    args = args if isinstance(args, dict) else {}
    q = str(args.get("query") or args.get("q") or "")
    new: list[Source] = []
    if name in ("search_wiki", "search_notes", "search_emails"):
        coll = {"search_wiki": ["wiki"], "search_notes": ["notes"], "search_emails": ["emails", "attachments"]}[name]
        new = add_hits(src, hybrid(q, {"collections": coll, **_days(args)}, top_k=top_k))
    elif name == "read_page":
        p = _find_page(str(args.get("page") or q))
        if p:
            for s in p.sections.all():
                if s.text.strip():
                    new.append(src.add("wiki", f"page:{p.id}:{s.key}", f"Page: {p.title} › {section_label(p.level, s.key, s.title)}", re.sub(r"\[e\d+\]", "", s.text),
                                       page_id=p.id, day=(s.updated_at.strftime("%Y-%m-%d") if s.updated_at else "")))
    elif name in ("get_email", "get_thread"):
        e = None
        if args.get("n") is not None:
            s = src.get(args.get("n"))
            if s and s.email_id:
                e = Email.objects.filter(id=s.email_id).first()
        if e is None and args.get("id"):
            e = Email.objects.filter(id=args.get("id")).first()
        if e and name == "get_email":
            atts = "; ".join(f"{a.filename}: {a.extract[:800]}" for a in e.attachments.all() if a.extract)
            new.append(src.add("email", f"email:{e.id}", f"{e.subject} · {e.from_name or e.from_addr}", e.new_text + (("\nAttachments: " + atts) if atts else ""),
                               email_id=e.id, day=e.day))
        elif e and name == "get_thread":
            lines = [f"{x.day} {x.from_name or x.from_addr}: {x.note or x.subject}" for x in Email.objects.filter(thread_id=e.thread_id).exclude(status="duplicate").order_by("sent_at")]
            t = Thread.objects.filter(id=e.thread_id).first()
            new.append(src.add("thread", f"thread:{e.thread_id}", f"Thread: {t.subject if t else e.subject}", "\n".join(lines[-40:]), email_id=e.id, day=e.day))
    elif name == "lookup_identifier":
        v = str(args.get("value") or q).strip()
        for ident in Identifier.objects.filter(value__iexact=v).select_related("email")[:6]:
            e = ident.email
            new.append(src.add("email", f"email:{e.id}", f"{e.subject} · {e.from_name or e.from_addr} ({ident.type} {v})", e.note or e.new_text[:600], email_id=e.id, day=e.day))
        if not new:
            new = add_hits(src, hybrid(v, {"collections": ["emails", "notes", "attachments"]}, top_k=4, mode="bm25"))
    elif name == "list_pages":
        qs = Page.objects.filter(archived=False)
        if args.get("status"):
            qs = qs.filter(status__iexact=str(args["status"]))
        d = _days(args)
        if d:
            qs = qs.filter(last_email_at__date__gte=d["day_from"])
        rows = [f"{p.title} — {p.level}, {p.status}, last mail {p.last_email_at:%Y-%m-%d}" if p.last_email_at else f"{p.title} — {p.level}, {p.status}"
                for p in qs.order_by("-last_email_at")[:25]]
        new.append(src.add("page", f"list:{args}", "Page list", "\n".join(rows) or "no pages"))
    else:
        return f"Unknown tool {name}. Use one of the listed tools.", []
    if not new:
        return f"{name} found nothing.", []
    text = "\n\n".join(s.render() for s in new)
    return clip_tokens(text, tool_tokens), new
