"""Rewrite wiki pages to the free structure (summary + the editor's own sections) and their length budget
(settings › Wiki pages). One editor request per page, through the same guards as every edit. Use it after changing
the page settings or on a store written with the old fixed sections:

    manage.py rewrite_pages              pages over budget or still in the old structure
    manage.py rewrite_pages --all        every page
    manage.py rewrite_pages --page 3 7   these pages
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Rewrite pages to the page length budget and the free structure (one model request per page)"

    def add_arguments(self, p):
        p.add_argument("--all", action="store_true", help="every page, not only those over budget")
        p.add_argument("--page", type=int, nargs="*", help="page ids")

    def handle(self, *a, **o):
        from llmwiki.pipeline.runner import rewrite_pages
        self.stdout.write(json.dumps(rewrite_pages(o.get("page") or None, everything=o["all"]), indent=1))
