"""Personal data (PII) removal and the rule which endpoint may see which stage.

- redact(text): phone numbers, e-mail addresses, IBANs, ID numbers and street addresses → [phone], [email], …
  Reference numbers (store.identifier_patterns: PO, delivery, invoice, container, MRN) are masked first, so a
  10-digit PO number is never mistaken for a phone number. Patterns live in settings.privacy.pii_patterns.
- endpoint_allows(ep, stage): an external endpoint (data leaves the company) only serves privacy.external_stages,
  unless the store holds only the synthetic test data (privacy.test_data_only).

Used by the pool on every prompt for endpoints with redact_pii/external, and on every model output when
privacy.redact_outputs is on (so the wiki, notes and reports never carry the data, even if the model copies it).
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache

from llmwiki.conf import get_settings

log = logging.getLogger(__name__)

PLACEHOLDER = {"phone": "[phone]", "phone_labelled": "[phone]", "phone_local": "[phone]", "email": "[email]", "iban": "[iban]",
               "kr_rrn": "[id-number]", "street": "[address]", "postcode_nl": "[address]", "kr_address": "[address]"}


_NAME_WITH_ID = re.compile(r"([^\x00-\u024F\s(]) \([A-Za-z0-9][\w.+-]{0,63}\)")


@lru_cache(maxsize=16)
def _compile(items: tuple) -> list[tuple[str, re.Pattern]]:
    out = []
    for name, pat in items:
        if not pat:
            continue
        try:
            out.append((name, re.compile(pat)))
        except re.error as e:
            log.error("privacy pattern %s is not a valid regex: %s", name, e)
    return out


def _keep_addresses() -> tuple[set[str], tuple[str, ...]]:
    s = get_settings()
    return {a.lower() for a in s.sources.dept_addresses}, tuple("@" + d.lower() for d in s.sources.internal_domains)


def redact(text: str, keep_internal: bool = False) -> tuple[str, int]:
    """Return (text without PII, number of replacements). keep_internal keeps department and internal-domain addresses."""
    if not text:
        return text, 0
    s = get_settings()
    # 1. protect reference numbers (they are business facts, not personal data)
    protected: list[str] = []

    def protect(m):
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"
    for pat in s.store.identifier_patterns.values():
        try:
            text = re.sub(pat, protect, text)
        except re.error:
            continue
    n = 0
    depts, domains = _keep_addresses() if keep_internal else (set(), ())
    for name, rx in _compile(tuple(sorted(s.privacy.pii_patterns.items()))):
        def repl(m, name=name):
            nonlocal n
            val = m.group(0)
            if name == "email" and keep_internal and (val.lower() in depts or val.lower().endswith(domains)):
                return val
            n += 1
            return PLACEHOLDER.get(name, f"[{name}]")
        text = rx.sub(repl, text)
    if not keep_internal:   # outside the company a name stays a name: "홍길동 (gd.hong)" loses the user id
        text = _NAME_WITH_ID.sub(r"\1", text)
    if protected:
        text = re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)
    return text, n


def endpoint_allows(ep, stage: str) -> bool:
    """Privacy rule on top of the endpoint's own stage list."""
    if not getattr(ep, "external", False):
        return True
    p = get_settings().privacy
    return p.test_data_only or stage in p.external_stages


def prompt_needs_redaction(ep) -> bool:
    return bool(getattr(ep, "redact_pii", False) or getattr(ep, "external", False))
