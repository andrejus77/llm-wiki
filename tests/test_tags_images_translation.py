"""Pictures in place, signature pictures, user ids in names, languages, translations that keep their form, tags (catalog terms,
exact words for new ones, what follows by script), and a Qdrant folder held by another process."""
import json
import struct
import subprocess
import sys
import textwrap
import time
import zlib
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest

from llmwiki import privacy
from llmwiki.conf import Settings, get_settings
from llmwiki.ingest import html2md, images
from llmwiki.ingest.store import add
from llmwiki.models import Attachment, Email, EmailOriginal, Event, Page, PageEmail, Section, Tag
from llmwiki.pipeline import tagger, translate
from llmwiki.util import detect_lang, display_name, model_text, words

BASE = datetime(2026, 10, 27, 8, 30, tzinfo=timezone.utc)
DEPT = "dl-ct@corp-europe.example"


@pytest.fixture
def client():
    from django.test import Client
    return Client()


def png(w, h, seed=0):
    row = b"\x00" + bytes([seed % 256]) * (w * 3)
    body = zlib.compress(row * h, 1)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", body) + chunk(b"IEND", b"")


def mail(mid, subject, frm, html, inline=(), files=(), when=BASE, to=(DEPT,), reply=None):
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}@t.example>"
    m["Subject"], m["From"], m["To"] = subject, frm, ", ".join(to)
    m["Date"] = format_datetime(when)
    if reply:
        m["In-Reply-To"] = f"<{reply}@t.example>"
    m.set_content("text")
    m.add_alternative(html, subtype="html")
    for cid, name, data in inline:
        m.get_payload()[1].add_related(data, maintype="image", subtype="png", cid=f"<{cid}>", filename=name, disposition="inline")
    for name, mt, st, data in files:
        m.add_attachment(data, maintype=mt, subtype=st, filename=name)
    return m.as_bytes()


def store(*a, **k):
    r = add(mail(*a, **k), "eml", "t")
    assert r.status == "stored", r
    return Email.objects.get(id=r.email_id)


def by_name(e):
    return {a.filename: a for a in e.attachments.all()}


# ---------------------------------------------------------------- names and languages
@pytest.mark.parametrize("name,addr,want", [
    ("김철수", "cs.kim@x.com", "김철수 (cs.kim)"),
    ("김철수 (cs.kim)", "cs.kim@x.com", "김철수 (cs.kim)"),
    ("Anna Novak", "a.novak@x.com", "Anna Novak"),
    ("Andrej Tibenský", "a.t@x.com", "Andrej Tibenský"),      # accents are Latin: nothing to add
    ("Иван Петров", "ivan@x.com", "Иван Петров (ivan)"),
    ("", "ivan@x.com", "ivan"),
])
def test_display_name(name, addr, want):
    assert display_name(name, addr) == want


def test_stored_name_has_the_user_id():
    e = store("n1", "테스트", "김철수 <cs.kim@corp-europe.example>", "<p>안녕하세요 확인 부탁드립니다</p>")
    assert e.from_name == "김철수 (cs.kim)"
    assert e.lang == "ko"


@pytest.mark.parametrize("text,want", [
    ("안녕하세요. 납품 건에 출하 보류가 걸려 있습니다.", "ko"),
    ("SAP VL02N 에서 해제가 안 됩니다 please check the delivery", "ko"),
    ("Hello, please check the delivery for me and let me know.", "en"),
    ("Wir haben die Lieferung nicht mit der Rechnung für Sie, bitte prüfen und eine Antwort auf die Frage", "de"),
    ("こんにちは。納品の件について確認をお願いします。", "ja"),
    ("Доставка задерживается, проверьте пожалуйста", "ru"),
    ("OK", "en"),
    ("", "en"),
])
def test_detect_lang(text, want):
    assert detect_lang(text) == want


def test_user_id_stays_inside_the_company():
    assert privacy.redact("From 김철수 (cs.kim) about SAP (ERP)")[0] == "From 김철수 about SAP (ERP)"
    assert privacy.redact("From 김철수 (cs.kim)", keep_internal=True)[0] == "From 김철수 (cs.kim)"


