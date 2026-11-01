"""Hybrid retrieval: BM25 (FTS5) + vectors, fused with weighted Reciprocal Rank Fusion and a recency boost.

    score(chunk) = w_source × Σ_retrievers 1 / (rrf_k + rank)  ×  (0.5 + 0.5 × 0.5^(age_days / half_life))

Ranks only (no score calibration between BM25 and cosine). Source weights implement "wiki first".
Fallbacks by setting: retrieval.mode = bm25 | vector.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from llmwiki.conf import get_settings
from llmwiki.models import Chunk
from llmwiki.rag import fts
from llmwiki.rag.embed import get_embedder
from llmwiki.rag.vstore import get_store

log = logging.getLogger(__name__)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    bm25_rank: int | None = None
    vec_rank: int | None = None
    cos: float | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self, text_chars: int = 600) -> dict:
        c = self.chunk
        return {"chunk_id": c.id, "collection": c.collection, "ref": c.ref, "header": c.header, "text": c.text[:text_chars],
                "page_id": c.page_id, "email_id": c.email_id, "day": c.day, "score": round(self.score, 5),
                "bm25_rank": self.bm25_rank, "vec_rank": self.vec_rank, "cos": None if self.cos is None else round(self.cos, 3)}


def weight(collection: str) -> float:
    r = get_settings().retrieval
    return {"wiki": r.w_wiki, "notes": r.w_notes, "emails": r.w_emails, "attachments": r.w_attachments}.get(collection, 0.5)


def hybrid(query: str, flt: dict | None = None, top_k: int | None = None, mode: str | None = None, weights: bool = True,
           recency: bool = True, top_n: int | None = None, rerank: bool | None = None, model: str | None = None) -> list[Hit]:
    r = get_settings().retrieval
    mode = mode or r.mode
    top_k = top_k or r.top_k
    top_n = top_n or r.top_n
    t0 = time.monotonic()
    bm, vec, cos = [], [], {}
    if mode in ("hybrid", "bm25"):
        bm = [cid for cid, _ in fts.search(query, top_n, flt)]
    if mode in ("hybrid", "vector"):
        try:
            emb = get_embedder(model)
            qv = emb.embed([query], query=True)[0]
            res = get_store(emb.name, emb.dim).search(qv, top_n, flt)
            vec = [cid for cid, _ in res]
            cos = dict(res)
        except Exception as e:
            log.error("vector search failed, using BM25 only: %s", e)
    scores: dict[int, float] = {}
    for rank, cid in enumerate(bm, 1):
        scores[cid] = scores.get(cid, 0) + 1.0 / (r.rrf_k + rank)
    for rank, cid in enumerate(vec, 1):
        scores[cid] = scores.get(cid, 0) + 1.0 / (r.rrf_k + rank)
    chunks = {c.id: c for c in Chunk.objects.filter(id__in=list(scores))}
    today = date.today()
    hits = []
    for cid, s in scores.items():
        c = chunks.get(cid)
        if not c:
            continue
        if weights:
            s *= weight(c.collection)
        if recency and r.recency_half_life_days and c.day:
            try:
                age = max(0, (today - date.fromisoformat(c.day)).days)
                s *= 0.5 + 0.5 * 0.5 ** (age / r.recency_half_life_days)
            except ValueError:
                pass
        hits.append(Hit(c, s, bm.index(cid) + 1 if cid in bm else None, vec.index(cid) + 1 if cid in vec else None, cos.get(cid)))
    hits.sort(key=lambda h: -h.score)
    if (r.rerank if rerank is None else rerank) and hits:
        try:
            from llmwiki.rag.rerank import rerank as rr
            hits = rr(query, hits[: max(top_k * 3, 20)])
        except Exception as e:
            log.warning("reranker failed, keeping the fused order: %s", e)
    log.debug("hybrid %r → %s hits (bm25 %s, vec %s) in %.0fms", query[:60], len(hits), len(bm), len(vec), (time.monotonic() - t0) * 1000)
    return hits[:top_k]


def candidate_pages(query: str, k: int, exclude_ids=None) -> list[dict]:
    """S2 router: wiki section hits grouped by page, best first. 'cos' = best vector similarity (new-page rule)."""
    flt = {"collections": ["wiki"]}
    if exclude_ids:
        flt["exclude_page_ids"] = list(exclude_ids)
    hits = hybrid(query, flt, top_k=k * 4, recency=False)
    pages: dict[int, dict] = {}
    for h in hits:
        pid = h.chunk.page_id
        if not pid:
            continue
        d = pages.setdefault(pid, {"page_id": pid, "score": 0.0, "cos": 0.0, "sections": []})
        d["score"] = max(d["score"], h.score)
        d["cos"] = max(d["cos"], h.cos or 0.0)
        d["sections"].append(h.chunk.ref.split(":")[-1])
    # make sure the best cosine is known even for pages that only came from BM25
    out = sorted(pages.values(), key=lambda d: -d["score"])[:k]
    return out


def best_cos(query: str) -> float:
    """Best vector similarity of the query to any wiki section (for the new-page threshold)."""
    try:
        emb = get_embedder()
        qv = emb.embed([query], query=True)[0]
        res = get_store(emb.name, emb.dim).search(qv, 1, {"collections": ["wiki"]})
        return res[0][1] if res else 0.0
    except Exception:
        return 0.0
