"""Stopping after repeated failures, rules on mail already stored, folders, and lists that stay small."""
import email.policy
import http.server
import itertools
import json
import threading
import time
from email.message import EmailMessage

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from llmwiki.conf import Endpoint, get_settings, invalidate, save_settings, use_settings
from llmwiki.ingest import store
from llmwiki.llm.pool import Pool, reset_pool
from llmwiki.models import Chunk, Email, EmailOriginal, Folder, KV, LlmCall, Page, PageEmail, Run, SkipRule, Thread

_n = itertools.count(1)
c = Client()


def raw(frm, subject="Hello", to="dl-ct@corp-europe.example", in_reply_to="", body=""):
    m = EmailMessage(policy=email.policy.SMTP)
    m["Message-ID"] = f"<stop-{next(_n)}@test.example>"
    m["Date"], m["From"], m["To"], m["Subject"] = "Mon, 19 Oct 2026 09:00:00 +0000", frm, to, subject
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    m.set_content(body or f"Some text written by a person about this, number {next(_n)}.")
    return m.as_bytes()


def add(*a, **kw) -> Email:
    return Email.objects.get(id=store.add(raw(*a, **kw), "eml", "test").email_id)


def post(path, body=None, method="post"):
    return getattr(c, method)("/api" + path, data=json.dumps(body or {}), content_type="application/json")


def with_endpoints(eps):
    s = get_settings().model_copy(deep=True)
    s.pool.endpoints = eps
    s.pool.validation_retries = 0
    return s


@pytest.fixture
def clean_breaker():
    KV.objects.filter(key="breaker").delete()
    reset_pool()
    yield
    KV.objects.filter(key="breaker").delete()
    reset_pool()


def waiting_mail(n=30):
    tag = next(_n)
    return [add(f"Person {i} <p{i}@corp-europe.example>", f"Topic {tag} number {i}").id for i in range(n)]


BROKEN = dict(kind="chaos", rpm=600, max_concurrent=1, options={"inner": "mock", "pempty": 1.0, "pbroken": 0, "p429": 0, "ptimeout": 0, "p5xx": 0, "seed": 1})


def test_a_run_stops_after_ten_failed_requests(clean_breaker):
    from llmwiki.pipeline.runner import process
    ids = waiting_mail()
    before = LlmCall.objects.count()
    with use_settings(with_endpoints([Endpoint(name="broken", **BROKEN)])):
        stats = process("batch")
    assert LlmCall.objects.count() - before <= 10             # not one request per email
    assert "stopped" in stats and "10 failed or rejected requests in a row" in stats["stopped"]
    assert Run.objects.order_by("-id").first().status == "stopped"
    assert Email.objects.filter(id__in=ids, status__in=["new", "failed"]).count() == len(ids)   # all still waiting
    # the state survives (another process, a restart) and the next scheduled run does not even start
    with use_settings(with_endpoints([Endpoint(name="broken", **BROKEN)])):
        reset_pool()
        made = LlmCall.objects.count()
        assert "stopped" in process("batch")["skipped"]
        assert LlmCall.objects.count() == made
        stopped = c.get("/api/meta").json()["stopped"]
        assert stopped[0]["endpoint"] == "broken" and stopped[0]["down"]
        assert post("/agent/resume").json()["resumed"] == 1
        assert c.get("/api/meta").json()["stopped"] == []


