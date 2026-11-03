"""Processing units and the context sections that go into prompts.

Unit = the emails processed together:
- batch/backfill: a thread-day (thread T on local day D)
- live: a thread delta (the thread's new emails since it was last processed)
Every stage receives the unit's emails in full (within budget). Emails are numbered [1..n] inside a prompt; the model
only ever writes those numbers, the script maps them back to Email ids.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from llmwiki.conf import get_settings
from llmwiki.models import Email, OpenIssue, Page, PageEmail, QuotedBlock
from llmwiki.pipeline.templates import TEMPLATES, free, section_label
from llmwiki.util import first_sentence, lang_name, local_dt, model_text


@dataclass
class Unit:
    key: str
    thread_id: int
    day: str
    emails: list[Email]
    mode: str = "batch"
    extra: dict = field(default_factory=dict)

    @property
    def ids(self):
        return [e.id for e in self.emails]

    def num(self, email_id: int) -> int:
        return self.ids.index(email_id) + 1


def build_units(emails: list[Email], mode: str) -> list[Unit]:
    groups: "OrderedDict[tuple, list[Email]]" = OrderedDict()
    for e in sorted(emails, key=lambda x: x.sent_at):
        k = (e.thread_id, e.day) if mode != "live" else (e.thread_id, "")
        groups.setdefault(k, []).append(e)
    units = []
    for (tid, day), es in groups.items():
        key = f"td:{tid}:{day}" if mode != "live" else f"dl:{tid}:{es[0].id}-{es[-1].id}"
        units.append(Unit(key=key, thread_id=tid, day=day or es[0].day, emails=es, mode=mode))
    return units


def fmt_time(e: Email) -> str:
    return local_dt(e.sent_at, get_settings().sources.time_zone).strftime("%Y-%m-%d %H:%M")


def email_block(n: int, e: Email, with_quotes_note: bool = True) -> str:
    who = f"{e.from_name} <{e.from_addr}>" if e.from_name else e.from_addr
    to = ", ".join(e.to[:8]) + (" …" if len(e.to) > 8 else "")
    cc = ", ".join(e.cc[:8]) + (" …" if len(e.cc) > 8 else "")
    atts = [a.filename for a in e.attachments.all() if a.is_file]
    lines = [f"### [{n}] {fmt_time(e)} · {who}", f"To: {to}" + (f" · Cc: {cc}" if cc else ""), f"Subject: {e.subject}"]
    if e.lang != "en":
        lines.append(f"(written in {lang_name(e.lang)})")
    if atts:
        lines.append("Attachments: " + ", ".join(atts[:10]))
    body = model_text(e.new_text).strip() or "(no new text — only quoted mail or attachments)"
    return "\n".join(lines) + "\n" + body


def parent_text(unit: Unit) -> str:
    first = unit.emails[0]
    p = first.parent if first.parent_id and first.parent_id not in unit.ids else None
    if p is None:
        p = (Email.objects.filter(thread_id=unit.thread_id, sent_at__lt=first.sent_at).exclude(status="duplicate")
             .exclude(id__in=unit.ids).order_by("-sent_at").first())
    if not p:
        return ""
    return f"[earlier email · {fmt_time(p)} · {p.from_name or p.from_addr}] Subject: {p.subject}\n{model_text(p.new_text).strip()}"


def outline_text(unit: Unit, limit: int = 40) -> list[str]:
    first = unit.emails[0]
    qs = (Email.objects.filter(thread_id=unit.thread_id, sent_at__lt=first.sent_at).exclude(status="duplicate").exclude(id__in=unit.ids)
          .order_by("-sent_at")[:limit])
    rows = [f"{e.day} | {e.from_name or e.from_addr} | {e.note or e.subject}" for e in reversed(list(qs))]
    return rows


def unseen_blocks(unit: Unit) -> list[QuotedBlock]:
    """Quoted history that matches no stored email (e.g. the department was CC'd late) and was never shown to the model."""
    sent = set(QuotedBlock.objects.filter(email__thread_id=unit.thread_id, sent_to_model=True).values_list("fingerprint", flat=True))
    return [b for b in QuotedBlock.objects.filter(email_id__in=unit.ids, linked__isnull=True, sent_to_model=False).order_by("email__sent_at", "idx")
            if b.fingerprint not in sent and len(b.text) > 40]


def unseen_text(blocks: list[QuotedBlock]) -> list[str]:
    seen, out = set(), []
    for b in blocks:
        if b.fingerprint in seen:
            continue
        seen.add(b.fingerprint)
        hdr = b.header.replace("\n", " · ")[:200]
        out.append(f"[quoted earlier mail, not stored separately{': ' + hdr if hdr else ''}]\n{model_text(b.text)}")
    return out


def attachment_texts(unit: Unit) -> list[str]:
    out = []
    for n, e in enumerate(unit.emails, 1):
        for a in e.attachments.all():
            if a.extract:
                out.append(f"[{n}] {a.filename}:\n{a.extract}")
            elif a.is_image and a.is_file:
                out.append(f"[{n}] {a.filename}: image (no text)")
    return out


def memory_pages(unit: Unit) -> list[Page]:
    ids = (PageEmail.objects.filter(email__thread_id=unit.thread_id).exclude(email_id__in=unit.ids)
           .values_list("page_id", flat=True).distinct())
    return list(Page.objects.filter(id__in=list(ids), archived=False))


def page_card(label: str, p: Page, sections_hit: list[str] | None = None, full_sections: bool = False) -> str:
    secs = {s.key: s for s in p.sections.all()}
    ov = secs.get("summary") or secs.get("overview") or secs.get("what")
    lines = [f"{label}: {p.title} — level {p.level}, status {p.status}"]
    if ov and ov.text:
        lines.append("  About: " + first_sentence(ov.text, 300))
    if free():
        titles = [section_label(p.level, s.key, s.title) for s in sorted(secs.values(), key=lambda x: x.position) if s.text and s.key != "summary"]
        if titles:
            lines.append("  Sections: " + ", ".join(titles))
    else:
        keys = [k for k, _, _ in TEMPLATES.get(p.level, TEMPLATES["topic"])]
        lines.append("  Sections: " + ", ".join(f"{k}{'*' if secs.get(k) and secs[k].text else ''}" for k in keys) + "   (* = has text)")
    for k in (sections_hit or [])[:2]:
        s = secs.get(k)
        if s and s.text and s.key not in ("summary", "overview", "what"):
            lines.append(f"  {section_label(p.level, k, s.title)}: " + first_sentence(s.text, 220))
    opens = list(OpenIssue.objects.filter(page=p, closed_at__isnull=True).order_by("opened_at")[:8])
    if opens:
        lines.append("  Open issues: " + "; ".join(f"{o.code} {o.text[:90]}" for o in opens))
    return "\n".join(lines)
