"""Skip rules: static (headers, sender glob, subject regex) and learned (sender + subject template).

Actions: skip = stored, greyed in the portal, never sent to the model; note_only = gets a note but is never routed.
Learned rules (P2): created from S1 verdicts; every Nth match is still processed as an audit; an important audit
result disables the rule automatically.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from datetime import timedelta

from django.db.models import Count

from llmwiki.conf import get_settings
from llmwiki.logs import event
from llmwiki.models import Email, SkipRule
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
        if not (r.sender or r.subject):
            return False
        ok = True
        if r.sender:
            ok = fnmatch.fnmatch(e.from_addr, r.sender.lower())
        if r.subject:   # matched on the subject as sent and without RE:/FW:/AW:/회신: prefixes
            try:
                ok = ok and bool(re.search(r.subject, e.subject, re.I) or re.search(r.subject, e.subject_norm or "", re.I))
            except re.error:
                return False
        return ok
    return bool(r.template) and e.subject_tpl == r.template and (not r.sender or e.from_addr == r.sender)


def check(e: Email) -> tuple[str, str, bool]:
    """Return (action, rule description, audit) for an email; action '' = process normally.

    Order: Auto-Submitted header → bulk headers → automatic replies (subjects, bounces, no-reply senders) → rules.
    Rules (static and learned) target automated mail. A reply or forward written by someone else (feedback on an
    automated mail) is NOT skipped by a rule unless the rule has include_replies (or skip.rules_skip_replies is on);
    a 'reply' from the rule's own sender (e.g. a system's acknowledgement) still matches, and automatic replies are
    caught before the rules anyway."""
    s = get_settings().skip
    h = {k.lower(): str(v).lower() for k, v in (e.headers or {}).items()}
    if h.get("auto-submitted", "no") not in ("", "no", "auto-forwarded") and s.auto_submitted != "process":
        return s.auto_submitted, f"header Auto-Submitted: {h['auto-submitted']}", False
    if (h.get("precedence") in ("bulk", "list", "junk") or "list-id" in h) and s.bulk != "process":
        return s.bulk, "header Precedence/List-Id (bulk mail)", False
    why = auto_reply_reason(e, h)
    if why and s.auto_reply != "process":
        return s.auto_reply, f"automatic: {why}", False
    reply = is_reply_or_forward(e)
    for r in SkipRule.objects.filter(enabled=True).exclude(kind="candidate").order_by("id"):
        ok = _rule_matches(r, e)
        if ok and reply and not (r.include_replies or s.rules_skip_replies):
            own_sender = bool(r.sender) and (fnmatch.fnmatch(e.from_addr, r.sender.lower()) if r.kind == "static" else e.from_addr == r.sender)
            if not own_sender:
                log.debug("rule %s matches email %s but it is a reply/forward by %s: processed", r.id, e.id, e.from_addr)
                continue
        if ok:
            SkipRule.objects.filter(id=r.id).update(hits=r.hits + 1)
            desc = f"{r.kind}: {r.sender or ''} {r.subject or r.template or ''}".strip()
            if r.kind == "learned" and s.learned_enabled and (r.hits + 1) % s.audit_every == 0:
                return "", desc + " (audit sample)", True
            if r.kind == "learned" and not s.learned_enabled:
                continue
            return r.action, desc, False
    return "", "", False


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