# ---------------------------------------------------------------- pictures
def test_reference_is_the_content_id_also_inside_a_table():
    text, _ = html2md.html_to_text('<p>before</p><p><img src="cid:a%40b" alt="그림"></p><table><tr><td>x</td><td><img src="cid:c@d"></td></tr><tr><td>1</td><td>2</td></tr></table>'
                                   '<img src="https://web.example/p.png" alt="p"><img src="https://t.example/x.gif" width="1" height="1">')
    assert "[image: cid:a@b]" in text and "[image: cid:c@d]" in text and "[image: https://web.example/p.png]" in text
    assert "x.gif" not in text


def test_image_sizes_from_the_header():
    assert images.size_of(png(300, 40)) == (300, 40)
    assert images.size_of(b"GIF89a" + struct.pack("<HH", 12, 7)) == (12, 7)
    jpeg = b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xc0\x00\x11\x08" + struct.pack(">HH", 50, 80) + b"\x00" * 8
    assert images.size_of(jpeg) == (80, 50)
    assert images.size_of(b"\x01\x00\x00\x00 emf") is None


def test_each_picture_stays_with_the_mail_that_wrote_it():
    sig, shot, reply_shot = png(380, 70, 1), png(640, 360, 2), png(700, 420, 3)
    a = store("p1", "Delivery block", "김철수 <cs.kim@corp-europe.example>",
              '<p class="MsoNormal">Please look at this.</p><p class="MsoNormal"><img src="cid:one@a"></p><p class="MsoNormal">Thanks</p><p class="MsoNormal"><img src="cid:two@a"></p>',
              inline=[("one@a", "image001.png", shot), ("two@a", "image002.png", sig)])
    assert by_name(a)["image001.png"].place == "body" and not by_name(a)["image001.png"].is_signature
    assert by_name(a)["image002.png"].place == "signature" and by_name(a)["image002.png"].is_signature
    assert a.n_attachments == 0
    b = store("p2", "RE: Delivery block", "Anna Novak <a.novak@corp-europe.example>",
              '<p class="MsoNormal">Fixed, see below.</p><p class="MsoNormal"><img src="cid:three@b"></p>'
              '<div style="border:none;border-top:solid #E1E1E1 1.0pt"><p><b>From:</b> Kim<br><b>Sent:</b> 27 October 2026<br><b>To:</b> dl<br><b>Subject:</b> Delivery block</p></div>'
              '<p class="MsoNormal">Please look at this.</p><p class="MsoNormal"><img src="cid:one@old"></p>',
              inline=[("three@b", "image101.png", reply_shot), ("one@old", "image001.png", shot)], when=BASE + timedelta(hours=2), reply="p1")
    atts = by_name(b)
    assert atts["image101.png"].place == "body" and atts["image001.png"].place == "quoted"
    assert b.n_attachments == 0   # neither is a file to download
    assert not atts["image001.png"].is_file


def test_real_files_and_unreferenced_pictures_are_attachments():
    e = store("p3", "Report", "a.novak@corp-europe.example", "<p>See the attached photo and report.</p>",
              files=[("site.png", "image", "png", png(900, 600, 5)), ("report.txt", "text", "plain", b"hello")])
    assert e.n_attachments == 2
    assert all(a.is_file for a in e.attachments.all())


def test_a_wide_strip_at_the_very_end_is_a_signature_but_a_screenshot_is_not():
    strip = store("s1", "A", "x1@corp-europe.example", '<p>Count is finished.</p><p><img src="cid:s@1"></p>', inline=[("s@1", "s.png", png(400, 80, 7))])
    assert by_name(strip)["s.png"].is_signature
    big = store("s2", "B", "x2@corp-europe.example", '<p>See the error.</p><p><img src="cid:b@1"></p>', inline=[("b@1", "b.png", png(800, 500, 8))])
    assert not by_name(big)["b.png"].is_signature
    middle = store("s3", "C", "x3@corp-europe.example", '<p>Before.</p><p><img src="cid:m@1"></p><p>' + "A long explanation that goes on below the picture. " * 4 + '</p>', inline=[("m@1", "m.png", png(400, 80, 9))])
    assert not by_name(middle)["m.png"].is_signature


