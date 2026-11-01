"""Small text and time helpers used everywhere. Pure functions, no Django imports."""
from __future__ import annotations

import hashlib
import re
import zlib
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힣]")

# reply/forward prefixes in EN, NL, DE, SK/CZ, FR, IT, ES, Nordic and Korean Outlook
SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|vs|tr|antw|doorst|rif|r|odp|re\s*\[\d+\]|회신|전달|답장|답신|fw\s*\[\d+\])\s*[:：]\s*"
    r"|\[(?:ext|external|extern|externe?)\]\s*|\((?:ext|external)\)\s*|external\s*:\s*)+",
    re.I)


def est_tokens(text: str) -> int:
    """Rough token estimate without a tokenizer: ~4 chars per token for Latin text, ~1 per Hangul/CJK char."""
    if not text:
        return 0
    cjk = len(CJK.findall(text))
    return int(cjk * 1.0 + (len(text) - cjk) / 3.8) + 1


def clip_tokens(text: str, budget: int) -> str:
    """Cut text to roughly `budget` tokens, at a line or word boundary, with a visible marker."""
    if budget <= 0:
        return ""
    if est_tokens(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # binary search on characters
        mid = (lo + hi + 1) // 2
        if est_tokens(text[:mid]) <= budget - 8:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    nl = cut.rfind("\n")
    if nl > lo * 0.7:
        cut = cut[:nl]
    else:
        sp = cut.rfind(" ")
        if sp > lo * 0.8:
            cut = cut[:sp]
    return cut.rstrip() + f"\n[… cut: {est_tokens(text) - est_tokens(cut)} more tokens]"


def normalize_mid(mid: str) -> str:
    mid = (mid or "").strip().strip("<>").strip().lower()
    return re.sub(r"\s+", "", mid)


def split_mids(header: str) -> list[str]:
    return [normalize_mid(m) for m in re.findall(r"<([^<>]+)>", header or "")] or ([normalize_mid(header)] if header and "@" in header else [])


def subject_norm(subject: str) -> str:
    s = subject or ""
    prev = None
    while prev != s:
        prev = s
        s = SUBJECT_PREFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def has_reply_prefix(subject: str) -> bool:
    return bool(SUBJECT_PREFIX.match(subject or ""))


def subject_template(subject: str) -> str:
    s = subject_norm(subject)
    s = re.sub(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b", "<date>", s)
    s = re.sub(r"\d+", "#", s)
    return s


def is_hangul(text: str, share: float = 0.15) -> bool:
    if not text:
        return False
    letters = sum(1 for c in text if c.isalpha())
    return letters > 0 and len(HANGUL.findall(text)) / letters >= share


def detect_lang(text: str) -> str:
    return "ko" if is_hangul(text) else "en"


def sha1(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def words(text: str) -> list[str]:
    return re.findall(r"[\w가-힣]+", (text or "").lower())


def _h(s: str) -> int:
    return zlib.crc32(s.encode("utf-8"))


def shingles(text: str, n: int = 5) -> set[int]:
    """Stable word n-gram hashes (crc32, not Python's randomised hash) for quote matching."""
    w = words(text)
    if len(w) < n:
        return {_h(" ".join(w))} if w else set()
    return {_h(" ".join(w[i:i + n])) for i in range(len(w) - n + 1)}


@lru_cache(maxsize=8)
def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def local_day(dt: datetime, tzname: str) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz(tzname)).strftime("%Y-%m-%d")


def local_dt(dt: datetime, tzname: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz(tzname))


def week_of(day: str) -> str:
    d = date.fromisoformat(day)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_range(week: str) -> tuple[str, str]:
    y, w = week.split("-W")
    start = date.fromisocalendar(int(y), int(w), 1)
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


def days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def first_sentence(text: str, max_chars: int = 240) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"\[\d+\]|\*\*", "", text or "")).strip()
    m = re.match(r"^(.{20,%d}?[.!?。])(\s|$)" % max_chars, t)
    return (m.group(1) if m else t[:max_chars]).strip()


def slugify(text: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower(), flags=re.U)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    s = re.sub(r"[^a-z0-9\-가-힣]", "", s)
    return (s[:maxlen].strip("-") or "page")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
