"""Split an email body into the new text, the signature and the quoted history; split the history into blocks.

Markers handled (first one wins):
- the QUOTE_MARK inserted by html2md for Outlook/Gmail reply separators
- "-----Original Message-----" (+ NL/DE/KO variants), forwarded-message banners
- an Outlook header block: From:/Van:/Von:/Od:/De:/보낸 사람: followed within 5 lines by Sent:/Date:/Verzonden:/Gesendet:/보낸 날짜:
- "On … wrote:" lines (EN/NL/DE/KO)
- a run of '>' quoted lines
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from llmwiki.ingest.html2md import QUOTE_MARK
from llmwiki.util import IMAGE_TOKEN

FROM = r"(?:from|van|von|od|de|da|보낸\s*사람|발신자|보낸사람)"
SENT = r"(?:sent|date|verzonden|gesendet|datum|odoslané|odesláno|envoyé|보낸\s*날짜|날짜|발송\s*일시)"
TO = r"(?:to|aan|an|komu|pour|받는\s*사람|수신자)"
SUBJ = r"(?:subject|onderwerp|betreff|predmet|předmět|objet|제목)"
ORIGINAL = re.compile(r"^\s*-{2,}\s*(?:original message|oorspronkelijk bericht|ursprüngliche nachricht|pôvodná správa|원본\s*메시지|forwarded message|doorgestuurd bericht|weitergeleitete nachricht|전달된\s*메시지)\s*-{2,}\s*$", re.I)
BEGIN_FWD = re.compile(r"^\s*(?:begin forwarded message|---------- forwarded message ----------)", re.I)
HDR_FROM = re.compile(rf"^\s*\**\s*{FROM}\s*\**\s*[:：]", re.I)
HDR_SENT = re.compile(rf"^\s*\**\s*{SENT}\s*\**\s*[:：]", re.I)
HDR_TO = re.compile(rf"^\s*\**\s*{TO}\s*\**\s*[:：]", re.I)
HDR_SUBJ = re.compile(rf"^\s*\**\s*{SUBJ}\s*\**\s*[:：]", re.I)
WROTE = re.compile(r"^\s*(?:on .{6,120} wrote:|op .{6,120} schreef .{0,80}:|am .{6,120} schrieb .{0,80}:|.{2,120}님이\s*작성(?:하였습니다|했습니다)?\s*:?)\s*$", re.I)
CLOSINGS = re.compile(r"^\s*(?:best regards|kind regards|regards|many thanks|thanks|thank you|cheers|br|met vriendelijke groet(?:en)?|mvg|groeten|mit freundlichen grüßen|viele grüße|s pozdravom|s pozdravem|감사합니다|수고하세요|고맙습니다)[,.!]?\s*$", re.I)
DISCLAIMER = re.compile(r"(?:this (?:e-?mail|message)[^\n]{0,80}(?:confidential|intended solely)|disclaimer:|the information contained in this|이 메일은.{0,40}(?:기밀|비밀))", re.I)


@dataclass
class Split:
    new_text: str
    signature: str
    quoted: str
    blocks: list[dict] = field(default_factory=list)   # [{header, text, from, date}]


def _header_block_at(lines: list[str], i: int) -> bool:
    if not HDR_FROM.match(lines[i]):
        return False
    look = lines[i + 1:i + 6]
    return any(HDR_SENT.match(x) for x in look) or (any(HDR_TO.match(x) for x in look) and any(HDR_SUBJ.match(x) for x in look))


def find_quote_start(lines: list[str]) -> int | None:
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s == QUOTE_MARK or ORIGINAL.match(ln) or BEGIN_FWD.match(ln) or WROTE.match(ln):
            return i
        if _header_block_at(lines, i):
            return i
        if s.startswith(">") and i + 1 < len(lines) and lines[i + 1].strip().startswith(">"):
            return i
    return None


def _cut_signature(text: str) -> tuple[str, str]:
    lines = text.rstrip().split("\n")
    # explicit "-- " delimiter
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].rstrip() == "--" or lines[i] == "-- ":
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    # disclaimer paragraph
    m = DISCLAIMER.search(text)
    sig_tail = ""
    if m and m.start() > len(text) * 0.3:
        sig_tail = text[m.start():]
        text = text[:m.start()].rstrip()
        lines = text.split("\n")
    # closing line followed by a few short lines (name, title, phone)
    for i in range(len(lines) - 1, max(-1, len(lines) - 12), -1):
        if CLOSINGS.match(lines[i]):
            tail = [x for x in lines[i + 1:] if x.strip()]
            if len(tail) <= 8 and all(len(IMAGE_TOKEN.sub("", x)) < 90 for x in tail):
                return "\n".join(lines[:i + 1]).rstrip(), ("\n".join(lines[i + 1:]).strip() + ("\n" + sig_tail if sig_tail else "")).strip()
            break
    return "\n".join(lines).rstrip(), sig_tail.strip()


def _split_blocks(quoted: str) -> list[dict]:
    lines = quoted.split("\n")
    starts = []
    for i in range(len(lines)):
        s = lines[i].strip()
        if s == QUOTE_MARK or ORIGINAL.match(lines[i]) or BEGIN_FWD.match(lines[i]) or WROTE.match(lines[i]) or _header_block_at(lines, i):
            if starts and i - starts[-1] <= 1:
                continue
            starts.append(i)
    if not starts or starts[0] != 0:
        starts = [0] + starts
    blocks = []
    for n, st in enumerate(starts):
        en = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = [re.sub(r"^\s*>+ ?", "", x) for x in lines[st:en] if x.strip() != QUOTE_MARK]
        hdr, body, frm, dt = [], [], "", ""
        j = 0
        while j < len(chunk) and j < 8 and (not chunk[j].strip() or ORIGINAL.match(chunk[j]) or BEGIN_FWD.match(chunk[j]) or WROTE.match(chunk[j])
                                            or HDR_FROM.match(chunk[j]) or HDR_SENT.match(chunk[j]) or HDR_TO.match(chunk[j])
                                            or HDR_SUBJ.match(chunk[j]) or re.match(r"^\s*(?:cc|kopie|참조)\s*[:：]", chunk[j], re.I)):
            if HDR_FROM.match(chunk[j]):
                frm = chunk[j].split(":", 1)[-1].split("：", 1)[-1].strip()
            if HDR_SENT.match(chunk[j]):
                dt = chunk[j].split(":", 1)[-1].strip()
            if WROTE.match(chunk[j]):
                frm = chunk[j].strip()
            hdr.append(chunk[j])
            j += 1
        body = "\n".join(chunk[j:]).strip()
        if body:
            nb, _sig = _cut_signature(body)
            blocks.append({"header": "\n".join(h for h in hdr if h.strip()), "text": nb or body, "from": frm, "date": dt})
    return blocks


def split_body(body: str) -> Split:
    body = (body or "").replace("\r\n", "\n")
    lines = body.split("\n")
    qs = find_quote_start(lines)
    if qs is None:
        new, quoted = body, ""
    else:
        new, quoted = "\n".join(lines[:qs]), "\n".join(lines[qs:])
    new = new.replace(QUOTE_MARK, "").strip()
    new_text, sig = _cut_signature(new)
    quoted = quoted.strip()
    return Split(new_text=new_text.strip(), signature=sig, quoted=quoted, blocks=_split_blocks(quoted) if quoted else [])
