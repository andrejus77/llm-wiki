"""Script checks around the model: automated mail, page level by size, the one owning department, parallel replies."""
import email.policy
import itertools
from email.message import EmailMessage

from llmwiki.ingest import store
from llmwiki.models import Email, Page, PageEmail, Section

_n = itertools.count(1)
D = "corp-europe.example"


def mk(frm, subject, body, day="19", to=f"dl-ct@{D}", cc="", mid=None, irt=""):
    m = EmailMessage(policy=email.policy.SMTP)
    m["Message-ID"] = mid or f"<dep-{next(_n)}@t.example>"
    m["Date"], m["From"], m["To"], m["Subject"] = f"Mon, {day} Oct 2026 09:{next(_n) % 60:02d}:00 +0000", frm, to, subject
    if cc:
        m["Cc"] = cc
    if irt:
        m["In-Reply-To"] = m["References"] = irt
    m.set_content(body)
    r = store.add(m.as_bytes(), "eml", "t")
    return Email.objects.get(id=r.email_id)


def test_automated_detection():
    from llmwiki.ingest.automated import is_automated
    assert is_automated(mk(f"SAP Workflow <workflow@{D}>", "PO 4500300001 waiting for your approval", "Please approve."))
    # the same template many times with near-identical text → automated, even from an unremarkable address
    es = [mk("Portal <portal@carrier.example>", f"Booking {700 + i} confirmed", f"Your booking {700 + i} for {i + 2} pallets is confirmed. Thank you for using the portal.")
          for i in range(5)]
    assert is_automated(es[-1])
    # a person's weekly report: same subject every week, different text → not automated
    texts = ["Accrual is 201k, Rotterdam leads with 66k after the TMS outage.", "Venlo went up to 52k because two carriers invoiced late this week.",
             "Down to 180k; the Veltrans backlog is cleared and Antwerp is stable.", "Peak season pushed it to 230k, mostly Hamburg detention charges.",
             "We closed October at 205k; the November cut-off moves to the 28th."]
    ws = [mk(f"Ana Novak <a.novak@{D}>", "Weekly freight accrual", t) for t in texts]
    assert not is_automated(ws[-1])
    # a reply to an automated mail is written by a person
    wf = Email.objects.filter(from_addr=f"workflow@{D}").first()
    rep = mk(f"Mark de Boer <m.deboer@{D}>", "RE: PO 4500300001 waiting for your approval", "Line 2 has the wrong price.", irt=f"<{wf.message_id}>")
    assert not is_automated(rep)


def test_level_is_checked_by_size_and_text_moves_with_the_template():
    from llmwiki.conf import Settings, use_settings
    s = Settings()
    s.pages.structure = "template"
    with use_settings(s):
        _level_and_template(s)


def _level_and_template(s):
    from llmwiki.pipeline import pages as pg
    p, created = pg.create_page("Zone B wave template fix", "project", "test", start_level="task")
    assert created and p.level == "task" and p.level_proposed == "project"
    pg.write_section(p, "what", "The zone B wave template sorts picks in the wrong aisle order.", [], [])
    pg.write_section(p, "status", "Waiting for the planning freeze to end on 17 Nov.", [], [])
    people = ["s.peeters", "h.schmidt", "l.becker"]
    for i in range(6):
        e = mk(f"X <{people[i % 3]}@{D}>", "Zone B wave template", f"Update {i} on the zone B wave template fix.", day=str(12 + i))
        PageEmail.objects.create(page=p, email=e)
    assert pg.size_of(p) == {"emails": 6, "days": 6, "people": 3}
    assert pg.refresh_level(p) == "project"
    p.refresh_from_db()
    secs = {s.key: s.text for s in Section.objects.filter(page=p)}
    assert "wrong aisle order" in secs["overview"] and "17 Nov" in secs["risks"]       # moved, not lost
    assert secs["what"] == "" and secs["status"] == ""
    # never above the model's proposal
    assert pg.effective_level("topic", {"emails": 50, "days": 20, "people": 9}) == "topic"


def test_department_assignment_ai_and_majority():
    from llmwiki.departments import assign, is_team
    p = Page.objects.create(slug="n8n-eta", title="n8n flow for carrier ETAs", level="topic")
    for i in range(3):   # sent to Control Tower, but about AI/automation work
        e = mk(f"Stefan Kovac <s.kovac@{D}>", "n8n ETA flow", f"The n8n flow step {i} posts ETAs.", to=f"dl-ct@{D}")
        Email.objects.filter(id=e.id).update(ai_related=True)
        PageEmail.objects.create(page=p, email=e)
    assert assign(p) == f"dl-ai@{D}"
    q = Page.objects.create(slug="credit-block", title="Credit block Z1", level="topic")
    for i, (to, cc) in enumerate([(f"dl-fin@{D}", f"dl-ct@{D}"), (f"dl-fin@{D}", ""), (f"dl-ct@{D}", f"dl-fin@{D}")]):
        PageEmail.objects.create(page=q, email=mk(f"Ana <a.novak@{D}>", "Credit block", f"Z1 again on delivery 8001234{i}.", to=to, cc=cc))
    assert assign(q) == f"dl-fin@{D}"
    q.dept, q.dept_locked = f"dl-cus@{D}", True
    q.save()
    assert assign(q) == f"dl-cus@{D}"                      # a person's choice is kept
    assert is_team(f"a.novak@{D}") and not is_team("pia@nordline.example") and not is_team(f"dl-ct@{D}")


def test_parallel_replies_get_their_own_lane():
    from llmwiki.api import thread_graph
    quoted = ("We need a decision on the Gdansk warehouse lease by Friday. The landlord offers 12,000 m2 at 4.10 EUR per m2 with a ten "
              "year term, and the alternative site in Kowale is cheaper but needs a new access road first.")
    body = "{}\n\nFrom: Jeroen de Vries <j.devries@corp-europe.example>\nSent: Monday, 19 October 2026 08:00\nTo: Team\nSubject: Lease decision\n\n" + quoted
    a = mk(f"Sofie <s.peeters@{D}>", "RE: Lease decision", body.format("Kowale works for us."), to=f"dl-fin@{D}", day="20")
    b = mk(f"Lukas <l.becker@{D}>", "RE: Lease decision", body.format("Customs prefers Gdansk."), to=f"dl-cus@{D}", day="20")
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.thread_id == b.thread_id and a.parallel_key and a.parallel_key == b.parallel_key
    c = mk(f"Jeroen <j.devries@{D}>", "RE: Lease decision", "Thanks Lukas, then Gdansk.", to=f"dl-cus@{D}", day="21", irt=f"<{b.message_id}>")
    es = list(Email.objects.filter(thread_id=a.thread_id).order_by("sent_at"))
    g = thread_graph(es)
    assert len(g["ghosts"]) == 1 and g["lanes"] == 2
    na, nb, nc = g["nodes"][f"e{a.id}"], g["nodes"][f"e{b.id}"], g["nodes"][f"e{c.id}"]
    assert na["parallel"]["n"] == 2 and nb["parallel"]["n"] == 2 and na["lane"] != nb["lane"]
    assert nc["lane"] == nb["lane"] and nc["parent"] == f"e{b.id}" and nc["parallel"] is None
