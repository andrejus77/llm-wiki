"""Is an email sent by a system rather than a person? (script, no model)

Automated = not a reply/forward written by someone, and one of:
- the sender matches pipeline.automated_senders (workflow, batch, noreply, alerts, track & trace …) or skip.noreply_senders;
- the same sender sent the same subject template ≥ pipeline.automated_template_min times in the last 30 days AND the
  bodies are near-identical apart from numbers (e.g. "PO # waiting for your approval", "Shipment # delivered") — a
  person's weekly report has the same subject every week but a different text, so it is not caught.

Used after the notes: an automated email the model rated 'routine' is capped (pipeline.automated_cap); automated mail
alone never creates a wiki page; and automated mail does not count towards a page's level (pipeline.level_min) or a
team member's involvement in a report.
"""
from __future__ import annotations

import fnmatch
from datetime import timedelta

from llmwiki.conf import get_settings


def sender_is_system(addr: str) -> bool:
    s = get_settings()
    a = (addr or "").lower()
    return any(fnmatch.fnmatch(a, g.lower()) for g in list(s.pipeline.automated_senders) + list(s.skip.noreply_senders))


def is_automated(e) -> bool:
    from llmwiki.ingest.skiprules import is_reply_or_forward
    from llmwiki.models import Email
    if sender_is_system(e.from_addr):
        return True
    if is_reply_or_forward(e) or not e.subject_tpl:
        return False
    s = get_settings().pipeline
    same = list(Email.objects.filter(from_addr=e.from_addr, subject_tpl=e.subject_tpl, sent_at__gte=e.sent_at - timedelta(days=30),
                                     sent_at__lte=e.sent_at).exclude(status="duplicate").exclude(id=e.id).order_by("-sent_at")
                .values_list("new_text", flat=True)[:50])
    if len(same) + 1 < s.automated_template_min:
        return False
    mine = _shape(e.new_text)
    if not mine:
        return True
    sims = [len(mine & _shape(t)) / max(1, len(mine | _shape(t))) for t in same[:3]]
    return bool(sims) and min(sims) >= 0.6


def _shape(text: str) -> set[str]:
    """Word 3-grams with numbers and codes blanked: two notifications of one template look the same."""
    import re
    w = re.sub(r"\S*\d\S*", "#", (text or "").lower()).split()
    return {" ".join(w[i:i + 3]) for i in range(max(0, len(w) - 2))}


def mark(emails) -> list[int]:
    """Set Email.automated for these emails; returns the ids that are automated."""
    from llmwiki.models import Email
    auto = [e.id for e in emails if is_automated(e)]
    for e in emails:
        e.automated = e.id in auto
    Email.objects.filter(id__in=[e.id for e in emails]).exclude(id__in=auto).update(automated=False)
    Email.objects.filter(id__in=auto).update(automated=True)
    return auto
