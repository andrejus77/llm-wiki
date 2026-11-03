"""Vector stores. The vectors themselves are rows in SQLite (ChunkVector); the store is an index over them.

- QdrantStore: Qdrant local mode (data/qdrant) or a Qdrant server (retrieval.qdrant_url). Rebuildable any time
  with `manage.py index --rebuild-vectors`.
- SqliteStore: brute force with numpy over ChunkVector rows (the one-line fallback: retrieval.vector_store=sqlite).
- LaggingStore: Qdrant local mode locks its folder to ONE process. Whoever comes second (a command while the portal
  runs, or the portal while a command runs) searches the same vectors in SQLite with the same results, and its writes
  are noted in KV 'vectors_behind'; the process holding Qdrant catches up in index.sync_vectors.

Filters (both stores): collections, day_from/day_to, page_ids, email_ids, thread_ids.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

import numpy as np

from llmwiki.conf import data_dir, get_settings
from llmwiki.rag.embed import from_blob

log = logging.getLogger(__name__)


def _daynum(day: str) -> int:
    return int(day.replace("-", "")) if day else 0


def payload_for(ch) -> dict:
    return {"cid": ch.cid, "collection": ch.collection, "day": _daynum(ch.day), "page_id": ch.page_id or 0,
            "email_id": ch.email_id or 0, "thread_id": (ch.payload or {}).get("thread_id", 0)}


class SqliteStore:
    _lock = threading.Lock()
    _mat: dict[str, tuple] = {}   # model -> (ids array, matrix, meta dict id->payload, version)

    def __init__(self, model: str):
        self.model = model

    def _load(self):
        from llmwiki.models import ChunkVector
        ver = ChunkVector.objects.filter(model=self.model).count(), (ChunkVector.objects.filter(model=self.model).order_by("-id").values_list("id", flat=True).first() or 0)
        with self._lock:
            cached = self._mat.get(self.model)
            if cached and cached[3] == ver:
                return cached
            rows = list(ChunkVector.objects.filter(model=self.model).select_related("chunk").values_list(
                "chunk_id", "vector", "chunk__collection", "chunk__day", "chunk__page_id", "chunk__email_id", "chunk__payload"))
            if not rows:
                c = (np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32), {}, ver)
            else:
                ids = np.array([r[0] for r in rows], dtype=np.int64)
                mat = np.stack([from_blob(r[1]) for r in rows])
                meta = {r[0]: {"collection": r[2], "day": _daynum(r[3]), "page_id": r[4] or 0, "email_id": r[5] or 0,
                               "thread_id": (r[6] or {}).get("thread_id", 0)} for r in rows}
                c = (ids, mat, meta, ver)
            self._mat[self.model] = c
            return c

    def upsert(self, items):  # vectors are already in SQLite; just invalidate
        with self._lock:
            self._mat.pop(self.model, None)

    def delete(self, chunk_ids):
        with self._lock:
            self._mat.pop(self.model, None)

    def search(self, vec: np.ndarray, top_n: int, flt: dict | None = None) -> list[tuple[int, float]]:
        ids, mat, meta, _ = self._load()
        if not len(ids):
            return []
        sims = mat @ vec.astype(np.float32)
        if flt:
            ok = np.array([_match(meta[i], flt) for i in ids.tolist()], dtype=bool)
            sims = np.where(ok, sims, -9.0)
        n = min(top_n, len(ids))
        idx = np.argpartition(-sims, n - 1)[:n]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(ids[i]), float(sims[i])) for i in idx if sims[i] > -9.0]


def _match(m: dict, f: dict) -> bool:
    if f.get("collections") and m["collection"] not in f["collections"]:
        return False
    if f.get("day_from") and m["day"] < _daynum(f["day_from"]):
        return False
    if f.get("day_to") and m["day"] > _daynum(f["day_to"]):
        return False
    if f.get("page_ids") is not None and m["page_id"] not in f["page_ids"]:
        return False
    if f.get("exclude_page_ids") and m["page_id"] in f["exclude_page_ids"]:
        return False
    if f.get("email_ids") is not None and m["email_id"] not in f["email_ids"]:
        return False
    if f.get("thread_ids") is not None and m["thread_id"] not in f["thread_ids"]:
        return False
    return True


class LaggingStore(SqliteStore):
    """Qdrant's folder is held by another process: read from SQLite, and tell the holder that it has to catch up."""

    def upsert(self, items):
        super().upsert(items)
        _note_behind()

    def delete(self, chunk_ids):
        super().delete(chunk_ids)
        if chunk_ids:
            _note_behind()


def _note_behind():
    from llmwiki.models import KV
    try:
        KV.set("vectors_behind", True)
    except Exception:
        log.warning("could not note that the vector index is behind", exc_info=True)


class QdrantBusy(Exception):
    pass


def _holder_file():
    return data_dir() / "qdrant.holder.json"


