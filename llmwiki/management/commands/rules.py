"""Mail rules as a YAML file, for moving them between machines or bringing them over from another tool:

    manage.py rules export rules.yaml
    manage.py rules import rules.yaml [--replace] [--dry-run] [--apply]
    manage.py rules apply [--dry-run]       run the enabled rules on the mail already stored

One entry per rule (sender = glob on the From address, subject = regex, case-insensitive, recipient = glob on To/Cc;
at least one of them; all that are set must match):

    - sender: "workflow@sap.your-company.com"
      subject: "^PO \\d+ waiting for (your )?approval"
      action: skip              # skip | note_only (a note is written, nothing goes to the wiki) | keep (processed as usual)
      folder: "SAP workflow"    # optional: file it there (the folder is created when missing); needed for action keep
      include_replies: false    # true = also skip people's replies/forwards to it
      why: "SAP approval notifications"

Import adds rules that don't exist yet (same sender + subject = same rule, updated); --replace first disables every
static rule not in the file. Rules only look at new mail until they are applied: --apply or `rules apply` also runs them
on the stored mail (waiting mail is skipped before any model sees it; processed mail is taken off its pages and those
pages are rewritten by the next run). Learned rules (made by the app) are exported with kind: learned and imported as static.
"""
import json
import re

import yaml
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Export / import mail rules as YAML, or run them on the stored mail"

    def add_arguments(self, p):
        p.add_argument("op", choices=["export", "import", "apply"])
        p.add_argument("file", nargs="?")
        p.add_argument("--replace", action="store_true", help="import: disable static rules that are not in the file")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--apply", action="store_true", help="import: also run the rules on the stored mail")

    def handle(self, *a, **o):
        from llmwiki.ingest import skiprules
        from llmwiki.logs import event
        from llmwiki.models import Folder, SkipRule
        if o["op"] == "apply":
            if o["dry_run"]:
                for r in SkipRule.objects.filter(enabled=True, kind="static").order_by("id"):
                    pv = skiprules.preview(r)
                    self.stdout.write(f"{skiprules.describe(r)}: {pv['matched']} match · {pv['waiting']} waiting, {pv['processed']} processed, "
                                      f"{pv['already_skipped']} already skipped, {pv['replies_kept']} replies kept, {pv['to_folder']} to its folder")
                return
            self.stdout.write(json.dumps(skiprules.apply_all()))
            return
        if not o["file"]:
            raise CommandError(f"usage: manage.py rules {o['op']} rules.yaml")
        if o["op"] == "export":
            rows = [{"sender": r.sender, "subject": r.subject, "recipient": r.recipient, "action": r.action, "folder": r.folder.name if r.folder_id else "",
                     "include_replies": r.include_replies, "enabled": r.enabled, "why": r.why, "kind": r.kind, "hits": r.hits}
                    for r in SkipRule.objects.exclude(kind="candidate").select_related("folder").order_by("id")]
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
            sender, subject, recipient = (str(d.get(k) or "").strip() for k in ("sender", "subject", "recipient"))
            if not (sender or subject or recipient):
                errors.append(f"#{i}: needs a sender glob, a subject regex or a recipient glob")
                continue
            if subject:
                try:
                    re.compile(subject, re.I)
                except re.error as e:
                    errors.append(f"#{i}: subject regex {subject!r} is invalid: {e}")
                    continue
            action = str(d.get("action") or "skip")
            if action not in ("skip", "note_only", "keep"):
                errors.append(f"#{i}: action must be skip, note_only or keep")
                continue
            folder = str(d.get("folder") or "").strip()[:120]
            if action == "keep" and not folder:
                errors.append(f"#{i}: action keep needs a folder, otherwise the rule does nothing")
                continue
            ok.append(({"sender": sender[:300], "subject": subject[:500], "recipient": recipient[:300], "action": action, "include_replies": bool(d.get("include_replies", False)),
                        "enabled": bool(d.get("enabled", True)), "why": str(d.get("why") or "imported")[:500]}, folder))
        for e in errors:
            self.stderr.write("  " + e)
        if errors:
            raise CommandError(f"{len(errors)} invalid rule(s); nothing imported")
        if o["dry_run"]:
            self.stdout.write(json.dumps([{**d, "folder": f} for d, f in ok], indent=1, ensure_ascii=False))
            self.stdout.write(f"dry run: {len(ok)} rules are valid")
            return
        made = updated = 0
        keep = set()
        for d, folder in ok:
            d["folder"] = Folder.objects.get_or_create(name=folder)[0] if folder else None
            r = SkipRule.objects.filter(sender=d["sender"], subject=d["subject"], recipient=d["recipient"]).exclude(kind="candidate").first()
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
        event("rules", f"Mail rules imported from {o['file']}: {made} new, {updated} updated, {off} disabled", new=made, updated=updated, disabled=off)
        self.stdout.write(f"{made} new, {updated} updated, {off} disabled")
        if o["apply"]:
            self.stdout.write(json.dumps(skiprules.apply_all()))
        else:
            self.stdout.write("New mail follows these rules. To also run them on the mail already stored: manage.py rules apply")
