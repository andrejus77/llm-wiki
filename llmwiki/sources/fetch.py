"""Fetch orchestration: run one or more sources through store.add() and reconcile the counts.

fetched = stored + merged + duplicate + errors  (checked and logged per run)
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.ingest import store
from llmwiki.ingest.health import check_store
from llmwiki.logs import event, set_current_run
from llmwiki.models import Run

log = logging.getLogger(__name__)


def _consume(it, source: str, stats: dict, limit: int | None = None):
    for item in it:
        raw, ref, conv = item[:3]
        stats["fetched"] += 1
        r = store.add(raw, source, ref, conv, dept=item[3] if len(item) > 3 else "")
        stats[r.status if r.status in ("stored", "merged", "duplicate") else "errors"] += 1
        if r.status == "error":
            stats.setdefault("error_refs", []).append(ref)
        if stats["fetched"] % 200 == 0:
            log.info("%s: %s messages so far (%s stored)", source, stats["fetched"], stats["stored"])
        if limit and stats["fetched"] >= limit:
            break


def fetch(sources: list[str] | None = None, eml_folder: str | None = None, pst_since: datetime | None = None,
          pst_until: datetime | None = None, limit: int | None = None) -> dict:
    """sources: any of 'pop3', 'eml', 'pst'. Default: every enabled source (POP3 and the eml folder)."""
    s = get_settings().sources
    if sources is None:
        sources = [x for x, on in (("pop3", s.pop3_enabled), ("eml", bool(s.eml_folder))) if on]
    run = Run.objects.create(kind="fetch", stats={})
    set_current_run(run.id)
    stats = {"fetched": 0, "stored": 0, "merged": 0, "duplicate": 0, "errors": 0}
    failed = []
    for src in sources:
        try:
            if src == "pop3":
                from llmwiki.sources.pop3 import iter_pop3
                _consume(iter_pop3(s), "pop3", stats, limit)
            elif src == "eml":
                from llmwiki.sources.eml_folder import iter_folder
                _consume(iter_folder(eml_folder or s.eml_folder), "eml", stats, limit)
            elif src == "pst":
                from llmwiki.sources import pst
                route = s.pst_route
                if route == "auto":
                    route = "com" if pst.com_available() else "pypff"
                it = pst.iter_pst_com if route == "com" else pst.iter_pst_pypff
                filt = {}
                _consume(it(s.pst_path, pst_since, pst_until, s.pst_folders or None, filt), "pst", stats, limit)
                if filt.get("not_dept"):
                    stats["pst_not_department"] = filt["not_dept"]
                    event("fetch", f".pst: {filt['not_dept']} personal messages without a department address in To/Cc were left out (never stored)",
                          not_dept=filt["not_dept"])
        except Exception as e:
            failed.append(f"{src}: {e}")
            log.exception("fetch from %s failed", src)
            event("fetch", f"Fetch from {src} failed: {e}", level="error")
    ok = stats["fetched"] == stats["stored"] + stats["merged"] + stats["duplicate"] + stats["errors"]
    health = check_store()
    stats["reconciled"] = ok
    stats["health_ok"] = health["ok"]
    run.stats = {**stats, "sources": sources, "failed": failed}
    run.status = "failed" if failed and not stats["fetched"] else "done"
    run.finished_at = timezone.now()
    run.save()
    event("fetch", f"Fetched {stats['fetched']}: {stats['stored']} stored, {stats['merged']} merged, {stats['duplicate']} duplicates, {stats['errors']} errors"
          + ("" if ok else " — COUNTS DO NOT RECONCILE"), level="info" if ok and not failed else "warning", **{k: v for k, v in stats.items() if k != "error_refs"})
    set_current_run(None)
    return stats
