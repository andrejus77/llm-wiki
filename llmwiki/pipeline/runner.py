"""Processing runs. One function, three modes:

- batch    (nightly 18:00): everything not yet processed, one window per day
- backfill (1–2 months)   : one window per week (pipeline.window_backfill) — a page touched 5× costs 1 editor request
- live     (every 5 min)  : one window with the new mail; single emails take the fast path (S123 with editing)

Per window, in order:  units (S1→S2, or the fast path) in parallel per thread  →  index the mail  →
editor per page with pending items  →  index the pages  →  tags (script + S4)  →  learned skip rules.
Every unit and every page is isolated: a failure is logged, the email stays 'new'/'failed' and is retried next run.
The same job keys are used by every mode, so the nightly batch only picks up what live mode missed.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.db import close_old_connections, connection
from django.utils import timezone

from llmwiki.conf import get_settings
from llmwiki.llm.pool import pool
from llmwiki.logs import event, set_current_run
from llmwiki.models import Email, Item, Job, Page, Run
from llmwiki.pipeline import context as cx
from llmwiki.pipeline import stages
from llmwiki.util import sha1, week_of

log = logging.getLogger(__name__)
_run_lock = threading.Lock()     # one processing run at a time
cancel_flag = threading.Event()


def _job(stage: str, unit: cx.Unit, run: Run | None) -> Job:
    key = f"{stage}:{unit.key}:{sha1(*unit.ids)[:12]}"
    j, _ = Job.objects.get_or_create(key=key, defaults={"stage": stage, "unit": unit.key, "run": run})
    j.status, j.started_at, j.attempts, j.run = "running", timezone.now(), j.attempts + 1, run
    j.save(update_fields=["status", "started_at", "attempts", "run"])
    return j


def _done(j: Job, status: str, result=None, error=""):
    j.status, j.finished_at = status, timezone.now()
    j.result = result or {}
    j.error = error[:4000]
    j.save(update_fields=["status", "finished_at", "result", "error"])


def process_unit(unit: cx.Unit, window: str, run: Run | None) -> dict:
    s = get_settings()
    note_only = all(e.skip_action == "note_only" for e in unit.emails)
    needs_s1 = [e for e in unit.emails if e.status in ("new", "failed")]
    single = len(unit.emails) <= s.pipeline.combine_max_emails
    use_fast = (s.pipeline.combine == "auto" and single and needs_s1 and not note_only and s.pipeline.s2_enabled) or \
               (s.pipeline.single_notetaker and needs_s1 and not note_only)
    if use_fast:
        # S123 rewrites single sections, so only with the template structure; the free structure's editor runs right after
        edit = unit.mode == "live" and s.pipeline.combine_edit_live and s.pipeline.s3_enabled and s.pages.structure == "template"
        stage = "s123" if edit else "s12"
        j = _job(stage, unit, run)
        try:
            r = stages.fast(unit, window, edit, job=j)
            _done(j, "done", _slim(r))
            return {"unit": unit.key, "path": stage, **_slim(r)}
        except Exception as e:
            _done(j, "failed", error=_err(e))
            log.warning("fast path failed for %s (%s) → staged pipeline", unit.key, e)
            event("pipeline", f"Fast path failed for {unit.key}; falling back to the stages", level="warning", unit=unit.key, error=str(e)[:300])
            for em in unit.emails:
                em.refresh_from_db()
    out = {"unit": unit.key, "path": "staged"}
    if any(e.status in ("new", "failed") for e in unit.emails) and s.pipeline.s1_enabled:
        j = _job("s1", unit, run)
        try:
            r = stages.s1(unit, job=j)
            _done(j, "done", _slim(r))
            out["s1"] = len(r["notes"])
        except Exception as e:
            _done(j, "failed", error=_err(e))
            Email.objects.filter(id__in=unit.ids, status="new").update(status="failed", fail_reason=f"S1: {e}"[:2000])
            raise
    for e in unit.emails:
        e.refresh_from_db()
    if note_only or not s.pipeline.s2_enabled:
        Email.objects.filter(id__in=unit.ids, status="noted").update(status="processed", processed_at=timezone.now())
        out["s2"] = "skipped (note only)" if note_only else "skipped (S2 off)"
        return out
    if any(e.status == "noted" for e in unit.emails):
        j = _job("s2", unit, run)
        try:
            r = stages.s2(unit, window, job=j)
            _done(j, "done" if "skipped" not in r else "skipped", _slim(r))
            out["s2"] = r.get("items", r.get("skipped"))
        except Exception as e:
            _done(j, "failed", error=_err(e))
            Email.objects.filter(id__in=unit.ids, status="noted").update(fail_reason=f"S2: {e}"[:2000])
            raise
    return out


def _slim(r: dict) -> dict:
    out = {}
    for k, v in (r or {}).items():
        if k == "notes" and isinstance(v, dict):
            out[k] = {str(i): n for i, n in v.items()}
        else:
            out[k] = v
    return out


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}\n" + "".join(traceback.format_exception(e)[-3:])


def _thread_task(units: list[cx.Unit], window: str, run_id: int | None) -> list[dict]:
    close_old_connections()
    run = Run.objects.filter(id=run_id).first() if run_id else None
    set_current_run(run_id)
    results = []
    try:
        for u in units:
            if cancel_flag.is_set():
                break
            # reload emails (another task may have updated statuses)
            u.emails = list(Email.objects.filter(id__in=u.ids).prefetch_related("attachments").order_by("sent_at"))
            try:
                results.append(process_unit(u, window, run))
            except Exception as e:
                log.error("unit %s failed: %s", u.key, e)
                results.append({"unit": u.key, "error": str(e)[:300]})
            try:
                from llmwiki.rag.index import index_emails
                index_emails(u.ids)
            except Exception:
                log.exception("indexing emails of %s failed", u.key)
    finally:
        connection.close()
    return results


def _workers() -> int:
    eps = [e for e in get_settings().pool.endpoints if e.enabled]
    return max(1, min(16, sum(e.max_concurrent for e in eps)))


def pending_emails(until_day: str | None = None, since_day: str | None = None):
    qs = Email.objects.filter(status__in=["new", "noted", "failed"]).exclude(status="duplicate").order_by("sent_at")
    if until_day:
        qs = qs.filter(day__lte=until_day)
    if since_day:
        qs = qs.filter(day__gte=since_day)
    return qs


def window_key(day: str, mode: str) -> str:
    s = get_settings().pipeline
    w = s.window_backfill if mode == "backfill" else s.window_batch if mode == "batch" else "run"
    return week_of(day) if w == "week" else day if w == "day" else "run"


def process(mode: str = "batch", until_day: str | None = None, since_day: str | None = None, max_windows: int | None = None,
            review: bool | None = None, progress=None) -> dict:
    if not _run_lock.acquire(blocking=False):
        log.info("a processing run is already active; skipping this one")
        return {"skipped": "another run is active"}
    cancel_flag.clear()
    run = Run.objects.create(kind=mode, stats={})
    set_current_run(run.id)
    t0 = time.monotonic()
    stats = {"units": 0, "unit_errors": 0, "emails": 0, "pages_edited": 0, "edit_rejected": 0, "new_pages": 0, "windows": 0, "fast": 0, "staged": 0}
    try:
        emails = list(pending_emails(until_day, since_day).prefetch_related("attachments"))
        windows: dict[str, list[Email]] = defaultdict(list)
        for e in emails:
            windows["run" if mode == "live" else window_key(e.day, mode)].append(e)
        order = sorted(windows) if mode != "live" else ["run"]
        if max_windows:
            order = order[:max_windows]
        event(mode, f"{mode} run: {len(emails)} emails waiting in {len(order)} window(s)", emails=len(emails), windows=len(order))
        for wi, w in enumerate(order, 1):
            if cancel_flag.is_set():
                break
            ws = _window(w, windows[w], mode, run, stats)
            stats["windows"] += 1
            run.stats = {**stats, "window": w, "progress": f"{wi}/{len(order)}"}
            run.save(update_fields=["stats"])
            if progress:
                progress(wi, len(order), w, ws)
        # learned skip rules + lint
        try:
            from llmwiki.ingest import skiprules
            stats["learned_rules"] = skiprules.learn()
        except Exception:
            log.exception("learning skip rules failed")
        do_review = review if review is not None else (mode == "batch" and get_settings().reviewer.schedule == "nightly")
        if do_review:
            from llmwiki.pipeline.reviewer import run_review
            changed = list(Page.objects.filter(archived=False, updated_at__gte=run.started_at))
            stats["reviewed"] = len(run_review(changed)["pages"])
        run.status = "done"
    except Exception as e:
        log.exception("%s run failed", mode)
        run.status = "failed"
        run.note = _err(e)[:4000]
        event(mode, f"{mode} run failed: {e}", level="error")
    finally:
        stats["seconds"] = round(time.monotonic() - t0, 1)
        run.stats = stats
        run.finished_at = timezone.now()
        run.save()
        event(mode, f"{mode} run finished in {stats['seconds']}s: {stats['units']} units ({stats['fast']} fast path), "
              f"{stats['pages_edited']} page edits, {stats['new_pages']} new pages, {stats['unit_errors']} unit errors",
              level="warning" if stats["unit_errors"] else "info", **stats)
        set_current_run(None)
        _run_lock.release()
    return stats


def _window(w: str, emails: list[Email], mode: str, run: Run, stats: dict) -> dict:
    t0 = time.monotonic()
    pages_before = set(Page.objects.values_list("id", flat=True))
    units = cx.build_units(emails, mode)
    by_thread: dict[int, list[cx.Unit]] = defaultdict(list)
    for u in units:
        by_thread[u.thread_id].append(u)
    log.info("window %s: %s emails, %s units, %s threads", w, len(emails), len(units), len(by_thread))
    results = []
    with ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="unit") as ex:
        futs = [ex.submit(_thread_task, us, w, run.id) for us in by_thread.values()]
        for f in as_completed(futs):
            try:
                results += f.result()
            except Exception:
                log.exception("thread task crashed")
    close_old_connections()
    stats["units"] += len(results)
    stats["unit_errors"] += sum(1 for r in results if "error" in r)
    stats["fast"] += sum(1 for r in results if r.get("path") in ("s12", "s123"))
    stats["staged"] += sum(1 for r in results if r.get("path") == "staged")
    stats["emails"] += len(emails)
    # script checks before the editor writes: level by size (the template may change) and the one owning department
    try:
        from llmwiki.departments import refresh_pages
        from llmwiki.models import PageEmail
        touched = list(PageEmail.objects.filter(email_id__in=[e.id for e in emails]).values_list("page_id", flat=True).distinct())
        rp = refresh_pages(touched)
        stats["level_changes"] = stats.get("level_changes", 0) + rp["changed_level"]
    except Exception:
        log.exception("page level/department checks failed")
    # editor: every page with pending items
    edited = editor_phase(run)
    stats["pages_edited"] += edited["pages"]
    stats["edit_rejected"] += edited["rejected"]
    new_pages = list(Page.objects.exclude(id__in=pages_before))
    stats["new_pages"] += len(new_pages)
    changed = list(Page.objects.filter(updated_at__gte=run.started_at) | Page.objects.filter(id__in=[p.id for p in new_pages]))
    try:
        from llmwiki.rag.index import index_pages
        index_pages([p.id for p in changed])
    except Exception:
        log.exception("indexing pages failed")
    try:
        from llmwiki.pipeline.tagger import run_tagger
        need = [p for p in changed if not p.tagged_at or p in new_pages]
        run_tagger(need)
    except Exception:
        log.exception("tagging failed")
    return {"units": len(results), "seconds": round(time.monotonic() - t0, 1), "edited": edited["pages"]}


def editor_phase(run: Run | None = None) -> dict:
    s = get_settings()
    if not s.pipeline.s3_enabled:
        return {"pages": 0, "rejected": 0}
    page_ids = list(Item.objects.filter(status="pending").values_list("page_id", flat=True).distinct())
    pages = list(Page.objects.filter(id__in=page_ids, archived=False))
    out = {"pages": 0, "rejected": 0}

    def one(p):
        close_old_connections()
        set_current_run(run.id if run else None)
        try:
            from llmwiki.pipeline.editor import edit_page
            items = list(Item.objects.filter(page=p, status="pending").order_by("created_at"))
            if not items:
                return {"page": p.id}
            j, _ = Job.objects.get_or_create(key=f"s3:{p.id}:{sha1(*[i.id for i in items])[:12]}", defaults={"stage": "s3", "unit": f"page:{p.id}", "run": run})
            j.status, j.started_at = "running", timezone.now()
            j.save(update_fields=["status", "started_at"])
            r = edit_page(p, items, job=j)
            rej = sum(len(x.get("rejected", [])) for x in r["requests"])
            _done(j, "done", {"requests": [{k: v for k, v in x.items() if k != "calls"} for x in r["requests"]]})
            return {"page": p.id, "rejected": rej}
        except Exception as e:
            log.exception("editor failed for page %s", p.id)
            return {"page": p.id, "error": str(e)}
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="edit") as ex:
        for r in ex.map(one, pages):
            out["pages"] += 1
            out["rejected"] += r.get("rejected", 0)
    close_old_connections()
    return out


def rewrite_pages(page_ids: list[int] | None = None, everything: bool = False) -> dict:
    """Rewrite pages to the free structure and their length budget (one editor request each, in parallel). Default: the
    pages that are over budget (by more than pages.tolerance) or not yet in the free structure; everything=True: all."""
    from llmwiki.pipeline.editor import rewrite_page
    from llmwiki.pipeline.pages import length_info
    s = get_settings()
    qs = Page.objects.filter(archived=False)
    if page_ids:
        qs = qs.filter(id__in=page_ids)
    todo = []
    for p in qs:
        if everything or page_ids:
            todo.append(p)
            continue
        info = length_info(p)
        if info["words"] > info["budget"] * (1 + s.pages.tolerance) or not p.sections.filter(key="summary").exclude(text="").exists():
            todo.append(p)
    event("editor", f"Rewriting {len(todo)} page(s) to the page budget", pages=len(todo))
    out = {"pages": len(todo), "accepted": 0, "rejected": 0, "failed": 0, "words_before": 0, "words_after": 0, "budget": 0}

    def one(p):
        close_old_connections()
        try:
            return rewrite_page(p)
        except Exception as e:
            log.exception("rewrite of page %s failed", p.id)
            return {"error": str(e)}
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="rewrite") as ex:
        for r in ex.map(one, todo):
            out["accepted"] += len(r.get("accepted", []))
            out["rejected"] += len(r.get("rejected", []))
            out["failed"] += 1 if "error" in r else 0
            if r.get("accepted"):
                out["words_before"] += r.get("words_before", 0)
                out["words_after"] += r.get("words_after", 0)
                out["budget"] += r.get("budget", 0)
    close_old_connections()
    try:
        from llmwiki.rag.index import index_pages
        index_pages([p.id for p in todo])
    except Exception:
        log.exception("indexing pages failed")
    event("editor", f"Rewrote {out['accepted']} of {len(todo)} pages: {out['words_before']} → {out['words_after']} words (budget {out['budget']})", **out)
    return out


def eta() -> dict:
    """Backlog ETA from the pending work and the endpoints' rpm and recent latency."""
    s = get_settings()
    waiting = pending_emails().count()
    units_est = max(1, int(waiting * 0.55)) if waiting else 0
    req_est = int(units_est * 1.6)
    eps = [e for e in s.pool.endpoints if e.enabled]
    st = {x["name"]: x for x in pool().status()}
    per_min = 0.0
    for e in eps:
        lat = (st.get(e.name, {}).get("avg_latency_ms") or 20000) / 1000
        per_min += min(e.rpm, e.max_concurrent * 60 / max(lat, 1))
    minutes = req_est / per_min if per_min else None
    return {"emails_waiting": waiting, "requests_est": req_est, "per_min": round(per_min, 1), "minutes": round(minutes, 1) if minutes else None,
            "items_pending": Item.objects.filter(status="pending").count()}
