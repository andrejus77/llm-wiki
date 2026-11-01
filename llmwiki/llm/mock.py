"""Deterministic offline model for tests. It reads the `TASK: <stage>` line and the numbered sections of the prompt
and returns schema-valid JSON built from the input text. Not smart — just enough to exercise every code path."""
from __future__ import annotations

import json
import re

from llmwiki.llm.adapters import Adapter

AUTO = re.compile(r"no-?reply|newsletter|notification|out of office|automatic reply|job report|batch job|unsubscribe|digest", re.I)


def _emails(prompt: str) -> list[dict]:
    out = []
    for m in re.finditer(r"^### \[(\d+)\] ([^\n]*)\n(.*?)(?=^### \[\d+\]|^## |\Z)", prompt, re.M | re.S):
        body = m.group(3)
        subj = re.search(r"^Subject: (.*)$", body, re.M)
        text = re.sub(r"^(To|Subject|Attachments|Note):.*$", "", body, flags=re.M).strip()
        out.append({"n": int(m.group(1)), "head": m.group(2), "subject": subj.group(1).strip() if subj else "", "text": text})
    return out


def _sentence(t: str, n: int = 220) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"(.{20,%d}?[.!?])\s" % n, t + " ")
    return (m.group(1) if m else t[:n]).strip() or "No text."


def _cands(prompt: str) -> list[tuple[str, str]]:
    return re.findall(r"^(P\d+): (.+?) — level", prompt, re.M)


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", s.lower())}


