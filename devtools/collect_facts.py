"""Collect the numbers for the guide. Run each part inside manage.py shell with the right LLMWIKI_DATA:

    (data_h4) manage.py shell -c "from devtools.collect_facts import main_store; main_store()"
    (data_ko) manage.py shell -c "from devtools.collect_facts import korean; korean()"
    python -m devtools.collect_facts merge <pytest summary> <out facts.json>
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "facts"


def main_store():
    from llmwiki.evaluation import ingest_metrics
    from llmwiki.models import Run
    OUT.mkdir(parents=True, exist_ok=True)
    r = Run.objects.filter(kind="backfill", status="done").order_by("-id").first()
    (OUT / "store.json").write_text(json.dumps({"run": r.stats, "ingest": ingest_metrics()}, indent=1, default=str), encoding="utf-8")
    print("store facts written")


def korean():
    from llmwiki.models import Email, Page, Run
    from llmwiki.rag.search import hybrid
    from llmwiki.util import is_hangul
    from devtools.check_truth import threading_scores
    OUT.mkdir(parents=True, exist_ok=True)
    ko = Email.objects.filter(lang="ko")
    notes = [e.note for e in ko if e.note]
    thr = threading_scores(ROOT / "testdata" / "truth_ko.json")
    h1 = hybrid("원산지 증명", top_k=3)
    h2 = hybrid("certificate of origin missing on invoices from the Korean plant", top_k=3)
    r = Run.objects.filter(kind="backfill", status="done").order_by("-id").first()
    facts = {
        "Emails in the Korean slice (Korean-language)": f"{Email.objects.count()} ({ko.count()})",
        "Threads with 회신/전달 prefixes and Korean quote headers: purity / completeness": f"{thr['thread_purity']} / {thr['thread_completeness']}",
        "Unit errors in the pipeline run": (r.stats or {}).get("unit_errors"),
        "Notes for Korean emails written in English": f"{sum(1 for n in notes if not is_hangul(n, 0.05))} of {len(notes)}",
        "Pages (English titles)": "; ".join(p.title for p in Page.objects.all()[:4]),
        "Hangul query '원산지 증명' (FTS5 trigram + vectors): top hits": "; ".join(f"{h.chunk.collection}: {h.chunk.header[:60]}" for h in h1),
        "English query → finds the page written from Korean mail": f"{h2[0].chunk.collection}: {h2[0].chunk.header[:70]}" if h2 else "—",
    }
    (OUT / "korean.json").write_text(json.dumps(facts, indent=1, ensure_ascii=False), encoding="utf-8")
    print("korean facts written")


def _page_stats() -> dict:
    from llmwiki.conf import get_settings
    from llmwiki.models import LlmCall, Page, SectionHistory
    from llmwiki.pipeline import guards
    from llmwiki.pipeline.pages import length_info
    s = get_settings().pages
    pages = list(Page.objects.filter(archived=False))
    infos = {p.id: length_info(p) for p in pages}
    rep = sum(1 for p in pages if guards.repeats([(x.key, x.text) for x in p.sections.all() if x.text], s.repeat_similarity))
    meta = sum(1 for p in pages if any(guards.drop_meta(x.text, s.drop_phrases)[1] for x in p.sections.all() if x.text))
    over = [p for p in pages if infos[p.id]["words"] > infos[p.id]["budget"] * (1 + s.tolerance)]
    edits = LlmCall.objects.filter(stage="s3")     # whole-page editor requests only (units ed:<page>:page[:confirm])
    return {"pages": pages, "infos": infos, "repeats": rep, "meta": meta, "over": over,
            "calls": edits.filter(status="ok", unit__endswith=":page").count(), "confirm": edits.filter(unit__endswith=":page:confirm", status="ok").count(),
            "rejected": SectionHistory.objects.filter(accepted=False, title="(whole-page draft)").exclude(reason__startswith="first draft").count()}


def rewrite_facts(before_words: int):
    """On the store whose pages were rewritten once (data_h6)."""
    st = _page_stats()
    words = sum(i["words"] for i in st["infos"].values())
    budget = sum(i["budget"] for i in st["infos"].values())
    src = sum(i["source_words"] for i in st["infos"].values())
    rows = [["Pages accepted by the guards", f"{len(st['pages'])} of {len(st['pages'])}"],
            ["Words on the pages", f"{before_words:,} → {words:,} (budget {budget:,}; the mail behind them: {src:,} words of new text by people)"],
            ["Pages over their budget (> 15 %)", len(st["over"])],
            ["Pages with a repeated sentence (script check)", st["repeats"]],
            ["Pages with a sentence about the sources", st["meta"]],
            ["Editor requests · confirm rounds (to shorten or fix)", f"{st['calls']} · {st['confirm']}"]]
    p = next((x for x in st["pages"] if "Felixstowe reefer import checklist" in x.title), None)
    ex = None
    if p:
        import html
        import re
        secs = sorted([x for x in p.sections.all() if x.text.strip()], key=lambda x: (x.key != "summary", x.position))
        def para(x):
            text = html.escape(re.sub(r"\[e\d+\]", "", x.text)).replace("\n", "<br>")
            return f"<p><b>{html.escape(x.title or 'Summary')}</b><br>{text}</p>"
        body = "".join(para(x) for x in secs)
        ex = {"title": p.title, "before": 759, "after": st["infos"][p.id]["words"], "source": st["infos"][p.id]["source_words"], "html": body}
    path = OUT / "pages.json"
    d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    d.update(rewrite=rows, example=ex)
    path.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    print("rewrite facts written")


def fresh_facts():
    """On a store processed from scratch with the free page structure (data_h7)."""
    from statistics import median
    from llmwiki.evaluation import ingest_metrics
    from llmwiki.models import Run
    st = _page_stats()
    infos = st["infos"].values()
    words = sum(i["words"] for i in infos)
    budget = sum(i["budget"] for i in infos)
    src = sum(i["source_words"] for i in infos)
    ing = ingest_metrics()
    rs: dict = {}
    for r in Run.objects.filter(kind="backfill"):   # a run stopped and resumed counts as one
        for k in ("emails", "units", "unit_errors", "seconds"):
            rs[k] = rs.get(k, 0) + ((r.stats or {}).get(k) or 0)
    from llmwiki.models import Email
    done = Email.objects.filter(status="processed").count()
    rows = [["Emails processed · units · unit errors", f"{done} · {rs.get('units')} · {rs.get('unit_errors')}"],
            ["Pages for 27 storylines · page purity · routing accuracy", f"{len(st['pages'])} · {ing.get('page_purity')} · {ing.get('routing_accuracy')}"],
            ["Words on all pages vs the mail behind them", f"{words:,} vs {src:,} ({words / max(1, src):.2f}×; budget {budget:,})"],
            ["Page length / mail length, median page", f"{median(i['words'] / max(1, i['source_words']) for i in infos):.2f}"],
            ["Pages over their budget (> 15 %)", f"{len(st['over'])} of {len(st['pages'])}"],
            ["Pages with a repeated sentence (script check)", st["repeats"]],
            ["Pages with a sentence about the sources", st["meta"]],
            ["Editor requests · confirm rounds · rewrites rejected by the guards", f"{st['calls']} · {st['confirm']} · {st['rejected']}"],
            ["Time", "≈ 36 min at 2–3 parallel requests (stopped once by the PC for low memory after 4 of 6 weeks, resumed where it stopped)"]]
    path = OUT / "pages.json"
    d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    d["fresh"] = rows
    path.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    print("fresh-run facts written")


def merge(pytest_summary: str, out: str):
    store = json.loads((OUT / "store.json").read_text(encoding="utf-8"))
    korean = json.loads((OUT / "korean.json").read_text(encoding="utf-8"))
    results = sorted((ROOT / "eval" / "results").glob("*.json"))
    evals = [json.loads(p.read_text(encoding="utf-8")) for p in results]
    final = [i for i, e in enumerate(evals) if "FINAL" in (e.get("note") or "")]
    first = final[-1] if final else 0
    retrieval_eval = evals[first] if final else evals[-1]
    answers = []
    for e in evals[first:]:   # answers judged before the final run used an older judge
        if "v3" not in (e.get("note") or "") and e is not retrieval_eval:
            continue
        for a in e.get("answers", []) or []:
            answers = [x for x in answers if x["name"] != a["name"]] + [{k: v for k, v in a.items() if k != "items"}]
    truth = json.loads((ROOT / "testdata" / "truth.json").read_text(encoding="utf-8"))
    golden = json.loads((ROOT / "testdata" / "golden.json").read_text(encoding="utf-8"))
    zips = sorted((ROOT / "dist").glob("*.zip"))
    shots = ROOT / "eval" / "screens"
    facts = {
        "date": date.today().strftime("%d %b %Y"), "pytest": pytest_summary,
        "retrieval": retrieval_eval["retrieval"], "ingest": store["ingest"], "run": store["run"], "answers": answers, "korean": korean,
        "dataset": {"storylines": 25, "files": truth["meta"]["files"], "story_emails": truth["meta"]["emails_written"], "noise": truth["meta"]["noise"], "golden": len(golden)},
        "h5": json.loads((OUT / "h5.json").read_text(encoding="utf-8")) if (OUT / "h5.json").exists() else None,
        "pages": json.loads((OUT / "pages.json").read_text(encoding="utf-8")) if (OUT / "pages.json").exists() else None,
        "recheck": json.loads((OUT / "recheck.json").read_text(encoding="utf-8")) if (OUT / "recheck.json").exists() else None,
        "chat_example": json.loads((OUT / "chat_example.json").read_text(encoding="utf-8")) if (OUT / "chat_example.json").exists() else None,
        "zip_mb": round(zips[-1].stat().st_size / 1e6) if zips else "?", "zip_path": str(zips[-1]) if zips else "dist\\ (run tools\\package.bat)",
        "screenshots": [[str(p), cap] for p, cap in [
            (shots / "reports.png", "Reports: one tab per department; Daily / Weekly / Monthly; the weekly report (Thursday) with the mail-per-day strip, coloured stat cards and 2-column story cards"),
            (shots / "reports_monthly.png", "A monthly report: the month as a calendar, story cards counting mail per week, the lists below at most one card tall"),
            (shots / "reports_ai.png", "The AI & Automation report: the n8n ETA flow was mailed to Control Tower only, but the AI flag puts it here"),
            (shots / "topicweek.png", "Reports › Topic week: which topics got mail on which day and hour (script only)"),
            (shots / "thread.png", "A thread with parallel branches: the department never received the original, two people answered it separately"),
            (shots / "page.png", "Wiki page: a summary and the editor's own sections within the length budget (header: words / budget), full width; people from the headers in the rail"),
            (shots / "wiki.png", "Wiki: cards with tag pills (2 rows max) and a one-row footer"),
            (shots / "emails.png", "Emails: list with the per-email notes, importance labels and the reading pane"),
            (shots / "drawer.png", "Side drawer: an email with the earlier emails of its thread"),
            (shots / "chat.png", "Chat: answer with numbered sources (LangGraph agent)"),
            (shots / "pipeline.png", "Agent › Pipeline: one unit's requests with prompt sections, budgets and answers"),
            (shots / "pool.png", "Agent › Pool: endpoints (external marked), and the Refusals panel that lists every blocked request"),
            (shots / "rules.png", "Agent › Rules: automatic replies always skipped; rules for automated mail let people's replies through unless 'also replies' is ticked"),
            (shots / "settings.png", "Agent › Settings: generated from the Pydantic model, endpoints with rpm per API")] if p.exists()],
    }
    Path(out).write_text(json.dumps(facts, indent=1, ensure_ascii=False), encoding="utf-8")
    print("facts merged into", out)


if __name__ == "__main__":
    if sys.argv[1] == "merge":
        merge(sys.argv[2], sys.argv[3])
