"""Skip rules as a YAML file, for moving them between machines or bringing them over from another tool:

    manage.py rules export rules.yaml
    manage.py rules import rules.yaml [--replace] [--dry-run]

One entry per rule (sender = glob on the From address, subject = regex, case-insensitive; at least one of them):

    - sender: "workflow@sap.your-company.com"
      subject: "^PO \\d+ waiting for (your )?approval"
      action: skip              # skip | note_only (a note is written, nothing goes to the wiki)
      include_replies: false    # true = also skip people's replies/forwards to it
      why: "SAP approval notifications"

Import adds rules that don't exist yet (same sender + subject = same rule, updated); --replace first disables every
static rule not in the file. Learned rules (made by the app) are exported with kind: learned and imported as static.
"""
import json
import re

import yaml
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Export / import skip rules as YAML"

    def add_arguments(self, p):
        p.add_argument("op", choices=["export", "import"])
        p.add_argument("file")
        p.add_argument("--replace", action="store_true", help="import: disable static rules that are not in the file")
        p.add_argument("--dry-run", action="store_true")

    def handle(self, *a, **o):
        from llmwiki.logs import event
        from llmwiki.models import SkipRule
        if o["op"] == "export":
            rows = [{"sender": r.sender, "subject": r.subject, "action": r.action, "include_replies": r.include_replies, "enabled": r.enabled,
                     "why": r.why, "kind": r.kind, "hits": r.hits} for r in SkipRule.objects.exclude(kind="candidate").order_by("id")]
            with open(o["file"], "w", encoding="utf-8") as f:
                yaml.safe_dump(rows, f, allow_unicode=True, sort_keys=False)
            self.stdout.write(f"{len(rows)} rules written to {o['file']}")
            return
        with open(o["file"], encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if not isinstance(data, list):
            raise CommandError("the file must be a YAML list of rules")
        errors, ok = [], []
        for i, d in enumerate(data, 1):
            if not isinstance(d, dict):
                errors.append(f"#{i}: not a mapping")
                continue
            sender, subject = str(d.get("sender") or "").strip(), str(d.get("subject") or "").strip()
            if not sender and not subject:
                errors.append(f"#{i}: needs a sender glob or a subject regex")
                continue
            if subject:
                try:
                    re.compile(subject, re.I)
                except re.error as e:
                    errors.append(f"#{i}: subject regex {subject!r} is invalid: {e}")
                    continue
            action = str(d.get("action") or "skip")
            if action not in ("skip", "note_only"):
                errors.append(f"#{i}: action must be skip or note_only")
                continue
            ok.append({"sender": sender[:300], "subject": subject[:500], "action": action, "include_replies": bool(d.get("include_replies", False)),
                       "enabled": bool(d.get("enabled", True)), "why": str(d.get("why") or "imported")[:500]})
        for e in errors:
            self.stderr.write("  " + e)
        if errors:
            raise CommandError(f"{len(errors)} invalid rule(s); nothing imported")
        if o["dry_run"]:
            self.stdout.write(json.dumps(ok, indent=1, ensure_ascii=False))
            self.stdout.write(f"dry run: {len(ok)} rules are valid")
            return
        made = updated = 0
        keep = set()
        for d in ok:
            r = SkipRule.objects.filter(sender=d["sender"], subject=d["subject"]).exclude(kind="candidate").first()
            if r:
                for k, v in d.items():
                    setattr(r, k, v)
                r.kind = "static" if r.kind != "learned" else r.kind
                r.save()
                updated += 1
            else:
                r = SkipRule.objects.create(kind="static", **d)
                made += 1
            keep.add(r.id)
        off = 0
        if o["replace"]:
            off = SkipRule.objects.filter(kind="static", enabled=True).exclude(id__in=keep).update(enabled=False)
        event("rules", f"Skip rules imported from {o['file']}: {made} new, {updated} updated, {off} disabled", new=made, updated=updated, disabled=off)
        self.stdout.write(f"{made} new, {updated} updated, {off} disabled")
