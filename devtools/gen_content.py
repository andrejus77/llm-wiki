"""Step 1 of the synthetic dataset: Sonnet (via `claude -p`) writes the email bodies, facts and golden questions per
storyline. Output is cached in testdata/raw/<key>.json, so re-runs only generate what's missing.

    python -m devtools.gen_content [--only key1,key2] [--korean] [--workers 6] [--model sonnet]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from devtools.world import DEPTS, KOREAN_PEOPLE, KOREAN_STORYLINES, PEOPLE, STORYLINES  # noqa: E402
from llmwiki.llm.jsonparse import extract_json  # noqa: E402

RAW = ROOT / "testdata" / "raw"

SYSTEM = """You generate realistic synthetic business emails for a software test dataset. The company is fictional:
Corp Europe B.V., the European logistics department of a manufacturer, with sites in Rotterdam, Venlo, Antwerp, Hamburg,
Felixstowe and Gdansk. Write like real colleagues: specific, sometimes terse, with numbers, dates, SAP t-codes and
small misunderstandings that get resolved. Never invent people outside the given list. Output JSON only."""

FORMAT = """Return JSON only, in exactly this format:
{
 "threads": [
  {"key": "t1", "subject": "short realistic subject (no RE:/FW:, the script adds them)",
   "emails": [
    {"id": "m1", "date": "2026-10-20 09:15", "from": "<person key>", "to": ["<person key or dept key>"], "cc": [],
     "reply_to": null, "forward": false,
     "body": "plain text, 40-220 words, no greeting line needed, NO quoted earlier mail, NO signature",
     "table": null,
     "attachment": null,
     "screenshot": null}
   ]}
 ],
 "facts": [{"id": "F1", "text": "one atomic fact stated in the emails", "emails": ["m3"]}],
 "questions": [{"q": "a question a colleague would ask the wiki", "a": "the short correct answer", "facts": ["F1"],
                "type": "fact|why|who|when|identifier|howto|multihop"}]
}
Rules:
- "reply_to" is the id of the email this one answers (null for the first email of a thread). Replies usually go to the sender of the previous email and keep the same people in Cc.
- "table": optional {"header": ["..."], "rows": [["..."]]} for data the sender pastes as a table in the mail body.
- "attachment": optional {"kind": "xlsx" | "pdf", "name": "file name", "rows": [["header",...],["..."]]} (xlsx) or {"kind": "pdf", "name": "...", "text": "..."}.
- "screenshot": optional {"title": "SAP screen title", "lines": ["a few lines of what the screen shows"]}.
- Dates: working days, 07:30-18:30, inside the given period, in order. Use the person keys from the list for from/to/cc; departments: dl-ct, dl-cus, dl-fin.
- Use realistic reference numbers: purchase orders 45xxxxxxxx (10 digits), deliveries 8xxxxxxx (8 digits), invoices INV-xxxxx, containers like MSKU1234567.
- 1-3 threads per storyline; most emails in the main thread. About 1 in 6 emails is routine (thanks, ok, will check).
- facts: 8-15 atomic facts covering dates, numbers, decisions and reasons, who does what.
- questions: 4-6 questions answerable ONLY from these emails, mixed types, plus 1 question that sounds related but is NOT answered in the emails (answer "not in the emails", type "unanswerable")."""


def people_list(korean: bool) -> str:
    ppl = dict(PEOPLE)
    if korean:
        ppl.update(KOREAN_PEOPLE)
    lines = [f"- {k}: {v[0]} — {v[1]} ({v[2]})" for k, v in ppl.items()]
    lines += [f"- {k}: department mailbox ({v})" for k, v in DEPTS.items()]
    return "\n".join(lines)


def prompt_for(sl, korean: bool) -> str:
    key, title, level, ppl, partners, (a, b), n, brief, extras = sl
    return (f"People and mailboxes (use these keys):\n{people_list(korean)}\n\n"
            f"Storyline: {title}\nType: {level}\nMain people: {', '.join(ppl)}; partners: {', '.join(partners) or 'none'}\n"
            f"Period: {a} to {b}\nNumber of emails: about {n}\nWhat happens:\n{brief}\n"
            + (f"Also include: {extras}\n" if extras else "")
            + ("Korean: the headquarters/plant people (kimhq, parkhq, leeplant) write their emails entirely in Korean (Hangul). European colleagues write English. "
               "Facts and questions in English.\n" if korean else "Everything in English.\n")
            + "\n" + FORMAT)


def claude(prompt: str, model: str, timeout: int = 900) -> str:
    exe = os.environ.get("LLMWIKI_CLAUDE") or str(Path.home() / ".local" / "bin" / "claude.exe")
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--tools", "", "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence", "--system-prompt", SYSTEM]
    p = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, cwd=tempfile.gettempdir())
    j = json.loads(p.stdout.decode("utf-8", "replace"))
    if j.get("is_error"):
        raise RuntimeError(str(j.get("result"))[:300])
    return j.get("result") or ""


def gen_one(sl, korean: bool, model: str) -> str:
    key = sl[0]
    out = RAW / f"{key}.json"
    if out.exists():
        return f"{key}: cached"
    for attempt in range(3):
        t0 = time.time()
        try:
            text = claude(prompt_for(sl, korean), model)
            obj, err = extract_json(text)
            if obj is None or not obj.get("threads"):
                raise ValueError(err or "no threads")
            n = sum(len(t["emails"]) for t in obj["threads"])
            obj["storyline"] = {"key": key, "title": sl[1], "level": sl[2], "korean": korean}
            out.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            return f"{key}: {n} emails, {len(obj.get('facts', []))} facts, {len(obj.get('questions', []))} questions in {time.time() - t0:.0f}s"
        except Exception as e:
            print(f"{key}: attempt {attempt + 1} failed: {e}", flush=True)
    return f"{key}: FAILED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--korean", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    sls = [(s, False) for s in STORYLINES] + ([(s, True) for s in KOREAN_STORYLINES] if a.korean else [])
    if a.only:
        keep = set(a.only.split(","))
        sls = [x for x in sls if x[0][0] in keep]
    with ThreadPoolExecutor(a.workers) as ex:
        for r in ex.map(lambda x: gen_one(x[0], x[1], a.model), sls):
            print(r, flush=True)


if __name__ == "__main__":
    main()