class Wrong(http.server.BaseHTTPRequestHandler):
    """An API connected the wrong way: every request is answered with 422."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(422)
        self.end_headers()
        self.wfile.write(b'{"detail": "wrong body"}')

    def log_message(self, *a):
        pass


def test_a_wrongly_connected_api_stops_the_run(clean_breaker):
    from llmwiki.pipeline.runner import process
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Wrong)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = Endpoint(name="wrong", kind="http_json", base_url=f"http://127.0.0.1:{srv.server_port}/v1", rpm=600, max_concurrent=2, options={"preset": "openai", "proxy": "none"})
    waiting_mail(25)
    before = LlmCall.objects.count()
    s = with_endpoints([ep])
    s.pool.max_retries_5xx = 0
    try:
        with use_settings(s):
            stats = process("batch")
    finally:
        srv.shutdown()
    assert LlmCall.objects.count() - before <= 12             # 10 + what was already running
    assert "stopped" in stats


def test_a_good_endpoint_carries_on_when_the_other_one_stops(clean_breaker):
    with use_settings(with_endpoints([Endpoint(name="bad", priority=0, **BROKEN), Endpoint(name="good", kind="mock", rpm=600, max_concurrent=1, priority=1, fallback_stages=["*"])])):
        p = Pool()
        def parse(o):
            return (o, []) if isinstance(o, dict) else (None, ["need an object"])
        def build(cap, scale):
            return "Return JSON.", "TASK: s1\n### [1] 2026-10-20 09:00 Â· A <a@x>\nSubject: hello\nText.", []
        for _ in range(11):
            try:
                p.run_structured("s1", "test", build, parse, max_validation=0)
            except Exception:
                pass
        by = {e["name"]: e for e in p.status()}
        assert by["bad"]["stopped"] and not by["good"]["stopped"]
        n = LlmCall.objects.filter(endpoint="bad").count()
        assert p.run_structured("s1", "test", build, parse).endpoint == "good"    # no Halted, and nothing more sent to bad
        assert LlmCall.objects.filter(endpoint="bad").count() == n


def test_after_the_cooldown_one_failure_stops_it_again_and_a_success_clears_it(clean_breaker):
    def build(cap, scale):
        return "Return JSON.", "TASK: s1\n### [1] 2026-10-20 09:00 Â· A <a@x>\nSubject: hello\nText.", []
    def parse(o):
        return (o, []) if isinstance(o, dict) else (None, ["need an object"])
    KV.set("breaker", {"broken": {"at": time.time() - 7200, "until": time.time() - 1, "reason": "earlier"}})
    with use_settings(with_endpoints([Endpoint(name="broken", **BROKEN)])):
        p = Pool()
        with pytest.raises(Exception):
            p.run_structured("s1", "test", build, parse, max_validation=0)
        assert p.eps["broken"].down() and KV.get("breaker")["broken"]["reason"].startswith("the trial request failed")
    KV.set("breaker", {"fine": {"at": time.time() - 7200, "until": time.time() - 1, "reason": "earlier"}})
    with use_settings(with_endpoints([Endpoint(name="fine", kind="mock", rpm=600, max_concurrent=1)])):
        p = Pool()
        p.run_structured("s1", "test", build, parse)
        assert not p.eps["fine"].stopped and KV.get("breaker") == {}


def test_chat_is_not_held_back_by_a_stopped_endpoint(clean_breaker):
    KV.set("breaker", {"fine": {"at": time.time(), "until": time.time() + 3600, "reason": "earlier"}})
    with use_settings(with_endpoints([Endpoint(name="fine", kind="mock", rpm=600, max_concurrent=1)])):
        p = Pool()
        st = p.acquire("chat", chat=True)
        p.release(st, chat=True)
        from llmwiki.llm.pool import Halted
        with pytest.raises(Halted):
            p.acquire("s1")


def _routed(e: Email, page: Page):
    PageEmail.objects.create(page=page, email=e)
    Email.objects.filter(id=e.id).update(status="processed", note="A note.")
    Chunk.objects.create(cid=f"c{e.id}", collection="emails", ref=f"email:{e.id}", text=e.new_text, email=e)


def test_a_new_rule_reclassifies_stored_mail_and_removing_it_undoes_that():
    tag = next(_n)
    sender = f"news{tag}@vendor.example"
    subj = f"Weekly offer {tag}"
    done_alone = add(f"News <{sender}>", subj)
    done_shared = add(f"News <{sender}>", subj)
    waiting = add(f"News <{sender}>", subj)
    other = add(f"Anna <anna{tag}@corp-europe.example>", f"Plan {tag}")
    reply = add(f"Anna <anna{tag}@corp-europe.example>", f"RE: {subj}", in_reply_to="<x@y>")
    only = Page.objects.create(slug=f"only-{tag}", title="Only that mail")
    shared = Page.objects.create(slug=f"shared-{tag}", title="Shared")
    _routed(done_alone, only)
    _routed(done_shared, shared)
    _routed(other, shared)

    pv = post("/agent/rules/preview", {"subject": subj, "action": "skip"}).json()
    assert (pv["matched"], pv["waiting"], pv["processed"], pv["replies_kept"]) == (4, 1, 2, 1)
    assert Email.objects.get(id=done_alone.id).status == "processed"                    # a preview changes nothing

    r = post("/agent/rules", {"sender": "", "subject": subj, "action": "skip"}).json()
    assert r["applied"]["skipped"] == 3 and r["applied"]["replies_kept"] == 1
    for e in (done_alone, done_shared, waiting):
        e.refresh_from_db()
        assert e.status == "skipped" and e.rule_ref == r["id"] and subj in e.skip_rule
    reply.refresh_from_db()
    other.refresh_from_db()
    assert reply.status != "skipped" and other.status == "processed"       # a person's reply, and unrelated mail
    assert not PageEmail.objects.filter(email_id__in=[done_alone.id, done_shared.id]).exists()
    assert not Chunk.objects.filter(email_id__in=[done_alone.id, done_shared.id]).exists()
    only.refresh_from_db()
    shared.refresh_from_db()
    assert only.archived and not shared.archived and shared.needs_rewrite  # no mail left / still has mail: rewrite it

    undone = post(f"/agent/rules/{r['id']}", method="delete").json()["undone"]
    assert undone["restored"] == 3
    for e in (done_alone, done_shared, waiting):
        e.refresh_from_db()
        assert e.status == "new" and e.rule_ref is None and e.skip_rule == ""  # processed again by the next run


def test_a_rule_can_apply_to_new_mail_only_and_new_mail_follows_it():
    tag = next(_n)
    sender = f"tool{tag}@vendor.example"
    old = add(f"Tool <{sender}>", "Report")
    r = post("/agent/rules", {"sender": sender, "action": "skip", "apply": False}).json()
    assert r["applied"] is None
    old.refresh_from_db()
    assert old.status == "new"
    new = add(f"Tool <{sender}>", "Report")
    assert new.status == "skipped" and new.rule_ref == r["id"]


def test_a_persons_decision_wins_over_rules():
    tag = next(_n)
    sender = f"mix{tag}@vendor.example"
    keep = add(f"Mix <{sender}>", "One")
    gone = add(f"Mix <{sender}>", "Two")
    assert post("/emails/bulk", {"ids": [gone.id], "skip": True}).json()["ids"] == 1
    gone.refresh_from_db()
    assert gone.status == "skipped" and gone.skip_manual
    r = post("/agent/rules", {"sender": sender, "action": "skip"}).json()
    keep.refresh_from_db()
    assert keep.status == "skipped"
    post(f"/agent/rules/{r['id']}", method="delete")
    gone.refresh_from_db()
    keep.refresh_from_db()
    assert gone.status == "skipped" and keep.status == "new"             # the rule's are undone, the person's stays
    assert post("/emails/bulk", {"ids": [gone.id], "skip": False}).json()["restored"] == 1
    gone.refresh_from_db()
    assert gone.status == "new"


def test_folders_rules_and_the_list():
    tag = next(_n)
    sender = f"file{tag}@vendor.example"
    fid = post("/folders", {"name": f"Vendors {tag}"}).json()["id"]
    assert post("/folders", {"name": f"vendors {tag}"}).status_code == 400   # no two folders with one name
    a = add(f"File <{sender}>", "Invoice one")
    b = add(f"File <{sender}>", "Invoice two")
    assert post("/agent/rules", {"sender": sender, "action": "keep"}).status_code == 400    # filing needs a folder
    r = post("/agent/rules", {"sender": sender, "action": "keep", "folder": fid}).json()
    assert r["applied"]["filed"] == 2
    for e in (a, b):
        e.refresh_from_db()
        assert e.folder_id == fid and e.status == "new"                  # filed, still processed
    d = c.get("/api/emails", {"folder": fid}).json()
    assert d["total"] == 2 and d["rows"][0]["folder"] == fid
    counts = c.get("/api/folders").json()
    assert [x["n"] for x in counts["folders"] if x["id"] == fid] == [2] and counts["inbox"] >= 0
    # new mail is filed by the rule; a person's move is left alone
    new = add(f"File <{sender}>", "Invoice three")
    assert new.folder_id == fid
    post("/emails/bulk", {"ids": [b.id], "folder": None})
    assert not c.get("/api/emails", {"folder": fid, "q": "Invoice two"}).json()["total"]
    post(f"/agent/rules/{r['id']}", {"subject": "Invoice"}, method="patch")
    b.refresh_from_db()
    assert b.folder_id is None and b.folder_manual
    # deleting the folder: mail back in the inbox, a filing-only rule off
    post(f"/folders/{fid}", method="delete")
    a.refresh_from_db()
    assert a.folder_id is None and not SkipRule.objects.get(id=r["id"]).enabled
    assert not Folder.objects.filter(id=fid).exists()


def test_the_sender_filter_lists_department_members_only():
    tag = next(_n)
    member = f"member{tag}@corp-europe.example"
    add(f"Member <{member}>", "Hi")
    add(f"Outsider <out{tag}@corp-europe.example>", "Hi")
    s = get_settings().model_copy(deep=True)
    s.departments.team_members = [member]
    with use_settings(s):
        d = c.get("/api/emails/facets").json()
        assert [x["addr"] for x in d["senders"]] == [member] and d["team_list"]
        assert [x["addr"] for x in c.get("/api/meta").json()["people"]] == [member]


def test_lists_are_paged_and_never_touch_the_stored_originals():
    for i in range(12):
        add(f"P <paged{i}@vendor.example>", f"Paged mail {i}", body="Long " * 500)
    with CaptureQueriesContext(connection) as q:
        d = c.get("/api/emails", {"limit": 5, "q": "Paged mail"}).json()
    assert d["total"] >= 12 and len(d["rows"]) == 5 and len(q) <= 6
    assert not any("emailoriginal" in x["sql"].lower() for x in q)
    assert not any('"new_text"' in x["sql"].split(" FROM ")[0] for x in q)    # the bodies are not loaded either
    more = c.get("/api/emails", {"limit": 5, "offset": 5, "q": "Paged mail"}).json()
    assert not {r["id"] for r in d["rows"]} & {r["id"] for r in more["rows"]}
    assert len(c.get("/api/emails", {"limit": 100000}).json()["rows"]) <= 500
    t = c.get("/api/emails/threads", {"q": "Paged mail", "limit": 3}).json()
    assert t["total"] >= 12 and len(t["rows"]) == 3 and t["rows"][0]["n"] >= 1
    assert len(c.get("/api/pages", {"limit": 2}).json()["rows"]) <= 2


def test_the_original_is_kept_in_its_own_table():
    e = add("O <orig@vendor.example>", "Original")
    o = EmailOriginal.objects.get(email=e)
    assert b"Original" in e.raw_bytes() and "Some text" in o.body_text
    assert not hasattr(Email, "raw") and Thread.objects.filter(id=e.thread_id).exists()


def test_admin_actions_need_an_admin_on_the_server():
    s = get_settings().model_copy(deep=True)
    s.server.mode = "server"
    save_settings(s, user="test", note="server mode test")
    invalidate()
    tag = next(_n)
    try:
        user, staff = Client(), Client()
        User.objects.create_user(f"plain{tag}", password="pw-12345")
        User.objects.create_user(f"boss{tag}", password="pw-12345", is_staff=True)
        assert user.login(username=f"plain{tag}", password="pw-12345") and staff.login(username=f"boss{tag}", password="pw-12345")
        body = {"data": json.dumps({"name": f"F{tag}"}), "content_type": "application/json"}
        assert user.get("/api/meta").json()["admin"] is False and staff.get("/api/meta").json()["admin"] is True
        assert user.post("/api/folders", **body).status_code == 403
        assert user.post("/api/agent/rules", data=json.dumps({"sender": "x@y"}), content_type="application/json").status_code == 403
        assert user.post("/api/emails/bulk", data=json.dumps({"ids": [], "skip": True}), content_type="application/json").status_code == 403
        assert staff.post("/api/folders", **body).status_code == 200
    finally:
        s.server.mode = "local"
        save_settings(s, user="test", note="back to local")
        invalidate()