def test_a_small_picture_in_several_threads_is_a_signature_also_for_older_mail():
    logo = png(300, 200, 11)    # too tall for the strip rule
    n = get_settings().store.signature_image_repeat
    mails = [store(f"r{i}", f"Topic number {i} of the logo test", "y@corp-europe.example", f'<p>Text {i}.</p><p><img src="cid:l{i}@x"></p><p>{"More text after it. " * 8}</p>',
                   inline=[(f"l{i}@x", "logo.png", logo)], when=BASE + timedelta(days=i * 40)) for i in range(n)]
    assert len({m.thread_id for m in mails}) == n
    assert all(Attachment.objects.get(email=m).is_signature for m in mails)


def test_image_references_do_not_change_quote_matching():
    assert words("same text [image: cid:image001.png@AAA] here") == words("same text [image: cid:image001.png@BBB] here")
    assert model_text("a [image: cid:long@ref] b") == "a [image] b"


def test_portal_gets_pictures_without_the_signature(client):
    e = store("p9", "Pictures for the portal", "김철수 <cs.kim@corp-europe.example>",
              '<p class="MsoNormal">Look.</p><p class="MsoNormal"><img src="cid:one@p"></p><p class="MsoNormal">Thanks</p><p class="MsoNormal"><img src="cid:two@p"></p>',
              inline=[("one@p", "image001.png", png(640, 360, 21)), ("two@p", "image002.png", png(380, 70, 22))],
              files=[("a.txt", "text", "plain", b"x")])
    d = client.get(f"/api/emails/{e.id}").json()
    assert "one@p" in d["body"]["new_text"] and "one@p" in d["body"]["images"]
    assert "two@p" not in d["body"]["images"]
    assert [a["filename"] for a in d["body"]["attachments"]] == ["a.txt"]
    assert [a["filename"] for a in d["body"]["signature_images"]] == ["image002.png"]
    img = client.get(f"/api/attachments/{d['body']['images']['one@p']['id']}?inline=true")
    assert img.status_code == 200 and img["Content-Type"] == "image/png"


def test_the_thread_views_get_pictures_and_files(client):
    a = store("q1", "Thread with pictures", "a.novak@corp-europe.example", '<p>First.</p><p><img src="cid:f@q"></p><p>' + "Words after the picture. " * 6 + "</p>",
              inline=[("f@q", "f.png", png(640, 360, 31))], files=[("x.txt", "text", "plain", b"x")])
    b = store("q2", "RE: Thread with pictures", "e.laurent@corp-europe.example", "<p>Second.</p>", when=BASE + timedelta(hours=1), reply="q1")
    d = client.get(f"/api/emails/{b.id}").json()
    x = d["earlier"][0]
    assert x["id"] == a.id and "f@q" in x["images"] and [f["filename"] for f in x["files"]] == ["x.txt"]
    t = client.get(f"/api/threads/{a.thread_id}").json()
    assert "f@q" in t["emails"][0]["images"]


def test_reparse_brings_old_mail_in_line():
    from llmwiki.ingest.reparse import needed, reparse_all
    e = store("o1", "Old mail", "김철수 <cs.kim@corp-europe.example>", '<p class="MsoNormal">확인 부탁드립니다.</p><p class="MsoNormal"><img src="cid:o@1" alt="그림 1"></p><p class="MsoNormal">' + "설명 " * 30 + "</p>",
              inline=[("o@1", "image001.png", png(640, 360, 41))])
    Email.objects.filter(id=e.id).update(new_text="확인 부탁드립니다.\n[image: 그림 1]\n" + "설명 " * 30, from_name="김철수", lang="en", n_attachments=1)
    EmailOriginal.objects.filter(email_id=e.id).update(body_text="old")
    Attachment.objects.filter(email=e).update(place="", width=None, height=None)
    r = reparse_all()
    e.refresh_from_db()
    assert r["failed"] == 0 and "[image: cid:o@1]" in e.new_text
    assert e.from_name == "김철수 (cs.kim)" and e.lang == "ko" and e.n_attachments == 0
    assert Attachment.objects.get(email=e).place == "body"
    assert not needed()


