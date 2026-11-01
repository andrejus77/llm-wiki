"""Editor guards: the script decides whether a rewritten section may replace the old one.

Hard reject : empty text; shrinking below min_keep_ratio × old length without a removed_reason.
Fixed       : headings inside a section → bold text; chatter like "Here is the updated section:" removed;
              [n] markers mapped to emails by the caller (unknown numbers dropped).
Warnings    : length change > warn_change; an item's key terms (numbers, codes, names, dates) missing; over max_words.
A warning triggers the confirm round (guards.confirm = on_warning).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from llmwiki.conf import get_settings

CHATTER = re.compile(r"^\s*(?:here(?:'s| is)|sure[,!.]|below is|the (?:updated|revised|new) (?:section|text)|updated section|i have|i've)\b[^\n]{0,120}:\s*$", re.I | re.M)


def clean(text: str) -> str:
    t = (text or "").strip()
    t = CHATTER.sub("", t, count=1).strip()
    t = re.sub(r"^#{1,6}\s*(.+)$", r"**\1**", t, flags=re.M)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def nwords(t: str) -> int:
    return len(re.findall(r"\S+", re.sub(r"\[\d+\]", "", t or "")))


KEY_TERM = re.compile(r"\b(?:\d[\d.,:/-]*\d|\d+|[A-Z]{2,}[\w/-]*\d[\w/-]*|/[A-Z]+/[A-Z_]+)\b")


def key_terms(items: list[str]) -> set[str]:
    terms = set()
    for t in items:
        for m in KEY_TERM.findall(t):
            if len(m) >= 2:
                terms.add(m)
    return terms


def sentences(t: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", re.sub(r"\[\d+\]", "", t or "")) if len(s.strip()) > 15]


def diff_summary(old: str, new: str) -> dict:
    nw = set(re.findall(r"\w+", new.lower()))
    removed = []
    for s in sentences(old):
        w = set(re.findall(r"\w+", s.lower()))
        if w and len(w & nw) / len(w) < 0.5:
            removed.append(s)
    old_terms = set(KEY_TERM.findall(old)) | set(re.findall(r"\b[A-Z][a-z]{2,}(?:\s[A-Z][a-z]+)?\b", old))
    new_terms = set(KEY_TERM.findall(new)) | set(re.findall(r"\b[A-Z][a-z]{2,}(?:\s[A-Z][a-z]+)?\b", new))
    return {"removed_sentences": removed[:12], "removed_terms": sorted(old_terms - new_terms)[:30],
            "old_words": nwords(old), "new_words": nwords(new)}


@dataclass
class GuardResult:
    rows: list = field(default_factory=list)   # [label, ok, warn_only]
    hard: bool = False
    warn: bool = False


STOP = set("the a an and or of to in on at for with from by is are was were be been has have had will would this that these those it its "
           "as not no but if then than so also all any each per via into after before about over under who what when which our their they "
           "we he she his her them us more less only one two".split())


def content_words(s: str) -> set[str]:
    return {w for w in re.findall(r"[\w가-힣][\w가-힣.,/-]*[\w가-힣]|\d", s.lower()) if w not in STOP and (len(w) > 2 or w.isdigit())}


def drop_meta(text: str, patterns: list[str]) -> tuple[str, list[str]]:
    """Remove sentences that talk about the sources or the page instead of giving facts. Returns (text, removed)."""
    if not patterns:
        return text, []
    rx = [re.compile(p, re.I) for p in patterns]
    removed, lines = [], []
    for line in (text or "").split("\n"):
        bullet = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line)
        if bullet:
            if any(r.search(line) for r in rx):
                removed.append(line.strip())
                continue
            lines.append(line)
            continue
        parts = re.split(r"(?<=[.!?])\s+", line)
        keep = [s for s in parts if not any(r.search(s) for r in rx)]
        removed += [s for s in parts if s not in keep]
        if parts and not keep and line.strip():
            continue
        lines.append(" ".join(keep))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out, [r for r in removed if r.strip()]


def repeats(parts: list[tuple[str, str]], sim: float, summary_key: str = "summary") -> list[tuple[str, str, str, str]]:
    """Repeated sentences across the page: [(key_a, sentence_a, key_b, sentence_b)]. Sentences of ≥ 5 content words whose
    overlap (share of the shorter one's words found in the other) is ≥ sim; summary vs a section needs sim + 0.15."""
    sents = [(k, s, content_words(s)) for k, t in parts for s in sentences(t)]
    sents = [x for x in sents if len(x[2]) >= 5]
    out = []
    for i in range(len(sents)):
        for j in range(i + 1, len(sents)):
            (ka, sa, wa), (kb, sb, wb) = sents[i], sents[j]
            need = sim + 0.15 if summary_key in (ka, kb) and ka != kb else sim
            if len(wa & wb) / min(len(wa), len(wb)) >= min(need, 1.0):
                out.append((ka, sa, kb, sb))
    return out


@dataclass
class PageDraft:
    summary: str
    sections: list[tuple[str, str]]          # [(title, text)]
    removed: str = ""

    def parts(self) -> list[tuple[str, str]]:
        return [("summary", self.summary)] + [(t, x) for t, x in self.sections]

    def words(self) -> int:
        return sum(nwords(x) for _, x in self.parts())

    def render(self) -> str:
        return f"### Summary\n{self.summary}\n\n" + "\n\n".join(f"### {t}\n{x}" for t, x in self.sections)


def check_page(old_words: int, d: PageDraft, item_texts: list[str], budget: int, final: bool = False) -> GuardResult:
    """Guards for a whole-page rewrite (free structure). Hard: empty summary; a large unexplained shrink below what the
    budget allows. Warnings (→ confirm round): over the budget / section limits, repeated sentences, missing key terms."""
    g, pg = get_settings().guards, get_settings().pages
    r = GuardResult()

    def add(label, ok, warn_only=False):
        r.rows.append([label, bool(ok), warn_only])
        if not ok:
            if warn_only:
                r.warn = True
            else:
                r.hard = True
    nw = d.words()
    add("summary not empty", nwords(d.summary) > 0)
    ref = min(old_words, budget)
    if ref >= 30:
        explained = len(d.removed or "") >= g.removed_reason_min_chars
        add(f"length {nw} words vs {old_words} before (≥ {g.min_keep_ratio}× of min(before, budget {budget}) unless 'removed' says why)",
            nw >= g.min_keep_ratio * ref or explained)
    tol = 1 + pg.tolerance
    over_hard = pg.over_budget == "reject" and final
    add(f"page ≤ {budget} words (budget from the mail behind it): {nw}", nw <= budget * tol, not over_hard)
    add(f"summary ≤ {pg.summary_words} words: {nwords(d.summary)}", nwords(d.summary) <= pg.summary_words * tol, True)
    add(f"≤ {pg.max_sections} sections: {len(d.sections)}", len(d.sections) <= pg.max_sections, True)
    long = [f"{t} ({nwords(x)})" for t, x in d.sections if nwords(x) > pg.section_words * tol]
    add(f"each section ≤ {pg.section_words} words" + (f" (over: {', '.join(long[:4])})" if long else ""), not long, True)
    if pg.repeat_check:
        rep = repeats(d.parts(), pg.repeat_similarity)
        add("no repeated sentences" + (f" ({len(rep)} repeated, e.g. '{rep[0][3][:90]}')" if rep else ""), not rep, True)
    if g.check_key_terms and item_texts:
        terms = key_terms(item_texts)
        text = d.render()
        missing = sorted(t for t in terms if t not in text)
        add("key terms of the new facts present" + (f" (missing: {', '.join(missing[:6])})" if missing else ""), not missing, True)
    return r


def check(old: str, new: str, item_texts: list[str], removed_reason: str) -> GuardResult:
    g = get_settings().guards
    r = GuardResult()
    ow, nw = nwords(old), nwords(new)

    def add(label, ok, warn_only=False):
        r.rows.append([label, bool(ok), warn_only])
        if not ok:
            if warn_only:
                r.warn = True
            else:
                r.hard = True

    add("not empty", nw > 0)
    if ow >= 30:
        ratio = nw / max(ow, 1)
        explained = len(removed_reason or "") >= g.removed_reason_min_chars
        add(f"length {nw} words vs {ow} before (≥ {g.min_keep_ratio}× unless a removed_reason is given)", ratio >= g.min_keep_ratio or explained)
        add(f"length change ≤ {int(g.warn_change * 100)} %", abs(nw - ow) / ow <= g.warn_change or nw > ow, True)
    if g.check_key_terms and item_texts:
        terms = key_terms(item_texts)
        missing = sorted(t for t in terms if t not in new)
        add("key terms of the new items present" + (f" (missing: {', '.join(missing[:6])})" if missing else ""), not missing, True)
    # over the word limit is only flagged for the reviewer (split), it does not trigger a confirm round
    r.rows.append([f"≤ {g.max_words} words (else flagged for a split)", nw <= g.max_words, True])
    return r
