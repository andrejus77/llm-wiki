"""Mail rules: static (headers, sender glob, subject regex, recipient glob) and learned (sender + subject template).

Actions: skip = stored, greyed in the portal, never sent to the model; note_only = gets a note but is never routed;
keep = processed normally. Any of them can also file the mail into a folder.
Learned rules (P2): created from S1 verdicts; every Nth match is still processed as an audit; an important audit
result disables the rule automatically.

New mail is checked in store.add(). A rule added later can be applied to the mail already stored (apply_rule, with
preview() first): waiting mail is skipped before any model sees it, processed mail is also taken off its wiki pages and
out of the search index, and the pages left behind are rewritten by the next run. Changing or removing a rule undoes
what it did (revert_rule). A person's own decision on an email (skip_manual, folder_manual) always wins over rules.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Count

from llmwiki.conf import get_settings
from llmwiki.logs import event
from llmwiki.models import Email, Item, Page, PageEmail, SkipRule
from llmwiki.util import has_reply_prefix, now_utc

log = logging.getLogger(__name__)


# Not here on purpose: X-MS-Exchange-Generated-Message-Source (also set on mail an inbox rule forwarded, which is
# real department mail) and Auto-Submitted: auto-forwarded (same reason; see check()).
_AUTO_HEADERS = ("x-autoreply", "x-autorespond")


def is_reply_or_forward(e: Email) -> bool:
    return bool(has_reply_prefix(e.subject) or e.in_reply_to or e.references)


def auto_reply_reason(e: Email, h: dict) -> str:
    """Why this looks like an automatic reply / bounce / robot ('' = written by a person). Header-free checks too, because
    .pst messages often have no transport headers."""
    s = get_settings().skip
    for k in _AUTO_HEADERS:
        if k in h:
            return f"header {k}"
    if "multipart/report" in h.get("content-type", ""):
        return "delivery report"
    subj = (e.subject or "").strip()
    for pat in s.auto_reply_subjects:
        try:
            if re.search(pat, subj, re.I):
                return f"automatic reply subject /{pat[:40]}/"
        except re.error:
            continue
    if any(fnmatch.fnmatch(e.from_addr or "", g.lower()) for g in s.noreply_senders):
        return f"no-reply sender {e.from_addr}"
    return ""


def _rule_matches(r: SkipRule, e: Email) -> bool:
    if r.kind == "static":
        if not (r.sender or r.subject or r.recipient):
            return False
        if r.sender and not fnmatch.fnmatch(e.from_addr, r.sender.lower()):
            return False
        if r.recipient and not any(fnmatch.fnmatch(a.lower(), r.recipient.lower()) for a in [*e.to, *e.cc]):
            return False
        if r.subject:   # matched on the subject as sent and without RE:/FW:/AW:/회신: prefixes
            try:
                return bool(re.search(r.subject, e.subject, re.I) or re.search(r.subject, e.subject_norm or "", re.I))
            except re.error:
                return False
        return True
    return bool(r.template) and e.subject_tpl == r.template and (not r.sender or e.from_addr == r.sender)


def _reply_exempt(r: SkipRule, e: Email) -> bool:
    """Rules target automated mail: a reply or forward written by someone else (feedback on an automated mail) is not
    skipped unless the rule says so. A 'reply' from the rule's own sender (a system's acknowledgement) still matches."""
    if r.include_replies or get_settings().skip.rules_skip_replies or not is_reply_or_forward(e):
        return False
    own_sender = bool(r.sender) and (fnmatch.fnmatch(e.from_addr, r.sender.lower()) if r.kind == "static" else e.from_addr == r.sender)
    return not own_sender


def describe(r: SkipRule) -> str:
    return f"{r.kind}: " + " ".join(x for x in (r.sender, r.subject or r.template, f"to {r.recipient}" if r.recipient else "") if x)


def check(e: Email) -> tuple[str, str, bool, int | None]:
    """Return (action, rule description, audit, rule id) for an email; action '' = process normally.

    Order: Auto-Submitted header → bulk headers → automatic replies (subjects, bounces, no-reply senders) → rules.
    Automatic replies are caught before the rules, so they stay skipped whatever a rule says."""
    s = get_settings().skip
    h = {k.lower(): str(v).lower() for k, v in (e.headers or {}).items()}
    if h.get("auto-submitted", "no") not in ("", "no", "auto-forwarded") and s.auto_submitted != "process":
        return s.auto_submitted, f"header Auto-Submitted: {h['auto-submitted']}", False, None
    if (h.get("precedence") in ("bulk", "list", "junk") or "list-id" in h) and s.bulk != "process":
        return s.bulk, "header Precedence/List-Id (bulk mail)", False, None
    why = auto_reply_reason(e, h)
    if why and s.auto_reply != "process":
        return s.auto_reply, f"automatic: {why}", False, None
    for r in SkipRule.objects.filter(enabled=True).exclude(kind="candidate").exclude(action="keep").order_by("id"):
        if not _rule_matches(r, e):
            continue
        if _reply_exempt(r, e):
            log.debug("rule %s matches email %s but it is a reply/forward by %s: processed", r.id, e.id, e.from_addr)
            continue
        SkipRule.objects.filter(id=r.id).update(hits=r.hits + 1)
        if r.kind == "learned" and s.learned_enabled and (r.hits + 1) % s.audit_every == 0:
            return "", describe(r) + " (audit sample)", True, None
        if r.kind == "learned" and not s.learned_enabled:
            continue
        return r.action, describe(r), False, r.id
    return "", "", False, None


def folder_for(e: Email) -> tuple[int | None, int | None]:
    """(folder id, rule id) from the first enabled rule that matches and files mail. Filing does not stop processing,
    so replies count too: a conversation stays together."""
    for r in SkipRule.objects.filter(enabled=True, folder__isnull=False).exclude(kind="candidate").order_by("id"):
        if _rule_matches(r, e):
            return r.folder_id, r.id
    return None, None


def validate(d: dict) -> list[str]:
    errors = []
    if not any(str(d.get(k) or "").strip() for k in ("sender", "subject", "recipient")):
        errors.append("A rule needs at least one condition: sender, subject or recipient.")
    if d.get("action", "skip") not in ("skip", "note_only", "keep"):
        errors.append("The action must be skip, note_only or keep.")
    if d.get("action") == "keep" and not d.get("folder"):
        errors.append("A rule that keeps processing the mail needs a folder, otherwise it does nothing.")
    if d.get("subject"):
        try:
            re.compile(d["subject"], re.I)
        except re.error as ex:
            errors.append(f"The subject is not a valid regular expression: {ex}")
    return errors


# ------------------------------------------------------------------ applying rules to the mail already stored
_FIELDS = ("id", "subject", "subject_norm", "subject_tpl", "from_addr", "from_name", "to", "cc", "in_reply_to", "references", "status",
           "day", "sent_at", "thread_id", "rule_ref", "skip_manual", "skip_action", "folder_id", "folder_rule", "folder_manual")
WAITING = ("new", "noted", "failed")


def _stored():
    return Email.objects.exclude(status="duplicate").only(*_FIELDS).order_by("-sent_at")


def preview(r: SkipRule, sample: int = 8) -> dict:
    """What the rule would do to the mail already stored. Reads only; `r` need not be saved."""
    out = {"matched": 0, "waiting": 0, "processed": 0, "already_skipped": 0, "replies_kept": 0, "manual": 0, "to_folder": 0, "sample": []}
    for e in _stored().iterator(chunk_size=500):
        if not _rule_matches(r, e):
            continue
        out["matched"] += 1
        if r.folder_id and not e.folder_manual and e.folder_id != r.folder_id:
            out["to_folder"] += 1
        exempt = r.action != "keep" and _reply_exempt(r, e)
        if len(out["sample"]) < sample:
            out["sample"].append({"id": e.id, "day": e.day, "from": e.from_name or e.from_addr, "subject": e.subject[:120], "status": e.status, "kept_as_reply": exempt})
        if r.action == "keep":
            continue
        if exempt:
            out["replies_kept"] += 1
        elif e.skip_manual:
            out["manual"] += 1
        elif e.status == "skipped":
            out["already_skipped"] += 1
        elif e.status in WAITING:
            out["waiting"] += 1
        else:
            out["processed"] += 1
    return out


def _batches(ids: list[int], n: int = 500):
    for k in range(0, len(ids), n):
        yield ids[k:k + n]


def unroute(ids: list[int]) -> dict:
    """Take emails off their pages and out of the search index. Pages with no email left are archived; the others are
    rewritten by the next run (Page.needs_rewrite), because their text still says what those emails said."""
    if not ids:
        return {"pages_changed": 0, "pages_archived": 0}
    gone = set(ids)
    for it in Item.objects.filter(status="pending"):
        if set(it.emails or []) & gone:
            left = [x for x in it.emails if x not in gone]
            if left:
                Item.objects.filter(id=it.id).update(emails=left)
            else:
                Item.objects.filter(id=it.id).delete()
    touched: set[int] = set()
    for part in _batches(ids):
        touched |= set(PageEmail.objects.filter(email_id__in=part).values_list("page_id", flat=True))
        PageEmail.objects.filter(email_id__in=part).delete()
    left = set(PageEmail.objects.filter(page_id__in=list(touched)).values_list("page_id", flat=True))
    empty = [p for p in touched if p not in left]
    Page.objects.filter(id__in=empty).update(archived=True)
    Page.objects.filter(id__in=list(left)).update(needs_rewrite=True)

    def unindex():   # after the commit: the indexer's lock must not wait inside this transaction
        try:
            from llmwiki.rag.index import remove_emails, remove_pages
            remove_emails(ids)
            remove_pages(empty)
        except Exception:
            log.exception("removing skipped emails from the search index failed; the next re-index fixes it")
    transaction.on_commit(unindex)
    return {"pages_changed": len(left), "pages_archived": len(empty)}


def skip_emails(ids: list[int], desc: str, rule_id: int | None, manual: bool = False) -> dict:
    """Mark emails as skipped; those a model has already seen are also unrouted."""
    seen = list(Email.objects.filter(id__in=ids, status__in=["noted", "processed"]).values_list("id", flat=True)) if len(ids) <= 500 else \
        [i for part in _batches(ids) for i in Email.objects.filter(id__in=part, status__in=["noted", "processed"]).values_list("id", flat=True)]
    for part in _batches(ids):
        Email.objects.filter(id__in=part).exclude(status="duplicate").update(status="skipped", skip_rule=desc[:300], skip_action="skip", rule_ref=rule_id, skip_manual=manual)
    return unroute(seen)


def restore_emails(ids: list[int], manual: bool = False) -> int:
    """Back to the queue: the next run processes them as new mail."""
    n = 0
    for part in _batches(ids):
        n += Email.objects.filter(id__in=part, status="skipped").update(status="new", skip_rule="", skip_action="", rule_ref=None, skip_manual=manual)
    return n


@transaction.atomic
def apply_rule(r: SkipRule) -> dict:
    """Apply one rule to the mail already stored. Safe to repeat."""
    out = {"skipped": 0, "note_only": 0, "filed": 0, "replies_kept": 0, "manual": 0, "pages_changed": 0, "pages_archived": 0}
    if not r.enabled or r.kind != "static":
        return out
    skip, note, filed = [], [], []
    for e in _stored().iterator(chunk_size=500):
        if not _rule_matches(r, e):
            continue
        if r.folder_id and not e.folder_manual and (e.folder_id is None or e.folder_rule == r.id) and e.folder_id != r.folder_id:
            filed.append(e.id)
        if r.action == "keep":
            continue
        if _reply_exempt(r, e):
            out["replies_kept"] += 1
        elif e.skip_manual:
            out["manual"] += 1
        elif r.action == "skip" and e.status != "skipped":
            skip.append(e.id)
        elif r.action == "note_only" and e.status in WAITING and e.skip_action != "note_only":
            note.append(e.id)
    desc = describe(r)
    if skip:
        out.update(skip_emails(skip, desc, r.id))
    for part in _batches(note):
        Email.objects.filter(id__in=part).update(skip_rule=desc[:300], skip_action="note_only", rule_ref=r.id)
    for part in _batches(filed):
        Email.objects.filter(id__in=part).update(folder_id=r.folder_id, folder_rule=r.id)
    out.update(skipped=len(skip), note_only=len(note), filed=len(filed))
    SkipRule.objects.filter(id=r.id).update(hits=Email.objects.filter(rule_ref=r.id).count())
    if skip or note or filed:
        event("rules", f"Rule applied to stored mail ({desc}): {len(skip)} skipped, {len(note)} note only, {len(filed)} filed, "
              f"{out['pages_changed']} pages to rewrite, {out['pages_archived']} archived", rule=r.id, **{k: v for k, v in out.items() if isinstance(v, int)})
    return out


@transaction.atomic
def revert_rule(rule_id: int) -> dict:
    """Undo what a rule did. Emails it skipped go back to the queue unless another enabled rule still skips them."""
    others = list(SkipRule.objects.filter(enabled=True, kind="static").exclude(id=rule_id).exclude(action="keep").order_by("id"))
    restored, reassigned = [], 0
    for e in Email.objects.filter(rule_ref=rule_id, skip_manual=False).only(*_FIELDS):
        other = next((r for r in others if _rule_matches(r, e) and not _reply_exempt(r, e)), None)
        if other:
            Email.objects.filter(id=e.id).update(rule_ref=other.id, skip_rule=describe(other)[:300], skip_action=other.action,
                                                 status="skipped" if other.action == "skip" else ("new" if e.status == "skipped" else e.status))
            reassigned += 1
        elif e.status == "skipped":
            restored.append(e.id)
        else:   # note only: it just stops being limited
            Email.objects.filter(id=e.id).update(skip_rule="", skip_action="", rule_ref=None)
    restore_emails(restored)
    filers = list(SkipRule.objects.filter(enabled=True, folder__isnull=False, kind="static").exclude(id=rule_id).order_by("id"))
    unfiled = 0
    for e in Email.objects.filter(folder_rule=rule_id, folder_manual=False).only(*_FIELDS):
        other = next((r for r in filers if _rule_matches(r, e)), None)
        Email.objects.filter(id=e.id).update(folder_id=other.folder_id if other else None, folder_rule=other.id if other else None)
        unfiled += 1
    if restored or unfiled:
        event("rules", f"Rule {rule_id} undone: {len(restored)} emails back in the queue, {unfiled} taken out of its folder", rule=rule_id)
    return {"restored": len(restored), "reassigned": reassigned, "unfiled": unfiled}


def apply_all() -> dict:
    """Apply every enabled static rule to the stored mail, oldest rule first (e.g. after importing rules)."""
    total = {"rules": 0, "skipped": 0, "note_only": 0, "filed": 0, "pages_changed": 0, "pages_archived": 0}
    for r in SkipRule.objects.filter(enabled=True, kind="static").order_by("id"):
        one = apply_rule(r)
        total["rules"] += 1
        for k in ("skipped", "note_only", "filed", "pages_changed", "pages_archived"):
            total[k] += one[k]
    return total


def learn() -> int:
    """Create learned rules from S1 verdicts. Returns the number of new rules."""
    s = get_settings().skip
    if not s.learned_enabled:
        return 0
    since = now_utc() - timedelta(days=s.learn_clean_days)
    groups = (Email.objects.filter(importance="not_important").exclude(subject_tpl="")
              .values("from_addr", "subject_tpl").annotate(n=Count("id"), days=Count("day", distinct=True))
              .filter(n__gte=s.learn_min, days__gte=s.learn_days))
    made = 0
    for g in groups:
        if SkipRule.objects.filter(kind="learned", sender=g["from_addr"], template=g["subject_tpl"]).exists():
            continue
        if Email.objects.filter(from_addr=g["from_addr"], subject_tpl=g["subject_tpl"], importance__in=["important", "routine"], sent_at__gte=since).exists():
            continue
        SkipRule.objects.create(kind="learned", sender=g["from_addr"], template=g["subject_tpl"], action="skip",
                                why=f"{g['n']} not_important verdicts on {g['days']} days, none important in {s.learn_clean_days} days")
        event("rules", f"Learned skip rule: {g['from_addr']} · \"{g['subject_tpl']}\"", n=g["n"], days=g["days"])
        made += 1
    return made


def audit_result(e: Email) -> None:
    """After S1 on an audit sample: an important verdict disables the learned rule."""
    if not e.audit or e.importance != "important":
        return
    for r in SkipRule.objects.filter(kind="learned", enabled=True, template=e.subject_tpl):
        r.enabled = False
        r.why += f" · disabled: audit of email {e.id} on {e.day} came back important"
        r.save(update_fields=["enabled", "why"])
        event("rules", f"Disabled learned rule \"{r.template}\": an audit sample was important", level="warning", email=e.id)
