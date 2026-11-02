"""API keys and passwords, stored in the Windows Credential Manager (or data/secrets.yaml) — never in environment
variables, settings or the repository. Settings refer to a secret by NAME (api_key_secret, password_secret).

    llmwiki secret set api-a                 asks for the value (hidden) → Windows Credential Manager "llmwiki/api-a"
    llmwiki secret set api-a --file          → data/secrets.yaml instead (machines without a Credential Manager)
    llmwiki secret list                      the names the settings need, and where each is found (values never shown)
    llmwiki secret delete api-a

Run it as the Windows user that runs the portal (the server's service account on the server).
"""
import getpass
import sys

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Set / list / delete secrets (Windows Credential Manager or data/secrets.yaml)"

    def add_arguments(self, p):
        p.add_argument("op", choices=["set", "list", "delete", "check"])
        p.add_argument("name", nargs="?")
        p.add_argument("--file", action="store_true", help="store in data/secrets.yaml instead of the Credential Manager")
        p.add_argument("--stdin", action="store_true", help="read the value from standard input (scripts)")

    def handle(self, *a, **o):
        from llmwiki import keystore
        op, name = o["op"], o.get("name")
        if op in ("set", "delete") and not name:
            raise CommandError(f"usage: llmwiki secret {op} <name>")
        if op == "set":
            if o["stdin"]:
                value = sys.stdin.readline().rstrip("\r\n")
            else:
                value = getpass.getpass(f"Value for '{name}' (typing is hidden): ")
                again = getpass.getpass("Once more: ")
                if value != again:
                    raise CommandError("the two values differ; nothing stored")
            if not value:
                raise CommandError("empty value; nothing stored")
            where = keystore.put(name, value, to_file=o["file"])
            self.stdout.write(f"stored '{name}' in the {where}")
            return
        if op == "delete":
            gone = keystore.delete(name)
            self.stdout.write(f"deleted '{name}' from: {', '.join(gone)}" if gone else f"'{name}' was not stored")
            return
        needed = keystore.needed()
        stored = keystore.stored_names()
        missing = 0
        self.stdout.write("Secrets the settings use:")
        for nm, used in needed:
            w = keystore.where(nm)
            missing += 0 if w else 1
            self.stdout.write(f"  {'OK ' if w else 'MISSING'} {nm:<24} {used}{'  (' + w + ')' if w else '  → llmwiki secret set ' + nm}")
        if not needed:
            self.stdout.write("  (none — no endpoint has api_key_secret and no POP3 mailbox is enabled)")
        extra = [n for n in stored if n not in {x for x, _ in needed}]
        if extra:
            self.stdout.write("Also stored (not used by the current settings): " + ", ".join(f"{n} ({stored[n]})" for n in extra))
        self.stdout.write("Note: data/company_adapter.py may read more secrets with keystore.get('<name>').")
        if op == "check" and missing:
            raise CommandError(f"{missing} secret(s) missing")
