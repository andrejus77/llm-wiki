"""Read and change the settings from the command line (the same settings as Agent › Settings; every change is a new
version in the settings history):

    manage.py config show [group]                     e.g. config show departments
    manage.py config set <group.key> <value>          value as JSON or plain text, e.g.
                                                      config set reports.stories 8
                                                      config set departments.team_members '["a@x.com","b@x.com"]'
    manage.py config apply <file.yaml> [--dry-run]    merge a YAML file (same shape as data/settings.yaml) into the settings
    manage.py config check                            validate the effective settings and list the machine keys

Why `apply`: data/settings.yaml is read at start; once settings are saved (portal or this command) the saved version wins
for everything except the machine keys (endpoints, file paths, POP3 mailboxes, server, test_data_only). So later changes
to departments, rules of thumb etc. go through the portal or `config set` / `config apply`.
"""
import json

import yaml
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Show / set / apply settings"

    def add_arguments(self, p):
        p.add_argument("op", choices=["show", "set", "apply", "check"])
        p.add_argument("values", nargs="*")     # not "args": Django reserves that name
        p.add_argument("--dry-run", action="store_true")

    def handle(self, *a, **o):
        from llmwiki.conf import MACHINE_KEYS, Settings, _deep_merge, get_settings, invalidate, pinned_keys, save_settings
        invalidate()
        s = get_settings()
        cur = json.loads(s.model_dump_json())
        op, args = o["op"], o["values"]
        if op == "show":
            d = cur[args[0]] if args else cur
            self.stdout.write(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))
            return
        if op == "check":
            Settings.model_validate(cur)
            self.stdout.write("settings are valid")
            self.stdout.write("machine keys (data/settings.yaml wins): " + ", ".join(MACHINE_KEYS))
            self.stdout.write("set in this machine's settings.yaml: " + (", ".join(pinned_keys()) or "none"))
            return
        if op == "set":
            if len(args) != 2:
                raise CommandError("usage: config set group.key value")
            path, raw = args
            try:
                val = json.loads(raw)
            except json.JSONDecodeError:
                val = raw
            new = json.loads(json.dumps(cur))
            node = new
            parts = path.split(".")
            for k in parts[:-1]:
                if k not in node or not isinstance(node[k], dict):
                    raise CommandError(f"unknown setting {path}")
                node = node[k]
            if parts[-1] not in node:
                raise CommandError(f"unknown setting {path}")
            node[parts[-1]] = val
        else:   # apply
            if not args:
                raise CommandError("usage: config apply file.yaml")
            with open(args[0], encoding="utf-8") as f:
                over = yaml.safe_load(f) or {}
            unknown = [g for g in over if g not in cur]
            if unknown:
                raise CommandError(f"unknown setting groups: {', '.join(unknown)}")
            new = _deep_merge(cur, over)
        try:
            ns = Settings.model_validate(new)
        except Exception as e:
            raise CommandError(f"invalid: {e}")
        changed = {g: [k for k in new[g] if new[g][k] != cur[g].get(k)] for g in new if new[g] != cur[g]}
        self.stdout.write("changes: " + (json.dumps({g: v for g, v in changed.items() if v}) if changed else "none"))
        machine = [m for m in MACHINE_KEYS if m.split(".")[0] in changed and (len(m.split(".")) == 1 or m.split(".")[1] in changed[m.split(".")[0]])]
        if machine:
            self.stdout.write(f"note: {', '.join(machine)} are machine keys: if this machine's data/settings.yaml sets them, the file still wins")
        if o["dry_run"] or not changed:
            return
        save_settings(ns, user="cli", note=f"config {op} {' '.join(args)[:200]}")
        self.stdout.write("saved (a new version in Agent › Settings › history)")