# ---------------------------------------------------------------- translations
def test_mask_hides_pictures_addresses_and_personal_data():
    masked, kept = translate.mask("전화 +82 10 1234 5678 / kim@x.com [image: cid:a@b] https://x.example/a?b=1 PO 4500123456")
    for secret in ("1234 5678", "kim@x.com", "cid:a@b", "x.example"):
        assert secret not in masked
    assert "4500123456" in masked      # a reference number is business text
    assert translate.unmask(masked, kept).startswith("전화 +82 10 1234 5678 / kim@x.com")


def _wait(e, seconds=30):
    t0 = time.time()
    while time.time() - t0 < seconds:
        d = translate.get(e)
        if d["status"] in ("done", "failed"):
            return d
        time.sleep(.1)
    raise AssertionError("still running")


def test_translation_keeps_the_form(client):
    e = store("t1", "출하 보류", "김철수 <cs.kim@corp-europe.example>",
              '<p class="MsoNormal">안녕하세요.</p><p class="MsoNormal">납품 80012345 건 입니다.</p><p class="MsoNormal"><img src="cid:t@1"></p>'
              '<table><tr><th>납품</th><th>상태</th></tr><tr><td>80012345</td><td>보류</td></tr></table><ul><li>첫째 항목</li><li>둘째 항목</li></ul><p class="MsoNormal">연락처 kim@corp-europe.example</p>',
              inline=[("t@1", "s.png", png(640, 360, 51))])
    r = client.post(f"/api/emails/{e.id}/translation", data="{}", content_type="application/json").json()
    assert r["status"] in ("running", "done") and r["needed"]
    d = _wait(e)
    assert d["status"] == "done", d
    src, out = e.new_text.split("\n"), d["text"].split("\n")
    assert len(src) == len(out)
    assert "[image: cid:t@1]" in d["text"] and "kim@corp-europe.example" in d["text"]
    assert d["subject"].startswith("EN ")
    table = [x for x in out if x.lstrip().startswith("|")]
    assert len(table) == 3 and table[1].count("|") == src[[i for i, x in enumerate(src) if x.lstrip().startswith("|")][1]].count("|")
    assert any(x.startswith("- EN ") for x in out)     # the list marker is kept by the script
    assert translate.get(e)["status"] == "done"
    # the prompt held neither the picture reference nor the address
    from llmwiki.models import LlmCall
    p = LlmCall.objects.filter(stage="translate", unit__startswith=f"translate:{e.id}:").order_by("-id").first().prompt
    assert "cid:t@1" not in p and "kim@corp-europe.example" not in p
    assert e.new_text.startswith("안녕하세요")   # the original is untouched


def test_a_new_text_makes_the_translation_stale():
    e = store("t2", "번역 테스트", "cs.kim@corp-europe.example", "<p>첫째 문장입니다. 확인 부탁드립니다.</p>")
    translate.start(e)
    assert _wait(e)["status"] == "done"
    Email.objects.filter(id=e.id).update(new_text="다른 문장입니다.")
    e.refresh_from_db()
    assert translate.get(e)["status"] == "none"


def test_lost_codes_get_the_model_a_second_try_then_the_line_stays_original(monkeypatch):
    from llmwiki.llm import mock
    e = store("t3", "코드 테스트", "cs.kim@corp-europe.example", "<p>VL02N 에서 납품 80012345 를 확인합니다.</p><p>다음 줄입니다.</p>")
    seen = []

    def bad(self, system, prompt):
        seen.append(prompt)
        lines = [{"n": int(n), "text": "Something else entirely"} for n, _ in __import__("re").findall(r"^(\d+): (.*)$", self._block(prompt, "Lines to translate"), __import__("re").M)]
        return json.dumps({"subject": "S", "lines": lines})
    monkeypatch.setattr(mock.Mock, "_translate", bad)
    translate.start(e)
    d = _wait(e)
    assert d["status"] == "done"
    assert len(seen) == get_settings().pool.validation_retries + 1 and "must keep these exactly" in seen[1]
    assert "VL02N" in d["text"] and "80012345" in d["text"]   # the bad line stayed in the original
    assert "Something else entirely" in d["text"]              # the good one was taken


