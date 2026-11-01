"""Skip rules around replies and automatic replies, and threads joined by shared quoted history."""
import email.policy
import itertools
from email.message import EmailMessage

from llmwiki.ingest import store
from llmwiki.models import Email, SkipRule

_n = itertools.count(1)


def raw(frm, to, subject, body, date="Mon, 19 Oct 2026 09:00:00 +0000", mid=None, in_reply_to="", headers=None, cc=""):
    m = EmailMessage(policy=email.policy.SMTP)
    m["Message-ID"] = mid or f"<rules-{next(_n)}@test.example>"
    m["Date"], m["From"], m["To"], m["Subject"] = date, frm, to, subject
    if cc:
        m["Cc"] = cc
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes()


def add(*a, **kw) -> Email:
    r = store.add(raw(*a, **kw), "eml", "test")
    return Email.objects.get(id=r.email_id)


def test_rules_skip_automated_mail_but_not_human_replies():
    SkipRule.objects.create(kind="static", sender="workflow@sap.corp-europe.example", subject="waiting for your approval", action="skip")
    SkipRule.objects.create(kind="static", sender="", subject=r"^Shipment notification", action="skip", include_replies=True)
    wf = "SAP Workflow <workflow@sap.corp-europe.example>"
    orig = add(wf, "dl-ct@corp-europe.example", "PO 4500200099 waiting for your approval", "Please approve PO 4500200099.", mid="<wf-1@sap.example>")
    assert orig.status == "skipped"
    # a person answers the automated mail: feedback → processed
    human = add("Mark de Boer <m.deboer@corp-europe.example>", "dl-ct@corp-europe.example", "RE: PO 4500200099 waiting for your approval",
                "The price on line 2 is wrong, please check with purchasing before anyone approves.", in_reply_to="<wf-1@sap.example>")
    assert human.status != "skipped" and human.skip_rule == ""
    # the system's own 'reply' (an acknowledgement) stays skipped
    ack = add(wf, "dl-ct@corp-europe.example", "RE: PO 4500200099 waiting for your approval", "Your comment was recorded.", in_reply_to="<wf-1@sap.example>")
    assert ack.status == "skipped"
    # automatic replies to it stay skipped, with or without headers
    ooo = add("Pia Lindqvist <pia@nordline.example>", "dl-ct@corp-europe.example", "Automatic reply: PO 4500200099 waiting for your approval",
              "I am out of the office until Monday.", in_reply_to="<wf-1@sap.example>")
    assert ooo.status == "skipped" and "automatic" in ooo.skip_rule
    kor = add("Kim <kim@hq-korea.example>", "dl-ct@corp-europe.example", "자동 회신: PO 4500200099", "부재중입니다.")
    assert kor.status == "skipped"
    hdr = add("Ana <ana@corp-europe.example>", "dl-ct@corp-europe.example", "RE: anything", "Away.", headers={"Auto-Submitted": "auto-replied"})
    assert hdr.status == "skipped"
    bot = add("Carrier portal <no-reply@carrier.example>", "dl-ct@corp-europe.example", "RE: Booking 7781", "We received your message.")
    assert bot.status == "skipped" and "no-reply" in bot.skip_rule
    # a rule with include_replies also skips replies by others
    add("TMS <tms@corp-europe.example>", "dl-ct@corp-europe.example", "Shipment notification 7781", "Shipped.", mid="<tms-1@x.example>")
    rep = add("Tom <t.hendriks@corp-europe.example>", "dl-ct@corp-europe.example", "RE: Shipment notification 7781", "Thanks.", in_reply_to="<tms-1@x.example>")
    assert rep.status == "skipped"
    # mail forwarded by an inbox rule is department mail, not an automatic reply
    fwd = add("Ruud <r.jansen@corp-europe.example>", "dl-ct@corp-europe.example", "Go-live plan", "Plan attached.",
              headers={"Auto-Submitted": "auto-forwarded", "X-MS-Exchange-Generated-Message-Source": "Mailbox Rules Agent"})
    assert fwd.status != "skipped"


QUOTED = ("We need a decision on the Gdansk warehouse lease by Friday. The landlord offers 12,000 m2 at 4.10 EUR per m2 "
          "with a ten year term, and the alternative site in Kowale is cheaper but needs a new access road first.")


def _reply(frm, to, body, mid, quoted_subject="Gdansk lease decision", subject="RE: Gdansk lease decision"):
    text = f"{body}\n\nFrom: Jeroen de Vries <j.devries@corp-europe.example>\nSent: Monday, 19 October 2026 08:00\nTo: Team\nSubject: {quoted_subject}\n\n{QUOTED}"
    return add(frm, to, subject, text, mid=mid, date="Tue, 20 Oct 2026 10:00:00 +0000")


def test_separate_replies_to_one_email_share_a_thread():
    # two people answer the same (never received) email; their mail systems dropped In-Reply-To/References and
    # they share no participant, so neither the headers nor the subject fallback can join them
    a = _reply("Sofie <s.peeters@corp-europe.example>", "dl-fin@corp-europe.example", "Kowale is fine for finance.", "<gd-a@x.example>")
    b = _reply("Lukas <l.becker@corp-europe.example>", "dl-cus@corp-europe.example", "Customs prefers Gdansk (bonded area).", "<gd-b@y.example>")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.thread_id == b.thread_id
    # the same quoted text under another subject is a different conversation: not joined
    c = _reply("Elena <e.rossi@corp-europe.example>", "dl-ct@corp-europe.example", "Unrelated forward.", "<gd-c@z.example>",
               subject="FW: Facilities newsletter", quoted_subject="Facilities newsletter")
    assert c.thread_id != a.thread_id
