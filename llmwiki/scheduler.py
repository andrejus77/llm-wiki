"""Background scheduler, started by `manage.py serve` inside the server process.

- Nightly batch (P0, always): at schedule.batch_time → fetch → process(batch) → reviewer (if nightly) → DB backup.
  Runs once per day (KV 'last_batch_day').
- Morning reports: at reports.daily_time (07:00) the 'past 7 days' report texts of every department (KV
  'last_report_day'); on reports.weekly_weekday (Thursday) that report is the weekly one.
- Live mode (P1, toggle): every live_tick_minutes → fetch; only if new mail arrived → process(live); every
  live_lint_minutes the reviewer on pages changed since the last lint. After live_max_failed_ticks failures in a row it
  switches itself off (settings saved, event logged) and the nightly batch catches up.
- Manual actions from the portal run through `run_action()` in their own thread.
Chat priority is handled by the pool (background requests wait while anyone chats).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from django.conf import settings as dj
from django.db import close_old_connections, connection
from django.utils import timezone

from llmwiki.conf import get_settings, save_settings
from llmwiki.logs import event
from llmwiki.models import KV, Page

log = logging.getLogger(__name__)
_actions_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}


def local_now() -> datetime:
    from llmwiki.util import tz
    return datetime.now(tz(get_settings().sources.time_zone))


def backup_db():
    s = get_settings().store
    if s.backup_keep_days <= 0:
        return
    d = dj.DATA_DIR / "backups"
    d.mkdir(exist_ok=True)
    target = d / f"llmwiki-{date.today():%Y%m%d}.sqlite3"
    src = sqlite3.connect(dj.DATABASES["default"]["NAME"])
    dst = sqlite3.connect(str(target))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    cutoff = date.today() - timedelta(days=s.backup_keep_days)
    for f in d.glob("llmwiki-*.sqlite3"):
        try:
            if datetime.strptime(f.stem.split("-")[1], "%Y%m%d").date() < cutoff:
                f.unlink()
        except Exception:
            pass
    log.info("database backup written to %s", target)


def run_batch(force: bool = False):
    from llmwiki.pipeline.runner import process
    from llmwiki.sources.fetch import fetch
    s = get_settings()
    event("batch", "Nightly batch started")
    try:
        fetch()
    except Exception as e:
        event("batch", f"fetch failed: {e}", level="error")
    process("batch", review=(s.reviewer.schedule == "nightly"))
    try:
        backup_db()
    except Exception:
        log.exception("backup failed")


def morning_reports(day: date | None = None) -> dict:
    """The 'past 7 days' report of every department for today (on reports.weekly_weekday it is the weekly report)."""
    from llmwiki.reports.briefing import generate_texts
    d = day or local_now().date()
    out = generate_texts(f"d:{d.isoformat()}")
    event("report", f"{'Weekly' if d.weekday() == get_settings().reports.weekly_weekday else 'Daily'} reports for {d.isoformat()} written: "
          + ", ".join(f"{k.split('@')[0]} {v}" for k, v in (out.get("generated") or {}).items()), **{"day": d.isoformat()})
    if d.day == 1 and "monthly" in get_settings().reports.kinds:   # the month before is complete
        prev = (d - timedelta(days=1)).strftime("%Y-%m")
        m = generate_texts(f"m:{prev}")
        event("report", f"Monthly reports for {prev} written: " + ", ".join(f"{k.split('@')[0]} {v}" for k, v in (m.get("generated") or {}).items()))
        out["monthly"] = m
    return out


def live_tick():
    from llmwiki.pipeline.runner import process
    from llmwiki.sources.fetch import fetch
    s = get_settings().schedule
    st = fetch()
    if st.get("stored", 0) > 0:
        r = process("live")
        if r.get("unit_errors"):
            raise RuntimeError(f"{r['unit_errors']} unit errors")
    last_lint = KV.get("last_live_lint", 0)
    if time.time() - last_lint >= s.live_lint_minutes * 60:
        since = timezone.now() - timedelta(minutes=s.live_lint_minutes)
        changed = list(Page.objects.filter(updated_at__gte=since, archived=False))
        if changed:
            from llmwiki.pipeline.reviewer import run_review
            run_review(changed)
        KV.set("last_live_lint", time.time())


def run_action(name: str, **kw) -> bool:
    """Start a manual action in a background thread. Returns False if the same action is already running."""
    from llmwiki.pipeline import runner
    from llmwiki.sources.fetch import fetch
    actions = {
        "fetch": lambda: fetch(),
        "process": lambda: runner.process(kw.get("mode", "batch"), since_day=kw.get("since"), until_day=kw.get("until")),
        "batch": lambda: run_batch(force=True),
        "backfill": lambda: runner.process("backfill", since_day=kw.get("since"), until_day=kw.get("until")),
        "review": lambda: __import__("llmwiki.pipeline.reviewer", fromlist=["run_review"]).run_review(),
        "editor": lambda: runner.editor_phase(),
        "rewrite_pages": lambda: runner.rewrite_pages(everything=bool(kw.get("all"))),
        "reindex": lambda: _reindex(),
        "report": lambda: __import__("llmwiki.reports.briefing", fromlist=["generate_texts"]).generate_texts(kw.get("period"), kw.get("dept")),
        "backup": backup_db,
        "morning_reports": lambda: morning_reports(),
        "pst": lambda: fetch(["pst"], pst_since=_d(kw.get("since")), pst_until=_d(kw.get("until"))),
        "sync_vectors": lambda: __import__("llmwiki.rag.index", fromlist=["sync_vectors"]).sync_vectors(),
    }
    if name not in actions:
        raise ValueError(f"unknown action {name}")
    with _actions_lock:
        t = _running.get(name)
        if t and t.is_alive():
            return False

        def target():
            if name in ("process", "batch", "backfill", "review", "editor", "rewrite_pages", "report"):
                from llmwiki.llm.pool import pool
                pool().resume("a manual start")   # started by hand: try again
            close_old_connections()
            try:
                actions[name]()
            except Exception as e:
                log.exception("action %s failed", name)
                event("action", f"{name} failed: {e}", level="error")
            finally:
                connection.close()
        t = threading.Thread(target=target, name=f"action-{name}", daemon=True)
        _running[name] = t
        t.start()
    event("action", f"Started: {name}", **{k: str(v) for k, v in kw.items()})
    return True


def _d(s):
    from datetime import timezone as _tz
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_tz.utc) if s else None


def running_actions() -> list[str]:
    return [k for k, t in _running.items() if t.is_alive()]


def _reindex():
    from llmwiki.models import Email
    from llmwiki.rag.index import index_emails, index_pages
    index_emails(list(Email.objects.exclude(status__in=["duplicate", "skipped"]).values_list("id", flat=True)))
    index_pages(list(Page.objects.values_list("id", flat=True)))


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(name="scheduler", daemon=True)
        self.stop_flag = threading.Event()

    def run(self):
        log.info("scheduler started")
        while not self.stop_flag.wait(20):
            close_old_connections()
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            finally:
                connection.close()

    def tick(self):
        s = get_settings().schedule
        now = local_now()
        if time.time() - KV.get("last_vector_sync", 0) > 600:
            KV.set("last_vector_sync", time.time())
            try:
                from llmwiki.rag.index import sync_vectors
                sync_vectors()
            except Exception:
                log.exception("vector sync failed")
        if s.batch_enabled:
            hh, mm = map(int, s.batch_time.split(":"))
            today = now.date().isoformat()
            if (now.hour, now.minute) >= (hh, mm) and KV.get("last_batch_day") != today:
                KV.set("last_batch_day", today)
                run_batch()
        if s.report_auto:
            rh, rm = map(int, get_settings().reports.daily_time.split(":"))
            today = now.date().isoformat()
            if (now.hour, now.minute) >= (rh, rm) and KV.get("last_report_day") != today:
                KV.set("last_report_day", today)
                run_action("morning_reports")
        if s.live_enabled:
            last = KV.get("last_live_tick", 0)
            if time.time() - last >= s.live_tick_minutes * 60:
                KV.set("last_live_tick", time.time())
                try:
                    live_tick()
                    KV.set("live_failures", 0)
                except Exception as e:
                    n = (KV.get("live_failures", 0) or 0) + 1
                    KV.set("live_failures", n)
                    log.exception("live tick failed (%s in a row)", n)
                    event("live", f"Live tick failed ({n} in a row): {e}", level="warning")
                    if n >= s.live_max_failed_ticks:
                        cur = get_settings().model_copy(deep=True)
                        cur.schedule.live_enabled = False
                        save_settings(cur, user="scheduler", note=f"live mode switched off after {n} failed ticks")
                        event("live", f"Live mode switched itself off after {n} failed ticks; the nightly batch will catch up", level="error")


_scheduler: Scheduler | None = None


def start_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = Scheduler()
        _scheduler.start()
    return _scheduler
