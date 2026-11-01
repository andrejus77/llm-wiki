"""store.add(): the one door every message goes through (POP3, .pst, .eml).

Invariant: every fetched message ends up as exactly one Email row in exactly one Thread.
Returns one of: 'stored', 'merged' (same Message-ID seen before, e.g. POP3 + .pst), 'duplicate' (same content under
another Message-ID within the window; stored with dup_of, never processed), 'error'.
"""
from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction

from llmwiki.conf import get_settings
from llmwiki.ingest import identifiers, skiprules, threads
from llmwiki.ingest.extract import extract
from llmwiki.ingest.mime import parse
from llmwiki.ingest.quotes import split_body
from llmwiki.models import Attachment, Blob, Email, Identifier, QuotedBlock, Tag
from llmwiki.util import detect_lang, local_day, sha1, shingles, subject_norm, subject_template, words

log = logging.getLogger(__name__)


@dataclass
class AddResult:
    status: str
    email_id: int | None = None
    detail: str = ""


def block_fingerprint(text: str) -> str:
    """Same quoted text in different replies (other line breaks, '>' marks) → same fingerprint."""
    return sha1(" ".join(words(text)))[:64]


def _containment(block: set[int], text: set[int]) -> float:
    return len(block & text) / len(block) if block else 0.0


def link_quotes(e: Email) -> None:
    """Link quoted blocks of `e` to stored emails; also link older unseen blocks that `e` turns out to be."""
    st = get_settings().store
    thr = st.quote_match_threshold
    cands = list(Email.objects.filter(thread=e.thread).exclude(id=e.id).exclude(status="duplicate")
                 .only("id", "new_text", "sent_at")) if e.thread_id else []
    if e.subject_norm:
        extra = Email.objects.filter(subject_norm=e.subject_norm, sent_at__gte=e.sent_at - timedelta(days=90)).exclude(id=e.id).exclude(thread=e.thread)[:40]
        cands += list(extra.only("id", "new_text", "sent_at"))
    sh = {c.id: shingles(c.new_text) for c in cands if c.new_text}
    for qb in QuotedBlock.objects.filter(email=e, linked__isnull=True):
        b = shingles(qb.text)
        best, score = None, 0.0
        for cid, s in sh.items():
            sc = _containment(b, s)
            if sc > score:
                best, score = cid, sc
        if best and score >= thr:
            qb.linked_id, qb.match = best, round(score, 3)
            qb.save(update_fields=["linked", "match"])
    # the other direction: blocks elsewhere in the thread that were "unseen history" may be this email
    mine = shingles(e.new_text)
    if mine and e.thread_id:
        for qb in QuotedBlock.objects.filter(email__thread=e.thread, linked__isnull=True).exclude(email=e):
            sc = _containment(shingles(qb.text), mine)
            if sc >= thr:
                qb.linked_id, qb.match = e.id, round(sc, 3)
                qb.save(update_fields=["linked", "match"])


def _richer(new_raw: bytes, old: Email) -> bool:
    return len(new_raw) > len(old.raw_bytes()) * 1.2 and old.status in ("new", "skipped")


