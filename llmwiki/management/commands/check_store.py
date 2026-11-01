import json

from django.core.management.base import BaseCommand

from llmwiki.ingest.health import check_store


class Command(BaseCommand):
    help = "Check the email store invariants (one row per message, one thread per email, notes, attachments)"

    def handle(self, *a, **o):
        r = check_store()
        self.stdout.write(json.dumps(r, indent=1, default=str))
        if not r["ok"]:
            raise SystemExit(1)
