"""Text extracts from attachments (xlsx, pdf, docx, pptx, csv/txt, attached emails). Runs in Python on the server,
which is also what the company DRM allows. Every extractor is isolated: a failure leaves an empty extract and a reason."""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)


def _xlsx(data: bytes, limit: int) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets[:6]:
        rows = []
        for r in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).replace("|", "/").strip() for v in r]
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                rows.append(cells)
            if len(rows) >= 60:
                break
        if not rows:
            continue
        w = max(len(r) for r in rows)
        rows = [r + [""] * (w - len(r)) for r in rows]
        out.append(f"Sheet {ws.title}:\n| " + " | ".join(rows[0]) + " |\n|" + "---|" * w + "\n" + "\n".join("| " + " | ".join(r) + " |" for r in rows[1:]))
        if sum(len(x) for x in out) > limit:
            break
    return "\n\n".join(out)


def _pdf(data: bytes, limit: int) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    out = []
    for p in r.pages[:20]:
        out.append(p.extract_text() or "")
        if sum(len(x) for x in out) > limit:
            break
    return "\n".join(out)


def _docx(data: bytes, limit: int) -> str:
    import docx
    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables[:5]:
        rows = [[c.text.strip().replace("|", "/") for c in row.cells] for row in t.rows]
        if rows:
            w = len(rows[0])
            parts.append("| " + " | ".join(rows[0]) + " |\n|" + "---|" * w + "\n" + "\n".join("| " + " | ".join(r) + " |" for r in rows[1:]))
    return "\n".join(parts)


def _pptx(data: bytes, limit: int) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(data))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
        if texts:
            out.append(f"Slide {i}: " + " / ".join(texts))
    return "\n".join(out)


def _eml(data: bytes, limit: int) -> str:
    from llmwiki.ingest.mime import parse
    p = parse(data)
    return f"Attached email: {p.subject}\nFrom: {p.from_addr} · {p.sent_at:%Y-%m-%d}\n{p.body_text}"


def _text(data: bytes, limit: int) -> str:
    for cs in ("utf-8", "cp1252", "euc-kr", "latin-1"):
        try:
            return data.decode(cs)
        except Exception:
            continue
    return ""


def extract(filename: str, content_type: str, data: bytes, limit: int) -> tuple[str, str]:
    """Return (text, error)."""
    name = (filename or "").lower()
    fn = None
    if name.endswith((".xlsx", ".xlsm")):
        fn = _xlsx
    elif name.endswith(".pdf") or content_type == "application/pdf":
        fn = _pdf
    elif name.endswith(".docx"):
        fn = _docx
    elif name.endswith(".pptx"):
        fn = _pptx
    elif name.endswith(".eml") or content_type == "message/rfc822":
        fn = _eml
    elif name.endswith((".txt", ".csv", ".log", ".md")) or content_type.startswith("text/"):
        fn = _text
    if fn is None or limit <= 0:
        return "", ""
    try:
        t = fn(data, limit) or ""
        return t[:limit], ""
    except Exception as e:  # isolated: one bad file never stops the ingest
        log.warning("extract failed for %s: %s", filename, e)
        return "", f"{type(e).__name__}: {e}"[:400]
