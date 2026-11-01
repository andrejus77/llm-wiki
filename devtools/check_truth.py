"""Compare the store against testdata/truth.json: duplicates, threading purity/completeness, skips.

    python manage.py shell -c "from devtools.check_truth import report; report()"
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def threading_scores(truth_path: Path | None = None) -> dict:
    from llmwiki.models import Email
    truth = json.loads((truth_path or ROOT / "testdata" / "truth.json").read_text(encoding="utf-8"))
    by_mid = {t["message_id"]: t for t in truth["emails"] if not t.get("alias_copy")}
    rows = list(Email.objects.values_list("message_id", "thread_id", "status", "dup_of_id"))
    stored = {m: (tid, st, dup) for m, tid, st, dup in rows}
    real = [t for t in by_mid.values() if not t["noise"] and not t.get("resent_duplicate")]
    # purity: for each stored thread, share of its emails from the dominant true thread
    groups = defaultdict(list)
    for t in real:
        if t["message_id"] in stored:
            groups[stored[t["message_id"]][0]].append(t["thread"])
    pure = sum(Counter(v).most_common(1)[0][1] for v in groups.values())
    total = sum(len(v) for v in groups.values())
    # completeness: for each true thread, share of its emails in its dominant stored thread
    tgroups = defaultdict(list)
    for t in real:
        if t["message_id"] in stored:
            tgroups[t["thread"]].append(stored[t["message_id"]][0])
    comp = sum(Counter(v).most_common(1)[0][1] for v in tgroups.values())
    missing = [t["message_id"] for t in by_mid.values() if t["message_id"] not in stored]
    resent = [t for t in by_mid.values() if t.get("resent_duplicate")]
    return {
        "true_emails": len(by_mid), "stored": len(stored), "missing": len(missing),
        "alias_copies_merged": sum(1 for t in truth["emails"] if t.get("alias_copy")),
        # either copy may arrive first; the later one must be marked duplicate
        "resent_marked_duplicate": sum(1 for t in resent if any(v[1] == "duplicate" for k, v in stored.items()
                                                               if k == t["message_id"] or t["message_id"].endswith(k))),
        "thread_purity": round(pure / total, 4) if total else None, "thread_completeness": round(comp / total, 4) if total else None,
        "true_threads": len(tgroups), "stored_threads_for_real_mail": len(groups),
        "noise_skipped": sum(1 for t in by_mid.values() if t["noise"] and stored.get(t["message_id"], (0, ""))[1] == "skipped"),
        "noise_total": sum(1 for t in by_mid.values() if t["noise"]),
        "quoted_only_in_truth": len(truth.get("quoted_only", [])),
    }


def dept_scores(truth_path: Path | None = None) -> dict:
    """After a pipeline run: are pages in the right department's report, is the AI flag right, is automated mail held back?"""
    from llmwiki.models import Email, PageEmail
    from devtools.world import REPORT_DEPT
    truth = json.loads((truth_path or ROOT / "testdata" / "truth.json").read_text(encoding="utf-8"))
    sl_of = {t["message_id"]: t["storyline"] for t in truth["emails"] if not t.get("alias_copy")}
    dom = "corp-europe.example"
    ai_sl = {k for k, v in REPORT_DEPT.items() if v == "dl-ai"}
    es = {e.message_id: e for e in Email.objects.exclude(status="duplicate")}
    # storyline → its dominant page → that page's department
    pages_of = defaultdict(Counter)
    dept_of = {}
    for pe in PageEmail.objects.select_related("page", "email"):
        sl = sl_of.get(pe.email.message_id)
        if sl and sl != "noise":
            pages_of[sl][pe.page_id] += 1
            dept_of[pe.page_id] = pe.page.dept
    right, rows = 0, []
    for sl, c in sorted(pages_of.items()):
        pid = c.most_common(1)[0][0]
        want = REPORT_DEPT.get(sl, "dl-ct") + "@" + dom
        ok = dept_of.get(pid) == want
        right += ok
        if not ok:
            rows.append(f"{sl}: page dept {dept_of.get(pid) or '-'} (want {want})")
    processed = [e for m, e in es.items() if m in sl_of and e.status == "processed"]
    story = [e for e in processed if sl_of[e.message_id] != "noise"]
    ai_true = [e for e in story if sl_of[e.message_id] in ai_sl]
    ai_flagged = [e for e in processed if e.ai_related]
    noise = [e for m, e in es.items() if sl_of.get(m) == "noise"]
    noise_seen = [e for e in noise if e.status != "skipped"]
    return {
        "storylines_in_right_report": f"{right}/{len(pages_of)}", "wrong": rows,
        "ai_recall": round(sum(1 for e in ai_true if e.ai_related) / max(1, len(ai_true)), 3),
        "ai_false_positives": sum(1 for e in ai_flagged if sl_of.get(e.message_id) not in ai_sl),
        "ai_emails": f"{sum(1 for e in ai_true if e.ai_related)} of {len(ai_true)}",
        "noise_not_skipped": len(noise_seen),
        "noise_automated": sum(1 for e in noise_seen if e.automated),
        "noise_not_important_or_skipped": round((len(noise) - len(noise_seen) + sum(1 for e in noise_seen if e.importance == "not_important")) / max(1, len(noise)), 3),
        "noise_routed_to_pages": sum(1 for e in noise_seen if e.page_links.exists()),
        "storyline_emails_marked_automated": sum(1 for e in story if e.automated),
    }


def report():
    print(json.dumps(threading_scores(), indent=1))