def test_translation_never_goes_to_an_external_endpoint():
    from llmwiki.conf import Endpoint
    ep = Endpoint(name="gem", kind="mock", external=True)
    assert not privacy.endpoint_allows(ep, "translate")


# ---------------------------------------------------------------- tags
def page_with(title, text, *emails):
    p = Page.objects.create(slug=f"p{time.time_ns()}", title=title, level="topic")
    Section.objects.create(page=p, key="summary", title="Summary", text=text, position=0)
    for e in emails:
        PageEmail.objects.create(page=p, email=e, role="primary")
    return p


def test_old_synthetic_lists_count_as_empty():
    old = sorted(["sap-s4", "sap-ewm", "tms", "customs-gateway", "rpa-orchestrator", "edi", "excel", "power-bi", "wms"])
    s = Settings.model_validate({"tags": {"systems": old}})
    assert s.tags.systems == []
    assert Settings.model_validate({"tags": {"systems": ["SAP"]}}).tags.systems == ["SAP"]


def test_term_matching():
    assert tagger.term_re("power-bi").search("we use Power BI here")
    assert tagger.term_re("SAP").search("in SAP.")
    assert not tagger.term_re("SAP").search("the sapling grew")
    assert tagger.term_re("창고").search("창고에서 확인")      # a particle attached to a Korean word


def test_a_catalog_tag_stays_without_its_words_and_an_unknown_one_comes_off():
    e = store("g1", "Interface", "a.novak@corp-europe.example", "<p>The SAP interface to Hamburg failed again.</p>")
    Tag.objects.get_or_create(type="process", value="month-end closing", defaults={"state": "active"})
    Tag.objects.get_or_create(type="system", value="SAP", defaults={"state": "active"})
    Tag.objects.get_or_create(type="site", value="Hamburg", defaults={"state": "active"})
    p = page_with("Interface problem", "The interface fails.", e)
    Page.objects.filter(id=p.id).update(tags={"process": ["month-end closing"], "system": ["Never Heard Of"], "work_type": ["incident"]})
    r = tagger.sweep([Page.objects.get(id=p.id)])
    t = Page.objects.get(id=p.id).tags
    assert t["process"] == ["month-end closing"]         # abstract: not in the mail, but in the catalog
    assert t["system"] == ["SAP"] and t["site"] == ["Hamburg"] and t["work_type"] == ["incident"]
    assert "system:Never Heard Of" in r["removed_values"]


def test_the_model_may_use_a_catalog_term_the_page_never_writes():
    Tag.objects.get_or_create(type="process", value="stock accuracy", defaults={"state": "active"})
    Tag.objects.get_or_create(type="system", value="Catalog System", defaults={"state": "active"})
    e = store("g1b", "Counting", "a.novak@corp-europe.example", "<p>We counted the shelves again.</p>")
    p = page_with("Shelf counting", "Shelves were counted.", e)
    r = tagger._take_answer(p, {"process": ["Stock Accuracy"], "system": ["catalog system"]}, tagger.Catalog(), "test")
    tags = Page.objects.get(id=p.id).tags
    assert tags["process"] == ["stock accuracy"] and tags["system"] == ["Catalog System"] and not r["refused"]
    tagger.sweep([Page.objects.get(id=p.id)])
    assert "stock accuracy" in Page.objects.get(id=p.id).tags["process"]    # and the script leaves it on


def test_a_t_code_means_sap_even_when_nobody_writes_it():
    Tag.objects.get_or_create(type="tcode", value="ZQ77N", defaults={"state": "active"})
    a = store("g1c", "Delivery change", "a.novak@corp-europe.example", "<p>Please run ZQ77N for this delivery.</p>")
    b = store("g1d", "Other point", "e.laurent@corp-europe.example", "<p>Nothing technical in this one.</p>")
    p = page_with("Delivery change", "A change was needed.", a, b)
    tagger.sweep([Page.objects.get(id=p.id)])
    assert "SAP" in Page.objects.get(id=p.id).tags["system"]      # one mention in two emails is enough


