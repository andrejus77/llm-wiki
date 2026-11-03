"""Which picture of a mail belongs where. A mail's MIME parts hold every picture of its whole quoted history, so "has an
image attachment" says nothing; the body says it: `[image: cid:…]` stands exactly where the picture was written.

- place = body / signature / quoted: where its reference stands (`""`: nowhere, so it is a real attachment).
- is_signature: a signature or logo picture. Kept and reachable, but not shown with the mail. Decided by scripts:
  small, and either below the closing, a wide strip at the very end of the mail, or written in several threads.
"""
from __future__ import annotations

import io
import logging
import struct

from llmwiki.conf import get_settings
from llmwiki.models import Attachment
from llmwiki.util import IMAGE_TOKEN

log = logging.getLogger(__name__)


def size_of(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the file header: PNG, GIF, BMP, JPEG. None for anything else (EMF, TIFF, …)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if data[:2] == b"\xff\xd8":
            f = io.BytesIO(data[2:])
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                while b == b"\xff":
                    b = f.read(1)
                if not b:
                    return None
                if 0xC0 <= b[0] <= 0xCF and b[0] not in (0xC4, 0xC8, 0xCC):
                    f.read(3)
                    h, w = struct.unpack(">HH", f.read(4))
                    return w, h
                n = struct.unpack(">H", f.read(2))[0]
                f.seek(n - 2, 1)
    except (struct.error, IndexError):
        return None
    return None


def _ref(token_text: str) -> str:
    v = token_text[len("[image:"):-1].strip().lower()
    return v[4:] if v.startswith("cid:") else v


def refs(text: str) -> set[str]:
    return {_ref(m.group(0)) for m in IMAGE_TOKEN.finditer(text or "")}


def keys_of(content_id: str, filename: str) -> list[str]:
    """Most exact first. A .pst import has no Content-ID, but Outlook's cid starts with the file name."""
    cid = (content_id or "").strip().lower()
    out = [cid, cid.split("@")[0]] if cid else []
    if filename:
        out.append(filename.strip().lower())
    return out


def _referenced(a, wanted: set[str]) -> bool:
    short = {r.split("@")[0] for r in wanted}
    cid = (a.content_id or "").strip().lower()
    if cid:      # with a Content-ID nothing weaker counts: every reply has its own image001.png
        return cid in wanted
    return (a.filename or "").strip().lower() in short


def _at_end(text: str, a) -> bool:
    """Nothing but a line or two of short text follows its reference, and something was written before it."""
    last = None
    for m in IMAGE_TOKEN.finditer(text or ""):
        if _referenced(a, {_ref(m.group(0))}):
            last = m
    if not last or not IMAGE_TOKEN.sub("", text[:last.start()]).strip():
        return False
    after = [x.strip() for x in IMAGE_TOKEN.sub("", text[last.end():]).split("\n") if x.strip()]
    return len(after) <= 2 and all(len(x) < 60 for x in after)


def classify(e, quoted: str, atts: list | None = None) -> dict:
    """Set place, size and is_signature of an email's pictures, and its count of real attachments."""
    s = get_settings().store
    atts = list(e.attachments.all()) if atts is None else atts
    body, sig_refs, quoted_refs = refs(e.new_text), refs(e.signature), refs(quoted)
    out = {"body": 0, "quoted": 0, "signature": 0}
    for a in atts:
        if not a.is_image:
            continue
        if a.width is None and a.blob_id:
            try:
                wh = size_of(a.blob.get())
            except Exception:
                log.debug("could not read image %s", a.id, exc_info=True)
                wh = None
            if wh:
                a.width, a.height = wh
        a.place = "body" if _referenced(a, body) else "signature" if _referenced(a, sig_refs) else "quoted" if _referenced(a, quoted_refs) else ""
        known = a.width and a.height
        small = (a.height <= s.signature_image_max_height and a.width <= s.signature_image_max_width) if known else a.size <= 30 * 1024
        strip = known and s.signature_strip_height and a.height <= s.signature_strip_height and a.width >= 1.8 * a.height and a.size <= 80 * 1024
        same = Attachment.objects.filter(blob_id=a.blob_id).exclude(email_id=e.id) if a.blob_id else Attachment.objects.none()
        written = same.filter(place__in=("body", "signature")).values("email__thread_id").distinct().count() + (1 if a.place in ("body", "signature") else 0)
        a.is_signature = bool(
            (a.place == "signature" and small)
            or (a.place == "body" and strip and _at_end(e.new_text, a))
            or (a.place in ("body", "signature") and small and written >= s.signature_image_repeat)
            or (a.blob_id and same.filter(is_signature=True).exists()))
        a.save(update_fields=["is_image", "place", "is_signature", "width", "height"])
        if a.is_signature:
            same.filter(is_signature=False).update(is_signature=True)   # the same picture elsewhere, older mail included
            out["signature"] += 1
        elif a.place:
            out[a.place] = out.get(a.place, 0) + 1
    e.n_attachments = sum(1 for a in atts if a.is_file)
    return out


def image_map(atts) -> dict:
    """Reference → picture, for the portal. A signature picture is left out: its reference is removed from the text."""
    out: dict[str, dict] = {}
    for a in atts:
        if a.is_image and not a.is_signature:
            for k in keys_of(a.content_id, a.filename):
                out.setdefault(k, {"id": a.id, "name": a.filename})
    return out


def visible_text(text: str, atts) -> str:
    """The text as it is shown: without the references of signature pictures."""
    hidden = {k for a in atts if a.is_image and a.is_signature for k in keys_of(a.content_id, a.filename)}
    if not hidden or "[image:" not in (text or ""):
        return text

    def repl(m):
        r = _ref(m.group(0))
        return "" if r in hidden or r.split("@")[0] in hidden else m.group(0)
    lines = [IMAGE_TOKEN.sub(repl, ln) for ln in text.split("\n")]
    return "\n".join(ln.rstrip() for ln in lines).strip()
