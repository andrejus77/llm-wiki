"""Incremental indexer. Chunk ids are content hashes: unchanged text is never re-embedded, removed text is deleted
from FTS5, the vector store and SQLite. Vectors are stored in ChunkVector (SQLite) and mirrored to the vector store."""
from __future__ import annotations

import logging
import threading

from django.db import transaction

from llmwiki.conf import get_settings
from llmwiki.models import Chunk, ChunkVector, Email, Page
from llmwiki.rag import chunking, fts
from llmwiki.rag.embed import get_embedder, to_blob
from llmwiki.rag.vstore import get_store, payload_for

log = logging.getLogger(__name__)
_lock = threading.Lock()   # one writer at a time (FTS + Qdrant local mode)


def _sync(ref_prefixes: list[str], wanted: list[dict]) -> tuple[int, int]:
    """Make the chunks under `ref_prefixes` exactly `wanted`. Returns (added, removed)."""
    want = {chunking.chunk_id(c): c for c in wanted}
    existing = {}
    for p in ref_prefixes:
        qs = Chunk.objects.filter(ref__startswith=p) if p.endswith(":") else Chunk.objects.filter(ref=p)
        for cid, pk in qs.values_list("cid", "id"):
            existing[cid] = pk
    stale = [pk for cid, pk in existing.items() if cid not in want]
    new = [c for cid, c in want.items() if cid not in existing]
    if stale:
        fts.delete(stale)
        Chunk.objects.filter(id__in=stale).delete()
        _store_delete(stale)
    rows = []
    for c in new:
        cid = chunking.chunk_id(c)
        ch = Chunk.objects.create(cid=cid, collection=c["collection"], ref=c["ref"], header=c["header"][:1000], text=c["text"],
                                  page=c.get("page"), email=c.get("email"), day=c.get("day", ""), payload=c.get("payload", {}))
        rows.append((ch.id, ch.header, ch.text))
    fts.insert(rows)
    return len(new), len(stale)


def _store_delete(ids):
    try:
        emb = get_embedder()
        get_store(emb.name, emb.dim).delete(ids)
    except Exception as e:
        log.warning("vector store delete failed: %s", e)


def embed_missing(model: str | None = None, limit: int | None = None) -> int:
    """Embed every chunk that has no vector for `model` (default: the active one) and push to the vector store."""
    emb = get_embedder(model)
    store = get_store(emb.name, emb.dim)
    qs = Chunk.objects.exclude(vectors__model=emb.name).order_by("id")
    if limit:
        qs = qs[:limit]
    todo = list(qs.only("id", "cid", "collection", "header", "text", "day", "page_id", "email_id", "payload"))
    done = 0
    for k in range(0, len(todo), 128):
        part = todo[k:k + 128]
        vecs = emb.embed([c.header + "\n" + c.text for c in part])
        with transaction.atomic():
            ChunkVector.objects.bulk_create([ChunkVector(chunk=c, model=emb.name, dim=emb.dim, vector=to_blob(v)) for c, v in zip(part, vecs)],
                                            ignore_conflicts=True)
        try:
            store.upsert([(c.id, v, payload_for(c)) for c, v in zip(part, vecs)])
        except Exception as e:
            log.error("vector store upsert failed (vectors are safe in SQLite): %s", e)
        done += len(part)
    if done:
        log.info("embedded %s chunks with %s", done, emb.name)
    return done


def index_emails(ids: list[int]) -> tuple[int, int]:
    added = removed = 0
    with _lock:
        for e in Email.objects.filter(id__in=ids).exclude(status__in=["duplicate", "skipped"]).prefetch_related("attachments", "quoted_blocks"):
            want = chunking.email_chunks(e)
            atts = [f"att:{a.id}" for a in e.attachments.all()]
            a, r = _sync([f"note:{e.id}", f"email:{e.id}"] + atts, want)
            added, removed = added + a, removed + r
        embed_missing()
    return added, removed


def remove_emails(ids: list[int]) -> int:
    """Take emails (their notes, text and attachments) out of the search index, e.g. when a rule now skips them."""
    pks: list[int] = []
    for k in range(0, len(ids), 500):
        pks += list(Chunk.objects.filter(email_id__in=ids[k:k + 500]).values_list("id", flat=True))
    if not pks:
        return 0
    with _lock:
        fts.delete(pks)
        for k in range(0, len(pks), 500):
            Chunk.objects.filter(id__in=pks[k:k + 500]).delete()
        _store_delete(pks)
    return len(pks)


def remove_pages(ids: list[int]) -> None:
    """Take archived pages out of the search index."""
    with _lock:
        for pid in ids:
            _sync([f"page:{pid}:"], [])


def index_pages(ids: list[int]) -> tuple[int, int]:
    added = removed = 0
    with _lock:
        for p in Page.objects.filter(id__in=ids).prefetch_related("sections"):
            if p.archived:
                a, r = _sync([f"page:{p.id}:"], [])
            else:
                a, r = _sync([f"page:{p.id}:"], chunking.page_chunks(p))
            added, removed = added + a, removed + r
        embed_missing()
    return added, removed


def rebuild_vectors(model: str | None = None):
    """Recreate the vector store from the vectors stored in SQLite (e.g. after deleting data/qdrant)."""
    emb = get_embedder(model)
    store = get_store(emb.name, emb.dim)
    qs = ChunkVector.objects.filter(model=emb.name).select_related("chunk")
    batch = []
    from llmwiki.rag.embed import from_blob
    n = 0
    for cv in qs.iterator(chunk_size=500):
        batch.append((cv.chunk_id, from_blob(cv.vector), payload_for(cv.chunk)))
        if len(batch) >= 256:
            store.upsert(batch)
            n += len(batch)
            batch = []
    if batch:
        store.upsert(batch)
        n += len(batch)
    log.info("rebuilt vector store for %s: %s vectors", emb.name, n)
    return n


def sync_vectors() -> int:
    """If the vector store has fewer points than SQLite has vectors (e.g. a CLI run wrote while the server held the
    Qdrant lock and fell back to SQLite), rebuild it from SQLite. Cheap no-op when in sync."""
    from llmwiki.rag.vstore import QdrantStore
    emb = get_embedder()
    store = get_store(emb.name, emb.dim)
    if not isinstance(store, QdrantStore):
        return 0
    n_sql = ChunkVector.objects.filter(model=emb.name).count()
    try:
        n_q = store.count()
    except Exception:
        return 0
    if n_q == n_sql:
        return 0
    log.warning("vector store out of sync (%s in Qdrant, %s in SQLite) — rebuilding", n_q, n_sql)
    with _lock:
        live = set(ChunkVector.objects.filter(model=emb.name).values_list("chunk_id", flat=True))
        try:
            ids, offset = [], None
            while True:
                with QdrantStore._io:
                    pts, offset = QdrantStore.client().scroll(store.coll, limit=1000, offset=offset, with_payload=False, with_vectors=False)
                ids += [int(p.id) for p in pts]
                if offset is None:
                    break
            stale = [i for i in ids if i not in live]
            if stale:
                store.delete(stale)
        except Exception as e:
            log.warning("could not prune stale points: %s", e)
        return rebuild_vectors()


def stats() -> dict:
    s = get_settings().retrieval
    from django.db.models import Count
    by = dict(Chunk.objects.values_list("collection").annotate(n=Count("id")))
    vec = dict(ChunkVector.objects.values_list("model").annotate(n=Count("id")))
    return {"chunks": by, "vectors": vec, "active_model": s.embed_model, "store": s.vector_store}
