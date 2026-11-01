"""The .pst import through Outlook COM, against a fake Outlook object model (no Outlook needed).

The .pst is a personal mailbox: only mail with a department address in To/Cc may be imported, and internal Exchange
mail without transport headers must still get its SMTP addresses and Message-ID/References from MAPI properties."""
import sys
import types
from datetime import datetime, timedelta, timezone

from llmwiki.ingest import store
from llmwiki.models import Email
from llmwiki.sources import pst


class FakeAtt:
    def __init__(self, i, name, data):
        self.Index, self.FileName, self._data = i, name, data

    def SaveAsFile(self, p):
        with open(p, "wb") as f:
            f.write(self._data)


class FakeItems(list):
    def Sort(self, key, desc):
        self.sort(key=lambda it: it.ReceivedTime, reverse=desc)


class FakeEntry:
    """An Exchange address entry: X.500 Address, SMTP only via GetExchangeUser / GetExchangeDistributionList."""
    def __init__(self, smtp, dl=False):
        self.Type, self.Address, self._smtp, self._dl = "EX", f"/o=ExchangeLabs/ou=Exchange/cn=Recipients/cn={smtp.split('@')[0]}", smtp, dl

    def GetExchangeUser(self):
        return None if self._dl else types.SimpleNamespace(PrimarySmtpAddress=self._smtp)

    def GetExchangeDistributionList(self):
        return types.SimpleNamespace(PrimarySmtpAddress=self._smtp) if self._dl else None


class FakeRecipient:
    def __init__(self, name, smtp, kind=1, dl=False):
        self.Name, self.Type, self.AddressEntry = name, kind, FakeEntry(smtp, dl)
        self.Address = self.AddressEntry.Address


class FakeMail:
    Class = 43

    def __init__(self, mid, subj, when, refs="", body="Hello", atts=(), recipients=None, headers=True):
        self.Subject, self.ReceivedTime, self.SentOn = subj, when, when
        self.SenderName, self.SenderEmailAddress = "Ruud Jansen", "/o=ExchangeLabs/ou=Exchange/cn=Recipients/cn=rjansen"
        self.To, self.CC = "DL Control Tower", ""          # display names only, like Outlook
        self.Recipients = recipients if recipients is not None else [FakeRecipient("DL Control Tower", "dl-ct@corp-europe.example", dl=True)]
        self.Sender = FakeEntry("r.jansen@corp-europe.example")
        self.HTMLBody, self.Body, self.EntryID, self.ConversationID, self.ConversationIndex = f"<p>{body}</p>", body, mid * 3, "CONV1", ""
        hdr = f"Message-ID: <{mid}@corp-europe.example>\r\nDate: {when.strftime('%a, %d %b %Y %H:%M:%S +0000')}\r\nSubject: {subj}\r\n"
        if refs:
            hdr += f"In-Reply-To: <{refs}@corp-europe.example>\r\nReferences: <{refs}@corp-europe.example>\r\n"
        self._props = {pst.PR_SENDER_SMTP_ADDRESS: "r.jansen@corp-europe.example", pst.PR_INTERNET_MESSAGE_ID: f"<{mid}@corp-europe.example>",
                       pst.PR_IN_REPLY_TO_ID: f"<{refs}@corp-europe.example>" if refs else "",
                       pst.PR_INTERNET_REFERENCES: f"<{refs}@corp-europe.example>" if refs else ""}
        if headers:
            self._props[pst.PR_TRANSPORT_MESSAGE_HEADERS] = hdr
        self.Attachments = [FakeAtt(i + 1, n, d) for i, (n, d) in enumerate(atts)]

    @property
    def PropertyAccessor(self):
        props = self._props

        def get(tag):
            if tag not in props:
                raise KeyError(tag)       # Outlook raises when a property is missing
            return props[tag]
        return types.SimpleNamespace(GetProperty=get)


class FakeFolder:
    def __init__(self, name, items, subs=()):
        self.Name, self.Items, self.Folders = name, FakeItems(items), list(subs)


