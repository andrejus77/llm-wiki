"""Evaluate retrieval (ablation grid), answers (LLM judge) and ingest against the synthetic ground truth.

    manage.py eval                         retrieval grid + ingest metrics (no LLM calls except embeddings)
    manage.py eval --answers 20            + 20 chat answers judged by the LLM (fast mode)
    manage.py eval --answers 12 --modes fast,deep --engines langgraph,plain
"""
import json

from django.core.management.base import BaseCommand

from llmwiki.evaluation import run


class Command(BaseCommand):
    help = "Run the evaluation and write eval/results/<timestamp>.json"

    def add_arguments(self, p):
        p.add_argument("--answers", type=int, default=0)
        p.add_argument("--models", default="")
        p.add_argument("--rerank", action="store_true")
        p.add_argument("--modes", default="fast")
        p.add_argument("--engines", default="")
        p.add_argument("--note", default="")

    def handle(self, *a, **o):
        res = run(answers=o["answers"], models=[m for m in o["models"].split(",") if m] or None, rerank=o["rerank"],
                  modes=tuple(o["modes"].split(",")), engines=tuple(x or None for x in (o["engines"].split(",") if o["engines"] else [""])), note=o["note"])
        for r in res["retrieval"]:
            self.stdout.write(f"{r['name']:<55} R@1 {r['recall_1']:.2f}  R@5 {r['recall_5']:.2f}  R@10 {r['recall_10']:.2f}  MRR {r['mrr']:.2f}  nDCG {r['ndcg_10']:.2f}  {r['ms']} ms")
        for a in res.get("answers", []):
            self.stdout.write(json.dumps({k: v for k, v in a.items() if k != "items"}))
        self.stdout.write(json.dumps(res["ingest"], indent=1, default=str))