def add(raw: bytes, source: str, source_ref: str = "", conversation_id: str = "", dept: str = "") -> AddResult:
    """dept: the department mailbox this copy came from (POP3 account). Counted for the email even when the address is
    not in To/Cc (Bcc, a list expanded by the mail server)."""
    try:
        p = parse(raw)
    except Exception as ex:
        log.exception("could not parse message from %s %s", source, source_ref)
        return AddResult("error", detail=f"parse: {ex}")
    s = get_settings()
    with transaction.atomic():
        old = Email.objects.filter(message_id=p.message_id).first()
        if old:
            changed = False
            if source not in old.sources:
                old.sources = old.sources + [source]
                changed = True
            if conversation_id and not old.conversation_id:
                old.conversation_id = conversation_id
                changed = True
            if dept and dept.lower() not in [d.lower() for d in old.depts]:   # the same mail in a second department mailbox
                old.depts = sorted(set(old.depts) | {dept.lower()})
                changed = True
            if _richer(raw, old):
                old.raw = zlib.compress(raw, 6)
                changed = True
                log.info("kept the richer copy of %s from %s", old.message_id, source)
            if changed:
                old.save()
            return AddResult("merged", old.id)
        sp = split_body(p.body_text)
        sn = subject_norm(p.subject)
        tzname = s.sources.time_zone
        depts = sorted({a for a in p.to + p.cc if a in [d.lower() for d in s.sources.dept_addresses]} | ({dept.lower()} if dept else set()))
        e = Email(
            message_id=p.message_id, sources=[source], source_ref=source_ref[:500], subject=p.subject[:1000],
            subject_norm=sn[:1000], subject_tpl=subject_template(p.subject)[:1000], from_addr=p.from_addr, from_name=p.from_name[:320],
            to=p.to, cc=p.cc, depts=depts, sent_at=p.sent_at, day=local_day(p.sent_at, tzname), in_reply_to=p.in_reply_to,
            references=p.references[-50:], thread_index=p.thread_index[:200], conversation_id=conversation_id[:200], headers=p.headers,
            raw=zlib.compress(raw, 6), body_text=p.body_text, new_text=sp.new_text, quoted_text=sp.quoted, signature=sp.signature,
            lang=detect_lang(sp.new_text or p.body_text), has_table=p.has_table or "\n|" in sp.new_text,
        )
        # near-duplicate under another Message-ID (two aliases, resent mail)
        win = timedelta(minutes=s.store.dup_window_min)
        if s.store.dup_window_min > 0:
            body_h = sha1(sp.new_text.strip())
            for c in Email.objects.filter(from_addr=e.from_addr, subject_norm=sn, sent_at__gte=e.sent_at - win, sent_at__lte=e.sent_at + win, dup_of__isnull=True)[:20]:
                if sha1(c.new_text.strip()) == body_h:
                    e.dup_of = c
                    e.status = "duplicate"
                    break
        e.save()
        # attachments → blobs + extracts
        n_att = 0
        for a in p.attachments:
            too_big = len(a.data) > s.store.max_attachment_mb * 1024 * 1024
            blob = None if too_big else Blob.put(a.data)
            text, err = ("", "") if (a.is_image or too_big) else extract(a.filename, a.content_type, a.data, s.store.att_extract_chars)
            Attachment.objects.create(email=e, filename=a.filename[:500], content_type=a.content_type[:200], size=len(a.data), blob=blob,
                                      is_image=a.is_image, inline=a.inline, content_id=a.content_id[:300], extract=text,
                                      extract_error=("too large, not stored" if too_big else err))
            if not a.inline:
                n_att += 1
        e.n_attachments = n_att
        e.save(update_fields=["n_attachments"])
        for i, b in enumerate(sp.blocks):
            QuotedBlock.objects.create(email=e, idx=i, header=b["header"][:2000], text=b["text"], fingerprint=block_fingerprint(b["text"]))
        threads.assign_thread(e, s.store.use_thread_index, s.store.subject_fallback_days)
        threads.link_parents(e)
        link_quotes(e)
        threads.join_shared_history(e)
        # identifiers + t-codes (script facts)
        full = e.new_text + "\n" + "\n".join(x.extract for x in e.attachments.all())
        Identifier.objects.bulk_create([Identifier(value=v[:100], type=t, email=e) for t, v in identifiers.find_identifiers(full, s.store.identifier_patterns)])
        _record_tcodes(e, full)
        if e.status != "duplicate":
            action, rule, audit = skiprules.check(e)
            if action == "skip":
                e.status, e.skip_rule, e.skip_action = "skipped", rule[:300], "skip"
            elif action == "note_only":
                e.skip_rule, e.skip_action = rule[:300], "note_only"
            e.audit = audit
            if rule and audit:
                e.skip_rule = rule[:300]
            e.save(update_fields=["status", "skip_rule", "skip_action", "audit"])
    return AddResult("duplicate" if e.status == "duplicate" else "stored", e.id)


def _record_tcodes(e: Email, text: str) -> None:
    s = get_settings()
    known = set(Tag.objects.filter(type="tcode", state__in=["active", "candidate"]).values_list("value", flat=True))
    found, cands = identifiers.find_tcodes(text, s.tags.tcode_stoplist, {k for k in known})
    for code in found:
        t, _ = Tag.objects.get_or_create(type="tcode", value=code, defaults={"state": "active", "first_seen": e.sent_at, "why": "seed list or promoted"})
        if not t.last_seen or t.last_seen < e.sent_at:
            t.last_seen = e.sent_at
            t.save(update_fields=["last_seen"])
    for code, ctx in cands.items():
        t, _ = Tag.objects.get_or_create(type="tcode", value=code, defaults={"state": "candidate", "first_seen": e.sent_at, "why": "found in t-code context"})
        ctxs = list(t.contexts or [])
        if len(ctxs) < 5:
            ctxs.append({"email": e.id, "thread": e.thread_id, "text": ctx[:200]})
        t.contexts = ctxs
        t.last_seen = e.sent_at
        n_threads = len({c.get("thread") for c in ctxs})
        t.threads = max(t.threads, n_threads)
        if t.state == "candidate" and n_threads >= s.tags.tcode_promote_threads:
            t.state = "active"
            t.why = f"promoted: seen in t-code context in {n_threads} threads"
            from llmwiki.logs import event
            event("tags", f"New t-code {code} promoted (seen in {n_threads} threads)", code=code)
        t.save()
