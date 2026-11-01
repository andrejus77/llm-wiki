"""The endpoint pool: rate limits, concurrency, chat priority, retries, and a log row for every call.

Usage (every LLM request in the app goes through `run_structured`):

    res = pool.run_structured(stage="s1", unit=key, build=lambda cap, scale: (system, prompt, sections),
                              parse=lambda obj: (model_or_None, errors), chat=False)

- A slot is acquired on the first free endpoint that allows the stage: sliding 60 s window (rpm) + max_concurrent.
  Background stages leave `chat_reserve_rpm` free, and wait completely while a chat is active
  (schedule.pause_background_on_chat; resumes chat_idle_resume_s after the last chat request).
- The prompt is built for that endpoint's cap (min(budget cap, endpoint context)) — `build(cap, scale)`.
- 429 → Retry-After or exponential backoff with jitter, endpoint cooling, rpm lowered after repeated 429s.
- Timeout → one retry with every budget × (1 - timeout_shrink). 5xx → up to max_retries_5xx.
- Unusable output → up to validation_retries extra rounds with the exact error list.
- Blocked input (e.g. a PII filter) or an error → failover: the same request goes to the next endpoint that allows
  the stage (pool.failover_on_error); only when none is left does the first endpoint get retried.
- Privacy: an external endpoint only gets privacy.external_stages (privacy.endpoint_allows); prompts to external
  endpoints and to endpoints with redact_pii have personal data removed; model outputs too (privacy.redact_outputs).
- schema: the stage's JSON schema (llm/schemas.py) is passed to endpoints with structured_output on.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from llmwiki.conf import get_settings
from llmwiki import privacy
from llmwiki.llm.adapters import Blocked, LlmError, RateLimited, ServerError, Timeout, make_adapter
from llmwiki.llm.jsonparse import extract_json

log = logging.getLogger(__name__)


class StageFailed(Exception):
    def __init__(self, msg, calls=None, raw=""):
        super().__init__(msg)
        self.calls = calls or []
        self.raw = raw


class Paused(Exception):
    pass


@dataclass
class EpState:
    cfg: object
    adapter: object
    window: deque = field(default_factory=deque)
    in_flight: int = 0
    cooling_until: float = 0.0
    rpm: int = 0
    n429: int = 0
    stats: dict = field(default_factory=lambda: {"ok": 0, "rejected": 0, "timeout": 0, "invalid": 0, "error": 0, "blocked": 0,
                                                 "latency_ms": deque(maxlen=50)})

    def allows(self, stage: str, failover: bool = False) -> bool:
        st = self.cfg.stages or ["*"]
        fb = self.cfg.fallback_stages if failover else []
        return self.cfg.enabled and ("*" in st or stage in st or "*" in fb or stage in fb) and privacy.endpoint_allows(self.cfg, stage)


@dataclass
class Result:
    obj: object
    raw: str
    calls: list
    endpoint: str
    rounds: int


class Pool:
    def __init__(self):
        self.cv = threading.Condition()
        self.eps: dict[str, EpState] = {}
        self.sig = None
        self.chat_waiting = 0
        self.chat_in_flight = 0
        self.last_chat = 0.0
        self.paused = False          # set by tests / maintenance: background never starts
        self._refresh()

    # ------------------------------------------------------------ endpoints
    def _refresh(self):
        s = get_settings().pool
        sig = json.dumps([e.model_dump() for e in s.endpoints], sort_keys=True)
        if sig == self.sig:
            return
        old = self.eps
        self.eps = {}
        for e in sorted(s.endpoints, key=lambda x: x.priority):
            st = old.get(e.name)
            if st and json.dumps(st.cfg.model_dump(), sort_keys=True) == json.dumps(e.model_dump(), sort_keys=True):
                self.eps[e.name] = st
                continue
            try:
                ad = make_adapter(e, s.endpoints)
            except Exception as ex:
                log.error("endpoint %s unusable: %s", e.name, ex)
                continue
            self.eps[e.name] = EpState(cfg=e, adapter=ad, rpm=e.rpm)
        self.sig = sig

    def status(self) -> list[dict]:
        now = time.monotonic()
        out = []
        with self.cv:
            self._refresh()
            for st in self.eps.values():
                while st.window and now - st.window[0] > 60:
                    st.window.popleft()
                lat = list(st.stats["latency_ms"])
                out.append({"name": st.cfg.name, "kind": st.cfg.kind, "model": st.cfg.model, "enabled": st.cfg.enabled,
                            "external": st.cfg.external, "redact_pii": privacy.prompt_needs_redaction(st.cfg), "structured": st.cfg.structured_output,
                            "rpm": st.rpm, "rpm_config": st.cfg.rpm, "used_60s": len(st.window), "in_flight": st.in_flight,
                            "max_concurrent": st.cfg.max_concurrent, "cooling_s": max(0, round(st.cooling_until - now)),
                            "context": st.cfg.context_tokens, "stages": st.cfg.stages, "timeout_s": st.cfg.timeout_s,
                            "stats": {k: v for k, v in st.stats.items() if k != "latency_ms"},
                            "avg_latency_ms": int(sum(lat) / len(lat)) if lat else None})
        return out

    def chat_active(self) -> bool:
        sch = get_settings().schedule
        if not sch.pause_background_on_chat:
            return False
        return self.chat_waiting > 0 or self.chat_in_flight > 0 or (time.monotonic() - self.last_chat) < sch.chat_idle_resume_s

    # ------------------------------------------------------------ slots
    def _free(self, stage: str, chat: bool, now: float, exclude: frozenset = frozenset()):
        reserve = get_settings().pool.chat_reserve_rpm
        best = None
        for st in self.eps.values():
            if st.cfg.name in exclude or not st.allows(stage, failover=bool(exclude)) or now < st.cooling_until:
                continue
            while st.window and now - st.window[0] > 60:
                st.window.popleft()
            limit = st.rpm if chat else max(1, st.rpm - reserve) if st.rpm > reserve else st.rpm
            if len(st.window) >= limit or st.in_flight >= st.cfg.max_concurrent:
                continue
            load = (len(st.window) / max(limit, 1)) + st.in_flight / st.cfg.max_concurrent
            if best is None or (st.cfg.priority, load) < (best[0], best[1]):
                best = (st.cfg.priority, load, st)
        return best[2] if best else None

    def eligible(self, stage: str, failover: bool = False) -> list[str]:
        with self.cv:
            self._refresh()
            return [n for n, st in self.eps.items() if st.allows(stage, failover)]

    def acquire(self, stage: str, chat: bool = False, wait_limit: float | None = None, exclude: frozenset = frozenset()) -> EpState:
        t0 = time.monotonic()
        with self.cv:
            self._refresh()
            if not any(st.allows(stage) for st in self.eps.values()):
                ext = [st.cfg.name for st in self.eps.values() if st.cfg.enabled and st.cfg.external]
                raise LlmError(f"no enabled endpoint allows stage '{stage}'"
                               + (f" ({', '.join(ext)} are external and may only serve privacy.external_stages)" if ext else ""))
            if chat:
                self.chat_waiting += 1
                self.last_chat = time.monotonic()
            try:
                while True:
                    now = time.monotonic()
                    blocked = not chat and (self.paused or self.chat_active())
                    st = None if blocked else self._free(stage, chat, now, exclude)
                    if st:
                        st.window.append(now)
                        st.in_flight += 1
                        if chat:
                            self.chat_in_flight += 1
                        return st
                    if wait_limit is not None and now - t0 > wait_limit:
                        raise Paused(f"no slot for {stage} within {wait_limit}s")
                    self.cv.wait(timeout=0.5)
                    self._refresh()
            finally:
                if chat:
                    self.chat_waiting -= 1

    def release(self, st: EpState, chat: bool = False):
        with self.cv:
            st.in_flight -= 1
            if chat:
                self.chat_in_flight -= 1
                self.last_chat = time.monotonic()
            self.cv.notify_all()

    def _cool(self, st: EpState, retry_after: float | None, attempt: int):
        s = get_settings().pool
        delay = retry_after if retry_after else s.backoff_base_s * (2 ** min(attempt - 1, 4)) * (0.8 + 0.4 * random.random())
        with self.cv:
            st.cooling_until = max(st.cooling_until, time.monotonic() + delay)
            st.n429 += 1
            if s.auto_lower_rpm and st.n429 >= 3 and st.rpm > 1:
                st.rpm -= 1
                st.n429 = 0
                from llmwiki.logs import event
                event("pool", f"Endpoint {st.cfg.name}: 3 rate-limit rejections → rpm lowered to {st.rpm}", level="warning")
        return delay

    # ------------------------------------------------------------ the main entry point
    def run_structured(self, stage: str, unit: str, build, parse, chat: bool = False, job=None, max_validation: int | None = None,
                       fix_hint: str = "", schema: str | None = None) -> Result:
        """build(cap, scale) -> (system, prompt, sections_meta); parse(raw_obj) -> (obj | None, [errors]).
        schema: name in llm/schemas.py (default by stage); sent only to endpoints with structured_output on."""
        from llmwiki.llm import schemas
        from llmwiki.models import LlmCall
        s = get_settings()
        max_val = s.pool.validation_retries if max_validation is None else max_validation
        schema_name = schema or STAGE_SCHEMA.get(stage)
        calls, scale, transient, val_round, timeouts, errors5 = [], 1.0, 0, 0, 0, 0
        prev = None
        failed: set[str] = set()      # endpoints that blocked or failed this request (failover skips them)
        while True:
            t_wait = time.monotonic()
            exclude = frozenset(failed) if set(self.eligible(stage, failover=True)) - failed else frozenset()
            st = self.acquire(stage, chat=chat, exclude=exclude)
            waited = int((time.monotonic() - t_wait) * 1000)
            cap = min(s.budgets.cap, st.cfg.context_tokens)
            try:
                system, prompt, sections = build(cap, scale)
            except Exception:
                self.release(st, chat)
                raise
            if prev is not None:  # validation retry: show the model its answer and the exact problems
                prompt = (prompt + "\n\n## Your previous answer\n" + prev[0][:6000] + "\n\n## Problems found by the checker\n- " + "\n- ".join(prev[1])
                          + "\n\nReturn the corrected JSON only. Fix exactly these problems and keep everything else." + (("\n" + fix_hint) if fix_hint else ""))
            n_red = 0
            if privacy.prompt_needs_redaction(st.cfg):
                keep = s.privacy.keep_internal_emails and not st.cfg.external
                system, n1 = privacy.redact(system, keep_internal=keep)
                prompt, n2 = privacy.redact(prompt, keep_internal=keep)
                n_red = n1 + n2
            sch = None
            if st.cfg.structured_output and schema_name:
                try:
                    sch = schemas.schema(schema_name)
                except KeyError:
                    log.warning("no schema named %s", schema_name)
            from llmwiki.util import est_tokens
            ptok = est_tokens(system) + est_tokens(prompt)
            row = LlmCall(job=job, stage=stage, unit=unit[:200], endpoint=st.cfg.name, attempt=len(calls) + 1, round=val_round + 1,
                          queue_wait_ms=waited, prompt_tokens=ptok, sections=sections, system=system, prompt=prompt, redactions=n_red)
            t0 = time.monotonic()
            status, raw, ra = "ok", "", None
            try:
                raw = st.adapter.complete(system, prompt, st.cfg.timeout_s or None, st.cfg.max_output_tokens, sch)
            except RateLimited as e:
                status = "rejected"
                ra = e.retry_after
                row.http_code = 429
                row.errors = [str(e)[:500]]
            except Timeout as e:
                status = "timeout"
                row.errors = [str(e)[:500]]
            except Blocked as e:
                status = "blocked"
                row.http_code = getattr(e, "code", None)
                row.errors = [str(e)[:500]]
            except ServerError as e:
                status = "error"
                row.http_code = getattr(e, "code", None)
                row.errors = [str(e)[:500]]
            except LlmError as e:
                status = "error"
                row.errors = [str(e)[:500]]
            finally:
                self.release(st, chat)
            row.latency_ms = int((time.monotonic() - t0) * 1000)
            row.response = raw
            obj, errs = None, []
            if status == "ok":
                if s.privacy.redact_outputs:
                    raw = privacy.redact(raw, keep_internal=True)[0]
                parsed, perr = extract_json(raw)
                if parsed is None:
                    errs = [perr]
                else:
                    row.parsed = parsed if isinstance(parsed, (dict, list)) else {"value": parsed}
                    obj, errs = parse(parsed)
                if obj is None:
                    status = "invalid"
                    row.errors = errs
            row.status = status
            try:
                row.save()
                calls.append(row.id)
            except Exception:
                log.exception("could not log the LLM call")
            with self.cv:
                st.stats[status] = st.stats.get(status, 0) + 1
                if status == "ok":
                    st.stats["latency_ms"].append(row.latency_ms)
                    st.n429 = 0
            log.info("llm %s %s via %s: %s in %sms (wait %sms, ~%s tokens)", stage, unit, st.cfg.name, status, row.latency_ms, waited, ptok)
            if status == "ok":
                return Result(obj, raw, calls, st.cfg.name, val_round + 1)
            if status == "blocked":   # every refusal is visible: Agent › Pool › Refusals, Agent › Logs, errors.log
                from llmwiki.logs import event
                err = (row.errors or [""])[0]
                log.warning("REFUSED by %s: %s %s (%s ~tokens, %s PII items removed): %s", st.cfg.name, stage, unit, ptok, n_red, err[:300])
                event("refusal", f"{st.cfg.name} refused {stage} {unit[:60]}: {err[:200]}", level="warning", endpoint=st.cfg.name,
                      stage=stage, unit=unit[:200], call=row.id, http=row.http_code)
            if status in ("blocked", "error") and s.pool.failover_on_error:
                failed.add(st.cfg.name)
                others = set(self.eligible(stage, failover=True)) - failed
                if others:
                    from llmwiki.logs import event
                    event("pool", f"{stage} {unit[:60]}: {st.cfg.name} {'blocked the input' if status == 'blocked' else 'failed'} → trying "
                          f"{', '.join(sorted(others))}", level="warning", endpoint=st.cfg.name, error=(row.errors or [''])[0][:200])
                    continue
            if status == "blocked":
                raise StageFailed(f"{stage}: input blocked by {', '.join(sorted(failed)) or st.cfg.name} and no other endpoint may take it: "
                                  f"{(row.errors or [''])[0][:200]}", calls, raw)
            if status == "rejected":
                transient += 1
                delay = self._cool(st, ra, transient)
                if transient > 8:
                    raise StageFailed(f"{stage}: rate limited {transient} times", calls, raw)
                log.warning("%s %s: 429 from %s, cooling %.0fs", stage, unit, st.cfg.name, delay)
                continue
            if status == "timeout":
                timeouts += 1
                if timeouts > 1:
                    raise StageFailed(f"{stage}: timed out twice", calls, raw)
                scale *= (1 - s.pool.timeout_shrink)
                continue
            if status == "error":
                errors5 += 1
                if errors5 > s.pool.max_retries_5xx:
                    raise StageFailed(f"{stage}: {row.errors[0] if row.errors else 'error'}", calls, raw)
                time.sleep(min(30, 2 ** errors5))
                continue
            # invalid output
            if val_round >= max_val:
                raise StageFailed(f"{stage}: unusable output after {val_round + 1} round(s): {'; '.join(errs)[:300]}", calls, raw)
            val_round += 1
            prev = (raw or "(empty)", errs)


STAGE_SCHEMA = {"s1": "notes", "s2": "route", "s3": "sections", "s12": "combined", "s123": "combined", "s4": "tags",
                "review": "review", "chat": "chat", "judge": "judge", "report": "story"}

_pool: Pool | None = None
_pool_lock = threading.Lock()


def pool() -> Pool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = Pool()
        return _pool


def reset_pool():
    global _pool
    with _pool_lock:
        _pool = None
