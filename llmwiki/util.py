"""Small text and time helpers used everywhere. Pure functions, no Django imports."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import zlib
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힣]")
# An image in a mail body. "[image: cid:…]" is matched to its attachment by Content-ID; "[image: https://…]" is a link.
IMAGE_TOKEN = re.compile(r"\[image:[^\]\n]*\]")

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


LANG_NAMES = {"en": "English", "ko": "Korean", "ja": "Japanese", "zh": "Chinese", "ru": "Russian", "ar": "Arabic", "he": "Hebrew", "th": "Thai", "el": "Greek", "hi": "Hindi",
              "de": "German", "nl": "Dutch", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "sk": "Slovak", "cs": "Czech", "pl": "Polish"}
_SCRIPTS = [("ko", HANGUL), ("ja", re.compile(r"[぀-ヿ]")), ("zh", re.compile(r"[㐀-䶿一-鿿]")), ("ru", re.compile(r"[Ѐ-ӿ]")), ("ar", re.compile(r"[؀-ۿ]")),
            ("he", re.compile(r"[֐-׿]")), ("th", re.compile(r"[฀-๿]")), ("el", re.compile(r"[Ͱ-Ͽ]")), ("hi", re.compile(r"[ऀ-ॿ]"))]
_COMMON = {"en": "the and to of is for with that this you are have will from please",
           "de": "der die das und ist nicht mit für wir sie ein eine auf bitte",
           "nl": "de het een en is niet met voor wij je van op dat graag",
           "fr": "le la les des et est pas pour nous vous une dans avec merci",
           "es": "el la los las y es no para con una por que gracias",
           "it": "il la gli e non per con una che sono grazie della",
           "pt": "o os as e não para com uma por que obrigado está",
           "sk": "a je na sa to že pre som nie ako prosím ďakujem",
           "cs": "a je na se to že pro jsem ne jako prosím děkuji",
           "pl": "i jest na się to że dla nie jak proszę dziękuję oraz"}
_COMMON_SETS = {k: set(v.split()) for k, v in _COMMON.items()}


def detect_lang(text: str) -> str:
    """ISO code of the mail's main language, by script (a share of the letters is enough: such mail mixes in English
    terms); Latin-script mail by its common words, English when in doubt."""
    t = IMAGE_TOKEN.sub(" ", re.sub(r"https?://\S+", " ", text or ""))
    letters = sum(1 for c in t if c.isalpha())
    if not letters:
        return "en"
    shares = {code: len(rx.findall(t)) / letters for code, rx in _SCRIPTS}
    if shares["ko"] < 0.08 <= shares["ja"] + shares["zh"] and shares["ja"] >= 0.05:
        return "ja"
    code = max(shares, key=shares.get)
    if shares[code] >= 0.08:
        return code
    found = re.findall(r"[^\W\d_]+", t.lower())
    hits = {k: sum(1 for w in found if w in v) for k, v in _COMMON_SETS.items()}
    best = max(hits, key=hits.get)
    return best if best != "en" and hits[best] >= 3 and hits[best] > hits["en"] * 1.5 else "en"


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def model_text(text: str) -> str:
    """Mail text for prompts and the search index: an image is only a marker, its long reference says nothing."""
    return IMAGE_TOKEN.sub("[image]", text or "")


def display_name(name: str, addr: str) -> str:
    """A name in another script (Korean, …) is followed by the user id from the address, so everyone can read and find it."""
    name, uid = (name or "").strip(), (addr or "").split("@")[0].strip()
    if not name:
        return uid
    foreign = any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in name)
    return f"{name} ({uid})" if foreign and uid and uid.lower() not in name.lower() else name


def sha1(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def words(text: str) -> list[str]:
    # without image references: Outlook gives the same picture another Content-ID in every reply
    return re.findall(r"[\w가-힣]+", IMAGE_TOKEN.sub(" ", text or "").lower())


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
