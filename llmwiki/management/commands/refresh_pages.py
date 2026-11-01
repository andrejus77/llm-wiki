"""Re-run the script checks on every page: automated-mail flags, page level by size (pipeline.level_min) and the owning
department. Runs after every pipeline window anyway; use it after changing those settings or on an older store:
`manage.py refresh_pages`. No model requests."""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Recompute automated flags, page levels and page departments (script only)"

    def handle(self, *a, **o):
        from llmwiki.departments import refresh_pages
        from llmwiki.ingest import automated
        from llmwiki.models import Email
        es = list(Email.objects.exclude(status="duplicate").order_by("sent_at"))
        n_auto = len(automated.mark(es))
        out = refresh_pages()
        out["automated_emails"] = n_auto
        self.stdout.write(json.dumps(out, indent=1))
