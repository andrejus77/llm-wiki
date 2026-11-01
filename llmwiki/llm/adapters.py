"""LLM adapters. Every adapter is text in → text out: `complete(system, prompt, timeout, max_tokens, schema=None) -> str`.

`schema` is the stage's JSON schema; it is passed only when the endpoint has structured_output on. An adapter that
gets a structured answer back (a dict) returns it as JSON text, so parsing and logging stay the same for everyone.

Errors are normalised so the pool can react: RateLimited (429, with retry_after), Timeout, ServerError (5xx),
Blocked (the API refused the input, e.g. a personal-data filter: the pool fails over to another endpoint),
LlmError (anything else).

Kinds:
- custom        `company_adapter.py` in the data folder (outside the repo): def call(system, prompt, timeout, **kw) -> str | dict
- http_text     URL + JSON body template with {system} {prompt} {model} {max_tokens}; response text at options.response_path
- openai_compat POST {base_url}/chat/completions (response_format json_schema when structured_output)
- claude_cli    `claude -p` (Claude Code) in text mode: haiku for tests, sonnet/opus for the backfill on a laptop
- gemini_cli    `gemini -p` (Gemini CLI) in text mode, read-only approval mode, system prompt via GEMINI_SYSTEM_MD
- codex_cli     `codex exec` (Codex CLI), read-only sandbox, last message to a file, --output-schema when structured
- mock          deterministic, offline (unit tests)
- chaos         wraps another endpoint and injects 429s, timeouts, broken JSON, empty and shrunk answers
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


class LlmError(Exception):
    pass


class RateLimited(LlmError):
    def __init__(self, msg="rate limited", retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class Timeout(LlmError):
    pass


class ServerError(LlmError):
    def __init__(self, msg="server error", code: int | None = None):
        super().__init__(msg)
        self.code = code


class Blocked(LlmError):
    """The API refused this input (content/PII filter, guardrail, policy). Retrying the same endpoint will not help."""
    def __init__(self, msg="input blocked", code: int | None = None):
        super().__init__(msg)
        self.code = code


_BLOCK_WORDS = re.compile(r"content[_ ]?filter|blocked|guardrail|policy|sensitive|personal (data|information)|\bpii\b|safety|moderation|"
                          r"responsible ai|prohibited|not allowed", re.I)


def classify(msg: str, code: int | None = None) -> LlmError:
    """Map an error message/status code to the pool's error classes (shared by all adapters and company_adapter.py)."""
    low = msg.lower()
    if code == 429 or "429" in msg or "rate limit" in low or "too many requests" in low or "resource_exhausted" in low or "quota" in low:
        return RateLimited(msg[:400])
    if "timed out" in low or "timeout" in low:
        return Timeout(msg[:400])
    if code in (400, 403, 422, 451) and _BLOCK_WORDS.search(msg) or (code is None and _BLOCK_WORDS.search(msg)):
        return Blocked(msg[:400], code)
    if code and int(code) >= 500 or "overloaded" in low or "unavailable" in low:
        return ServerError(msg[:400], int(code) if code else None)
    return ServerError(msg[:400], int(code) if code else None)


def as_text(x) -> str:
    """Structured answers (dict/list/pydantic) → JSON text; strings unchanged."""
    if isinstance(x, str):
        return x
    if hasattr(x, "model_dump"):
        x = x.model_dump()
    try:
        return json.dumps(x, ensure_ascii=False)
    except TypeError:
        return str(x)


class Adapter:
    def __init__(self, ep):
        self.ep = ep

    def complete(self, system: str, prompt: str, timeout: float | None, max_tokens: int, schema: dict | None = None) -> str:
        raise NotImplementedError


def _http(url: str, body: dict, headers: dict, timeout: float | None) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or None) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")[:500]
        if e.code == 429:
            ra = e.headers.get("Retry-After")
            raise RateLimited(f"429: {txt}", float(ra) if ra and ra.replace(".", "").isdigit() else None)
        raise classify(f"HTTP {e.code}: {txt}", e.code)
    except (TimeoutError, urllib.error.URLError) as e:
        if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
            raise Timeout(str(e))
        raise ServerError(f"connection: {e}")


def _dig(obj, path: str):
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


