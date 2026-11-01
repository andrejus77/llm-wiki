"""Step 2 of the synthetic dataset: turn testdata/raw/*.json into .eml files exactly as the department mailbox would
receive them, plus ground truth and the golden question set.

Mechanics done by script (not by the LLM): Message-ID / In-Reply-To / References, Outlook Thread-Index, RE:/FW:
prefixes, Outlook/Gmail-style quoting of the real previous emails, signatures and disclaimers, HTML tables, inline PNG
screenshots, xlsx/pdf attachments, automated noise mail, and the hard cases:
- late CC: the department is CC'd only from the 6th email of a thread → earlier mail exists only as quoted history
- the same mail through two department aliases (same Message-ID twice) and a resend under a new Message-ID
- forwards, replies without References/Thread-Index (subject fallback), long threads, new t-codes

    python -m devtools.build_emails [--korean]
Outputs: testdata/eml/*.eml, testdata/truth.json, testdata/golden.json (and testdata/ko/ for the Korean slice)
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import re
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from devtools.world import DEPTS, DOMAIN, END, KOREAN_PEOPLE, PEOPLE, START  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")
RAW = ROOT / "testdata" / "raw"
rng = random.Random(42)

DEPT_OF = {"customs": "dl-cus", "aeo": "dl-cus", "reefer_holds": "dl-cus", "adr": "dl-cus", "ko_customs_docs": "dl-cus",
           "veltrans_invoices": "dl-fin", "accrual": "dl-fin", "baltic_claim": "dl-fin", "detention": "dl-fin", "rpa_bot": "dl-fin",
           "ai_mail_classifier": "dl-ai"}
LATE_CC = {"customs", "ko_hq_forecast"}


def person(key: str) -> tuple[str, str, str, str]:
    """(name, address, role, domain)"""
    if key in DEPTS:
        return (key.upper().replace("-", " "), f"{key}@{DOMAIN}", DEPTS[key], DOMAIN)
    ppl = {**PEOPLE, **KOREAN_PEOPLE}
    if key not in ppl:
        key = "rjansen"
    name, role, dom = ppl[key]
    if dom == DOMAIN:
        first, last = name.split(" ", 1)
        a = f"{first[0].lower()}.{last.lower().replace(' ', '')}@{dom}"
    else:
        a = f"{key}@{dom}"
    return name, a, role, dom


def is_korean(key: str) -> bool:
    return key in KOREAN_PEOPLE


def fmt_addr(key: str) -> str:
    n, a, _, _ = person(key)
    return f'"{n}" <{a}>'


# ------------------------------------------------------------------ rendering helpers
def screenshot_png(title: str, lines: list[str]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    w, h = 760, 70 + 22 * max(4, len(lines))
    img = Image.new("RGB", (w, h), (240, 243, 247))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 30], fill=(28, 66, 120))
    try:
        f = ImageFont.truetype("consola.ttf", 14)
        fb = ImageFont.truetype("segoeuib.ttf", 14)
    except Exception:
        f = fb = ImageFont.load_default()
    d.text((10, 7), title[:90], fill=(255, 255, 255), font=fb)
    for i, ln in enumerate(lines[:30]):
        d.text((14, 44 + 22 * i), str(ln)[:100], fill=(20, 28, 40), font=f)
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def xlsx_bytes(rows: list[list]) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append([str(x) for x in r])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def pdf_bytes(text: str) -> bytes:
    """A minimal valid one-page PDF with Helvetica text (enough for pypdf extraction)."""
    lines = []
    for para in text.split("\n"):
        while len(para) > 95:
            cut = para.rfind(" ", 0, 95)
            cut = cut if cut > 0 else 95
            lines.append(para[:cut])
            para = para[cut:].lstrip()
        lines.append(para)
    def pdf_str(s):
        s = s.encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 50 800 Td 13 TL\n" + "\n".join(f"({pdf_str(ln)}) '" for ln in lines[:58]) + "\nET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content.encode('latin-1', 'replace'))} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1", "replace"))
    x = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode())
    return out.getvalue()


def body_html(text: str) -> str:
    parts = []
    for para in re.split(r"\n\s*\n", text.strip()):
        lines = para.split("\n")
        if all(l.strip().startswith(("- ", "* ")) for l in lines if l.strip()):
            parts.append("<ul>" + "".join(f"<li>{escape(l.strip()[2:])}</li>" for l in lines if l.strip()) + "</ul>")
        else:
            parts.append("<p>" + "<br>".join(escape(l) for l in lines) + "</p>")
    return "\n".join(parts)


def table_html(t: dict) -> str:
    head = "".join(f"<th style='border:1px solid #999;padding:3px 6px'>{escape(str(h))}</th>" for h in t.get("header", []))
    rows = "".join("<tr>" + "".join(f"<td style='border:1px solid #999;padding:3px 6px'>{escape(str(c))}</td>" for c in r) + "</tr>" for r in t.get("rows", []))
    return f"<table style='border-collapse:collapse;font-size:10pt'><tr>{head}</tr>{rows}</table>"


def table_text(t: dict) -> str:
    rows = [t.get("header", [])] + t.get("rows", [])
    return "\n".join(" | ".join(str(c) for c in r) for r in rows)


def signature(key: str) -> tuple[str, str]:
    name, a, role, dom = person(key)
    if is_korean(key):
        txt = f"감사합니다.\n\n{name}\n{role}"
    elif dom == DOMAIN:
        closing = rng.choice(["Kind regards,", "Best regards,", "Regards,", "Thanks,"])
        txt = (f"{closing}\n{name}\n{role}\nCorp Europe B.V. | Rotterdam\nT +31 10 {rng.randint(200, 899)} {rng.randint(1000, 9999)}\n\n"
               "This e-mail and any attachments are confidential and intended solely for the addressee. If you received it in error, please delete it.")
    else:
        txt = f"Best regards,\n{name}\n{role}"
    return txt, "<p>" + "<br>".join(escape(l) for l in txt.split("\n")) + "</p>"


def quote_header_html(prev: dict, korean_sender: bool, partner_style: bool) -> str:
    dt = prev["dt"]
    if partner_style and not korean_sender:
        return f"<div class='gmail_quote'>On {dt.strftime('%a, %d %b %Y at %H:%M')}, {escape(prev['from_name'])} &lt;{escape(prev['from_addr'])}&gt; wrote:<br>"
    if korean_sender:
        return (f"<div id='divRplyFwdMsg'><b>보낸 사람:</b> {escape(prev['from_name'])} &lt;{escape(prev['from_addr'])}&gt;<br>"
                f"<b>보낸 날짜:</b> {dt.year}년 {dt.month}월 {dt.day}일 {dt.strftime('%H:%M')}<br><b>받는 사람:</b> {escape(prev['to_text'])}<br>"
                f"<b>제목:</b> {escape(prev['subject'])}</div>")
    return (f"<div style='border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm'><p><b>From:</b> {escape(prev['from_name'])} &lt;{escape(prev['from_addr'])}&gt;<br>"
            f"<b>Sent:</b> {dt.strftime('%A, %d %B %Y %H:%M')}<br><b>To:</b> {escape(prev['to_text'])}<br>"
            + (f"<b>Cc:</b> {escape(prev['cc_text'])}<br>" if prev["cc_text"] else "") + f"<b>Subject:</b> {escape(prev['subject'])}</p></div>")


def thread_index(guid: bytes, dt: datetime, depth: int) -> str:
    ft = int((dt.timestamp() + 11644473600) * 10_000_000)
    head = ft.to_bytes(8, "big")[:6] + guid
    tail = b"".join(rng.randbytes(5) for _ in range(depth))
    return base64.b64encode(head + tail).decode()


# ------------------------------------------------------------------ build one storyline
def build_storyline(raw: dict, out_dir: Path, truth: list, skipped: list) -> int:
    sl = raw["storyline"]["key"]
    dept = DEPT_OF.get(sl, "dl-ct")
    n_written = 0
    for ti, th in enumerate(raw.get("threads", [])):
        guid = hashlib.md5(f"{sl}:{th.get('key', ti)}".encode()).digest()
        by_id = {}
        emails = sorted(th.get("emails", []), key=lambda e: e.get("date", ""))
        for idx, e in enumerate(emails):
            try:
                dt = datetime.strptime(e["date"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            except Exception:
                dt = datetime.strptime(START + " 09:00", "%Y-%m-%d %H:%M").replace(tzinfo=TZ) + timedelta(hours=idx * 5)
            frm = e.get("from") or "rjansen"
            to = [k for k in (e.get("to") or []) if k] or ["dl-ct"]
            cc = [k for k in (e.get("cc") or []) if k and k not in to]
            late = sl in LATE_CC and ti == 0 and idx < 5
            if not late and dept not in to + cc:
                cc.append(dept)
            parent = by_id.get(e.get("reply_to")) if e.get("reply_to") else None
            fwd = bool(e.get("forward"))
            kor = is_korean(frm)
            pre = ("전달: " if fwd else "회신: ") if kor else ("FW: " if fwd else ("Re: " if person(frm)[3] != DOMAIN else "RE: "))
            subject = (pre if (parent or fwd) else "") + th.get("subject", "Update")
            fname, faddr, _, fdom = person(frm)
            mid = f"<{sl}.{th.get('key', ti)}.{e.get('id', idx)}.{hashlib.md5((sl + str(idx) + str(ti)).encode()).hexdigest()[:10]}@{fdom}>"
            refs = (parent["refs"] + [parent["mid"]]) if parent else []
            depth = (parent["depth"] + 1) if parent else 0
            body = e.get("body", "").strip()
            html = body_html(body)
            text = body
            atts, inline = [], []
            if e.get("table"):
                html += table_html(e["table"])
                text += "\n\n" + table_text(e["table"])
            if e.get("screenshot"):
                sc = e["screenshot"]
                png = screenshot_png(sc.get("title", "SAP"), sc.get("lines", []))
                cid = f"img{idx}.{hashlib.md5(png).hexdigest()[:8]}@corp"
                html += f"<p><img src='cid:{cid}' alt='{escape(sc.get('title', 'screenshot'))}' width='600'></p>"
                inline.append((cid, png, f"{sc.get('title', 'screenshot')[:40]}.png"))
            if e.get("attachment"):
                a = e["attachment"]
                if a.get("kind") == "xlsx" and a.get("rows"):
                    atts.append((a.get("name", "data.xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx_bytes(a["rows"])))
                elif a.get("kind") == "pdf":
                    atts.append((a.get("name", "document.pdf"), "application/pdf", pdf_bytes(a.get("text") or "\n".join(" ".join(map(str, r)) for r in a.get("rows", [])))))
            sig_t, sig_h = signature(frm)
            html += sig_h
            text += "\n\n" + sig_t
            partner_style = fdom != DOMAIN and not kor
            if parent:
                html += quote_header_html(parent, kor, partner_style) + parent["html"] + ("</div>" if partner_style else "")
                text += "\n\n-----Original Message-----\n" + f"From: {parent['from_name']} <{parent['from_addr']}>\nSent: {parent['dt']:%A, %d %B %Y %H:%M}\nTo: {parent['to_text']}\nSubject: {parent['subject']}\n\n" + parent["text"]
            rec = {"mid": mid, "refs": refs, "depth": depth, "dt": dt, "from_name": fname, "from_addr": faddr,
                   "to_text": "; ".join(person(k)[0] for k in to), "cc_text": "; ".join(person(k)[0] for k in cc), "subject": subject,
                   "html": html, "text": text}
            by_id[e.get("id", str(idx))] = rec
            msg = EmailMessage()
            msg["Message-ID"] = mid
            msg["Date"] = format_datetime(dt)
            msg["From"] = fmt_addr(frm)
            msg["To"] = ", ".join(fmt_addr(k) for k in to)
            if cc:
                msg["Cc"] = ", ".join(fmt_addr(k) for k in cc)
            msg["Subject"] = subject
            broken = bool(parent) and partner_style and (idx % 4 == 3)
            if parent and not broken:
                msg["In-Reply-To"] = parent["mid"]
                msg["References"] = " ".join(refs)
            if fdom == DOMAIN and not broken:
                msg["Thread-Index"] = thread_index(guid, dt, depth)
            msg.set_content(text)
            msg.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
            if inline:
                html_part = msg.get_payload()[1]
                for cid, png, nm in inline:
                    html_part.add_related(png, maintype="image", subtype="png", cid=f"<{cid}>", filename=nm)
            for name, ctype, data in atts:
                mt, st = ctype.split("/", 1)
                msg.add_attachment(data, maintype=mt, subtype=st, filename=name)
            raw_bytes = msg.as_bytes()
            t = {"message_id": mid.strip("<>").lower(), "storyline": sl, "thread": f"{sl}:{th.get('key', ti)}", "email_key": e.get("id"),
                 "day": dt.strftime("%Y-%m-%d"), "from": faddr, "subject": subject, "delivered": not late, "noise": False, "korean": kor,
                 "has_table": bool(e.get("table")), "has_attachment": bool(atts), "has_image": bool(inline), "broken_refs": broken}
            if late:
                skipped.append(t)
                continue
            fn = out_dir / f"{dt:%Y%m%d-%H%M}-{sl}-{th.get('key', ti)}-{e.get('id', idx)}.eml"
            fn.write_bytes(raw_bytes)
            truth.append(t)
            n_written += 1
            # alias duplicate: the same message delivered through a second department alias (same Message-ID)
            if not kor and idx == 2 and ti == 0 and sl in ("z1_block", "veltrans_invoices", "ewm_rotterdam"):
                (out_dir / (fn.stem + "-alias2.eml")).write_bytes(raw_bytes)
                truth.append({**t, "alias_copy": True})
    return n_written


# ------------------------------------------------------------------ noise
def noise(out_dir: Path, truth: list) -> int:
    n = 0
    start = datetime.strptime(START, "%Y-%m-%d").replace(tzinfo=TZ)
    end = datetime.strptime(END, "%Y-%m-%d").replace(tzinfo=TZ)
    d = start
    po = 4500200000
    while d <= end:
        if d.weekday() < 5:
            items = []
            items.append(("sap-batch", "SAP Batch", f"Job ZLE_DELIVERY_MONITOR finished {d:%d.%m.%Y}", f"Job ZLE_DELIVERY_MONITOR (step 1) finished.\nRuntime: {rng.randint(40, 300)} s\nDeliveries checked: {rng.randint(800, 2000)}\nErrors: {rng.choice([0, 0, 0, 1, 2])}\nSpool list attached.", {"X-Mailer": "SAP NetWeaver 7.5"}, 6, 5))
            for _ in range(rng.choice([1, 2, 2, 3])):
                po += rng.randint(3, 40)
                items.append(("approve", "SAP Workflow", f"PO {po} waiting for your approval", f"Purchase order {po} (value EUR {rng.randint(800, 25000):,}) is waiting for your approval.\nOpen the workflow inbox (SBWP) to approve or reject.", {}, rng.randint(7, 17), rng.randint(0, 59)))
            for _ in range(rng.choice([0, 1, 1, 2])):
                dl = rng.randint(80400000, 80499999)
                items.append(("noreply-veltrans", "Veltrans Track & Trace", f"Shipment {dl} delivered", f"Your shipment {dl} was delivered at {rng.randint(8, 17)}:{rng.randint(10, 59)}.\nProof of delivery: available in the portal.\nThis is an automatic message, please do not reply.", {}, rng.randint(9, 18), rng.randint(0, 59)))
            for _ in range(rng.choice([0, 1, 1])):   # booking confirmations from an ordinary-looking address (no noreply)
                bk = rng.randint(30000, 39999)
                items.append(("bookings", "Nordline Bookings", f"Booking NL-{bk} confirmed", f"Dear customer,\nyour booking NL-{bk} for {rng.randint(1, 26)} pallets "
                              f"from Rotterdam to Gdansk on {(d + timedelta(days=rng.randint(2, 9))):%d.%m.%Y} is confirmed.\nKind regards, Nordline Bookings",
                              {}, rng.randint(8, 16), rng.randint(0, 59)))
            if d.weekday() == 0:
                items.append(("news", "Logistics Weekly", f"Logistics Weekly — {d:%d %B %Y}", "This week: port congestion in Northern Europe, new EU emission rules for trucks, and five tips for peak season.\nUnsubscribe here.", {"List-Id": "<weekly.logistics-news.example>", "Precedence": "bulk"}, 7, 0))
            if rng.random() < 0.18:
                who = rng.choice(["rjansen", "anovak", "lbecker", "thendriks"])
                items.append((who, None, "Automatic reply: " + rng.choice(["Weekly status", "Question about delivery", "Planning"]),
                              f"I am out of the office until {(d + timedelta(days=rng.randint(2, 6))):%d %B}. For urgent matters please contact dl-ct.", {"Auto-Submitted": "auto-replied"}, 10, 2))
            if rng.random() < 0.12:
                items.append(("it-service", "IT Service Desk", "Planned maintenance: SAP P01 on Saturday 06:00-10:00", "SAP P01 will be unavailable on Saturday between 06:00 and 10:00 for maintenance. Please plan accordingly.", {}, 16, 30))
            for key, name, subj, body, hdrs, hh, mm in items:
                if key in PEOPLE:
                    fname, faddr = person(key)[0], person(key)[1]
                else:
                    dom = "veltrans.example" if "veltrans" in key else ("logistics-news.example" if key == "news" else "nordline.example" if key == "bookings" else DOMAIN)
                    fname, faddr = name, f"{key.replace('noreply-veltrans', 'noreply')}@{dom}"
                dt = d.replace(hour=hh, minute=mm)
                msg = EmailMessage()
                mid = f"<noise.{key}.{dt:%Y%m%d%H%M}.{rng.randint(1000, 9999)}@{faddr.split('@')[1]}>"
                msg["Message-ID"] = mid
                msg["Date"] = format_datetime(dt)
                msg["From"] = f'"{fname}" <{faddr}>'
                msg["To"] = f"dl-ct@{DOMAIN}"
                msg["Subject"] = subj
                for k, v in hdrs.items():
                    msg[k] = v
                msg.set_content(body)
                fn = out_dir / f"{dt:%Y%m%d-%H%M}-noise-{key}-{rng.randint(100, 999)}.eml"
                fn.write_bytes(msg.as_bytes())
                truth.append({"message_id": mid.strip("<>").lower(), "storyline": "noise", "thread": f"noise:{key}:{subj[:30]}", "day": dt.strftime("%Y-%m-%d"),
                              "from": faddr, "subject": subj, "delivered": True, "noise": True, "korean": False})
                n += 1
        d += timedelta(days=1)
    return n


def resend_duplicate(out_dir: Path, truth: list):
    """One email re-sent under a new Message-ID (the store must mark it dup_of)."""
    files = sorted(out_dir.glob("*-accrual-*.eml"))
    if not files:
        return
    raw = files[0].read_bytes().decode("utf-8", "replace")
    raw2 = re.sub(r"Message-ID: <([^>]+)>", lambda m: f"Message-ID: <resent.{m.group(1)}>", raw, count=1)
    (out_dir / (files[0].stem + "-resent.eml")).write_bytes(raw2.encode("utf-8"))
    m = re.search(r"Message-ID: <([^>]+)>", raw2)
    truth.append({"message_id": m.group(1).lower(), "storyline": "accrual", "thread": "dup", "delivered": True, "noise": False, "resent_duplicate": True})


def main():
    korean = "--korean" in sys.argv
    out = ROOT / "testdata" / ("ko" if korean else "eml")
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.eml"):
        f.unlink()
    truth, skipped, golden = [], [], []
    n = 0
    for p in sorted(RAW.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        if bool(raw["storyline"].get("korean")) != korean:
            continue
        n += build_storyline(raw, out, truth, skipped)
        for i, q in enumerate(raw.get("questions", [])):
            fact_emails = {e for f in raw.get("facts", []) if f["id"] in q.get("facts", []) for e in f.get("emails", [])}
            golden.append({"id": f"{raw['storyline']['key']}-q{i + 1}", "storyline": raw["storyline"]["key"], "q": q["q"], "a": q["a"],
                           "type": q.get("type", "fact"), "facts": [f["text"] for f in raw.get("facts", []) if f["id"] in q.get("facts", [])],
                           "gold_email_keys": sorted(fact_emails)})
    nn = 0 if korean else noise(out, truth)
    if not korean:
        resend_duplicate(out, truth)
    # map gold email keys → message ids
    mid_by = {(t["storyline"], t.get("email_key")): t["message_id"] for t in truth + skipped if t.get("email_key")}
    for g in golden:
        g["gold_message_ids"] = [mid_by[(g["storyline"], k)] for k in g["gold_email_keys"] if (g["storyline"], k) in mid_by]
    meta = {"emails_written": n, "noise": nn, "quoted_only": len(skipped), "files": len(list(out.glob("*.eml")))}
    (out.parent / ("truth_ko.json" if korean else "truth.json")).write_text(json.dumps({"meta": meta, "emails": truth, "quoted_only": skipped}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out.parent / ("golden_ko.json" if korean else "golden.json")).write_text(json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta), f"golden questions: {len(golden)}")


if __name__ == "__main__":
    main()
