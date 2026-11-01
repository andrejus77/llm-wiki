"""POP3 fetch from one or more department mailboxes (sources.pop3_accounts; or the single pop3_host/pop3_user fields).
Keeps a UIDL seen-list in SQLite (SeenUidl) per mailbox, so a re-run never downloads a message twice. Never deletes
mail from the server. Mail sent to two department mailboxes is stored once (Message-ID) with both departments."""
from __future__ import annotations

import logging
import os
import poplib

from llmwiki.models import SeenUidl

log = logging.getLogger(__name__)


def accounts(cfg) -> list[dict]:
    """The configured mailboxes as dicts (host, port, ssl, user, password_env, dept, key)."""
    out = []
    for a in cfg.pop3_accounts:
        if a.enabled and a.host and a.user:
            out.append({"host": a.host, "port": a.port, "ssl": a.ssl, "user": a.user, "password_env": a.password_env, "dept": a.dept,
                        "key": f"{a.user}@{a.host}"})
    if not out and cfg.pop3_host:   # the single-mailbox fields (older settings); its seen-list keys stay bare UIDLs
        out.append({"host": cfg.pop3_host, "port": cfg.pop3_port, "ssl": cfg.pop3_ssl, "user": cfg.pop3_user,
                    "password_env": cfg.pop3_password_env, "dept": "", "key": ""})
    return out


def connect(a: dict, timeout: int = 60):
    conn = poplib.POP3_SSL(a["host"], a["port"], timeout=timeout) if a["ssl"] else poplib.POP3(a["host"], a["port"], timeout=timeout)
    conn.user(a["user"])
    conn.pass_(os.environ.get(a["password_env"], ""))
    return conn


def iter_pop3(cfg):
    accs = accounts(cfg)
    if not accs:
        raise RuntimeError("No POP3 mailbox is set (Settings › Sources › pop3_accounts)")
    for a in accs:
        yield from _iter_account(a, cfg.pop3_max_per_fetch)


def _iter_account(a: dict, max_n: int):
    conn = connect(a)
    prefix = f"{a['key']}:" if a["key"] else ""
    try:
        _resp, listing, _ = conn.uidl()
        pairs = []
        for line in listing:
            num, uid = line.decode("ascii", "replace").split(" ", 1)
            pairs.append((int(num), prefix + uid.strip()))
        seen = set(SeenUidl.objects.filter(uidl__in=[u for _, u in pairs]).values_list("uidl", flat=True))
        todo = [(n, u) for n, u in pairs if u not in seen][:max_n]
        log.info("POP3 %s: %s on the server, %s new", a["key"] or a["user"], len(pairs), len(todo))
        for num, uid in todo:
            _resp, lines, _ = conn.retr(num)
            raw = b"\r\n".join(lines) + b"\r\n"
            yield raw, f"pop3:{uid}", "", a["dept"]
            SeenUidl.objects.get_or_create(uidl=uid)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
