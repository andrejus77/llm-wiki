"""Move the processed data from one machine to another (work laptop → server).

The whole state is one SQLite file (emails, attachments, notes, pages, history, vectors, settings, logs), so a data
package is a consistent online copy of that file (sqlite3 backup API, safe while the server runs) plus a manifest.
Left out on purpose: settings.yaml (machine-specific: endpoints, paths, server), secret.txt, logs, the Qdrant folder
(rebuilt from the vectors in SQLite) and the model files (they come with the offline package).
company_adapter.py is included when asked: the server calls the same company APIs.

    export_package(out_zip)  → zip with data/llmwiki.sqlite3 (+ data/company_adapter.py) + MANIFEST.json
    import_package(zip)      → replaces data/llmwiki.sqlite3 (the old one is kept in data/backups), migrates,
                               embeds what is missing for this machine's model and rebuilds the vector index
"""
from __future__ import annotations

import json
import logging
import platform
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
DB_NAME = "llmwiki.sqlite3"


def _db_path() -> Path:
    from django.conf import settings as dj
    return Path(dj.DATABASES["default"]["NAME"])


def manifest() -> dict:
    from django.db.models import Count
    from llmwiki.models import Email, LlmCall, Page, Run
    last = Run.objects.filter(status="done").exclude(kind="fetch").order_by("-id").first()
    return {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "machine": platform.node(),
            "emails": Email.objects.count(), "by_status": dict(Email.objects.values_list("status").annotate(n=Count("id"))),
            "pages": Page.objects.count(), "llm_calls": LlmCall.objects.count(),
            "latest_mail": str(Email.objects.order_by("-sent_at").values_list("sent_at", flat=True).first() or ""),
            "last_run": {"kind": last.kind, "finished": str(last.finished_at), "stats": last.stats} if last else None}


def export_package(out_zip: str | Path, include_adapter: bool = True) -> dict:
    from llmwiki.conf import data_dir
    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="llmwiki-export-"))
    try:
        copy = tmp / DB_NAME
        src = sqlite3.connect(str(_db_path()))
        dst = sqlite3.connect(str(copy))
        with dst:
            src.backup(dst)                     # consistent copy, even while the server writes
        src.close()
        dst.close()
        man = manifest()
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(copy, f"data/{DB_NAME}")
            ad = data_dir() / "company_adapter.py"
            if include_adapter and ad.exists():
                z.write(ad, "data/company_adapter.py")
                man["company_adapter"] = True
            z.writestr("MANIFEST.json", json.dumps(man, indent=1, default=str))
        log.info("data package written to %s (%s emails, %s pages)", out_zip, man["emails"], man["pages"])
        from llmwiki.logs import event
        event("transfer", f"Data package exported: {out_zip.name} ({man['emails']} emails, {man['pages']} pages)", path=str(out_zip))
        return man
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_package(src: str | Path, force: bool = False, reindex: bool = True) -> dict:
    """Install a data package into this machine's data folder. Refuses to overwrite a store with emails unless force."""
    from django.core.management import call_command
    from django.db import connections
    from llmwiki.conf import data_dir, invalidate
    src = Path(src)
    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        if f"data/{DB_NAME}" not in names:
            raise ValueError(f"{src} is not a data package (no data/{DB_NAME})")
        man = json.loads(z.read("MANIFEST.json")) if "MANIFEST.json" in names else {}
        db = _db_path()
        existing = 0
        if db.exists():
            try:
                c = sqlite3.connect(str(db))
                existing = c.execute("select count(*) from llmwiki_email").fetchone()[0]
                c.close()
            except sqlite3.Error:
                existing = 0
        if existing and not force:
            raise RuntimeError(f"this machine already has {existing} emails in {db}; use --force to replace them (a copy is kept in data/backups)")
        connections.close_all()
        if db.exists():
            bdir = data_dir() / "backups"
            bdir.mkdir(exist_ok=True)
            keep = bdir / f"before-import-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
            shutil.copy2(db, keep)
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db) + suffix)
                if p.exists():
                    p.unlink()
            log.info("previous database kept as %s", keep)
        with z.open(f"data/{DB_NAME}") as fsrc, open(db, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, 1 << 20)
        ad = data_dir() / "company_adapter.py"
        if "data/company_adapter.py" in names and not ad.exists():
            ad.write_bytes(z.read("data/company_adapter.py"))
    qd = data_dir() / "qdrant"
    if qd.exists():
        shutil.rmtree(qd, ignore_errors=True)     # rebuilt from the vectors in SQLite
    call_command("migrate", verbosity=0)
    invalidate()
    out = {"manifest": man, "replaced_emails": existing}
    if reindex:
        from llmwiki.rag.index import embed_missing, rebuild_vectors
        out["embedded"] = embed_missing()
        out["vectors"] = rebuild_vectors()
    from llmwiki.logs import event
    event("transfer", f"Data package imported from {src.name}: {man.get('emails', '?')} emails, {man.get('pages', '?')} pages "
          f"(exported {man.get('created', '?')} on {man.get('machine', '?')})", **{k: v for k, v in out.items() if k != "manifest"})
    return out
