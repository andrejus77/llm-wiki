"""Outlook .pst import (the 24 GB archive). One message at a time, never the whole file in memory. Resumable:
already-stored messages are recognised by Message-ID, and the last imported folder/entry is kept in KV.

Route 1 (preferred): Outlook COM via pywin32 — classic Outlook must be installed. The .pst is attached to the
profile (AddStore) if needed. Raw headers come from PR_TRANSPORT_MESSAGE_HEADERS, so threading still works.
Route 2: pypff (libpff-python) reads the file directly, if its wheel is installed.

Both routes rebuild an RFC 822 message (headers + text/html body + attachments) and hand it to store.add().

The .pst is the user's PERSONAL mailbox: only messages with a department address in To/Cc are imported
(sources.pst_dept_only; is_dept_mail()). Internal Exchange mail often has no transport headers and shows recipients
as display names / X.500 addresses, so the COM route resolves every recipient to its SMTP address and reads
Message-ID / In-Reply-To / References from MAPI properties; dept_aliases catches display names that cannot be resolved.
"""
from __future__ import annotations

import email.policy
import logging
import os
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage

log = logging.getLogger(__name__)
PR_TRANSPORT_MESSAGE_HEADERS = "http://schemas.microsoft.com/mapi/proptag/0x007D001F"
PR_ATTACH_DATA_BIN = "http://schemas.microsoft.com/mapi/proptag/0x37010102"
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
PR_IN_REPLY_TO_ID = "http://schemas.microsoft.com/mapi/proptag/0x1042001F"
PR_INTERNET_REFERENCES = "http://schemas.microsoft.com/mapi/proptag/0x1039001F"
OL_TO, OL_CC = 1, 2


def dept_matchers() -> tuple[set[str], set[str]]:
    """(department addresses, lower-case display-name aliases) from the settings."""
    from llmwiki.conf import get_settings
    s = get_settings().sources
    addrs = {a.lower() for a in s.dept_addresses}
    names = {n.lower().strip() for a, ns in s.dept_aliases.items() for n in ns if n.strip()}
    names |= {a.split("@")[0] for a in addrs}          # 'dl-ct' as a display name
    return addrs, names


def is_dept_mail(recipients: list[tuple[str, str]], sender: tuple[str, str] | None = None) -> bool:
    """recipients: (name, smtp address) of To + Cc. True if one is a department address (or a known alias)."""
    from llmwiki.conf import get_settings
    s = get_settings().sources
    if not s.pst_dept_only:
        return True
    addrs, names = dept_matchers()
    people = list(recipients) + ([sender] if (sender and s.pst_match_from) else [])
    for name, addr in people:
        if (addr or "").lower() in addrs or (name or "").lower().strip().strip("'\"") in names:
            return True
    return False


def dept_from_headers(raw: bytes) -> bool:
    """Department check on a rebuilt/raw message (pypff route, or any message whose headers are complete)."""
    import email.utils
    from email.parser import BytesHeaderParser
    h = BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
    rec = email.utils.getaddresses([str(v) for k in ("to", "cc") for v in (h.get_all(k) or [])])
    snd = email.utils.parseaddr(str(h.get("from") or ""))
    return is_dept_mail([(n, a) for n, a in rec], (snd[0], snd[1]))


def build_rfc822(headers_text: str, subject: str, sender: str, to: str, cc: str, sent: datetime, html: str, text: str,
                 attachments: list[tuple[str, str, bytes]], message_id: str = "", conversation_index: str = "",
                 in_reply_to: str = "", references: str = "") -> bytes:
    """Rebuild a message. Original transport headers win where present (Message-ID, References, Thread-Index...)."""
    msg = EmailMessage(policy=email.policy.SMTP)
    keep = {}
    for line in (headers_text or "").replace("\r\n\t", " ").replace("\r\n ", " ").replace("\n\t", " ").replace("\n ", " ").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            if k.lower() in ("message-id", "in-reply-to", "references", "thread-index", "thread-topic", "auto-submitted",
                             "precedence", "list-id", "date", "from", "to", "cc", "subject"):
                keep.setdefault(k.lower(), (k, v.strip()))
    def put(name, value):
        if value:
            try:
                msg[name] = value
            except Exception:
                msg[name] = value.encode("ascii", "replace").decode()
    put("Message-ID", keep.get("message-id", ("", message_id))[1] or message_id)
    put("Date", keep.get("date", ("", ""))[1] or sent.strftime("%a, %d %b %Y %H:%M:%S %z"))
    put("From", keep.get("from", ("", ""))[1] or sender)
    put("To", keep.get("to", ("", ""))[1] or to)
    put("Cc", keep.get("cc", ("", ""))[1] or cc)
    put("Subject", subject or keep.get("subject", ("", ""))[1])
    for h in ("in-reply-to", "references", "thread-index", "thread-topic", "auto-submitted", "precedence", "list-id"):
        if h in keep:
            put(keep[h][0], keep[h][1])
    if in_reply_to and "in-reply-to" not in keep:
        put("In-Reply-To", in_reply_to)
    if references and "references" not in keep:
        put("References", references)
    if conversation_index and "thread-index" not in keep:
        put("Thread-Index", conversation_index)
    if html:
        msg.set_content(text or " ")
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text or "")
    for name, ctype, data in attachments:
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
    return msg.as_bytes()


