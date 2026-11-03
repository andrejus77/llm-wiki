"""Bring the mail already stored in line with the current parser. MAIL_FORMAT goes up when a code change means stored mail
should be re-derived: the portal's scheduler then runs this once by itself (KV 'mail_format')."""
from __future__ import annotations

import logging
import time
import zlib

from llmwiki.ingest import images
from llmwiki.ingest.mime import is_image_file, parse
from llmwiki.ingest.quotes import split_body
from llmwiki.ingest.store import block_fingerprint
from llmwiki.logs import event
from llmwiki.models import Attachment, Email, EmailOriginal, KV, QuotedBlock
from llmwiki.util import detect_lang, display_name

log = logging.getLogger(__name__)
MAIL_FORMAT = 2


def needed() -> bool:
    """Due, and not tried in the last 6 hours (a failure must not restart it on every scheduler tick)."""
    return KV.get("mail_format", 0) != MAIL_FORMAT and time.time() - KV.get("mail_format_tried", 0) > 6 * 3600 and EmailOriginal.objects.exists()


def reparse_all(dry_run: bool = False) -> dict:
    """Oldest first, so a picture's earlier uses are known when deciding whether it is a signature."""
    if not dry_run:
        KV.set("mail_format_tried", time.time())
    changed: list[int] = []
    out = {"emails": 0, "changed": 0, "quoted_blocks_updated": 0, "failed": 0, "pictures_in_text": 0, "pictures_in_history": 0, "signature_pictures": 0, "dry_run": dry_run}
    ids = list(Email.objects.order_by("sent_at").values_list("id", flat=True))
    for k in range(0, len(ids), 50):
        for orig in EmailOriginal.objects.filter(email_id__in=ids[k:k + 50]).exclude(raw=None).select_related("email"):
            e = orig.email
            out["emails"] += 1
            try:
                p = parse(zlib.decompress(bytes(orig.raw)))
            except Exception:
                out["failed"] += 1
                log.warning("could not re-parse email %s", e.id, exc_info=True)
                continue
            sp = split_body(p.body_text)
            changes = (p.body_text, sp.new_text, sp.signature) != (orig.body_text, e.new_text, e.signature)
            if changes:
                changed.append(e.id)
            if dry_run:
                continue
            try:
                old_name, old_lang = e.from_name, e.lang
                e.new_text, e.signature = sp.new_text, sp.signature
                e.lang = detect_lang(sp.new_text or p.body_text)
                e.from_name = display_name(e.from_name, e.from_addr)[:320]
                if changes:
                    EmailOriginal.objects.filter(email_id=e.id).update(body_text=p.body_text, quoted_text=sp.quoted)
                    old = list(QuotedBlock.objects.filter(email=e).order_by("id"))
                    if len(old) == len(sp.blocks):     # same quote structure: refresh the texts, keep the links
                        for qb, nb in zip(old, sp.blocks):
                            t = nb.get("text")
                            if t and t != qb.text:
                                out["quoted_blocks_updated"] += 1
                            QuotedBlock.objects.filter(id=qb.id).update(text=t or qb.text, fingerprint=block_fingerprint(t or qb.text))
                atts = list(e.attachments.all())
                for att in atts:
                    att.is_image = att.is_image or is_image_file(att.filename, att.content_type)
                n0 = e.n_attachments
                done = images.classify(e, sp.quoted, atts)
                out["pictures_in_text"] += done["body"]
                out["pictures_in_history"] += done["quoted"]
                out["signature_pictures"] += done["signature"]
                fields = {}
                if changes:
                    fields.update(new_text=e.new_text, signature=e.signature)
                if e.from_name != old_name:
                    fields["from_name"] = e.from_name
                if e.lang != old_lang:
                    fields["lang"] = e.lang
                if e.n_attachments != n0:
                    fields["n_attachments"] = e.n_attachments
                if fields:
                    Email.objects.filter(id=e.id).update(**fields)
            except Exception:
                out["failed"] += 1
                log.warning("could not update email %s", e.id, exc_info=True)
    out["changed"] = len(changed)
    if not dry_run:
        out["signature_pictures"] = Attachment.objects.filter(is_signature=True).count()
        if changed:
            from llmwiki.rag.index import index_emails
            index_emails(changed)
        KV.set("mail_format", MAIL_FORMAT)
        event("store", f"Stored mail re-parsed: {out['emails']} emails, {out['changed']} texts changed, {out['pictures_in_text']} pictures placed in the text, "
              f"{out['signature_pictures']} treated as signatures")
    return out
