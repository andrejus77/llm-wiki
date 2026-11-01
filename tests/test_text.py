"""Pure functions: subjects, tokens, quotes, HTML → text, JSON repair, budgets, guards, output fixers."""
from llmwiki.ingest.html2md import QUOTE_MARK, html_to_text
from llmwiki.ingest.identifiers import find_identifiers, find_tcodes
from llmwiki.ingest.quotes import split_body
from llmwiki.llm.jsonparse import extract_json
from llmwiki.pipeline import guards, parse
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.util import clip_tokens, est_tokens, has_reply_prefix, shingles, subject_norm, subject_template


def test_subject_norm_handles_prefixes_in_many_languages():
    assert subject_norm("RE: FW: Delivery block Z1") == "delivery block z1"
    assert subject_norm("AW: WG: [EXT] Order 4500123456") == "order 4500123456"
    assert subject_norm("회신: 전달: 4분기 물동량 예측") == "4분기 물동량 예측"
    assert has_reply_prefix("Re: x") and not has_reply_prefix("Report")
    assert subject_template("PO 4500200020 waiting for your approval") == "po # waiting for your approval"


def test_tokens_and_clipping():
    assert est_tokens("hello world " * 100) > 200
    assert est_tokens("안녕하세요") >= 5          # Hangul counts ~1 token per char
    t = clip_tokens("word " * 2000, 100)
    assert est_tokens(t) <= 120 and "cut:" in t


def test_shingles_are_stable_across_processes():
    assert shingles("the quick brown fox jumps over the lazy dog") == shingles("The quick brown fox jumps over the lazy dog!")


def test_split_outlook_english_header():
    body = "Thanks, approved.\n\nKind regards,\nMark de Boer\nHead of Logistics\n\nFrom: Ruud Jansen <r.jansen@corp.example>\nSent: Monday, 19 October 2026 09:15\nTo: Mark\nSubject: Go-live\n\nCan we move the go-live?"
    s = split_body(body)
    assert s.new_text == "Thanks, approved.\n\nKind regards,"
    assert "Mark de Boer" in s.signature
    assert s.blocks and "Can we move the go-live?" in s.blocks[0]["text"]


def test_split_korean_outlook_header():
    body = "확인했습니다. 내일까지 보내드리겠습니다.\n\n보낸 사람: Kim Min-jun <kimhq@hq-korea.example>\n보낸 날짜: 2026년 10월 20일 09:15\n받는 사람: Tom\n제목: 4분기 물동량\n\n예측 자료를 보내주세요."
    s = split_body(body)
    assert s.new_text.startswith("확인했습니다")
    assert "예측 자료" in s.blocks[0]["text"]


def test_split_wrote_and_gt_quotes():
    s = split_body("Sounds good.\n\nOn Tue, 20 Oct 2026 at 09:15, Pia <p@nordline.example> wrote:\n> We can deliver on 10 Nov.\n> Pia")
    assert s.new_text == "Sounds good."
    assert "deliver on 10 Nov" in s.quoted


def test_html_tables_and_reply_separator():
    html = ("<p>See the table:</p><table><tr><th>Delivery</th><th>Block</th></tr><tr><td>80012345</td><td>Z1</td></tr></table>"
            "<div id='divRplyFwdMsg'><b>From:</b> X<br><b>Sent:</b> Mon</div><p>older text</p>")
    text, has_table = html_to_text(html)
    assert has_table and "| Delivery | Block |" in text and "| 80012345 | Z1 |" in text
    assert QUOTE_MARK in text and text.index(QUOTE_MARK) < text.index("older text")