def _fake_outlook(monkeypatch, tmp_path, mails):
    root = FakeFolder("Root", [], [FakeFolder("Inbox", mails)])
    fake_store = types.SimpleNamespace(FilePath=str(tmp_path / "x.pst"), GetRootFolder=lambda: root)
    ns = types.SimpleNamespace(Stores=[fake_store], AddStore=lambda p: None)
    outlook = types.SimpleNamespace(GetNamespace=lambda n: ns)
    pkg = types.ModuleType("win32com")
    client = types.ModuleType("win32com.client")
    client.Dispatch = lambda name: outlook
    pkg.client = client
    monkeypatch.setitem(sys.modules, "win32com", pkg)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    pc = types.ModuleType("pythoncom")
    pc.CoInitialize = lambda: None
    monkeypatch.setitem(sys.modules, "pythoncom", pc)


def test_pst_com_import(monkeypatch, tmp_path):
    t0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    mails = [FakeMail("pst-a", "Warehouse lease Kowale", t0, body="We compare two sites for the new warehouse."),
             FakeMail("pst-b", "RE: Warehouse lease Kowale", t0 + timedelta(days=1), refs="pst-a", body="Kowale is cheaper.", atts=[("rent.txt", b"rent 4.10 EUR/m2")]),
             # internal Exchange mail: no transport headers, CC to the department list
             FakeMail("pst-c", "RE: Warehouse lease Kowale", t0 + timedelta(days=2), refs="pst-b", body="Agreed, Kowale.", headers=False,
                      recipients=[FakeRecipient("Ruud Jansen", "r.jansen@corp-europe.example"), FakeRecipient("DL Control Tower", "dl-ct@corp-europe.example", kind=2, dl=True)]),
             # personal mail in the same mailbox: must never be imported
             FakeMail("pst-p", "Dinner on Friday?", t0 + timedelta(days=2), body="Shall we book the table?",
                      recipients=[FakeRecipient("Pat Owner", "p.owner@corp-europe.example")]),
             FakeMail("pst-old", "Old mail", t0 - timedelta(days=60), body="Too old for the backfill window.")]
    _fake_outlook(monkeypatch, tmp_path, mails)
    scan = pst.scan_pst_com(str(tmp_path / "x.pst"), since=t0 - timedelta(days=7), until=t0 + timedelta(days=7))
    assert scan["dept"] == 3 and scan["not_dept"] == 1 and scan["folders"] == {"/Root/Inbox": 3}
    stats = {}
    got = list(pst.iter_pst_com(str(tmp_path / "x.pst"), since=t0 - timedelta(days=7), until=t0 + timedelta(days=7), stats=stats))
    assert len(got) == 3 and stats["not_dept"] == 1          # the old mail is outside the window; the personal mail is left out
    for raw, ref, conv in got:
        assert store.add(raw, "pst", ref, conv).status in ("stored", "merged")
    assert not Email.objects.filter(subject__icontains="Dinner").exists()
    a = Email.objects.get(message_id="pst-a@corp-europe.example")
    b = Email.objects.get(message_id="pst-b@corp-europe.example")
    c = Email.objects.get(message_id="pst-c@corp-europe.example")        # Message-ID from PR_INTERNET_MESSAGE_ID
    assert a.thread_id == b.thread_id == c.thread_id and b.parent_id == a.id and c.parent_id == b.id
    assert c.from_addr == "r.jansen@corp-europe.example" and "dl-ct@corp-europe.example" in c.cc and c.depts == ["dl-ct@corp-europe.example"]
    assert "pst" in a.sources and b.attachments.filter(filename="rent.txt").exists()
    assert b.attachments.get(filename="rent.txt").extract.startswith("rent 4.10")


def test_dept_filter_aliases_and_headers():
    from llmwiki.conf import Settings, use_settings
    s = Settings()
    s.sources.dept_aliases = {"dl-cus@corp-europe.example": ["Customs Team"]}
    with use_settings(s):
        assert pst.is_dept_mail([("Customs Team", "")])                     # display name only
        assert pst.is_dept_mail([("x", "DL-FIN@corp-europe.example")])
        assert not pst.is_dept_mail([("Pat Owner", "p.owner@corp-europe.example")])
        assert not pst.is_dept_mail([], ("DL CT", "dl-ct@corp-europe.example"))       # sender only counts with pst_match_from
        s.sources.pst_match_from = True
        assert pst.is_dept_mail([], ("DL CT", "dl-ct@corp-europe.example"))
        assert pst.dept_from_headers(b"From: a@x.example\r\nTo: Team <dl-ct@corp-europe.example>\r\n\r\n")
        s.sources.pst_dept_only = False
        assert pst.is_dept_mail([("Pat Owner", "p.owner@corp-europe.example")])
