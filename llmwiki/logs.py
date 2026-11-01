"""Logging setup.

Three layers, each for a different reader:
1. Python logging → data/logs/llmwiki.log (everything, rotated) and data/logs/errors.log (WARNING+).
   For developers and agents fixing issues: every module logs with `log = logging.getLogger(__name__)`.
2. `LlmCall` rows: every model request with prompt sections, budgets, cuts, the raw response, status and timing.
3. `Event` rows: user-visible events (fetch results, runs, guard rejections, rule changes) shown on Agent › Logs.
   Use `llmwiki.logs.event(kind, message, level="info", **data)`.
"""
from __future__ import annotations

import logging
import threading
import traceback

log = logging.getLogger("llmwiki")


def django_logging_config(log_dir, level="INFO") -> dict:
    fmt = "%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"std": {"format": fmt}},
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "std", "level": level},
            "file": {"class": "logging.handlers.RotatingFileHandler", "filename": str(log_dir / "llmwiki.log"),
                     "maxBytes": 20 * 1024 * 1024, "backupCount": 10, "encoding": "utf-8", "formatter": "std", "level": "DEBUG"},
            "errors": {"class": "logging.handlers.RotatingFileHandler", "filename": str(log_dir / "errors.log"),
                       "maxBytes": 10 * 1024 * 1024, "backupCount": 5, "encoding": "utf-8", "formatter": "std", "level": "WARNING"},
        },
        "loggers": {
            "llmwiki": {"handlers": ["console", "file", "errors"], "level": "DEBUG", "propagate": False},
            "django": {"handlers": ["console", "file", "errors"], "level": "INFO", "propagate": False},
            "waitress": {"handlers": ["console", "file", "errors"], "level": "INFO", "propagate": False},
            "httpx": {"handlers": ["file"], "level": "WARNING", "propagate": False},
        },
        "root": {"handlers": ["console", "file", "errors"], "level": "WARNING"},
    }


_local = threading.local()


def current_run():
    return getattr(_local, "run_id", None)


def set_current_run(run_id):
    _local.run_id = run_id


def event(kind: str, message: str, level: str = "info", **data) -> None:
    """Record a user-visible event (Agent › Logs) and mirror it to the Python log. Never raises."""
    getattr(log, level if level in ("debug", "info", "warning", "error") else "info")("[%s] %s %s", kind, message, data or "")
    try:
        from llmwiki.models import Event
        Event.objects.create(kind=kind, level=level, message=message[:2000], data=_jsonable(data), run_id=current_run())
    except Exception:  # logging must never break the caller
        log.debug("event() could not write to the DB: %s", traceback.format_exc(limit=2))


def _jsonable(d):
    import json
    try:
        json.dumps(d)
        return d
    except TypeError:
        return {k: str(v) for k, v in d.items()}