# ------------------------------------------------------------------ Outlook COM
def _aware(dt) -> datetime | None:
    """pywintypes datetimes may be naive local time or carry a tzinfo; return an aware UTC datetime."""
    if dt is None:
        return None
    try:
        d = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, tzinfo=getattr(dt, "tzinfo", None))
        if d.tzinfo is None:
            d = d.astimezone()   # naive = the PC's local time
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def com_available() -> bool:
    try:
        import win32com.client  # noqa: F401
        import pythoncom  # noqa: F401
        return True
    except Exception:
        return False


def _com_store(ns, path):
    """The Outlook store to read: a .pst file (attached to the profile if needed), or with path "default" / empty the
    profile's own mailbox (an Exchange mailbox cached in an .ost cannot be opened as a file, but COM reads it)."""
    if not path or path.strip().lower() in ("default", "outlook", "mailbox"):
        return ns.DefaultStore
    path = os.path.abspath(path)
    for st in ns.Stores:
        try:
            if st.FilePath and os.path.abspath(st.FilePath).lower() == path.lower():
                return st
        except Exception:
            continue
    ns.AddStore(path)
    for st in ns.Stores:
        try:
            if st.FilePath and os.path.abspath(st.FilePath).lower() == path.lower():
                return st
        except Exception:
            continue
    raise RuntimeError(f"Outlook could not open {path}")


def _prop(pa, tag: str) -> str:
    try:
        v = pa.GetProperty(tag)
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


def _smtp_of(entry, fallback: str = "") -> str:
    """SMTP address of an Outlook AddressEntry (Exchange user, Exchange distribution list, or plain SMTP)."""
    if entry is None:
        return fallback
    try:
        if (getattr(entry, "Type", "") or "").upper() == "SMTP":
            return entry.Address or fallback
    except Exception:
        pass
    for get in ("GetExchangeUser", "GetExchangeDistributionList"):
        try:
            x = getattr(entry, get)()
            if x is not None and getattr(x, "PrimarySmtpAddress", ""):
                return x.PrimarySmtpAddress
        except Exception:
            continue
    try:
        v = entry.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
        if v:
            return v
    except Exception:
        pass
    addr = getattr(entry, "Address", "") or fallback
    return addr if "@" in addr else fallback