OUTLOOK_HTML = """<html xmlns:o="urn:schemas-microsoft-com:office:office"><head><style><!-- p.MsoNormal {margin:0cm;} --></style></head>
<body lang=EN-US link="#0563C1"><div class=WordSection1>
<p class=MsoNormal><span lang=EN-US>Hi Marta,<o:p></o:p></span></p>
<p class=MsoNormal><span lang=EN-US><o:p>&nbsp;</o:p></span></p>
<p class=MsoNormal><span lang=EN-US>The two reefers MSKU4021553 and TGHU7781204 are released. Harbour amended the
commodity code this morning, so demurrage stops&nbsp;today.<o:p></o:p></span></p>
<p class=MsoNormal><span lang=EN-US><o:p>&nbsp;</o:p></span></p>
<p class=MsoNormal><span lang=EN-US><o:p>&nbsp;</o:p></span></p>
<p class=MsoNormal><span lang=EN-US>Next steps:<o:p></o:p></span></p>
<ul style='margin-top:0cm' type=disc><li class=MsoListParagraph>Check the invoice<o:p></o:p></li>
<li class=MsoListParagraph>Book the cost to logistics Felixstowe<o:p></o:p></li></ul>
<p class=MsoNormal>Line one<br><br><br><br>Line two&#8203;<o:p></o:p></p>
<p class=MsoNormal><span lang=EN-US>Regards,<o:p></o:p></span></p><p class=MsoNormal><span lang=EN-US>Oliver<o:p></o:p></span></p>
<div style='border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm'><p class=MsoNormal><b>From:</b> Marta Kowalska<br><b>Sent:</b>
Monday, 5 October 2026 09:12<br><b>Subject:</b> Reefers held<o:p></o:p></p></div>
<p class=MsoNormal>Can you check the hold?<o:p></o:p></p></div></body></html>"""


def test_outlook_html_whitespace():
    text, _ = html_to_text(OUTLOOK_HTML)
    assert "\n\n\n" not in text                                    # never more than one blank line
    assert "Hi Marta,\n\nThe two reefers" in text                  # an &nbsp; paragraph is one blank line
    assert "amended the commodity code this morning" in text       # source line wrapping is a space, not a line break
    assert "stops today." in text and "​" not in text
    assert "Next steps:\n- Check the invoice\n- Book the cost" in text
    assert "Line one\n\nLine two" in text                          # <br><br><br><br> → one blank line
    assert "Regards,\nOliver" in text                              # Outlook paragraphs are lines
    assert "From: Marta Kowalska\nSent: Monday, 5 October 2026 09:12\nSubject: Reefers held" in text
    assert text.index(QUOTE_MARK) < text.index("From: Marta")
    from llmwiki.ingest.html2md import clean_text
    assert clean_text("a  \r\n\r\n\r\n\r\nb  c") == "a\n\nb c"


def test_identifiers_and_tcodes():
    ids = find_identifiers("PO 4500123456, delivery 80012345, INV-88213, container MSKU1234567",
                           {"po": r"\b45\d{8}\b", "delivery": r"\b8\d{7}\b", "invoice": r"\bINV-?\d{4,8}\b", "container": r"\b[A-Z]{3}U\d{7}\b"})
    assert {t for t, _ in ids} == {"po", "delivery", "invoice", "container"}
    known, cands = find_tcodes("Use VL02N and VKM3. New transaction ZLE_MON2 in t-code menu; ISO9001 is not one.", ["ISO9001"])
    assert {"VL02N", "VKM3"} <= known
    assert "ISO9001" not in known and "ISO9001" not in cands


def test_extract_json_is_lenient():
    assert extract_json('Sure! ```json\n{"a": 1, "b": [1,2,],}\n```')[0] == {"a": 1, "b": [1, 2]}
    assert extract_json('Here you go: {"notes": [{"note": "line1\nline2"}]} hope it helps')[0]["notes"][0]["note"] == "line1\nline2"
    assert extract_json("")[0] is None
    assert extract_json('{"a": 1')[0] == {"a": 1}  # unbalanced → closed


def test_budget_fair_share_and_cap():
    secs = [Section("new", "Emails", ["a " * 4000, "b " * 100], budget=1500, min_each=200), Section("related", "Related", ["c " * 3000], budget=2000)]
    text, meta = assemble(secs, cap=1600, cut_order=["related", "new"], system="sys")
    m = {x["key"]: x for x in meta}
    assert m["new"]["sent"] <= 1500 and m["related"]["cut_cap"] > 0
    assert "b b b" in text   # the small email is kept whole


def test_guards():
    old = "The go-live is planned for 3 November 2026. " * 10
    assert guards.check(old, "", [], "").hard
    shrunk = guards.check(old, "Go-live moved.", [], "")
    assert shrunk.hard
    ok_reason = guards.check(old, "Go-live moved to 17 November 2026 because Nordline slipped.", [], "the old plan dates were replaced by the new plan")
    assert not ok_reason.hard and ok_reason.warn
    terms = guards.check(old, old + " New date.", ["The go-live moved to 17 Nov, PO 4500123456"], "")
    assert terms.warn and any("4500123456" in r[0] for r in terms.rows)
    assert guards.clean("Here is the updated section:\n## Plan\nText") == "**Plan**\nText"