def test_implies_can_be_set_for_other_things_and_a_rejected_target_stays_off():
    from llmwiki import conf
    s = get_settings().model_copy(deep=True)
    s.tags.implies = {"system:EWMX": ["system:Parent Suite"], "system:Parent Suite": ["site:Headquarters"]}
    old = conf._override
    conf._override = s
    try:
        Tag.objects.get_or_create(type="system", value="EWMX", defaults={"state": "active"})
        e = store("g1e", "Bins", "a.novak@corp-europe.example", "<p>EWMX bins are full.</p>")
        p = page_with("Bins", "Full.", e)
        tagger.sweep([Page.objects.get(id=p.id)])
        t = Page.objects.get(id=p.id).tags
        assert {"EWMX", "Parent Suite"} <= set(t["system"]) and "Headquarters" in t["site"]      # a chain works
        Tag.objects.filter(type="site", value="Headquarters").update(state="rejected")
        tagger.sweep([Page.objects.get(id=p.id)])
        assert "Headquarters" not in Page.objects.get(id=p.id).tags.get("site", [])
    finally:
        conf._override = old


def test_a_new_tag_needs_its_exact_words():
    e = store("g2", "Warehouse scanner", "a.novak@corp-europe.example", "<p>The Zebra Link scanner stopped in the warehouse.</p>")
    p = page_with("Scanner outage", "Scanners stopped; Acme Ledger shows the backlog.", e)
    cat = tagger.Catalog()
    r = tagger._take_answer(p, {"system": ["Zebra Link", "Acme Ledger", "Made Up Tool"], "process": ["made up process"], "work_type": ["incident", "nonsense"]}, cat, "test")
    tags = Page.objects.get(id=p.id).tags
    assert set(tags["system"]) == {"Zebra Link", "Acme Ledger"}     # one from the mail, one from the page
    assert "process" not in tags and tags["work_type"] == ["incident"]
    assert {x[1] for x in r["refused"]} == {"Made Up Tool", "made up process", "nonsense"}
    assert Tag.objects.get(type="system", value="Zebra Link").state == "active"
    assert Event.objects.filter(kind="tags", message__contains="Refused tag system: Made Up Tool").exists()


def test_stricter_evidence_setting_takes_the_mail_only():
    s = get_settings().model_copy(deep=True)
    s.tags.evidence = "emails"
    from llmwiki import conf
    old = conf._override
    conf._override = s
    try:
        e = store("g3", "Ledger", "a.novak@corp-europe.example", "<p>Nothing about it here.</p>")
        p = page_with("Ledger", "Blue Ledger shows it.", e)
        r = tagger._take_answer(p, {"system": ["Blue Ledger"]}, tagger.Catalog(), "test")
        assert [x[1] for x in r["refused"]] == ["Blue Ledger"]
    finally:
        conf._override = old


def test_a_rejected_term_stays_out():
    e = store("g4", "Banana", "a.novak@corp-europe.example", "<p>We talked about Banana System today.</p>")
    Tag.objects.create(type="system", value="Banana System", state="rejected")
    p = page_with("Banana", "About it.", e)
    r = tagger._take_answer(p, {"system": ["banana system"]}, tagger.Catalog(), "test")
    assert r["refused"] and "system" not in Page.objects.get(id=p.id).tags


def test_a_term_found_later_reaches_older_pages():
    e1 = store("g5", "Old one", "a.novak@corp-europe.example", "<p>Trouble with the Foxtrot Portal login.</p>")
    old = page_with("Old topic", "A login problem.", e1)
    assert "system" not in (Page.objects.get(id=old.id).tags or {})
    Tag.objects.create(type="system", value="Foxtrot Portal", state="active")
    tagger.sweep([Page.objects.get(id=old.id)])
    assert Page.objects.get(id=old.id).tags["system"] == ["Foxtrot Portal"]