class Mock(Adapter):
    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        task = (re.search(r"^TASK: (\w+)", prompt, re.M) or [None, ""])[1]
        fn = getattr(self, "_" + task, None)
        if fn is None:
            if "chat agent" in system.lower() or "TASK: chat" in prompt:
                return self._chat(system, prompt)
            return json.dumps({"error": f"mock has no handler for {task!r}"})
        return fn(system, prompt)

    def _notes(self, prompt):
        notes = []
        for e in _emails(prompt):
            txt = e["text"]
            imp = "not_important" if AUTO.search(e["head"] + " " + e["subject"] + " " + txt[:200]) else "important" if re.search(r"decid|block|urgent|deadline|issue|problem|error|moved|delay", txt, re.I) else "routine"
            kind = "decision" if re.search(r"decid|agreed|we will|approved", txt, re.I) else "issue" if re.search(r"block|error|problem|fail|delay", txt, re.I) else "request" if "?" in txt else "info"
            due = re.search(r"\b(20\d\d-\d\d-\d\d)\b", txt)
            notes.append({"email": e["n"], "note": _sentence(e["subject"] + ": " + txt, 260), "kind": kind, "importance": imp, "due": due.group(1) if due else None})
        return notes

    def _s1(self, system, prompt):
        return json.dumps({"notes": self._notes(prompt)})

    def _route(self, prompt):
        ems = _emails(prompt)
        cands = _cands(prompt)
        words = _words(" ".join(e["subject"] + " " + e["text"][:400] for e in ems))
        best, score = None, 0
        for lab, title in cands:
            sc = len(words & _words(title))
            if sc > score:
                best, score = lab, sc
        items = [{"section": "overview", "text": _sentence(e["text"], 300), "emails": [e["n"]]} for e in ems if len(e["text"]) > 30][:3]
        if not items:
            return []
        if best and score >= 1:
            return [{"page": best, "role": "primary", "items": items, "status": None, "open_add": [], "open_close": []}]
        title = re.sub(r"^(re|fw|fwd|aw)\s*:\s*", "", ems[0]["subject"], flags=re.I)[:80] or "Untitled topic"
        return [{"page": "new", "title": title, "level": "topic", "why": "no candidate matches", "role": "primary", "items": items}]

    def _s2(self, system, prompt):
        return json.dumps({"pages": self._route(prompt)})

    def _s12(self, system, prompt):
        notes = self._notes(prompt)
        pages = [] if all(n["importance"] == "not_important" for n in notes) else self._route(prompt)
        return json.dumps({"notes": notes, "pages": pages})

    def _s123(self, system, prompt):
        d = json.loads(self._s12(system, prompt))
        cur = re.findall(r"^### section: (\w+)\n(.*?)(?=^### |^## |\Z)", prompt, re.M | re.S)
        if cur and d["pages"] and d["pages"][0]["page"] != "new":
            d["sections"] = [{"section": k, "text": (t.strip() + " " + d["pages"][0]["items"][0]["text"]).strip(), "removed_reason": ""} for k, t in cur[:1]]
        return json.dumps(d)

    def _s3(self, system, prompt):
        secs = re.findall(r"^### section: (\w+) — [^\n]*\n(.*?)(?=^### section|^## |\Z)", prompt, re.M | re.S)
        items = re.findall(r"^\[(\w+)\] (.+?) \(emails", prompt, re.M)
        out = []
        for k, cur in secs:
            cur = cur.strip()
            cur = "" if cur == "(empty)" else cur
            add = " ".join(t for kk, t in items if kk == k)
            out.append({"section": k, "text": (cur + ("\n\n" if cur else "") + add).strip() or "No information yet.", "removed_reason": ""})
        return json.dumps({"sections": out})

    def _s3c(self, system, prompt):
        secs = re.findall(r"^### section: (\w+) — [^\n]*\nORIGINAL:\n(.*?)\n\nDRAFT:\n(.*?)\n\nCHECKER:", prompt, re.M | re.S)
        items = re.findall(r"NEW ITEMS: (.*)$", prompt, re.M)
        out = []
        for i, (k, orig, draft) in enumerate(secs):
            base = orig.strip() if orig.strip() != "(empty)" else ""
            extra = items[i] if i < len(items) else ""
            out.append({"section": k, "text": (base + "\n\n" + extra.replace(" | ", " ")).strip() or draft, "removed_reason": ""})
        return json.dumps({"sections": out})

    @staticmethod
    def _block(prompt: str, title: str) -> str:
        m = re.search(r"^## " + re.escape(title) + r"[^\n]*\n(.*?)(?=^## |\Z)", prompt, re.M | re.S)
        return m.group(1) if m else ""

    def _page(self, system, prompt):
        cur = re.findall(r"^### ([^\n]+)\n(.*?)(?=^### |\Z)", self._block(prompt, "Current page"), re.M | re.S)
        facts = re.findall(r"^(.+?) \(emails[^)]*\)$", self._block(prompt, "New facts to write in"), re.M)
        secs = {t.strip(): x.strip() for t, x in cur if x.strip() and "(empty" not in x}
        summary = secs.pop("Summary", "") or secs.pop("Overview", "") or (_sentence(facts[0], 200) if facts else "A topic of the team.")
        if facts:
            secs["Details"] = (secs.get("Details", "") + " " + " ".join(facts)).strip()
        return json.dumps({"summary": summary, "sections": [{"title": t, "text": x} for t, x in secs.items()], "removed": ""})

    def _pagec(self, system, prompt):
        d = re.search(r"^DRAFT:\n(.*?)\n\nCHECKER:", prompt, re.M | re.S)
        parts = re.findall(r"^### ([^\n]+)\n(.*?)(?=^### |\Z)", d.group(1) if d else "", re.M | re.S)
        secs = {t.strip(): x.strip() for t, x in parts}
        summary = secs.pop("Summary", "") or "A topic of the team."
        return json.dumps({"summary": summary, "sections": [{"title": t, "text": x} for t, x in secs.items()], "removed": ""})

    def _s4(self, system, prompt):
        systems = re.search(r"system \(0–2\): (.*)$", system, re.M).group(1).split(", ")
        procs = re.search(r"process \(0–2\): (.*)$", system, re.M).group(1).split(", ")
        wts = re.search(r"work_type \(exactly 1\): (.*)$", system, re.M).group(1).split(", ")
        pages = []
        for lab, rest in re.findall(r"^(P\d+): (.*)$", prompt, re.M):
            low = rest.lower()
            sy = [x for x in systems if x.split("-")[-1] in low][:2] or systems[:1]
            pr = [x for x in procs if x.split("-")[0] in low][:2]
            pages.append({"page": lab, "system": sy, "process": pr, "work_type": [wts[0]]})
        codes = [{"code": c, "is_tcode": True} for c in re.findall(r"^([A-Z/][A-Z0-9_/]+): …", prompt, re.M)]
        return json.dumps({"pages": pages, "tcodes": codes})

    def _review(self, system, prompt):
        return json.dumps({"findings": []})

    def _lead(self, system, prompt):
        facts = re.sub(r"\s+", " ", prompt.split("\n", 2)[-1])[:1200]
        lo = int(re.search(r"(\d+)–(\d+) characters", system).group(1))
        txt = ("This week the department worked on several topics. " + facts)[: max(lo + 50, 400)]
        return json.dumps({"lead": txt.rsplit(" ", 1)[0] + "."})

    def _story(self, system, prompt):
        lo, hi = map(int, re.search(r"(\d+)–(\d+) characters", system).groups())
        facts = re.sub(r"\s+", " ", prompt.split("\n", 2)[-1])
        txt = ("This week: " + facts)[: (lo + hi) // 2]
        while len(txt) < lo:
            txt += " More details are on the page."
        return json.dumps({"text": txt.rsplit(" ", 1)[0] + "."})

    def _judge(self, system, prompt):
        return json.dumps({"correct": 1, "faithful": 1, "abstained": 1, "reason": "mock"})

    def _plan(self, system, prompt):
        q = (re.search(r"^Question: (.*)$", prompt, re.M) or [None, "the question"])[1]
        return json.dumps({"subquestions": [{"q": q, "source": "wiki"}, {"q": q, "source": "emails"}]})

    def _chat(self, system, prompt):
        # agent rounds: answer immediately from numbered sources if any
        nums = re.findall(r"^\[(\d+)\] ", prompt, re.M)
        if not nums and "search_wiki" in system and "ROUND 1" in prompt:
            q = (re.search(r"^Question: (.*)$", prompt, re.M) or [None, "x"])[1]
            return json.dumps({"action": "tool", "tool": "search_wiki", "args": {"query": q}})
        if nums:
            first = re.search(r"^\[%s\] ([^\n]*)\n([^\n]*)" % nums[0], prompt, re.M)
            snippet = first.group(2)[:300] if first else ""
            return json.dumps({"action": "answer", "answer": f"According to the sources, {snippet} [{nums[0]}]", "sources": [int(nums[0])], "confidence": "medium"})
        return json.dumps({"action": "answer", "answer": "Not in the wiki.", "sources": [], "confidence": "none"})

    _research = _chat
    _write = _chat
