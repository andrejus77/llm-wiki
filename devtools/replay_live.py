"""Live-mode replay: drip the synthetic emails from a given day onward into a drop folder in time order and run a live
tick after every batch (exactly what the scheduler does every 5 minutes, without waiting).

    manage.py shell -c "from devtools.replay_live import replay; replay('2026-10-26', per_tick=3, ticks=10)"
"""
from __future__ import annotations

import email.utils
import shutil
from pathlib import Path

from llmwiki.conf import get_settings, save_settings
from llmwiki.pipeline.runner import process
from llmwiki.sources.fetch import fetch

ROOT = Path(__file__).resolve().parents[1]


def replay(since: str, per_tick: int = 3, ticks: int = 10, src: str = "testdata/eml") -> list[dict]:
    files = []
    for p in sorted((ROOT / src).glob("*.eml")):
        raw = p.read_bytes()
        hdr = raw[:4000].decode("utf-8", "replace")
        date = next((l[5:].strip() for l in hdr.splitlines() if l.lower().startswith("date:")), "")
        try:
            dt = email.utils.parsedate_to_datetime(date)
        except Exception:
            continue
        if dt.strftime("%Y-%m-%d") >= since:
            files.append((dt, p))
    files.sort()
    drop = Path(get_settings().sources.eml_folder or (ROOT / "data" / "live-drop"))
    drop.mkdir(parents=True, exist_ok=True)
    s = get_settings().model_copy(deep=True)
    s.sources.eml_folder = str(drop)
    save_settings(s, user="replay", note="live replay drop folder")
    out = []
    for t in range(ticks):
        batch = files[t * per_tick:(t + 1) * per_tick]
        if not batch:
            break
        for _, p in batch:
            shutil.copy2(p, drop / p.name)
        st = fetch(["eml"], eml_folder=str(drop))
        r = process("live") if st["stored"] else {"skipped": "no new mail"}
        out.append({"tick": t + 1, "new": st["stored"], **{k: r.get(k) for k in ("units", "fast", "staged", "pages_edited", "unit_errors", "seconds")}})
        print(out[-1], flush=True)
    return out
