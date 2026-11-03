"""HTML mail body → readable text with markdown tables.

Outlook reply separators (divRplyFwdMsg, the grey top border, gmail_quote, blockquote type=cite) are replaced by a
QUOTE_MARK line so the quote splitter finds the start of the quoted history reliably.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag

QUOTE_MARK = "-----QUOTED-----"
BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr", "table", "ul", "ol"}


def _cell(td: Tag) -> str:
    t = re.sub(r"\s+", " ", td.get_text(" ", strip=True))
    return t.replace("|", "/")


def table_md(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = [_cell(td) for td in tr.find_all(["td", "th"]) if td.find_parent("tr") is tr]
        if any(c for c in cells):
            rows.append(cells)
    if not rows:
        return ""
    if len(rows) == 1 or max(len(r) for r in rows) == 1:  # layout table, not data
        return "\n".join(" ".join(r) for r in rows)
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n" + "\n".join(out) + "\n"


def _is_reply_separator(el: Tag) -> bool:
    if el.name not in ("div", "hr", "p"):
        return False
    ident = (el.get("id") or "") + " " + " ".join(el.get("class") or [])
    if "divRplyFwdMsg" in ident or "gmail_quote" in ident or "OutlookMessageHeader" in ident or "moz-cite-prefix" in ident:
        return True
    style = (el.get("style") or "").replace(" ", "").lower()
    if "border-top:solid" in style and ("#e1e1e1" in style or "#b5c4df" in style or "1.0pt" in style):
        return True
    return False


def _image_token(img: Tag) -> str:
    """The reference that finds the picture again: its Content-ID (or web address), not its alt text. Tracking pixels go."""
    if str(img.get("width", "")).strip() in ("0", "1") and str(img.get("height", "")).strip() in ("0", "1"):
        return ""
    src = (img.get("src") or "").strip()
    alt = re.sub(r"[\[\]\s]+", " ", img.get("alt") or "").strip()
    if src.lower().startswith("cid:"):
        ref = "cid:" + unquote(src[4:]).strip()
    elif src.lower().startswith(("http://", "https://")):
        ref = src
    elif src.lower().startswith("data:"):
        ref = alt or "embedded image"
    else:
        ref = re.split(r"[/\\]", src)[-1][:60] or alt
    return f" [image: {re.sub(r'[][]', '', ref)}] "


def html_to_text(html: str) -> tuple[str, bool]:
    """Return (text, has_table)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for bad in soup(["style", "script", "head", "title", "meta", "xml"]):
        bad.decompose()
    has_table = False
    for img in soup.find_all("img"):      # first: a picture inside a table cell must survive the table conversion
        img.replace_with(NavigableString(_image_token(img)))
    # mark the first reply separator: everything after it is quoted history
    for el in soup.find_all(True):
        if _is_reply_separator(el) or (el.name == "blockquote" and el.get("type") == "cite"):
            mark = soup.new_tag("div")
            mark.string = QUOTE_MARK
            el.insert_before(mark)
            break
    for t in soup.find_all("table"):
        if t.find_parent("table"):
            continue
        md = table_md(t)
        if md.startswith("\n|"):
            has_table = True
        pre = soup.new_tag("pre")      # keeps the table's line breaks
        pre.string = md.strip("\n")
        t.replace_with(pre)
    return _render(soup), has_table


# Line breaks are tokens, not text: HTML source newlines are spaces (Outlook wraps its source at ~76 characters), a
# block boundary is one soft break (several merge into one), <br> adds a hard break, an empty paragraph (&nbsp;) is a
# blank line, and never more than one blank line in a row. That is what removes the 2–4 empty lines Outlook HTML
# otherwise leaves between every row.
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")


def _render(soup) -> str:
    out: list = []          # str pieces and int break tokens (number of newlines wanted)

    def brk(n: int, hard: bool = False):
        if out and isinstance(out[-1], int):
            out[-1] = min(2, out[-1] + n) if hard else max(out[-1], n)
        else:
            out.append(min(2, n))

    def is_empty_para(t: Tag) -> bool:
        return not _INVISIBLE.sub("", t.get_text()).replace("\xa0", " ").strip() and not t.find(["table", "pre"])

    def walk(node, pre: bool):
        for ch in node.children:
            if isinstance(ch, NavigableString):
                if type(ch).__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction", "CData"):
                    continue
                s = _INVISIBLE.sub("", str(ch)).replace("\xa0", " ")
                if pre:
                    for i, part in enumerate(s.split("\n")):
                        if i:
                            brk(1, hard=True)
                        out.append(part)
                else:
                    s = re.sub(r"[ \t\r\n\f\v]+", " ", s)
                    if s == " " and (not out or isinstance(out[-1], int)):
                        continue        # source indentation between blocks must not split two breaks
                    out.append(s)
            elif isinstance(ch, Tag):
                name = "" if ":" in ch.name else ch.name     # o:p, w:sdt … (Word namespaces) are inline wrappers
                if name == "br":
                    brk(1, hard=True)
                    continue
                if name == "p" and is_empty_para(ch):   # <p class=MsoNormal>&nbsp;</p> = Outlook's blank line
                    brk(2)
                    continue
                block = name in BLOCK
                # Outlook/Word paragraphs (class Mso…) are lines; other mailers' <p> have a margin = a blank line
                gap = 2 if name == "p" and not any("mso" in c.lower() for c in (ch.get("class") or [])) else 1
                if block:
                    brk(gap)
                if name == "li":
                    out.append("- ")
                walk(ch, pre or name == "pre")
                if block:
                    brk(gap)
    walk(soup, False)
    text = "".join(("\n" * x) if isinstance(x, int) else x for x in out)
    return clean_text(text)


def clean_text(text: str) -> str:
    """Whitespace clean-up for any body text (plain-text parts too): CRLF, invisible characters, trailing spaces, runs of
    spaces, and at most one blank line in a row."""
    t = _INVISIBLE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " "))
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"(?<=\S)[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
