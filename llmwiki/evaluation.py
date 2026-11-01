"""Evaluation: how the RAG parameters were chosen.

1. Retrieval — golden questions (testdata/golden.json, generated with the synthetic world, so the gold emails are known).
   A retrieved chunk is relevant if it is (or quotes) a gold email, or it is a wiki section of a page that cites a
   gold email. Metrics: recall@1/5/10, MRR, nDCG@10, latency. Grid: bm25 / vector / hybrid × source weights ×
   recency × embedding models (× reranker).
2. Answers (optional, costs LLM calls) — the chat agent answers N questions; an LLM judge scores correctness and
   faithfulness against the gold answer; source precision; abstention on unanswerable questions.
3. Ingest — thread purity/completeness, notes per email, routing accuracy and duplicate pages per storyline,
   retry / fallback / guard-rejection rates.
Results: eval/results/<timestamp>.json (shown on Agent › Eval).
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from django.conf import settings as dj

from llmwiki.conf import get_settings
from llmwiki.models import Email, Item, Job, LlmCall, PageEmail, SectionHistory

log = logging.getLogger(__name__)


def load_golden(path: Path | None = None) -> list[dict]:
    p = path or dj.BASE_DIR / "testdata" / "golden.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _gold_ids(q: dict) -> tuple[set[int], set[int]]:
    mids = [m.lower() for m in q.get("gold_message_ids", [])]
    eids = set(Email.objects.filter(message_id__in=mids).values_list("id", flat=True))
    pids = set(PageEmail.objects.filter(email_id__in=eids).values_list("page_id", flat=True))
    return eids, pids


def _relevant(hit, eids: set[int], pids: set[int]) -> bool:
    c = hit.chunk
    if c.email_id and c.email_id in eids:
        return True
    return bool(c.page_id and c.page_id in pids)


def retrieval_grid(questions: list[dict], models: list[str] | None = None, rerank: bool = False) -> list[dict]:
    from llmwiki.rag.index import embed_missing
    from llmwiki.rag.search import hybrid
    s = get_settings().retrieval
    models = models or s.eval_models
    qs = [q for q in questions if q.get("type") != "unanswerable" and q.get("gold_message_ids")]
    gold = {q["id"]: _gold_ids(q) for q in qs}
    qs = [q for q in qs if gold[q["id"]][0]]
    configs = []
    for m in models:
        try:
            embed_missing(m)
        except Exception as e:
            log.error("could not embed with %s: %s", m, e)
            continue
        configs += [(f"vector only · {m}", dict(mode="vector", weights=False, recency=False, model=m)),
                    (f"hybrid RRF · {m}", dict(mode="hybrid", weights=False, recency=False, model=m)),
                    (f"hybrid + source weights · {m}", dict(mode="hybrid", weights=True, recency=False, model=m)),
                    (f"hybrid + weights + recency · {m}", dict(mode="hybrid", weights=True, recency=True, model=m))]
    configs.insert(0, ("BM25 only (FTS5)", dict(mode="bm25", weights=False, recency=False)))
    if rerank:
        configs.append((f"hybrid + weights + reranker · {s.embed_model}", dict(mode="hybrid", weights=True, recency=False, model=s.embed_model, rerank=True)))
    out = []
    for name, kw in configs:
        r1 = r5 = r10 = mrr = ndcg = 0.0
        t0 = time.monotonic()
        per_type = defaultdict(lambda: [0, 0])
        for q in qs:
            eids, pids = gold[q["id"]]
            hits = hybrid(q["q"], top_k=10, **kw)
            rel = [_relevant(h, eids, pids) for h in hits]
            first = next((i for i, x in enumerate(rel) if x), None)
            r1 += 1 if first is not None and first < 1 else 0
            r5 += 1 if first is not None and first < 5 else 0
            r10 += 1 if first is not None else 0
            mrr += 1 / (first + 1) if first is not None else 0
            dcg = sum(1 / math.log2(i + 2) for i, x in enumerate(rel) if x)
            idcg = sum(1 / math.log2(i + 2) for i in range(min(10, max(1, sum(rel)))))
            ndcg += dcg / idcg if idcg else 0
            per_type[q.get("type", "fact")][0] += 1
            per_type[q.get("type", "fact")][1] += 1 if first is not None and first < 5 else 0
        n = max(1, len(qs))
        out.append({"name": name, "recall_1": round(r1 / n, 3), "recall_5": round(r5 / n, 3), "recall_10": round(r10 / n, 3), "mrr": round(mrr / n, 3),
                    "ndcg_10": round(ndcg / n, 3), "ms": round((time.monotonic() - t0) * 1000 / n, 1),
                    "recall_5_by_type": {k: round(v[1] / v[0], 3) for k, v in per_type.items()}})
        log.info("eval %s: %s", name, out[-1])
    return out


JUDGE = ("You grade an assistant's answer against a reference answer for a question about a company's email wiki.\n"
         "correct = 1 if the answer states the key facts of the reference (wording may differ), else 0. "
         "faithful = 1 if every claim in the answer is supported by the provided sources, else 0.\n"
         'Return JSON only: {"correct": 0 or 1, "faithful": 0 or 1, "reason": "short"}')
ABSTAIN = ("The question below is NOT answered by the knowledge base. Did the assistant say that the information is not available "
           "(it may add related facts it did find)? abstained = 1 if it did, 0 if it presented an answer as if it were known.\n"
           'Return JSON only: {"abstained": 0 or 1, "reason": "short"}')


def answer_eval(questions: list[dict], n: int = 20, mode: str = "fast", engine: str | None = None) -> dict:
    from llmwiki.chat.agent import answer
    from llmwiki.conf import use_settings
    from llmwiki.llm.pool import StageFailed, pool
    s = get_settings().model_copy(deep=True)
    if engine:
        s.chat.engine = engine
    picked = [q for q in questions if q.get("type") != "unanswerable"][: max(1, n - n // 6)] + [q for q in questions if q.get("type") == "unanswerable"][: n // 6]
    res = {"name": f"{s.chat.engine} · {mode}", "n": 0, "correct": 0, "faithful": 0, "source_precision": 0.0, "abstention": None, "seconds": 0.0, "items": []}
    abst = []
    t_all = 0.0
    with use_settings(s):
        for q in picked:
            t0 = time.monotonic()
            try:
                a = answer(q["q"], mode=mode)
            except Exception:
                log.exception("answer failed")
                continue
            dt = time.monotonic() - t0
            t_all += dt
            if q.get("type") == "unanswerable":
                def build_a(cap, scale, q=q, a=a):
                    return ABSTAIN, f"TASK: judge\nQuestion: {q['q']}\n\nAssistant's answer:\n{a['answer']}", []
                try:
                    j = pool().run_structured("judge", "eval", build_a, lambda o: (o, []) if isinstance(o, dict) and "abstained" in o else (None, ["give abstained"]), chat=True,
                                             schema="abstain").obj
                    ok = bool(int(j.get("abstained", 0)))
                except StageFailed:
                    ok = False
                abst.append(1 if ok else 0)
                res["items"].append({"q": q["q"], "answer": a["answer"][:500], "abstained": ok})
                continue
            eids, pids = _gold_ids(q)
            used = a["sources"]
            prec = (sum(1 for x in used if (x.get("email_id") in eids) or (x.get("page_id") in pids)) / len(used)) if used else 0.0
            src_text = "\n\n".join(a.get("evidence") or [f"[{x['n']}] {x['title']}: {x['snippet']}" for x in a["all_sources"][:12]])

            def build(cap, scale, q=q, a=a, src_text=src_text):
                return JUDGE, f"TASK: judge\nQuestion: {q['q']}\nReference answer: {q['a']}\n\nAssistant's answer:\n{a['answer']}\n\nSources (the cited ones, full text):\n{src_text[:30000]}", []
            try:
                j = pool().run_structured("judge", "eval", build, lambda o: (o, []) if isinstance(o, dict) and "correct" in o else (None, ["give correct and faithful"]),
                                         chat=True).obj   # chat priority: the eval is interactive, not background work
            except StageFailed:
                j = {"correct": 0, "faithful": 0, "reason": "judge failed"}
            res["n"] += 1
            res["correct"] += int(j.get("correct", 0))
            res["faithful"] += int(j.get("faithful", 0))
            res["source_precision"] += prec
            res["items"].append({"q": q["q"], "gold": q["a"], "answer": a["answer"][:800], "judge": j, "precision": round(prec, 2), "seconds": round(dt, 1)})
    n = max(1, res["n"])
    res["correct"] = round(res["correct"] / n, 3)
    res["faithful"] = round(res["faithful"] / n, 3)
    res["source_precision"] = round(res["source_precision"] / n, 3)
    res["abstention"] = round(sum(abst) / len(abst), 3) if abst else None
    res["seconds"] = round(t_all / max(1, len(picked)), 1)
    return res


def ingest_metrics(truth_path: Path | None = None) -> dict:
    p = truth_path or dj.BASE_DIR / "testdata" / "truth.json"
    if not p.exists():
        return {}
    truth = json.loads(p.read_text(encoding="utf-8"))
    from devtools.check_truth import threading_scores
    thr = threading_scores(p)
    by_story = defaultdict(list)
    mid2story = {t["message_id"]: t["storyline"] for t in truth["emails"] if not t.get("noise")}
    emails = {e.message_id: e for e in Email.objects.filter(message_id__in=list(mid2story))}
    links = defaultdict(list)
    for pe in PageEmail.objects.filter(email__in=list(emails.values()), role="primary").select_related("page"):
        links[pe.email_id].append(pe.page_id)
    for mid, st in mid2story.items():
        e = emails.get(mid)
        if e and e.importance in ("important", "routine"):
            by_story[st].append(links.get(e.id, []))
    route_acc, dup_pages = [], []
    for st, lists in by_story.items():
        flat = [p for l in lists for p in l]
        if not flat:
            continue
        top, n = Counter(flat).most_common(1)[0]
        route_acc.append(sum(1 for l in lists if top in l) / len(lists))
        dup_pages.append(len([p for p, c in Counter(flat).items() if c >= 2]))
    # page purity: how much of each page's (primary) mail comes from its dominant storyline; storylines with no own page
    per_page = defaultdict(Counter)
    for pe in PageEmail.objects.filter(role="primary").select_related("email"):
        s = mid2story.get(pe.email.message_id)
        if s:
            per_page[pe.page_id][s] += 1
    purities = [c.most_common(1)[0][1] / sum(c.values()) for c in per_page.values() if c]
    dominant = {c.most_common(1)[0][0] for c in per_page.values() if c}
    processed = Email.objects.filter(status="processed").count()
    noted = Email.objects.filter(status="processed").exclude(note="").count()
    units = Job.objects.filter(stage__in=["s1", "s12", "s123"]).count()
    retried = LlmCall.objects.filter(round__gt=1).values("unit").distinct().count()
    fast_fail = Job.objects.filter(stage__in=["s12", "s123"], status="failed").count()
    hist = SectionHistory.objects.all()
    return {**thr, "emails_processed": processed, "notes_per_processed_email": round(noted / max(1, processed), 4),
            "routing_accuracy": round(sum(route_acc) / max(1, len(route_acc)), 3), "pages_per_storyline (≥2 emails)": round(sum(dup_pages) / max(1, len(dup_pages)), 2),
            "storylines": len(route_acc), "pages": len(per_page), "page_purity": round(sum(purities) / max(1, len(purities)), 3),
            "storylines_with_own_page": f"{len(dominant)}/{len(by_story)}", "units": units, "units_retried_share": round(retried / max(1, units), 3), "fast_path_fallbacks": fast_fail,
            "section_versions": hist.filter(accepted=True).count(), "drafts_rejected_or_replaced": hist.filter(accepted=False).count(),
            "items_written": Item.objects.filter(status="written").count(), "items_pending_or_failed": Item.objects.exclude(status="written").count(),
            "llm_calls": LlmCall.objects.count(), "llm_calls_by_status": dict(Counter(LlmCall.objects.values_list("status", flat=True)))}


def run(answers: int = 0, models: list[str] | None = None, rerank: bool = False, modes=("fast",), engines=(None,), note: str = "") -> dict:
    qs = load_golden()
    out = {"created": datetime.now().isoformat(timespec="seconds"), "questions": len(qs), "note": note,
           "settings": {"embed_model": get_settings().retrieval.embed_model, "rrf_k": get_settings().retrieval.rrf_k, "top_k": get_settings().retrieval.top_k}}
    out["retrieval"] = retrieval_grid(qs, models, rerank)
    if answers:
        out["answers"] = [answer_eval(qs, answers, m, e) for m in modes for e in engines]
    out["ingest"] = ingest_metrics()
    d = dj.BASE_DIR / "eval" / "results"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"eval-{datetime.now():%Y%m%d-%H%M%S}.json"
    f.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("eval written to %s", f)
    return out
