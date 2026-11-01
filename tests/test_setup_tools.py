"""Setup on a new machine: several POP3 department mailboxes, skip rules from a YAML file, settings from the CLI."""
import email.policy
import json
from email.message import EmailMessage

import pytest
from django.core.management import call_command

from llmwiki.conf import Pop3Account, get_settings, invalidate, use_settings
from llmwiki.models import Email, SettingsVersion, SkipRule

D = "corp-europe.example"


def _msg(mid, to, subject="Pallet count Venlo", body="We counted 1,150 pallets this morning."):
    m = EmailMessage(policy=email.policy.SMTP)
    m["Message-ID"], m["Date"], m["From"], m["To"], m["Subject"] = f"<{mid}@t.example>", "Tue, 20 Oct 2026 09:00:00 +0000", f"Jeroen <j.devries@{D}>", to, subject
    m.set_content(body)
    return m.as_bytes()


class FakePOP3:
    boxes: dict = {}

    def __init__(self, host, port, timeout=60):
        self.host = host

    def user(self, u):
        self.u = u

    def pass_(self, pw):
        if pw != "secret":
            raise Exception("-ERR bad password")

    def uidl(self):
        msgs = self.boxes[self.u]
        return b"+OK", [f"{i} uid-{i}".encode() for i in range(1, len(msgs) + 1)], 0

    def retr(self, n):
        return b"+OK", self.boxes[self.u][n - 1].split(b"\r\n"), 0

    def stat(self):
        return len(self.boxes[self.u]), 1000

    def quit(self):
        pass


def test_several_pop3_mailboxes(monkeypatch):
    import poplib
    from llmwiki.sources.fetch import fetch
    monkeypatch.setattr(poplib, "POP3_SSL", FakePOP3)
    monkeypatch.setenv("PW_CT", "secret")
    monkeypatch.setenv("PW_CUS", "secret")
    shared = _msg("shared-1", f"dl-ct@{D}, dl-cus@{D}")
    FakePOP3.boxes = {"ct": [shared, _msg("bcc-1", f"someone@{D}", "Bcc to control tower")],   # uid-1 in both boxes: per-mailbox seen-lists
                      "cus": [shared]}
    s = get_settings().model_copy(deep=True)
    s.sources.pop3_enabled = True
    s.sources.pop3_accounts = [Pop3Account(dept=f"dl-ct@{D}", host="pop.t", user="ct", password_env="PW_CT"),
                               Pop3Account(dept=f"dl-cus@{D}", host="pop.t", user="cus", password_env="PW_CUS")]
    with use_settings(s):
        st = fetch(["pop3"])
        assert st["fetched"] == 3 and st["stored"] == 2 and st["merged"] == 1      # the shared mail is stored once
        assert Email.objects.get(message_id="bcc-1@t.example").depts == [f"dl-ct@{D}"]   # Bcc: the mailbox's department counts
        assert set(Email.objects.get(message_id="shared-1@t.example").depts) == {f"dl-ct@{D}", f"dl-cus@{D}"}
        assert fetch(["pop3"])["fetched"] == 0                                     # nothing downloaded twice


def test_rules_import_export_and_config(tmp_path):
    f = tmp_path / "rules.yaml"
    f.write_text('- sender: "workflow@sap.corp.example"\n  subject: "^PO \\\\d+ waiting"\n  why: SAP approvals\n'
                 '- subject: "^Daily stock report"\n  action: note_only\n  include_replies: true\n', encoding="utf-8")
    call_command("rules", "import", str(f))
    r = SkipRule.objects.get(sender="workflow@sap.corp.example")
    assert r.subject == r"^PO \d+ waiting" and r.action == "skip" and not r.include_replies
    assert SkipRule.objects.get(subject="^Daily stock report").action == "note_only"
    call_command("rules", "import", str(f))                                           # importing again updates, never duplicates
    assert SkipRule.objects.filter(sender="workflow@sap.corp.example").count() == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text('- subject: "(unclosed"\n', encoding="utf-8")
    with pytest.raises(Exception):
        call_command("rules", "import", str(bad))
    out = tmp_path / "out.yaml"
    call_command("rules", "export", str(out))
    assert "workflow@sap.corp.example" in out.read_text(encoding="utf-8")
    # settings from the command line: a new version in the history, validated
    n = SettingsVersion.objects.count()
    call_command("config", "set", "reports.stories", "8")
    call_command("config", "set", "departments.team_members", json.dumps([f"a.novak@{D}"]))
    y = tmp_path / "s.yaml"
    y.write_text("pages:\n  budget_ratio: 0.4\n", encoding="utf-8")
    call_command("config", "apply", str(y))
    invalidate()
    s = get_settings()
    assert s.reports.stories == 8 and s.departments.team_members == [f"a.novak@{D}"] and s.pages.budget_ratio == 0.4
    assert SettingsVersion.objects.count() == n + 3
    with pytest.raises(Exception):
        call_command("config", "set", "reports.stories", "99")                        # out of range → refused
    with pytest.raises(Exception):
        call_command("config", "set", "reports.nonsense", "1")
