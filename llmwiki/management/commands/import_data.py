"""Install a data package made by export_data: `manage.py import_data llmwiki-data.zip [--force] [--no-reindex]`.
Stop the server first. The previous database is kept in data/backups; this machine's settings.yaml keeps its endpoints."""
import json

from django.core.management.base import BaseCommand

from llmwiki.transfer import import_package


class Command(BaseCommand):
    help = "Import a data package (from export_data or a package built with --with-data)"

    def add_arguments(self, p):
        p.add_argument("src", help="the data zip")
        p.add_argument("--force", action="store_true", help="replace a store that already has emails")
        p.add_argument("--no-reindex", action="store_true", help="skip embedding/rebuilding the vector index")

    def handle(self, *a, **o):
        out = import_package(o["src"], force=o["force"], reindex=not o["no_reindex"])
        self.stdout.write(json.dumps(out, indent=1, default=str))
