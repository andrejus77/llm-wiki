"""The tag catalog.

    manage.py tags list                       what the catalog holds
    manage.py tags sweep [--dry-run]          no model: drop tags without proof, add catalog terms the pages mention
    manage.py tags check [--limit N] [--all]  the wiki lint for tags: asks the model about pages under the minimum (--all: every page)
    manage.py tags reject TYPE VALUE          this term is wrong: off every page, and never proposed again
    manage.py tags alias TYPE VALUE ALIAS…    other spellings of a term (they count as its proof)
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "List, check and correct the tag catalog"

    def add_arguments(self, p):
        p.add_argument("op", choices=["list", "sweep", "check", "reject", "alias"])
        p.add_argument("args_", nargs="*", metavar="ARG")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--limit", type=int)
        p.add_argument("--all", action="store_true", help="check: every page, not only those under the minimum")

    def handle(self, *a, **o):
        from llmwiki.models import Tag
        from llmwiki.pipeline import tagger
        op, args = o["op"], o["args_"]
        if op == "list":
            tagger.seed_catalog()
            rows = [{"type": t.type, "value": t.value, "state": t.state, "aliases": t.aliases, "why": t.why} for t in Tag.objects.exclude(type="tcode").order_by("type", "value")]
            self.stdout.write(json.dumps(rows, ensure_ascii=False, indent=1))
        elif op == "sweep":
            self.stdout.write(json.dumps(tagger.sweep(dry_run=o["dry_run"]), ensure_ascii=False, indent=1))
        elif op == "check":
            from llmwiki.llm.pool import pool
            pool().resume("a manual start")
            self.stdout.write(json.dumps(tagger.check_tags(o["limit"], o["all"]), ensure_ascii=False, indent=1))
        elif op == "reject":
            if len(args) != 2:
                raise CommandError("usage: tags reject TYPE VALUE")
            Tag.objects.update_or_create(type=args[0], value=args[1], defaults={"state": "rejected", "why": "rejected by hand"})
            self.stdout.write(json.dumps(tagger.sweep(), ensure_ascii=False, indent=1))
        elif op == "alias":
            if len(args) < 3:
                raise CommandError("usage: tags alias TYPE VALUE ALIAS [ALIAS …]")
            t = Tag.objects.filter(type=args[0], value=args[1]).first()
            if not t:
                raise CommandError(f"no tag {args[0]}: {args[1]}")
            t.aliases = sorted(set(t.aliases or []) | set(args[2:]))
            t.save(update_fields=["aliases"])
            self.stdout.write(json.dumps(tagger.sweep(), ensure_ascii=False, indent=1))
