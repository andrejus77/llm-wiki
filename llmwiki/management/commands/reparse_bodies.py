"""Re-derive the text of stored emails from their raw .eml with the current HTML converter (whitespace clean-up:
Outlook paragraphs as lines, no runs of empty lines). Notes, threads and pages are not touched; the changed emails are
re-indexed. Safe to run any time: `manage.py reparse_bodies [--dry-run]`."""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Re-parse stored email bodies from the raw mail (whitespace clean-up); no model requests"

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true", help="only count what would change")

    def handle(self, *a, **o):
        from llmwiki.ingest.mime import parse
        from llmwiki.ingest.quotes import split_body
        from llmwiki.models import Email, QuotedBlock
        changed, blocks, failed = [], 0, 0
        for e in Email.objects.exclude(raw=None).iterator(chunk_size=200):
            try:
                p = parse(e.raw_bytes())
            except Exception:
                failed += 1
                continue
            sp = split_body(p.body_text)
            if (p.body_text, sp.new_text) == (e.body_text, e.new_text):
                continue
            changed.append(e.id)
            if o["dry_run"]:
                continue
            Email.objects.filter(id=e.id).update(body_text=p.body_text, new_text=sp.new_text, quoted_text=sp.quoted, signature=sp.signature)
            old = list(QuotedBlock.objects.filter(email=e).order_by("id"))
            if len(old) == len(sp.blocks):     # same quote structure: refresh the texts, keep the links
                for qb, nb in zip(old, sp.blocks):
                    t = nb.get("text")
                    if t and t != qb.text:
                        QuotedBlock.objects.filter(id=qb.id).update(text=t)
                        blocks += 1
        if changed and not o["dry_run"]:
            from llmwiki.rag.index import index_emails
            index_emails(changed)
        self.stdout.write(json.dumps({"changed": len(changed), "quoted_blocks_updated": blocks, "failed": failed, "dry_run": o["dry_run"]}, indent=1))