def com_recipients(it) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(To, Cc) as (display name, SMTP address) lists."""
    to, cc = [], []
    try:
        for r in it.Recipients:
            try:
                kind = r.Type
                name = r.Name or ""
                addr = _smtp_of(getattr(r, "AddressEntry", None), r.Address if "@" in (r.Address or "") else "")
                (to if kind == OL_TO else cc if kind == OL_CC else []).append((name, addr))
            except Exception:
                continue
    except Exception:
        pass
    return to, cc


def com_sender(it) -> tuple[str, str]:
    name = getattr(it, "SenderName", "") or ""
    addr = _prop(it.PropertyAccessor, PR_SENDER_SMTP_ADDRESS)
    if not addr:
        try:
            addr = _smtp_of(it.Sender, getattr(it, "SenderEmailAddress", "") or "")
        except Exception:
            addr = getattr(it, "SenderEmailAddress", "") or ""
    return name, addr if "@" in addr else ""


def _fmt(people: list[tuple[str, str]]) -> str:
    import email.utils
    return ", ".join(email.utils.formataddr((n, a)) if a else n for n, a in people)


def scan_pst_com(path: str, since: datetime | None, until: datetime | None, folders: list[str] | None = None) -> dict:
    """Dry run: count department vs personal messages per folder, reading only the recipients (nothing is stored)."""
    out: dict = {"folders": {}, "dept": 0, "not_dept": 0}
    stats: dict = {}
    for _raw, ref, _conv in iter_pst_com(path, since, until, folders, stats, count_only=True):
        folder = ref.split(":")[1] if ref.count(":") >= 2 else "?"
        out["folders"][folder] = out["folders"].get(folder, 0) + 1
        out["dept"] += 1
    out["not_dept"] = stats.get("not_dept", 0)
    return out


def iter_pst_com(path: str, since: datetime | None, until: datetime | None, folders: list[str] | None = None, stats: dict | None = None,
                 count_only: bool = False):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    outlook = win32com.client.Dispatch("Outlook.Application")
    ns = outlook.GetNamespace("MAPI")
    store = _com_store(ns, path)
    tmp = tempfile.mkdtemp(prefix="llmwiki-att-")

    def walk(folder, prefix=""):
        name = f"{prefix}/{folder.Name}"
        yield name, folder
        for sub in folder.Folders:
            yield from walk(sub, name)

    for fname, folder in walk(store.GetRootFolder()):
        if folders and not any(fname.lower().endswith(f.lower()) or f.lower() in fname.lower() for f in folders):
            continue
        try:
            items = folder.Items
            # Restrict() date strings depend on the Windows locale, so sort newest first and filter in Python instead
            items.Sort("[ReceivedTime]", True)
        except Exception as e:
            log.warning("skipping folder %s: %s", fname, e)
            continue
        log.info("pst/COM: folder %s (%s items)", fname, getattr(items, "Count", "?"))
        for it in items:
            try:
                if getattr(it, "Class", 0) != 43:  # olMail only
                    continue
                rt = _aware(getattr(it, "ReceivedTime", None))
                if rt and until and rt >= until:
                    continue
                if rt and since and rt < since:
                    break        # sorted newest first: everything after this is older
                to, cc = com_recipients(it)
                sender = com_sender(it)
                if not is_dept_mail(to + cc, sender):
                    if stats is not None:
                        stats["not_dept"] = stats.get("not_dept", 0) + 1
                    continue     # personal mail: never read further, never stored
                if count_only:
                    yield None, f"pst:{fname}:{it.EntryID[:40]}", ""
                    continue
                pa = it.PropertyAccessor
                hdrs = _prop(pa, PR_TRANSPORT_MESSAGE_HEADERS)
                atts = []
                for a in it.Attachments:
                    try:
                        p = os.path.join(tmp, f"a{a.Index}")
                        a.SaveAsFile(p)
                        with open(p, "rb") as fh:
                            atts.append((a.FileName, "application/octet-stream", fh.read()))
                        os.remove(p)
                    except Exception as e:
                        log.debug("attachment skipped: %s", e)
                sent = _aware(it.SentOn) or rt or datetime.now(timezone.utc)   # the Date header from the transport headers wins anyway
                raw = build_rfc822(hdrs, it.Subject or "", _fmt([sender]) if sender[1] else (sender[0] or ""), _fmt(to) or (it.To or ""),
                                   _fmt(cc) or (it.CC or ""), sent, it.HTMLBody or "", it.Body or "", atts,
                                   message_id=_prop(pa, PR_INTERNET_MESSAGE_ID), conversation_index=getattr(it, "ConversationIndex", "") or "",
                                   in_reply_to=_prop(pa, PR_IN_REPLY_TO_ID), references=_prop(pa, PR_INTERNET_REFERENCES))
                yield raw, f"pst:{fname}:{it.EntryID[:40]}", getattr(it, "ConversationID", "") or ""
            except Exception as e:
                log.warning("pst/COM: item skipped in %s: %s", fname, e)


# ------------------------------------------------------------------ pypff
def pypff_available() -> bool:
    try:
        import pypff  # noqa: F401
        return True
    except Exception:
        return False


def iter_pst_pypff(path: str, since: datetime | None, until: datetime | None, folders: list[str] | None = None, stats: dict | None = None):
    """pypff sees only the transport headers: internal mail without them cannot be matched to a department and is
    skipped when pst_dept_only is on (counted as not_dept). Prefer the Outlook COM route for a personal mailbox."""
    import pypff
    f = pypff.file()
    f.open(path)

    def walk(folder, prefix=""):
        name = f"{prefix}/{folder.name or ''}"
        yield name, folder
        for i in range(folder.number_of_sub_folders):
            yield from walk(folder.get_sub_folder(i), name)
    try:
        for fname, folder in walk(f.get_root_folder()):
            if folders and not any(x.lower() in fname.lower() for x in folders):
                continue
            for i in range(folder.number_of_sub_messages):
                try:
                    m = folder.get_sub_message(i)
                    hdrs = m.transport_headers or ""
                    sent = m.delivery_time or m.client_submit_time
                    if sent and sent.tzinfo is None:
                        sent = sent.replace(tzinfo=timezone.utc)
                    if since and sent and sent < since or until and sent and sent >= until:
                        continue
                    if not dept_from_headers(hdrs.encode("utf-8", "replace") + b"\r\n\r\n"):
                        if stats is not None:
                            stats["not_dept"] = stats.get("not_dept", 0) + 1
                        continue
                    html = (m.html_body or b"").decode("utf-8", "replace") if isinstance(m.html_body, bytes) else (m.html_body or "")
                    text = (m.plain_text_body or b"").decode("utf-8", "replace") if isinstance(m.plain_text_body, bytes) else (m.plain_text_body or "")
                    atts = []
                    for j in range(m.number_of_attachments):
                        try:
                            a = m.get_attachment(j)
                            data = a.read_buffer(a.size)
                            atts.append((getattr(a, "name", None) or f"attachment-{j}", "application/octet-stream", data))
                        except Exception:
                            continue
                    raw = build_rfc822(hdrs, m.subject or "", m.sender_name or "", "", "", sent or datetime.now(timezone.utc), html, text, atts)
                    yield raw, f"pst:{fname}:{i}", ""
                except Exception as e:
                    log.warning("pst/pypff: message %s in %s skipped: %s", i, fname, e)
    finally:
        f.close()