def holder() -> dict:
    """Who opened the Qdrant folder last: {pid, command, since}. Only informative: the lock itself is Qdrant's."""
    try:
        return json.loads(_holder_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


class QdrantStore:
    _client = None
    _lock = threading.Lock()
    _io = threading.RLock()   # Qdrant local mode is not thread-safe: serialise every call
    _busy_until = 0.0         # don't try the locked folder on every search

    def __init__(self, model: str, dim: int):
        self.model, self.dim = model, dim
        self.coll = "chunks_" + model.replace(".", "_").replace("-", "_")
        self._ensure()

    @classmethod
    def client(cls):
        with cls._lock:
            if cls._client is None:
                from qdrant_client import QdrantClient
                url = get_settings().retrieval.qdrant_url
                if url:
                    cls._client = QdrantClient(url=url, timeout=30)
                else:
                    if time.monotonic() < cls._busy_until:
                        raise QdrantBusy()
                    p = data_dir() / "qdrant"
                    p.mkdir(exist_ok=True)
                    try:
                        cls._client = QdrantClient(path=str(p))
                    except RuntimeError as e:
                        if "already accessed" not in str(e):
                            raise
                        cls._busy_until = time.monotonic() + 60
                        raise QdrantBusy() from None
                    try:
                        _holder_file().write_text(json.dumps({"pid": os.getpid(), "command": " ".join(sys.argv[1:3]), "since": time.strftime("%Y-%m-%d %H:%M")}), encoding="utf-8")
                    except OSError:
                        pass
                import atexit
                atexit.register(cls.close)   # close cleanly (avoids noisy __del__ tracebacks at interpreter exit)
            return cls._client

    @classmethod
    def close(cls):
        with cls._lock:
            if cls._client is not None:
                try:
                    cls._client.close()
                except Exception:
                    pass
                cls._client = None

    def _ensure(self):
        from qdrant_client.models import Distance, VectorParams
        c = self.client()
        with self._io:
            if not c.collection_exists(self.coll):
                c.create_collection(self.coll, vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE))
                log.info("created Qdrant collection %s", self.coll)

    def count(self) -> int:
        with self._io:
            return self.client().count(self.coll).count

    def upsert(self, items):
        """items: [(chunk_id, vector, payload)]"""
        from qdrant_client.models import PointStruct
        pts = [PointStruct(id=int(i), vector=np.asarray(v, dtype=np.float32).tolist(), payload=p) for i, v, p in items]
        with self._io:
            for k in range(0, len(pts), 256):
                self.client().upsert(self.coll, points=pts[k:k + 256], wait=True)

    def delete(self, chunk_ids):
        from qdrant_client.models import PointIdsList
        if chunk_ids:
            with self._io:
                self.client().delete(self.coll, points_selector=PointIdsList(points=[int(i) for i in chunk_ids]))

    def search(self, vec, top_n, flt=None):
        from qdrant_client.models import FieldCondition, Filter, MatchAny, Range
        must, must_not = [], []
        f = flt or {}
        if f.get("collections"):
            must.append(FieldCondition(key="collection", match=MatchAny(any=list(f["collections"]))))
        if f.get("day_from") or f.get("day_to"):
            must.append(FieldCondition(key="day", range=Range(gte=_daynum(f.get("day_from", "")) or None, lte=_daynum(f.get("day_to", "")) or None)))
        for key, fk in (("page_id", "page_ids"), ("email_id", "email_ids"), ("thread_id", "thread_ids")):
            if f.get(fk) is not None:
                must.append(FieldCondition(key=key, match=MatchAny(any=[int(x) for x in f[fk]] or [-1])))
        if f.get("exclude_page_ids"):
            must_not.append(FieldCondition(key="page_id", match=MatchAny(any=[int(x) for x in f["exclude_page_ids"]])))
        qf = Filter(must=must or None, must_not=must_not or None) if (must or must_not) else None
        with self._io:
            res = self.client().query_points(self.coll, query=np.asarray(vec, dtype=np.float32).tolist(), query_filter=qf, limit=top_n, with_payload=False)
        return [(int(p.id), float(p.score)) for p in res.points]


_said_busy = False


def get_store(model: str, dim: int):
    global _said_busy
    if get_settings().retrieval.vector_store == "qdrant":
        try:
            return QdrantStore(model, dim)
        except QdrantBusy:
            if not _said_busy:
                _said_busy = True
                h = holder()
                log.info("The Qdrant folder is in use by another process (%s). This one uses the same vectors from SQLite, with the same results; "
                         "the index catches up by itself.", f"pid {h.get('pid')}, {h.get('command') or 'a command'}, since {h.get('since')}" if h else "unknown")
        except Exception as e:
            log.error("Qdrant unavailable (%s) — falling back to the SQLite vector store", e)
        return LaggingStore(model)
    return SqliteStore(model)