def test_tag_check_fills_pages_refuses_inventions_and_counts_the_minimum():
    e = store("g6", "ORION rollout", "a.novak@corp-europe.example", "<p>The ORION rollout and the ZETA interface moved.</p>")
    p = page_with("ORION rollout", "ORION goes live, ZETA follows.", e)
    Page.objects.filter(id=p.id).update(tags={})
    r = tagger.check_tags(limit=50)
    tags = Page.objects.get(id=p.id).tags
    assert {"ORION", "ZETA"} <= set(tags["system"])
    assert "Imaginary Suite" not in tags["system"]            # the mock always proposes this one
    assert r["refused"] >= 1 and "below_minimum" in r and r["catalog"] >= 2
    assert not Tag.objects.filter(value="Imaginary Suite").exists()


def test_tag_check_sends_page_text_only():
    from llmwiki.models import LlmCall
    e = store("g7", "Private mail", "a.novak@corp-europe.example", "<p>SECRETWORD must stay in the mail, and QUARK too.</p>")
    p = page_with("A topic about QUARK", "QUARK is the topic.", e)
    Page.objects.filter(id=p.id).update(tags={})
    tagger.check_tags(limit=200)
    call = LlmCall.objects.filter(unit__startswith="tags:", stage="review").order_by("-id").first()
    assert call and "SECRETWORD" not in call.prompt


def test_rejecting_through_the_portal(client):
    e = store("g8", "Delta", "a.novak@corp-europe.example", "<p>The Delta Board was down.</p>")
    t = Tag.objects.create(type="system", value="Delta Board", state="active")
    p = page_with("Delta", "Down.", e)
    tagger.sweep([Page.objects.get(id=p.id)])
    assert Page.objects.get(id=p.id).tags["system"] == ["Delta Board"]
    assert client.post(f"/api/agent/tags/{t.id}/reject", data="{}", content_type="application/json").status_code == 200
    assert "system" not in Page.objects.get(id=p.id).tags
    d = client.get("/api/agent/tags").json()
    assert {"below_minimum", "minimum", "pages"} <= set(d)


# ---------------------------------------------------------------- Qdrant held by another process
def test_a_held_qdrant_folder_is_not_an_error(tmp_path):
    """Two real processes on one folder: the second reads SQLite without complaint; the holder catches up later."""
    script = textwrap.dedent("""
        import os, sys, time
        os.environ["LLMWIKI_DATA"] = sys.argv[1]
        os.environ["DJANGO_SETTINGS_MODULE"] = "llmwiki_site.settings"
        sys.path.insert(0, sys.argv[2])
        import django; django.setup()
        from django.core.management import call_command
        call_command("migrate", verbosity=0)
        from llmwiki.rag.vstore import LaggingStore, QdrantStore, get_store
        role = sys.argv[3]
        if role == "holder":
            st = get_store("hashed", 8)
            print("HOLDER", type(st).__name__, flush=True)
            sys.stdin.readline()
        else:
            import numpy as np
            st = get_store("hashed", 8)
            print("SECOND", type(st).__name__, st.search(np.zeros(8), 3), flush=True)
            st.upsert([(1, np.ones(8), {})])
            from llmwiki.models import KV
            print("BEHIND", KV.get("vectors_behind"), flush=True)
    """)
    (tmp_path / "settings.yaml").write_text("retrieval:\n  embed_model: hashed\n  vector_store: qdrant\n", encoding="utf-8")
    f = tmp_path / "t.py"
    f.write_text(script, encoding="utf-8")
    from django.conf import settings as dj
    run = lambda role, **kw: subprocess.Popen([sys.executable, str(f), str(tmp_path), str(dj.BASE_DIR), role], text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)  # noqa: E731
    h = run("holder", stdin=subprocess.PIPE)
    try:
        assert "HOLDER QdrantStore" in h.stdout.readline()
        out, err = run("second").communicate(timeout=120)
        assert "SECOND LaggingStore" in out and "BEHIND True" in out, out + err
        assert "ERROR" not in err and "Qdrant unavailable" not in err, err
    finally:
        h.stdin.write("\n")
        h.stdin.close()
        h.wait(timeout=60)
