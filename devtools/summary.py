"""Print a compact summary of the store, pipeline and index (manage.py shell -c "import devtools.summary")."""
import json

from django.db.models import Count

from llmwiki.models import Chunk, ChunkVector, Email, Event, Item, Job, LlmCall, Page, Run, SectionHistory


def main():
    out = {
        "emails": dict(Email.objects.values_list("status").annotate(n=Count("id"))),
        "importance": dict(Email.objects.values_list("importance").annotate(n=Count("id"))),
        "pages": Page.objects.filter(archived=False).count(),
        "pages_by_level": dict(Page.objects.values_list("level").annotate(n=Count("id"))),
        "items": dict(Item.objects.values_list("status").annotate(n=Count("id"))),
        "jobs": dict(Job.objects.values_list("stage", "status").annotate(n=Count("id")).values_list("stage", "status", "n").order_by()) if False else
                [f"{s}:{st}={n}" for s, st, n in Job.objects.values_list("stage", "status").annotate(n=Count("id")).values_list("stage", "status", "n")],
        "calls": [f"{s}:{st}={n}" for s, st, n in LlmCall.objects.values_list("stage", "status").annotate(n=Count("id")).values_list("stage", "status", "n")],
        "history": dict(SectionHistory.objects.values_list("accepted").annotate(n=Count("id"))),
        "chunks": dict(Chunk.objects.values_list("collection").annotate(n=Count("id"))),
        "vectors": dict(ChunkVector.objects.values_list("model").annotate(n=Count("id"))),
        "runs": [(r.kind, r.status, r.stats.get("seconds"), r.stats.get("units"), r.stats.get("unit_errors")) for r in Run.objects.order_by("-id")[:4]],
        "warnings": [e.message[:160] for e in Event.objects.filter(level__in=["warning", "error"]).order_by("-id")[:8]],
    }
    print(json.dumps(out, indent=1, default=str))
    print("pages:", [p.title[:50] for p in Page.objects.order_by("-id")[:30]])


main()
