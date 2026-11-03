"""Re-derive stored emails from their raw .eml with the current code: the text (image references, whitespace), each
picture's place and whether it is a signature, the language, the sender's display name. Notes, threads and pages are not
touched; the emails whose text changed are re-indexed. No model requests. Safe to run any time; the portal also does it
once by itself after an update. `manage.py reparse_bodies [--dry-run]`."""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Re-parse stored email bodies from the raw mail (text, picture places, language, names); no model requests"

    def add_arguments(self, p):
        p.add_argument("--dry-run", action="store_true", help="only count what would change")

    def handle(self, *a, **o):
        from llmwiki.ingest.reparse import reparse_all
        self.stdout.write(json.dumps(reparse_all(dry_run=o["dry_run"]), indent=1))
