"""Keyword search with SQLite FTS5 (BM25).

Two indexes over the same chunks (rowid = Chunk.id):
- chunk_fts      unicode61 tokenizer, '-_/.' kept inside tokens so VL02N, /SCWM/MON and 4500123 match exactly
- chunk_fts_tri  trigram tokenizer, used for Hangul/CJK queries (Korean has no spaces between particles and words)
Both are created by migration 0002.
"""
from __future__ import annotations

import logging
import re

from django.db import connection

from llmwiki.conf import get_settings
from llmwiki.util import CJK

log = logging.getLogger(__name__)
STOP = set("the a an and or of to in on for is are was were be been with at by from this that it as what who when where why how do does did "
           "can could should would i we you they he she my our your about which there their any all into not no yes".split())


def insert(rows: list[tuple[int, str, str]]):
    """rows: (chunk_id, header, text)"""
    if not rows:
        return
    with connection.cursor() as c:
        c.executemany("INSERT INTO chunk_fts(rowid, header, body) VALUES (%s, %s, %s)", rows)
        if get_settings().retrieval.fts_trigram:
            c.executemany("INSERT INTO chunk_fts_tri(rowid, body) VALUES (%s, %s)", [(r[0], r[1] + "\n" + r[2]) for r in rows])


def delete(ids: list[int]):
    if not ids:
        return
    with connection.cursor() as c:
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            ph = ",".join(["%s"] * len(part))
            c.execute(f"DELETE FROM chunk_fts WHERE rowid IN ({ph})", part)
            c.execute(f"DELETE FROM chunk_fts_tri WHERE rowid IN ({ph})", part)


def _terms(q: str) -> list[str]:
    toks = re.findall(r"[/\w][\w\-/.]*[\w]|\w", q or "", re.U)
    out = []
    for t in toks:
        tl = t.lower().strip(".")
        if len(tl) < 2 or tl in STOP:
            continue
        out.append(tl)
    return list(dict.fromkeys(out))[:24]


def _where(flt: dict | None) -> tuple[str, list]:
    f = flt or {}
    cl, args = [], []
    if f.get("collections"):
        cl.append("ch.collection IN (" + ",".join(["%s"] * len(f["collections"])) + ")")
        args += list(f["collections"])
    if f.get("day_from"):
        cl.append("ch.day >= %s")
        args.append(f["day_from"])
    if f.get("day_to"):
        cl.append("ch.day <= %s")
        args.append(f["day_to"])
    for col, key in (("ch.page_id", "page_ids"), ("ch.email_id", "email_ids")):
        if f.get(key) is not None:
            vals = list(f[key]) or [-1]
            cl.append(f"{col} IN (" + ",".join(["%s"] * len(vals)) + ")")
            args += vals
    if f.get("exclude_page_ids"):
        vals = list(f["exclude_page_ids"])
        cl.append("(ch.page_id IS NULL OR ch.page_id NOT IN (" + ",".join(["%s"] * len(vals)) + "))")
        args += vals
    if f.get("thread_ids") is not None:
        vals = [int(x) for x in f["thread_ids"]] or [-1]
        cl.append("CAST(json_extract(ch.payload, '$.thread_id') AS INTEGER) IN (" + ",".join(["%s"] * len(vals)) + ")")
        args += vals
    return (" AND " + " AND ".join(cl)) if cl else "", args


def search(q: str, top_n: int, flt: dict | None = None) -> list[tuple[int, float]]:
    """Return [(chunk_id, bm25_score)] best first (higher = better)."""
    where, args = _where(flt)
    out: dict[int, float] = {}
    terms = _terms(q)
    if terms:
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = (f"SELECT f.rowid, bm25(chunk_fts, 0.5, 1.0) AS s FROM chunk_fts f JOIN llmwiki_chunk ch ON ch.id = f.rowid "
               f"WHERE chunk_fts MATCH %s{where} ORDER BY s LIMIT %s")
        try:
            with connection.cursor() as c:
                c.execute(sql, [match] + args + [top_n])
                for rid, s in c.fetchall():
                    out[rid] = -float(s)
        except Exception as e:
            log.warning("FTS query failed (%s): %r", e, match)
    if get_settings().retrieval.fts_trigram and CJK.search(q or ""):
        grams = [g for g in re.findall(r"[\w가-힣]{3,}", q) if CJK.search(g)]
        if grams:
            match = " OR ".join('"' + g.replace('"', '""') + '"' for g in grams[:12])
            sql = (f"SELECT f.rowid, bm25(chunk_fts_tri) AS s FROM chunk_fts_tri f JOIN llmwiki_chunk ch ON ch.id = f.rowid "
                   f"WHERE chunk_fts_tri MATCH %s{where} ORDER BY s LIMIT %s")
            try:
                with connection.cursor() as c:
                    c.execute(sql, [match] + args + [top_n])
                    for rid, s in c.fetchall():
                        out[rid] = max(out.get(rid, -1e9), -float(s))
            except Exception as e:
                log.warning("trigram FTS query failed (%s)", e)
    return sorted(out.items(), key=lambda x: -x[1])[:top_n]
