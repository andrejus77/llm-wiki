"""Secrets (API keys, POP3 passwords) without environment variables.

Settings only ever hold a secret's NAME (e.g. `api_key_secret: api-a`); the value is looked up here, in this order:
 1. the Windows Credential Manager, generic credential "llmwiki/<name>" (per Windows user; set with
    `llmwiki secret set <name>` — the value is typed hidden, never shown again)
 2. data/secrets.yaml  (name: value; for machines without a Credential Manager; the data folder is never committed)
 3. an environment variable of that name (older setups)

Run `llmwiki secret set` as the Windows account that runs the portal: Credential Manager entries belong to one user.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)
PREFIX = "llmwiki/"


# ------------------------------------------------------------------ Windows Credential Manager (ctypes, no extra package)
def _wincred():
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))]
    adv.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, wintypes, CREDENTIAL, adv


GENERIC, PERSIST_LOCAL_MACHINE = 1, 2


def _win_get(name: str) -> str | None:
    w = _wincred()
    if not w:
        return None
    ctypes, _wt, CREDENTIAL, adv = w
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(PREFIX + name, GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        adv.CredFree(p)


def _win_set(name: str, value: str) -> None:
    w = _wincred()
    if not w:
        raise RuntimeError("the Windows Credential Manager is only available on Windows; use --file")
    ctypes, _wt, CREDENTIAL, adv = w
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = CREDENTIAL()
    c.Type, c.TargetName, c.Persist = GENERIC, PREFIX + name, PERSIST_LOCAL_MACHINE
    c.CredentialBlobSize, c.CredentialBlob = len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.UserName, c.Comment = "llmwiki", "LLM Wiki secret (llmwiki secret set)"
    if not adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW failed")


def _win_delete(name: str) -> bool:
    w = _wincred()
    return bool(w and w[3].CredDeleteW(PREFIX + name, GENERIC, 0))


def _win_list() -> list[str]:
    w = _wincred()
    if not w:
        return []
    ctypes, wt, CREDENTIAL, adv = w
    n = wt.DWORD()
    arr = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not adv.CredEnumerateW(PREFIX + "*", 0, ctypes.byref(n), ctypes.byref(arr)):
        return []
    try:
        return sorted(arr[i].contents.TargetName[len(PREFIX):] for i in range(n.value))
    finally:
        adv.CredFree(arr)


# ------------------------------------------------------------------ data/secrets.yaml
def _file() -> Path:
    from llmwiki.conf import data_dir
    return data_dir() / "secrets.yaml"


def _file_all() -> dict:
    p = _file()
    if not p.exists():
        return {}
    import yaml
    try:
        return {str(k): str(v) for k, v in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).items()}
    except Exception:
        log.exception("data/secrets.yaml could not be read")
        return {}


def _file_set(name: str, value: str | None) -> None:
    import yaml
    d = _file_all()
    if value is None:
        d.pop(name, None)
    else:
        d[name] = value
    p = _file()
    p.write_text("# LLM Wiki secrets (never commit; prefer the Windows Credential Manager: llmwiki secret set NAME)\n"
                 + yaml.safe_dump(d, allow_unicode=True, sort_keys=True), encoding="utf-8")


# ------------------------------------------------------------------ public API
def get(name: str | None, default: str = "") -> str:
    """The secret's value, or `default` if it is set nowhere."""
    if not name:
        return default
    try:
        v = _win_get(name)
        if v is not None:
            return v
    except Exception:
        log.debug("credential manager read failed for %s", name, exc_info=True)
    v = _file_all().get(name)
    if v is not None:
        return v
    return os.environ.get(name, default)


def where(name: str) -> str:
    """Where a secret is found: 'credential manager', 'data/secrets.yaml', 'environment' or '' (missing)."""
    try:
        if _win_get(name) is not None:
            return "credential manager"
    except Exception:
        pass
    if name in _file_all():
        return "data/secrets.yaml"
    if os.environ.get(name):
        return "environment"
    return ""


def put(name: str, value: str, to_file: bool = False) -> str:
    if to_file or sys.platform != "win32":
        _file_set(name, value)
        return "data/secrets.yaml"
    _win_set(name, value)
    return "credential manager"


def delete(name: str) -> list[str]:
    gone = []
    if _win_delete(name):
        gone.append("credential manager")
    if name in _file_all():
        _file_set(name, None)
        gone.append("data/secrets.yaml")
    return gone


def stored_names() -> dict[str, str]:
    out = {n: "credential manager" for n in _win_list()}
    for n in _file_all():
        out.setdefault(n, "data/secrets.yaml")
    return out


def needed() -> list[tuple[str, str]]:
    """(secret name, what uses it) for the current settings."""
    from llmwiki.conf import get_settings
    s = get_settings()
    out = []
    for ep in s.pool.endpoints:
        if ep.enabled and ep.api_key_secret:
            out.append((ep.api_key_secret, f"endpoint {ep.name}"))
        for k, v in (ep.options.get("headers") or {}).items():
            for nm in _refs(str(v)):
                out.append((nm, f"endpoint {ep.name} header {k}"))
    if s.sources.pop3_enabled:
        from llmwiki.sources.pop3 import accounts
        for a in accounts(s.sources):
            out.append((a["password_secret"], f"POP3 {a['user']}@{a['host']}"))
    return out


def _refs(text: str) -> list[str]:
    import re
    return re.findall(r"\{secret:([\w.@-]+)\}", text)


def fill(text: str) -> str:
    """Replace {secret:NAME} placeholders (headers, URLs in endpoint options)."""
    import re
    return re.sub(r"\{secret:([\w.@-]+)\}", lambda m: get(m.group(1)), text)