def test_parse_notes_requires_one_note_per_email_until_the_last_round():
    obj = {"notes": [{"email": 1, "note": "x", "kind": "weird", "importance": "HIGH", "due": "2026-13-45"}]}
    fixed, errs = parse.parse_notes(obj, 2, 300, final=False)
    assert fixed is None and "[2]" in errs[0]
    fixed, errs = parse.parse_notes(obj, 2, 300, final=True)
    assert fixed["notes"][1] == {"note": "x", "kind": "info", "importance": "important", "due": "", "ai": False} and fixed["missing"] == [2]
    assert parse.fix_note({"note": "n8n flow", "ai": "yes"}, 300)["ai"] is True


def test_title_overlap_separates_same_topic_from_same_company():
    from llmwiki.pipeline.stages import title_overlap
    # measured in the v2 run: all three had cosine 0.88-0.89 with e5; only the first is the same topic
    assert title_overlap("Venlo pallet supply (PalletPro) and PO release approval", "Venlo pallet supply (PalletPro)") >= 0.5
    assert title_overlap("Venlo customer delivery blocks and temporary credit limits", "Weekly freight accrual (delivered, not yet invoiced)") < 0.5
    assert title_overlap("Venlo credit block Z1 from credit segment 0002 change", "Rotterdam DC EWM go-live") < 0.5


def test_parse_route_fuzzy_labels_and_new_pages():
    class P:
        def __init__(self, title, level):
            self.title, self.level = title, level
    cands = {"P1": P("Q4 customs migration", "project"), "P2": P("Invoice bot errors", "topic")}
    obj = {"pages": [{"page": "p2", "role": "primary", "items": [{"section": "Decisions and reasons", "text": "Fallback added.", "emails": [1, 9]}]},
                     {"page": "Q4 Customs Migration", "items": [{"section": "milestones", "text": "Go-live 17 Nov."}]},
                     {"page": "new", "title": "Pallet shortage Venlo", "level": "topic", "why": "none fits", "items": ["Only 300 pallets left."]}]}
    from llmwiki.conf import Settings, use_settings
    s = Settings()
    s.pages.structure = "template"
    with use_settings(s):
        r, errs = parse.parse_route(obj, 1, cands, 4, False)
    assert not errs
    labels = [p["label"] for p in r["pages"]]
    assert labels == ["P2", "P1", None]
    assert r["pages"][0]["items"][0] == ("decisions", "Fallback added.", [1])
    assert r["pages"][1]["items"][0][0] == "plan"
    assert r["pages"][2]["title"] == "Pallet shortage Venlo"
    r, _ = parse.parse_route(obj, 1, cands, 4, False)   # free structure (default): the editor places the facts
    assert {it[0] for p in r["pages"] for it in p["items"]} == {"page"}


def test_page_guards_budget_repeats_and_meta_talk():
    from llmwiki.pipeline import guards
    from llmwiki.conf import get_settings
    pats = get_settings().pages.drop_phrases
    t, gone = guards.drop_meta("The checklist is in use since 12 Oct 2026. The sources do not show who approved it.\n"
                               "- Step 1: send the invoice.\n- The step order is described in the How section.", pats)
    assert "in use since 12 Oct 2026" in t and "Step 1" in t and len(gone) == 2 and "sources" not in t
    rep = guards.repeats([("summary", "Harbour Agency checks the commodity code two working days before the vessel ETA."),
                          ("How it works", "Step 2: Harbour Agency checks the commodity code at least two working days before the vessel ETA."),
                          ("Costs", "The demurrage of EUR 1,260 stays with Corp Europe because our declaration was wrong.")], 0.7)
    assert len(rep) == 1 and rep[0][0] == "summary"
    d = guards.PageDraft("Short summary of the topic.", [("A", "word " * 200), ("B", "Another fact on 3 Nov.")], "")
    g = guards.check_page(old_words=900, d=d, item_texts=["Fact on 3 Nov."], budget=150)
    assert not g.hard and g.warn                                   # over budget and a long section: confirm round, not a reject
    assert next(r for r in g.rows if r[0].startswith("page ≤ 150 words"))[1] is False
    d2 = guards.PageDraft("Tiny.", [], "")
    assert guards.check_page(old_words=300, d=d2, item_texts=[], budget=200).hard   # an unexplained collapse is rejected
