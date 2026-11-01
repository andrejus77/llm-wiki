"""Thread assembly: union-find over ThreadKey rows.

Keys per email: 'mid:<own id>', 'mid:<in-reply-to>', 'mid:<each reference>', 'conv:<Thread-Index GUID>', 'cid:<ConversationID>'.
- All keys that already point to threads → those threads are merged (oldest wins); the email joins it.
- No known key → fallback: a reply-prefixed subject with the same normalised subject and a shared participant
  within N days. Fallback joins are logged, and refused when the email's own conversation GUID already belongs elsewhere.
- Otherwise a new thread. Keys of referenced-but-unseen messages are registered too, so a parent that arrives
  later (e.g. from the .pst backfill) joins the same thread.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction

from llmwiki.logs import event
from llmwiki.models import Email, Thread, ThreadKey
from llmwiki.util import has_reply_prefix

log = logging.getLogger(__name__)


def keys_for(e: Email, use_thread_index: bool) -> list[str]:
    keys = [f"mid:{e.message_id}"]
    if e.in_reply_to:
        keys.append(f"mid:{e.in_reply_to}")
    keys += [f"mid:{r}" for r in e.references if r]
    if use_thread_index:
        from llmwiki.ingest.mime import thread_index_guid
        g = thread_index_guid(e.thread_index) if e.thread_index else ""
        if g and g != "0" * 32:
            keys.append(f"conv:{g}")
        if e.conversation_id:
            keys.append(f"cid:{e.conversation_id}")
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k[:500])
    return out


def _merge(into: Thread, other: Thread) -> None:
    if into.id == other.id:
        return
    n = Email.objects.filter(thread=other).update(thread=into)
    ThreadKey.objects.filter(thread=other).update(thread=into)
    other.merged_into = into
    other.n_emails = 0
    other.save(update_fields=["merged_into", "n_emails"])
    log.info("merged thread %s into %s (%s emails)", other.id, into.id, n)


def _fallback(e: Email, days: int) -> Thread | None:
    if days <= 0 or not has_reply_prefix(e.subject) or len(e.subject_norm) < 6:
        return None
    people = {e.from_addr, *e.to, *e.cc}
    cands = (Email.objects.filter(subject_norm=e.subject_norm, sent_at__gte=e.sent_at - timedelta(days=days),
                                  sent_at__lte=e.sent_at + timedelta(days=days), thread__isnull=False)
             .exclude(id=e.id).order_by("-sent_at")[:50])
    for c in cands:
        if people & {c.from_addr, *c.to, *c.cc}:
            return c.thread
    return None


@transaction.atomic
def assign_thread(e: Email, use_thread_index: bool, fallback_days: int) -> Thread:
    keys = keys_for(e, use_thread_index)
    rows = list(ThreadKey.objects.filter(key__in=keys).select_related("thread"))
    threads = {}
    for r in rows:
        t = r.thread
        while t.merged_into_id:
            t = t.merged_into
        threads[t.id] = t
    how = "headers"
    if threads:
        tl = sorted(threads.values(), key=lambda t: t.id)
        thread = tl[0]
        for other in tl[1:]:
            _merge(thread, other)
            event("thread", f"Merged thread T{other.id} into T{thread.id}: a later email linked them", thread=thread.id, merged=other.id, email=e.id)
    else:
        thread = _fallback(e, fallback_days)
        if thread is not None:
            own_conv = [k for k in keys if k.startswith("conv:")]
            clash = own_conv and ThreadKey.objects.filter(key__in=own_conv).exclude(thread=thread).exists()
            if clash:
                thread = None
            else:
                how = "subject fallback"
                event("thread", f"Joined '{e.subject[:80]}' to T{thread.id} by subject + shared participant (no matching headers)",
                      level="info", email=e.id, thread=thread.id)
        if thread is None:
            thread = Thread.objects.create(subject=e.subject[:500], first_at=e.sent_at, last_at=e.sent_at)
            how = "new"
    existing = set(ThreadKey.objects.filter(key__in=keys).values_list("key", flat=True))
    ThreadKey.objects.bulk_create([ThreadKey(key=k, thread=thread) for k in keys if k not in existing], ignore_conflicts=True)
    ThreadKey.objects.filter(key__in=keys).exclude(thread=thread).update(thread=thread)
    e.thread = thread
    e.save(update_fields=["thread"])
    refresh_thread(thread)
    log.debug("email %s → thread %s (%s)", e.id, thread.id, how)
    return thread


def refresh_thread(t: Thread) -> None:
    qs = Email.objects.filter(thread=t).exclude(status="duplicate")
    agg = list(qs.order_by("sent_at").values_list("sent_at", "from_addr", "subject"))
    if not agg:
        return
    t.first_at, t.last_at, t.n_emails = agg[0][0], agg[-1][0], len(agg)
    t.participants = sorted({a for _, a, _ in agg})
    if not t.subject:
        t.subject = agg[0][2][:500]
    t.save(update_fields=["first_at", "last_at", "n_emails", "participants", "subject"])


def _root(t: Thread) -> Thread:
    while t.merged_into_id:
        t = t.merged_into
    return t


def join_shared_history(e: Email) -> list[int]:
    """Separate replies to one email can end up in different threads when headers are lost (other mail systems,
    forwards, .pst copies without transport headers). Their quoted history shows they belong together:
    1. a quoted block of `e` was linked (link_quotes) to a stored email in another thread, or
    2. another thread's email quotes the same earlier message (same block fingerprint; same normalised subject).
    Blocks shorter than store.shared_history_min_words never join (greetings, disclaimers). Returns merged thread ids."""
    from llmwiki.conf import get_settings
    from llmwiki.models import QuotedBlock
    from llmwiki.util import words
    st = get_settings().store
    if not st.join_shared_history or not e.thread_id or e.status == "duplicate":
        return []
    win = timedelta(days=st.shared_history_days)
    mine = [qb for qb in QuotedBlock.objects.filter(email=e).select_related("linked") if len(words(qb.text)) >= st.shared_history_min_words]
    targets: dict[int, str] = {}
    for qb in mine:
        lk = qb.linked
        if lk and lk.thread_id and lk.thread_id != e.thread_id and lk.status != "duplicate" and abs(lk.sent_at - e.sent_at) <= win:
            targets[lk.thread_id] = f"quotes email {lk.id}"
    fps = [qb.fingerprint for qb in mine if qb.fingerprint]
    if fps:
        for o in (QuotedBlock.objects.filter(fingerprint__in=fps).exclude(email__thread_id=e.thread_id).exclude(email__status="duplicate")
                  .select_related("email")[:50]):
            oe = o.email
            if oe.thread_id and oe.subject_norm == e.subject_norm and abs(oe.sent_at - e.sent_at) <= win:
                targets.setdefault(oe.thread_id, f"same quoted message as email {oe.id}")
    # record what e answers, so the portal can draw separate replies as parallel branches (not as replies to each other)
    first = next((qb for qb in QuotedBlock.objects.filter(email=e).select_related("linked").order_by("idx")), None)
    if first is not None and not e.parent_id:
        if first.linked_id and first.linked_id != e.id:
            Email.objects.filter(id=e.id).update(parent_id=first.linked_id)
        elif first.fingerprint and len(words(first.text)) >= st.shared_history_min_words:
            Email.objects.filter(id=e.id).update(parallel_key=first.fingerprint)
            Email.objects.filter(quoted_blocks__fingerprint=first.fingerprint, quoted_blocks__idx=0, parent__isnull=True,
                                 parallel_key="").update(parallel_key=first.fingerprint)
    merged = []
    with transaction.atomic():
        roots = {_root(Thread.objects.get(id=e.thread_id))}
        for tid, why in targets.items():
            roots.add(_root(Thread.objects.get(id=tid)))
        if len(roots) < 2:
            return []
        keep, *others = sorted(roots, key=lambda t: t.id)
        for o in others:
            _merge(keep, o)
            merged.append(o.id)
        refresh_thread(keep)
        e.refresh_from_db(fields=["thread"])
    event("thread", f"Merged thread(s) {', '.join('T' + str(x) for x in merged)} into T{keep.id}: they share quoted history "
          f"({'; '.join(sorted(set(targets.values())))[:200]})", thread=keep.id, merged=merged, email=e.id)
    return merged


def link_parents(e: Email) -> None:
    """Set e.parent from In-Reply-To (or the last reference); also adopt stored replies that point at e."""
    target = e.in_reply_to or (e.references[-1] if e.references else "")
    if target and not e.parent_id:
        p = Email.objects.filter(message_id=target).first()
        if p:
            e.parent = p
            e.save(update_fields=["parent"])
    Email.objects.filter(in_reply_to=e.message_id, parent__isnull=True).update(parent=e)
