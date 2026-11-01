"""Fetch mail: `manage.py fetch` (enabled sources) · `--eml <folder>` · `--pst [--since 2026-09-01 --until 2026-10-01]` · `--pop3`"""
import json
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from llmwiki.sources.fetch import fetch


class Command(BaseCommand):
    help = "Fetch mail from POP3, an .eml folder or the Outlook .pst into the store"

    def add_arguments(self, p):
        p.add_argument("--eml", help="folder of .eml files")
        p.add_argument("--pst", action="store_true", help="import from the .pst in Settings › Sources")
        p.add_argument("--pop3", action="store_true")
        p.add_argument("--since", help="pst: YYYY-MM-DD")
        p.add_argument("--until", help="pst: YYYY-MM-DD")
        p.add_argument("--limit", type=int)

    def handle(self, *a, **o):
        sources = []
        if o["pop3"]:
            sources.append("pop3")
        if o["eml"]:
            sources.append("eml")
        if o["pst"]:
            sources.append("pst")
        d = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc) if s else None  # noqa: E731
        stats = fetch(sources or None, eml_folder=o["eml"], pst_since=d(o["since"]), pst_until=d(o["until"]), limit=o["limit"])
        self.stdout.write(json.dumps({k: v for k, v in stats.items() if k != "error_refs"}))
