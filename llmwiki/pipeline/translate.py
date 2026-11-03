"""Translate a stored email on request, keeping its form.

Formatting is kept by scripts, not by asking nicely. The model gets numbered lines and returns numbered lines: blank lines,
list markers and lines without words never reach it, and the answer is checked line by line (every line back, tables with
the same cells, codes and long numbers unchanged). Pictures, web addresses and personal data are replaced by markers before
the prompt and put back afterwards, so the model never sees them and the reader still does.

Context: the thread's notes, the wiki pages this email feeds and the tag catalog tell the model what the mail is about and
how the team writes its terms. Runs in the background (a slow model must not hold a web server thread) with chat priority:
someone is waiting for it. Internal endpoints only, because stage `translate` is not in privacy.external_stages.
"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import close_old_connections, connection
from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.ingest.images import visible_text
from llmwiki.llm.pool import pool
from llmwiki.models import Email, EmailTranslation, PageEmail, Section as PageSection, Tag
from llmwiki.pipeline import prompts
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.util import IMAGE_TOKEN, clip_tokens, est_tokens, lang_name, sha1

log = logging.getLogger(__name__)
MARK = "⟦{}⟧"
_MARK = re.compile(r"⟦\d+⟧")
_URL = re.compile(r"https?://[^\s|)\]>]+")
_LIST = re.compile(r"^(\s*(?:[-*•]|\d{1,3}[.)])\s+)(.*)$")
_CODE = re.compile(r"\b(?=[A-Z0-9_/-]*\d)(?=[A-Z0-9_/-]*[A-Z])[A-Z0-9][A-Z0-9_/-]{2,}\b|\b\d{4,}\b")
STALE = timedelta(minutes=20)

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def needed(e: Email) -> bool:
    return e.lang not in get_settings().translate.skip_langs


def source_of(e: Email) -> str:
    return visible_text(e.new_text, list(e.attachments.all()))


def _hash(e: Email, text: str) -> str:
    return sha1(e.subject, text)


def mask(text: str) -> tuple[str, list[str]]:
    """Replace what must not change or leave (pictures, addresses, personal data) by markers. Reference numbers stay: they are text."""
    from llmwiki.privacy import _compile
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return MARK.format(len(kept) - 1)
    text = IMAGE_TOKEN.sub(keep, text)
    text = _URL.sub(keep, text)
    for _, rx in _compile(tuple(sorted(get_settings().privacy.pii_patterns.items()))):
        text = rx.sub(keep, text)
    return text, kept


def unmask(text: str, kept: list[str]) -> str:
    return re.sub(r"⟦(\d+)⟧", lambda m: kept[int(m.group(1))] if int(m.group(1)) < len(kept) else "", text)


def _has_words(line: str) -> bool:
    return any(c.isalpha() for c in _MARK.sub("", line))


def _context(e: Email) -> list[Section]:
    budget = get_settings().translate.context_tokens
    if not budget:
        return []
    notes = [f"{x.day} {x.from_name or x.from_addr}: {x.note}" for x in Email.objects.filter(thread_id=e.thread_id).exclude(status="duplicate").exclude(note="")
             .order_by("-sent_at").only("day", "from_name", "from_addr", "note")[:12]][::-1]
    pids = list(PageEmail.objects.filter(email=e).values_list("page_id", "page__title"))
    tops = {s.page_id: s.text for s in PageSection.objects.filter(page_id__in=[p for p, _ in pids], key__in=("summary", "overview")).exclude(text="")}
    pages = [title + ": " + clip_tokens(re.sub(r"\[e\d+\]", "", tops.get(pid, "")), 300) for pid, title in pids[:3]]
    terms = list(Tag.objects.filter(state="active").exclude(type="work_type").order_by("type", "value").values_list("value", flat=True)[:200])
    people = list(dict.fromkeys(Email.objects.filter(thread_id=e.thread_id).order_by().values_list("from_name", flat=True)))[:20]
    return [Section("notes", "What the thread is about (notes in the team's words)", notes, int(budget * .4)),
            Section("memory", "Wiki pages of this email", pages, int(budget * .35)),
            Section("related", "Terms the team uses: spell them like this", [", ".join(terms)] if terms else [], int(budget * .15)),
            Section("outline", "People: keep their names as written", [", ".join(people)] if people else [], int(budget * .1))]


def _parts(rows: list[tuple[int, str]], size: int) -> list[list[tuple[int, str]]]:
    out, cur, used = [], [], 0
    for n, line in rows:
        t = est_tokens(line) + 4
        if cur and used + t > size:
            out.append(cur)
            cur, used = [], 0
        cur.append((n, line))
        used += t
    return out + ([cur] if cur else [])


def _translate_part(e: Email, part: list[tuple[int, str]], subject: str, unit: str, system: str, context: list[Section]) -> tuple[dict[int, str], str, str, list]:
    s = get_settings()
    wanted = dict(part)
    rounds = {"n": 0}

    def build(cap, scale):
        lines = "\n".join(f"{n}: {line}" for n, line in part)
        secs = context + [Section("subject", "Subject", [subject], 0), Section("new", "Lines to translate", [lines], 0)]
        text, meta = assemble(secs, cap, s.budgets.cut_order, system, scale)
        return system, "TASK: translate\n\n" + text, meta

    def parse(o):
        rounds["n"] += 1
        if not isinstance(o, dict) or not isinstance(o.get("lines"), list):
            return None, ['the answer must have a "lines" list of {"n": line number, "text": translated line}']
        got: dict[int, str] = {}
        for x in o["lines"]:
            if isinstance(x, dict) and str(x.get("n", "")).lstrip("-").isdigit():
                got[int(x["n"])] = str(x.get("text") or "").strip()
        problems, bad = [], set()
        for n, src in wanted.items():
            new = got.get(n)
            if not new:
                problems.append(f"line {n} is missing")
                bad.add(n)
                continue
            lost = [m for m in _MARK.findall(src) if m not in new] + [c for c in _CODE.findall(src) if c not in new]
            if lost:
                problems.append(f"line {n} must keep these exactly: {', '.join(lost[:6])}")
                bad.add(n)
            if src.lstrip().startswith("|") and (src.count("|") != new.count("|") or not new.startswith("|") or not new.endswith("|")):
                problems.append(f"line {n} is a table row: keep it starting and ending with | and keep its {src.count('|')} | characters")
                bad.add(n)
        last = rounds["n"] > s.pool.validation_retries
        if problems and not last:
            return None, problems[:20]
        # the last round: the good lines count, a bad line stays in its original language
        return {"lines": {n: (wanted[n] if n in bad else got[n]) for n in wanted}, "subject": str(o.get("subject") or "").strip(), "left": len(bad)}, []
    res = pool().run_structured("translate", unit, build, parse, chat=True, schema="translation")
    return res.obj["lines"], res.obj["subject"], res.endpoint, res.calls


def _run(email_id: int, language: str) -> None:
    close_old_connections()
    try:
        e = Email.objects.get(id=email_id)
        src = source_of(e)
        s = get_settings().translate
        masked, kept = mask(src)
        subj_masked, subj_kept = mask(e.subject)
        lines = masked.split("\n")
        prefix, rows = {}, []
        for n, line in enumerate(lines, 1):
            m = _LIST.match(line)
            prefix[n] = m.group(1) if m else ""
            body = m.group(2) if m else line
            if _has_words(body):
                rows.append((n, body))
        system = prompts.translate_system(language, lang_name(e.lang))
        context = _context(e)
        done: dict[int, str] = {}
        subject, endpoint, calls = "", "", []
        parts = _parts(rows, s.part_tokens) or [[]]
        for i, part in enumerate(parts, 1):
            if not part and subject:
                continue
            got, sj, endpoint, cs = _translate_part(e, part, subj_masked, f"translate:{e.id}:{i}", system, context)
            done.update(got)
            subject = subject or sj
            calls += cs
        out = [prefix[n] + done[n] if n in done else line for n, line in enumerate(lines, 1)]
        EmailTranslation.objects.filter(email_id=email_id, language=language).update(
            status="done", text=unmask("\n".join(out), kept), subject=unmask(subject, subj_kept), error="", endpoint=endpoint, calls=calls, source_hash=_hash(e, src))
    except Exception as ex:
        log.warning("translation of email %s failed: %s", email_id, ex)
        EmailTranslation.objects.filter(email_id=email_id, language=language).update(status="failed", error=str(ex)[:500])
    finally:
        connection.close()


def get(e: Email, language: str | None = None) -> dict:
    language = language or get_settings().translate.language
    row = EmailTranslation.objects.filter(email=e, language=language).first()
    out = {"language": language, "from": lang_name(e.lang), "needed": needed(e), "status": "none"}
    if not row:
        return out
    if row.status == "running":
        if timezone.now() - row.updated_at > STALE:      # the process stopped while it ran
            return out
        return {**out, "status": "running"}
    if row.status == "failed":
        return {**out, "status": "failed", "error": row.error}
    if row.source_hash != _hash(e, source_of(e)):        # the text was re-parsed since
        return out
    return {**out, "status": "done", "text": row.text, "subject": row.subject, "endpoint": row.endpoint}


def start(e: Email) -> dict:
    """Start it unless it is already there or under way. Answers at once; the portal asks again until it is done."""
    global _executor
    s = get_settings().translate
    cur = get(e, s.language)
    if cur["status"] in ("running", "done"):
        return cur
    EmailTranslation.objects.update_or_create(email=e, language=s.language, defaults={"status": "running", "error": "", "text": "", "subject": ""})
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=s.workers, thread_name_prefix="translate")
        _executor.submit(_run, e.id, s.language)
    return {**cur, "status": "running"}
