"""Store health: the invariants of the email store, checked after every fetch and shown on Agent › Logs."""
from __future__ import annotations

from django.db.models import Count

from llmwiki.models import Attachment, Email, QuotedBlock, Thread


def check_store() -> dict:
    total = Email.objects.count()
    no_thread = Email.objects.filter(thread__isnull=True).count()
    merged_threads_with_mail = Thread.objects.filter(merged_into__isnull=False, emails__isnull=False).distinct().count()
    dup_ids = Email.objects.values("message_id").annotate(n=Count("id")).filter(n__gt=1).count()
    by_status = dict(Email.objects.values_list("status").annotate(n=Count("id")))
    processed_without_note = Email.objects.filter(status__in=["noted", "processed"], note="").count()
    stuck_new = Email.objects.filter(status="new").count()
    orphan_att = Attachment.objects.filter(email__isnull=True).count()
    att_missing_blob = Attachment.objects.filter(blob__isnull=True).exclude(extract_error__startswith="too large").count()
    unseen = QuotedBlock.objects.filter(linked__isnull=True).count()
    unseen_sent = QuotedBlock.objects.filter(linked__isnull=True, sent_to_model=True).count()
    checks = [
        ["every email is in exactly one thread", no_thread == 0 and merged_threads_with_mail == 0, f"{no_thread} without a thread"],
        ["one row per Message-ID", dup_ids == 0, f"{dup_ids} duplicated ids"],
        ["every processed email has a note", processed_without_note == 0, f"{processed_without_note} without a note"],
        ["no orphan attachments", orphan_att == 0 and att_missing_blob == 0, f"{orphan_att} orphans, {att_missing_blob} missing blobs"],
    ]
    return {
        "ok": all(c[1] for c in checks), "checks": checks, "total": total, "by_status": by_status, "waiting": stuck_new,
        "threads": Thread.objects.filter(merged_into__isnull=True).count(), "unseen_blocks": unseen, "unseen_sent": unseen_sent,
    }
