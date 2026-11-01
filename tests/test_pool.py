"""The endpoint pool: retries on rate limits / broken output, chat priority."""
import threading
import time

import pytest

from llmwiki.conf import Endpoint, get_settings, use_settings
from llmwiki.llm.pool import Pool, StageFailed


def settings_with(endpoints):
    s = get_settings().model_copy(deep=True)
    s.pool.endpoints = endpoints
    s.pool.backoff_base_s = 1
    s.schedule.chat_idle_resume_s = 1
    return s


def build(cap, scale):
    return "Return JSON.", "TASK: s1\n### [1] 2026-10-20 09:00 · A <a@x>\nSubject: hello\nA decision was made.", [{"key": "new", "label": "x", "want": 10, "budget": 10, "sent": 10}]


def parse_ok(o):
    return (o, []) if isinstance(o, dict) and o.get("notes") else (None, ["need notes"])


def test_chaos_429_and_broken_json_are_retried():
    eps = [Endpoint(name="chaos", kind="chaos", rpm=600, max_concurrent=4,
                    options={"inner": "mock", "p429": 0.5, "ptimeout": 0, "p5xx": 0, "pbroken": 0.3, "pempty": 0, "retry_after": 0.05, "seed": 3})]
    with use_settings(settings_with(eps)):
        p = Pool()
        ok = 0
        for _ in range(12):
            try:
                p.run_structured("s1", "test", build, parse_ok)
                ok += 1
            except StageFailed:
                pass
        assert ok >= 9            # most succeed despite 50 % 429s and 30 % broken JSON
        st = p.status()[0]["stats"]
        assert st["rejected"] > 0


def test_background_waits_while_chat_is_active():
    eps = [Endpoint(name="mock", kind="mock", rpm=600, max_concurrent=4)]
    with use_settings(settings_with(eps)):
        p = Pool()
        st = p.acquire("chat", chat=True)
        got = []
        t = threading.Thread(target=lambda: got.append(p.acquire("s1")))
        t.start()
        time.sleep(0.6)
        assert not got            # paused while the chat request runs
        p.release(st, chat=True)
        t.join(timeout=5)         # resumes chat_idle_resume_s (1 s) after the chat
        assert got


def test_rate_limit_window():
    eps = [Endpoint(name="slow", kind="mock", rpm=2, max_concurrent=4)]
    s = settings_with(eps)
    s.pool.chat_reserve_rpm = 0
    with use_settings(s):
        p = Pool()
        a = p.acquire("s1")
        b = p.acquire("s1")
        p.release(a)
        p.release(b)
        from llmwiki.llm.pool import Paused
        with pytest.raises(Paused):
            p.acquire("s1", wait_limit=1.0)   # a third request within 60 s must wait
