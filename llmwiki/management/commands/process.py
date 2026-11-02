"""Process waiting mail: `manage.py process [--mode batch|backfill|live] [--since D] [--until D] [--windows N] [--review]`"""
import json

from django.core.management.base import BaseCommand

from llmwiki.pipeline.runner import process


class Command(BaseCommand):
    help = "Run the pipeline (notes, routing, editor, index, tags) on mail that is not processed yet"

    def add_arguments(self, p):
        p.add_argument("--mode", default="batch", choices=["batch", "backfill", "live"])
        p.add_argument("--since")
        p.add_argument("--until")
        p.add_argument("--windows", type=int, help="stop after N windows (days/weeks)")
        p.add_argument("--review", action="store_true", help="run the reviewer on changed pages afterwards")

    def handle(self, *a, **o):
        def prog(i, n, w, ws):
            self.stdout.write(f"window {i}/{n} {w}: {ws}")
        from llmwiki.llm.pool import pool
        if pool().resume("manage.py process"):   # started by hand: try again
            self.stdout.write("Stopped endpoints were resumed for this run.")
        stats = process(o["mode"], until_day=o["until"], since_day=o["since"], max_windows=o["windows"], review=o["review"] or None, progress=prog)
        self.stdout.write(json.dumps(stats))