class OpenAICompat(Adapter):
    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        key = os.environ.get(self.ep.api_key_env, "") if self.ep.api_key_env else ""
        body = {"model": self.ep.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": self.ep.options.get("temperature", 0.2)}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": schema.get("title", "answer"), "schema": schema}}
        r = _http(self.ep.base_url.rstrip("/") + "/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {}, timeout)
        return _dig(r, "choices.0.message.content") or ""


class HttpText(Adapter):
    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        o = self.ep.options
        key = os.environ.get(self.ep.api_key_env, "") if self.ep.api_key_env else ""
        tpl = o.get("body_template") or '{"system": {system}, "prompt": {prompt}, "model": {model}, "max_tokens": {max_tokens}}'
        body_s = (tpl.replace("{system}", json.dumps(system)).replace("{prompt}", json.dumps(prompt))
                  .replace("{model}", json.dumps(self.ep.model)).replace("{max_tokens}", str(max_tokens))
                  .replace("{schema}", json.dumps(schema) if schema else "null"))
        headers = {k: str(v).replace("{api_key}", key) for k, v in (o.get("headers") or {}).items()}
        r = _http(self.ep.base_url, json.loads(body_s), headers, timeout)
        return as_text(_dig(r, o.get("response_path", "text")))


class Custom(Adapter):
    """Loads data/company_adapter.py (never in the repo). It must define call(system, prompt, timeout, **optional) -> str | dict.

    Optional keyword arguments, passed only if call() declares them: endpoint (this endpoint's name), model,
    max_tokens, schema (the stage's JSON schema; only when structured_output is on for the endpoint).
    Errors: raise llmwiki.llm.adapters.Blocked / RateLimited / Timeout yourself, or any exception with
    `.status_code` — it is classified by message and code (429 → rate limit, 400/403/422 with 'blocked', 'policy',
    'PII', 'content filter'… → Blocked, which makes the pool try the next endpoint)."""
    _mod = None

    @classmethod
    def module(cls):
        if cls._mod is None:
            from llmwiki.conf import data_dir
            p = Path(os.environ.get("LLMWIKI_ADAPTER", data_dir() / "company_adapter.py"))
            if not p.exists():
                raise LlmError(f"custom adapter not found: {p} (copy tools/company_adapter.example.py there)")
            spec = importlib.util.spec_from_file_location("company_adapter", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls._mod = mod
        return cls._mod

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        try:
            fn = self.module().call
            import inspect
            params = inspect.signature(fn).parameters
            has_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
            opts = (("endpoint", self.ep.name), ("model", self.ep.model), ("max_tokens", max_tokens), ("schema", schema))
            kw = {k: v for k, v in opts if (k in params or has_kw) and not (k == "schema" and v is None)}
            return as_text(fn(system, prompt, timeout, **kw))
        except LlmError:
            raise
        except TimeoutError as e:
            raise Timeout(str(e))
        except Exception as e:
            code = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
            err = classify(f"{type(e).__name__}: {e}", int(code) if code else None)
            if isinstance(err, RateLimited):
                err.retry_after = getattr(e, "retry_after", None)
            raise err


def _find_exe(env: str, names: list[str], extra: list[Path]) -> str:
    for c in [os.environ.get(env)] + [shutil.which(n) for n in names] + [str(p) for p in extra]:
        if c and Path(c).exists():
            return c
    raise LlmError(f"{names[0]} CLI not found (install it and log in, or set {env})")


def claude_exe() -> str:
    return _find_exe("LLMWIKI_CLAUDE", ["claude"], [Path.home() / ".local" / "bin" / "claude.exe"])


class _Cli(Adapter):
    """Shared plumbing for the coding-agent CLIs: an empty working folder, stdin for the prompt, error mapping."""
    _cwd: str | None = None

    @classmethod
    def cwd(cls) -> str:
        if _Cli._cwd is None:
            _Cli._cwd = tempfile.mkdtemp(prefix="llmwiki-cli-")
        return _Cli._cwd

    def run(self, cmd: list[str], stdin: str, timeout, env: dict | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, input=stdin.encode("utf-8"), capture_output=True, timeout=timeout or 600, cwd=self.cwd(),
                                  env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired:
            raise Timeout(f"{Path(cmd[0]).stem} timed out after {timeout}s")

    @staticmethod
    def fail(p: subprocess.CompletedProcess, out: str = "") -> LlmError:
        err = (p.stderr.decode("utf-8", "replace") + "\n" + out)[:600].strip()
        return classify(f"exit {p.returncode}: {err}")


class ClaudeCli(_Cli):
    """`claude -p` in TEXT mode: the same JSON-from-text parsing path as the work APIs."""

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        cmd = [claude_exe(), "-p", "--model", self.ep.model or "haiku", "--output-format", "json", "--tools", "",
               "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence", "--system-prompt", system]
        p = self.run(cmd, prompt, timeout, {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        out = p.stdout.decode("utf-8", "replace")
        try:
            j = json.loads(out)
        except Exception:
            raise self.fail(p, out[:300])
        if j.get("is_error"):
            msg = str(j.get("result") or j.get("error") or j)[:400]
            raise RateLimited(msg) if "limit" in msg.lower() else classify(msg)
        return j.get("result") or ""


class GeminiCli(_Cli):
    """`gemini -p` (Gemini CLI, logged in with a Google account or GEMINI_API_KEY).

    The system prompt replaces Gemini CLI's own agent prompt via GEMINI_SYSTEM_MD (a temp file per call); approval
    mode 'plan' is read-only, so the model cannot edit files or run commands; the prompt goes in on stdin."""

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        exe = _find_exe("LLMWIKI_GEMINI", ["gemini", "gemini.cmd"], [Path(os.environ.get("APPDATA", "")) / "npm" / "gemini.cmd"])
        fd, sys_path = tempfile.mkstemp(prefix="sys-", suffix=".md", dir=self.cwd())
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system + ("\n\nAnswer with the JSON only, no tool calls." if "JSON" in system else ""))
        try:
            cmd = [exe, "-p", self.ep.options.get("instruction", "Follow the instructions above and answer."), "-o", "json",
                   "--approval-mode", "plan", "--skip-trust"]
            if self.ep.model:
                cmd += ["-m", self.ep.model]
            p = self.run(cmd, prompt, timeout, {"GEMINI_SYSTEM_MD": sys_path, "NO_COLOR": "1"})
        finally:
            try:
                os.remove(sys_path)
            except OSError:
                pass
        out = p.stdout.decode("utf-8", "replace").strip()
        start = out.find("{")
        try:
            j = json.loads(out[start:]) if start >= 0 else {}
        except Exception:
            j = {}
        if j.get("error"):
            e = j["error"]
            raise classify(str(e.get("message") or e)[:400], e.get("code") if isinstance(e.get("code"), int) and e.get("code") >= 400 else None)
        if "response" in j:
            return j.get("response") or ""
        if p.returncode != 0:
            raise self.fail(p, out[:300])
        return out


class CodexCli(_Cli):
    """`codex exec` (Codex CLI, logged in with ChatGPT or OPENAI_API_KEY): read-only sandbox, no session files,
    the final message written to a file. The system prompt is put in front of the prompt (codex exec has no
    system-prompt flag). With structured_output the stage schema goes to --output-schema."""

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        exe = _find_exe("LLMWIKI_CODEX", ["codex", "codex.cmd"], [Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"])
        d = tempfile.mkdtemp(prefix="codex-", dir=self.cwd())
        out_path = os.path.join(d, "answer.txt")
        cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never", "-C", d, "-o", out_path]
        if self.ep.model:
            cmd += ["-m", self.ep.model]
        for k, v in (self.ep.options.get("config") or {"model_reasoning_effort": "low"}).items():
            cmd += ["-c", f"{k}={json.dumps(v)}"]
        if schema:
            sp = os.path.join(d, "schema.json")
            Path(sp).write_text(json.dumps(schema), encoding="utf-8")
            cmd += ["--output-schema", sp]
        cmd.append("-")
        try:
            p = self.run(cmd, f"# Instructions\n{system}\n\n# Task\n{prompt}\n\nDo not run commands or edit files; just answer.", timeout)
            text = Path(out_path).read_text(encoding="utf-8") if os.path.exists(out_path) else ""
        finally:
            shutil.rmtree(d, ignore_errors=True)
        if p.returncode != 0 and not text:
            raise self.fail(p, p.stdout.decode("utf-8", "replace")[-300:])
        return text


class Chaos(Adapter):
    """Fault injection: options = {inner: endpoint name or 'mock', p429, ptimeout, pbroken, pempty, pshrink, pblock, seed}."""

    def __init__(self, ep, inner: Adapter):
        super().__init__(ep)
        self.inner = inner
        self.rng = random.Random(ep.options.get("seed", 7))

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        o = self.ep.options
        r = self.rng.random
        if r() < o.get("p429", 0.1):
            raise RateLimited("chaos 429", retry_after=o.get("retry_after", 1))
        if r() < o.get("ptimeout", 0.05):
            time.sleep(min(timeout or 1, o.get("timeout_sleep", 0.2)))
            raise Timeout("chaos timeout")
        if r() < o.get("p5xx", 0.03):
            raise ServerError("chaos 503", 503)
        if r() < o.get("pblock", 0.0):
            raise Blocked("chaos: input blocked by the content filter", 400)
        text = self.inner.complete(system, prompt, timeout, max_tokens, schema)
        if r() < o.get("pempty", 0.05):
            return ""
        if r() < o.get("pbroken", 0.1):
            return "Sure! Here is the JSON:\n" + text[: max(1, int(len(text) * 0.6))]
        if r() < o.get("pshrink", 0.05) and '"text"' in text:
            return re.sub(r'("text"\s*:\s*")([^"]{40,})(")', lambda m: m.group(1) + m.group(2)[:40] + '.' + m.group(3), text)
        return text


def make_adapter(ep, all_eps=None) -> Adapter:
    kinds = {"custom": Custom, "http_text": HttpText, "openai_compat": OpenAICompat, "claude_cli": ClaudeCli,
             "gemini_cli": GeminiCli, "codex_cli": CodexCli}
    if ep.kind in kinds:
        return kinds[ep.kind](ep)
    if ep.kind == "mock":
        from llmwiki.llm.mock import Mock
        return Mock(ep)
    if ep.kind == "chaos":
        inner_name = ep.options.get("inner", "mock")
        inner_ep = next((e for e in (all_eps or []) if e.name == inner_name), None)
        if inner_ep is None:
            from llmwiki.conf import Endpoint
            inner_ep = Endpoint(name="mock-inner", kind="mock")
        return Chaos(ep, make_adapter(inner_ep, all_eps))
    raise LlmError(f"unknown adapter kind {ep.kind}")
