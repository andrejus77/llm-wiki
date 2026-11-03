"""Parse a raw RFC 822 message (bytes) into a ParsedEmail. Every source (POP3, .pst, .eml) ends up here."""
from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from llmwiki.ingest.html2md import clean_text, html_to_text
from llmwiki.util import display_name, normalize_mid, sha1, split_mids

log = logging.getLogger(__name__)


def is_image_file(filename: str, content_type: str) -> bool:
    """By type, or by name when the type says nothing (the .pst route rebuilds every attachment as octet-stream)."""
    if content_type.startswith("image/"):
        return True
    return content_type in ("", "application/octet-stream") and (mimetypes.guess_type(filename or "")[0] or "").startswith("image/")


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    data: bytes
    inline: bool = False
    content_id: str = ""

    @property
    def is_image(self):
        return is_image_file(self.filename, self.content_type)


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    from_addr: str
    from_name: str
    to: list[str]
    cc: list[str]
    sent_at: datetime
    in_reply_to: str
    references: list[str]
    thread_index: str
    body_text: str
    has_table: bool
    headers: dict
    attachments: list[ParsedAttachment] = field(default_factory=list)
    raw: bytes = b""
    conversation_id: str = ""
    synthetic_mid: bool = False


def _addrs(values) -> list[str]:
    out = []
    for name, addr in email.utils.getaddresses([str(v) for v in values if v]):
        if addr and "@" in addr:
            out.append(addr.strip().lower())
    return out


def thread_index_guid(value: str) -> str:
    """Outlook Thread-Index: base64; bytes 6..22 (after a 6-byte FILETIME) are the conversation GUID."""
    try:
        raw = base64.b64decode(value.strip() + "==="[: (4 - len(value.strip()) % 4) % 4])
        return raw[6:22].hex() if len(raw) >= 22 else ""
    except Exception:
        return ""


def _decode_part(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        for cs in (part.get_content_charset(), "utf-8", "cp1252", "latin-1"):
            if not cs:
                continue
            try:
                return payload.decode(cs)
            except Exception:
                continue
        return payload.decode("utf-8", "replace")


def parse(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    hdr = lambda k: str(msg.get(k, "") or "")  # noqa: E731
    try:
        dt = email.utils.parsedate_to_datetime(hdr("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)
        log.warning("email without a valid Date header; using now (subject=%r)", hdr("Subject")[:80])
    frm = email.utils.parseaddr(hdr("From"))
    plain, html, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = (part.get_content_disposition() or "").lower()
        fname = part.get_filename()
        cid = (part.get("Content-ID") or "").strip("<> ")
        if ctype == "text/plain" and not fname and disp != "attachment":
            plain.append(_decode_part(part))
        elif ctype == "text/html" and not fname and disp != "attachment":
            html.append(_decode_part(part))
        elif ctype == "message/rfc822":
            inner = part.get_payload()
            data = inner[0].as_bytes() if isinstance(inner, list) and inner else bytes(part.get_payload(decode=True) or b"")
            atts.append(ParsedAttachment(fname or "attached-message.eml", ctype, data))
        else:
            data = part.get_payload(decode=True) or b""
            if not data:
                continue
            atts.append(ParsedAttachment(fname or (f"inline-{cid or len(atts)}." + ctype.split("/")[-1]), ctype, data,
                                         inline=disp == "inline" or bool(cid and not fname), content_id=cid))
    if html:
        body, has_table = html_to_text("\n".join(html))
    else:
        body = clean_text("\n".join(plain))
        has_table = bool(re.search(r"^\s*\|.+\|\s*$\n^\s*\|[-:| ]+\|", body, re.M))
    mid = normalize_mid(hdr("Message-ID"))
    synthetic = False
    if not mid:
        mid = "content-" + sha1(frm[1].lower(), hdr("Date"), hdr("Subject"), body[:2000])[:32]
        synthetic = True
    keep = {k: hdr(k) for k in ("Auto-Submitted", "Precedence", "List-Id", "X-Auto-Response-Suppress", "Thread-Topic",
                                "X-MS-Exchange-Generated-Message-Source", "Return-Path", "X-Mailer") if msg.get(k)}
    return ParsedEmail(
        message_id=mid, subject=re.sub(r"\s+", " ", hdr("Subject")).strip(), from_addr=frm[1].strip().lower(),
        from_name=display_name(frm[0], frm[1].strip().lower()), to=_addrs(msg.get_all("To", [])), cc=_addrs(msg.get_all("Cc", [])),
        sent_at=dt.astimezone(timezone.utc), in_reply_to=(split_mids(hdr("In-Reply-To")) or [""])[0],
        references=split_mids(hdr("References")), thread_index=hdr("Thread-Index").strip(), body_text=body,
        has_table=has_table, headers=keep, attachments=atts, raw=raw, synthetic_mid=synthetic)
