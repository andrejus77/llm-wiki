"""Integration on the synthetic dataset with the mock model: store invariants, threading against the ground truth,
the whole pipeline, the index, the chat engines, the Briefing and the API."""
import json
from datetime import date
from pathlib import Path

import pytest
from django.test import Client

from llmwiki.models import Email, Item, Page, SectionHistory

ROOT = Path(__file__).resolve().parents[1]
EML = ROOT / "testdata" / "eml"
pytestmark = pytest.mark.skipif(not EML.exists(), reason="synthetic dataset not built (python -m devtools.build_emails)")


@pytest.fixture(scope="module")
def ingested():
    from llmwiki.sources.fetch import fetch
    return fetch(["eml"], eml_folder=str(EML))


def test_fetch_reconciles_and_store_is_healthy(ingested):
    from llmwiki.ingest.health import check_store
    s = ingested
    assert s["fetched"] == s["stored"] + s["merged"] + s["duplicate"] + s["errors"] and s["errors"] == 0
    assert s["merged"] >= 3            # alias copies
    assert s["duplicate"] >= 1         # resent under a new Message-ID
    assert check_store()["ok"]


def test_threading_matches_ground_truth(ingested):
    from devtools.check_truth import threading_scores
    t = threading_scores()
    assert t["missing"] == 0
    assert t["thread_purity"] >= 0.99            # never joins unrelated mail
    assert t["thread_completeness"] >= 0.97


def test_rerun_is_idempotent(ingested):
    from llmwiki.sources.fetch import fetch
    n = Email.objects.count()
    s = fetch(["eml"], eml_folder=str(EML))
    assert s["stored"] == 0 and Email.objects.count() == n


def test_pipeline_end_to_end(ingested):
    from llmwiki.pipeline.runner import process
    stats = process("backfill")
    assert stats["unit_errors"] == 0 and stats["units"] > 200
    assert stats["fast"] > 0 and stats["staged"] > 0
    assert Email.objects.filter(status__in=["new", "noted", "failed"]).count() == 0
    assert Email.objects.filter(status="processed", note="").count() == 0       # one note per processed email
    assert Page.objects.count() > 5 and Item.objects.filter(status="written").count() > 50
    assert SectionHistory.objects.filter(accepted=True).count() > 10


def test_free_structure_pages_and_rewrite(ingested):
    from llmwiki.models import Section
    from llmwiki.pipeline.runner import rewrite_pages
    pages = list(Page.objects.filter(archived=False, items__isnull=False).distinct())   # pages the pipeline wrote
    missing = [(p.title, list(p.sections.values_list("key", flat=True))) for p in pages
               if not Section.objects.filter(page=p, key="summary").exclude(text="").exists()]
    assert not missing, missing
    assert not Section.objects.filter(key__in=["background", "goals", "people"]).exclude(text="").exists()   # no fixed template sections
    assert Section.objects.exclude(key="summary").exclude(text="").exclude(title="").exists()                # the editor's own titles
    r = rewrite_pages([pages[0].id])
    assert r["pages"] == 1 and r["failed"] == 0 and r["accepted"] + r["rejected"] == 1


def test_index_and_hybrid_search(ingested):
    from llmwiki.rag.search import hybrid
    hits = hybrid("credit block VL02N deliveries Venlo", top_k=8)
    assert hits and any("VL02N" in h.chunk.text or "Z1" in h.chunk.text for h in hits)
    assert hybrid("4500123456", mode="bm25")


@pytest.mark.parametrize("engine,mode", [("plain", "fast"), ("langgraph", "fast"), ("langgraph", "deep"), ("plain", "deep")])
def test_chat_engines(ingested, engine, mode):
    from llmwiki.chat.agent import answer
    from llmwiki.conf import get_settings, use_settings
    s = get_settings().model_copy(deep=True)
    s.chat.engine = engine
    with use_settings(s):
        a = answer("Why were deliveries blocked with Z1 in Venlo?", mode=mode)
    assert a["answer"] and a["engine"].startswith(engine)
    assert a["sources"], a


def test_chat_view_context(ingested):
    from llmwiki.chat.service import view_context
    p = Page.objects.first()
    assert p.title in view_context({"kind": "page", "id": p.slug})
    assert p.title in view_context({"kind": "page", "id": str(p.id)})
    e = Email.objects.filter(status="processed").first()
    assert e.subject in view_context({"kind": "email", "id": str(e.id)})


