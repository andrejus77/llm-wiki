"""Dry run of the .pst import: how many messages per folder have a department address in To/Cc (and would be
imported), and how many are personal (left out). Reads only recipients, stores nothing.

    manage.py pst_scan --days 7
    manage.py pst_scan --since 2026-09-01 --until 2026-10-01
"""
import json
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from llmwiki.conf import get_settings


def _d(x: str | None):
    return datetime.fromisoformat(x).replace(tzinfo=timezone.utc) if x else None


class Command(BaseCommand):
    help = "Count department vs personal mail in the .pst / Outlook mailbox without importing anything"

    def add_arguments(self, p):
        p.add_argument("--days", type=int, default=0, help="the last N days (default: --since/--until or everything)")
        p.add_argument("--since")
        p.add_argument("--until")

    def handle(self, *a, **o):
        from llmwiki.sources import pst
        s = get_settings().sources
        since = datetime.now(timezone.utc) - timedelta(days=o["days"]) if o["days"] else _d(o["since"])
        res = pst.scan_pst_com(s.pst_path, since, _d(o["until"]), s.pst_folders or None)
        self.stdout.write(json.dumps(res, indent=1))
        self.stdout.write(f"{res['dept']} department messages would be imported; {res['not_dept']} personal messages are left out "
                          f"(department addresses: {', '.join(s.dept_addresses)}; aliases: {s.dept_aliases or 'none'})")
