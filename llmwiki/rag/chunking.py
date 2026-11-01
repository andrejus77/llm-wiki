"""Turn emails, notes, attachments and wiki sections into chunks with script-built context headers (no LLM)."""
from __future__ import annotations

import re

from llmwiki.conf import get_settings
from llmwiki.util import est_tokens, sha1

TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def _split_tables(text: str) -> list[tuple[str, bool]]:
    """Split into (block, is_table) keeping each markdown table whole."""
    out, cur, in_tab = [], [], False
    for ln in (text or "").split("\n"):
        t = bool(TABLE_LINE.match(ln))
        if t != in_tab and cur:
            out.append(("\n".join(cur), in_tab))
            cur = []
        in_tab = t
        cur.append(ln)
    if cur:
        out.append(("\n".join(cur), in_tab))
    return [(b.strip(), t) for b, t in out if b.strip()]


def _table_parts(table: str, max_tok: int) -> list[str]:
    lines = table.split("\n")
    head = lines[:2] if len(lines) > 2 and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[1]) else lines[:1]
    body = lines[len(head):]
    parts, cur = [], []
    for ln in body:
        if cur and est_tokens("\n".join(head + cur + [ln])) > max_tok:
            parts.append("\n".join(head + cur))
            cur = []
        cur.append(ln)
    if cur or not parts:
        parts.append("\n".join(head + cur))
    return parts


def split_text(text: str, max_tok: int, overlap: int) -> list[str]:
    """Paragraph-aware chunks of ≤ max_tok tokens with ~overlap tokens carried over; each table is its own chunk(s)."""
    chunks = []
    for block, is_table in _split_tables(text):
        if is_table:
            chunks += _table_parts(block, max_tok)
            continue
        paras = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
        cur = ""
        for p in paras:
            if est_tokens(p) > max_tok:  # very long paragraph → sentences
                sents = re.split(r"(?<=[.!?。])\s+", p)
            else:
                sents = [p]
            for s in sents:
                cand = (cur + "\n\n" + s).strip() if cur else s
                if cur and est_tokens(cand) > max_tok:
                    chunks.append(cur)
                    tail = cur[-overlap * 4:] if overlap else ""
                    cur = (tail.split(" ", 1)[-1] + "\n\n" + s).strip() if tail else s
                else:
                    cur = cand
        if cur:
            chunks.append(cur)
    return [c for c in chunks if c.strip()]


def email_chunks(e, tags_by_page: dict | None = None) -> list[dict]:
    r = get_settings().retrieval
    sender = e.from_name or e.from_addr
    pay = {"thread_id": e.thread_id or 0, "sender": e.from_addr, "kind": e.kind, "importance": e.importance, "depts": e.depts}
    hdr = f"{e.subject} · {sender} · {e.day}"
    out = []
    if e.note:
        out.append({"collection": "notes", "ref": f"note:{e.id}", "header": hdr, "text": e.note, "email": e, "day": e.day, "payload": pay})
    body = e.new_text
    unseen = [qb.text for qb in e.quoted_blocks.all() if qb.linked_id is None]
    if unseen:
        body += "\n\n" + "\n\n".join(f"(quoted earlier mail) {t}" for t in unseen)
    for i, t in enumerate(split_text(body, r.chunk_tokens, r.chunk_overlap)):
        out.append({"collection": "emails", "ref": f"email:{e.id}", "header": hdr, "text": t, "email": e, "day": e.day, "payload": {**pay, "part": i}})
    if r.index_attachments:
        for a in e.attachments.all():
            if a.extract:
                for i, t in enumerate(split_text(a.extract, r.chunk_tokens, r.chunk_overlap)[:20]):
                    out.append({"collection": "attachments", "ref": f"att:{a.id}", "header": f"{a.filename} in {e.subject} · {e.day}", "text": t,
                                "email": e, "day": e.day, "payload": {**pay, "att": a.id, "part": i}})
    return out


def page_chunks(page) -> list[dict]:
    from llmwiki.pipeline.templates import section_label
    r = get_settings().retrieval
    tg = page.tags or {}
    facets = ", ".join(v for k in ("system", "process") for v in tg.get(k, [])[:2])
    out = []
    for s in page.sections.all():
        if not s.text.strip():
            continue
        hdr = f"Page: {page.title} ({page.level}{'; ' + facets if facets else ''}) › {section_label(page.level, s.key, s.title)}"
        words = s.text.split()
        parts = [s.text] if len(words) <= r.section_max_words else split_text(s.text, int(r.section_max_words * 1.4), 40)
        day = (s.updated_at or page.updated_at).strftime("%Y-%m-%d") if (s.updated_at or page.updated_at) else ""
        for i, t in enumerate(parts):
            out.append({"collection": "wiki", "ref": f"page:{page.id}:{s.key}", "header": hdr, "text": t, "page": page, "day": day,
                        "payload": {"section": s.key, "part": i, "status": page.status, "level": page.level}})
    return out


def chunk_id(c: dict) -> str:
    return sha1(c["collection"], c["ref"], c["header"], c["text"])[:40]