def test_briefing_and_pairing(ingested):
    from llmwiki.reports.briefing import build, generate_texts, periods, topic_week
    ps = periods()
    assert {p["kind"] for p in ps} == {"daily", "weekly", "monthly"}
    assert all(p["key"].startswith("m:") == (p["kind"] == "monthly") for p in ps)
    assert all(date.fromisoformat(p["key"][2:]).weekday() == 3 for p in periods("weekly"))       # Thursdays
    # a monthly report covers the calendar month, its story cards count mail per week
    m = build(None, {"kind": "monthly"})
    assert m["kind"] == "monthly" and m["from"].endswith("-01") and len(m["per_day"]) >= 28 and all(p["kind"] == "monthly" for p in m["periods"])
    if m["stories"]:
        assert all("week" in x for x in m["stories"][0]["dots"])
    # the report date with the most mail in its window
    busiest = max(periods("daily"), key=lambda p: build(p["key"], {"kind": "daily"})["kpis"]["emails"])["key"]
    d = build(busiest)
    assert d["stories"] and d["rows"] and len(d["per_day"]) == 7
    for row in d["rows"]:
        if len(row) == 2:
            assert abs(row[0]["h"] - row[1]["h"]) <= 6
    # one report per department; a topic never appears in two of them, and only in its own department's
    seen = {}
    for dd in d["depts"]:
        x = build(busiest, {"dept": dd["addr"]})
        for pid in [s["page_id"] for s in x["stories"]] + [r["page_id"] for r in x["rest"]]:
            assert pid not in seen, (pid, seen.get(pid), dd["addr"])
            seen[pid] = dd["addr"]
            assert Page.objects.get(id=pid).dept == dd["addr"]
        assert all(e["day"] >= x["from"] and e["day"] <= x["to"] for e in x["decisions"])
    assert len(set(seen.values())) >= 2
    r = generate_texts(busiest, d["dept"])
    assert r["generated"] >= 1
    assert build(busiest, {"dept": d["dept"]})["agent_texts"]["done"] >= 1
    w = topic_week(busiest, d["dept"])
    assert w["rows"] and len(w["days"]) == 7 and all(Page.objects.get(id=x["page_id"]).dept == d["dept"] for x in w["rows"])
    # Report parts: a switched-off part is neither shown nor requested from the agent
    from llmwiki.conf import get_settings, use_settings
    s = get_settings().model_copy(deep=True)
    s.report_parts.story_text = s.report_parts.lead = s.report_parts.story_key_emails = s.report_parts.story_tags = False
    with use_settings(s):
        x = build(busiest, {"dept": d["dept"]})
        assert all(st["text"] == "" and st["key_emails"] == [] and st["tags"] == [] for st in x["stories"]) and x["lead"] == ""
        assert x["agent_texts"]["total"] == 0 and not x["parts"]["story_text"]


def test_team_filter_and_window_only_input(ingested):
    from llmwiki.conf import get_settings, use_settings
    from llmwiki.reports.briefing import _story_input, _window, build, periods
    p0 = periods()[0]["key"]
    s = get_settings().model_copy(deep=True)
    s.departments.team_members = ["nobody@corp-europe.example"]
    with use_settings(s):
        x = build(p0)
        assert not x["stories"] and x["left_out"]          # no team member wrote → nothing reported
    d = build(p0)
    st = d["stories"][0]
    p = Page.objects.get(id=st["page_id"])
    from datetime import date
    d0, d1 = _window(date.fromisoformat(p0[2:]))
    es = list(Email.objects.filter(page_links__page=p, day__gte=d0, day__lte=d1))
    inp = _story_input(p, es, d0, d1)
    assert f"Window: {d0} to {d1}" in inp
    older = Email.objects.filter(page_links__page=p, day__lt=d0).exclude(note="").first()
    if older:
        assert older.note not in inp                       # nothing from before the window


def test_api_endpoints(ingested):
    c = Client()
    for path in ["/api/health", "/api/meta", "/api/emails?days=7", "/api/emails/week", "/api/pages", "/api/reports/briefing", "/api/agent/logs",
                 "/api/agent/pool", "/api/agent/rules", "/api/agent/tags", "/api/settings", "/api/search?q=VL02N", "/api/agent/units",
                 "/api/emails?depts=dl-cus@corp-europe.example", "/api/pages?depts=dl-fin@corp-europe.example", "/api/reports/briefing?dept=dl-ct@corp-europe.example",
                 "/api/reports/week?dept=dl-cus@corp-europe.example", "/api/agent/refusals", "/api/threads/1"]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.content[:300])
    assert json.loads(c.get("/api/emails?depts=dl-cus@corp-europe.example").content)["total"] > 0
    e = Email.objects.filter(status="processed").first()
    assert c.get(f"/api/emails/{e.id}").status_code == 200
    p = Page.objects.first()
    assert c.get(f"/api/pages/{p.slug}").status_code == 200
    # settings round-trip through the API
    vals = json.loads(c.get("/api/settings").content)["values"]
    vals["pool"]["validation_retries"] = 2
    assert c.put("/api/settings", json.dumps({"values": vals}), content_type="application/json").status_code == 200
    assert json.loads(c.get("/api/settings").content)["values"]["pool"]["validation_retries"] == 2
