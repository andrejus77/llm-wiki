"""LLM adapters. Every adapter is text in → text out: `complete(system, prompt, timeout, max_tokens, schema=None) -> str`.

`schema` is the stage's JSON schema; it is passed only when the endpoint has structured_output on. An adapter that
gets a structured answer back (a dict) returns it as JSON text, so parsing and logging stay the same for everyone.

Errors are normalised so the pool can react: RateLimited (429, with retry_after), Timeout, ServerError (5xx),
Blocked (the API refused the input, e.g. a personal-data filter: the pool fails over to another endpoint),
LlmError (anything else).

Kinds:
- http_json     an HTTP API described in the settings: options.preset (openai, azure_openai, langserve, simple, ollama) and/or
                body / response_path / auth / headers / TLS / proxy. `llmwiki api probe` finds them, `llmwiki api test` checks
                them (http_text is the older name)
- custom        `company_adapter.py` in the data folder (outside the repo): def call(system, prompt, timeout, **kw) -> str | dict
                — for APIs that need an SDK or the old project's Python client
- openai_compat POST {base_url}/chat/completions (= http_json with preset openai)
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


def _opener(o: dict):
    """urllib opener for one endpoint: TLS (verify_tls, ca_bundle — the Windows certificate store is used by default,
    which holds the company's own CA) and proxy ('system' = the PC's proxy settings, 'none', or a URL). Internal APIs
    usually need proxy 'none' when the PC has an internet proxy configured."""
    import ssl
    ctx = ssl.create_default_context(cafile=o.get("ca_bundle") or None)
    if o.get("verify_tls") is False:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    proxy = o.get("proxy", "system")
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy == "none":
        handlers.append(urllib.request.ProxyHandler({}))
    elif proxy and proxy != "system":
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _http(url: str, body: dict | None, headers: dict, timeout: float | None, o: dict | None = None, method: str = "POST", raw: bool = False):
    """POST JSON, return the parsed JSON answer (raw=True: (status, text)). Errors are mapped to the pool's classes,
    with a hint for the usual connection problems."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json", **headers}, method=method)
    try:
        with _opener(o or {}).open(req, timeout=timeout or None) as r:
            text = r.read().decode("utf-8", "replace")
            if raw:
                return r.status, text
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_text": text}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")[:800]
        if raw:
            return e.code, txt
        if e.code == 429:
            ra = e.headers.get("Retry-After")
            raise RateLimited(f"429: {txt}", float(ra) if ra and ra.replace(".", "").isdigit() else None)
        hint = {401: " (the API key/secret is wrong or missing: llmwiki secret set …)", 403: " (no access, or a filter refused the input)",
                404: " (wrong URL or path)", 405: " (wrong method or path)", 415: " (the API wants another content type)",
                422: " (the request body does not match what the API expects: check options.body)"}.get(e.code, "")
        raise classify(f"HTTP {e.code}{hint}: {txt}", e.code)
    except (TimeoutError, urllib.error.URLError) as e:
        msg = str(getattr(e, "reason", e))
        if "timed out" in msg.lower() or isinstance(e, TimeoutError):
            raise Timeout(msg)
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg += " (the company CA is not trusted: set options.ca_bundle to its .pem, or as a test options.verify_tls: false)"
        elif "getaddrinfo" in msg or "Name or service not known" in msg:
            msg += " (host name not found: check base_url, VPN / company network)"
        elif "refused" in msg.lower() or "10061" in msg:
            msg += " (nothing listens there: check host and port)"
        elif "proxy" in msg.lower() or "407" in msg:
            msg += " (proxy problem: for an internal API try options.proxy: none)"
        raise ServerError(f"connection: {msg}")


def _dig(obj, path: str):
    if not path:
        return obj
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


# Common places of the answer text in API responses (used by response_path: auto and by `llmwiki api probe`)
ANSWER_PATHS = ["choices.0.message.content", "choices.0.text", "output.content", "output.text", "output", "message.content", "content",
                "text", "response", "result", "answer", "generated_text", "data.text", "data.content", "data.answer", "outputs.0.text",
                "candidates.0.content.parts.0.text", "_text"]

# Request shapes of the usual API styles. Placeholders in body values: a value that is exactly "{name}" is replaced by the
# object (messages stay a list, max_tokens a number); placeholders inside longer strings are replaced as text.
PRESETS = {
    "openai": {"path": "/chat/completions", "body": {"model": "{model}", "messages": "{messages}", "max_tokens": "{max_tokens}", "temperature": 0.2},
               "response_path": "choices.0.message.content", "schema_field": "openai"},
    "azure_openai": {"path": "", "auth": "header", "auth_header": "api-key", "auth_prefix": "",
                     "body": {"messages": "{messages}", "max_tokens": "{max_tokens}", "temperature": 0.2},
                     "response_path": "choices.0.message.content", "schema_field": "openai"},
    "langserve": {"path": "/invoke", "body": {"input": "{lc_messages}"}, "response_path": "auto"},
    "langserve_text": {"path": "/invoke", "body": {"input": "{system_prompt}"}, "response_path": "auto"},
    "simple": {"path": "", "body": {"system": "{system}", "prompt": "{prompt}", "max_tokens": "{max_tokens}"}, "response_path": "auto"},
    "ollama": {"path": "/api/chat", "body": {"model": "{model}", "messages": "{messages}", "stream": False}, "response_path": "message.content"},
}


def _subst(tpl, values: dict):
    if isinstance(tpl, dict):
        return {k: _subst(v, values) for k, v in tpl.items()}
    if isinstance(tpl, list):
        return [_subst(v, values) for v in tpl]
    if isinstance(tpl, str):
        m = re.fullmatch(r"\{(\w+)\}", tpl)
        if m and m.group(1) in values:
            return values[m.group(1)]
        return re.sub(r"\{(\w+)\}", lambda mm: str(values[mm.group(1)]) if mm.group(1) in values and not isinstance(values[mm.group(1)], (dict, list))
                      else mm.group(0), tpl)
    return tpl


def auto_answer(r) -> tuple[str | None, object]:
    """Find the answer in a response: (path, value) of the first known place with a non-empty value."""
    for p in ANSWER_PATHS:
        try:
            v = _dig(r, p)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if isinstance(v, str) and v.strip() or isinstance(v, (dict, list)) and v:
            return p, v
    return None, None


class HttpJson(Adapter):
    """An HTTP API described entirely in the settings (no code): options.preset fills in path, body, response_path; any
    key can be overridden. Find the right options with `llmwiki api probe`, check them with `llmwiki api test`."""

    def config(self) -> dict:
        o = dict(self.ep.options or {})
        if "body_template" in o and "body" not in o:     # the older http_text option (a JSON string)
            try:
                o["body"] = json.loads(re.sub(r"\{(\w+)\}", r'"{\1}"', o["body_template"]))
            except Exception:
                pass
        preset = PRESETS.get(o.get("preset") or ("simple" if "body" not in o else ""), {})
        return {**preset, **{k: v for k, v in o.items() if k != "preset"}}

    def request(self, system, prompt, max_tokens, schema=None) -> tuple[str, dict, dict]:
        c = self.config()
        from llmwiki import keystore
        key = keystore.get(self.ep.api_key_secret)
        url = keystore.fill((self.ep.base_url or c.get("url", "")).rstrip("/") + c.get("path", ""))
        if c.get("query"):
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode({k: keystore.fill(str(v)) for k, v in c["query"].items()})
        headers = {k: keystore.fill(str(v)) for k, v in (c.get("headers") or {}).items()}
        auth = c.get("auth", "bearer" if key else "none")
        if key and auth == "bearer":
            headers.setdefault("Authorization", f"Bearer {key}")
        elif key and auth == "header":
            headers.setdefault(c.get("auth_header", "Authorization"), f"{c.get('auth_prefix', '')}{key}")
        elif key and auth == "basic":
            import base64
            headers.setdefault("Authorization", "Basic " + base64.b64encode(f"{c.get('auth_user', '')}:{key}".encode()).decode())
        elif key and auth == "query":
            from urllib.parse import quote
            url += ("&" if "?" in url else "?") + f"{c.get('auth_param', 'api_key')}={quote(key)}"
        values = {"system": system, "prompt": prompt, "model": self.ep.model, "max_tokens": max_tokens, "schema": schema,
                  "system_prompt": f"{system}\n\n{prompt}",
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                  "lc_messages": [{"type": "system", "content": system}, {"type": "human", "content": prompt}]}
        body = _subst(c.get("body") or {}, values)
        if schema and not _uses(c.get("body"), "schema"):
            sf = c.get("schema_field")
            if sf == "openai":
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": str(schema.get("title", "answer"))[:60], "schema": schema}}
            elif sf:
                node = body
                parts = sf.split(".")
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = schema
        return url, headers, body

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        c = self.config()
        url, headers, body = self.request(system, prompt, max_tokens, schema)
        r = _http(url, body, headers, timeout, c, c.get("method", "POST"))
        path = c.get("response_path", "auto")
        if path == "auto":
            found, v = auto_answer(r)
            if found is None:
                raise LlmError(f"no answer found in the response (keys: {list(r)[:12] if isinstance(r, dict) else type(r).__name__}); "
                               "set options.response_path — `llmwiki api test` shows the response")
            return as_text(v)
        try:
            return as_text(_dig(r, path))
        except (KeyError, IndexError, TypeError, ValueError):
            found, _v = auto_answer(r)
            raise LlmError(f"response_path '{path}' not in the response" + (f"; the answer seems to be at '{found}'" if found else "")
                           + " — `llmwiki api test` shows the response")


def _uses(tpl, name: str) -> bool:
    return f"{{{name}}}" in json.dumps(tpl or {})


class OpenAICompat(HttpJson):
    """POST {base_url}/chat/completions (OpenAI, vLLM, LiteLLM, most gateways); the same as http_json with preset openai."""

    def config(self) -> dict:
        return {**PRESETS["openai"], **{k: v for k, v in (self.ep.options or {}).items() if k != "preset"}}


HttpText = HttpJson   # older kind name


class Custom(Adapter):
    """Loads data/company_adapter.py (never in the repo). It must define call(system, prompt, timeout, **optional) -> str | dict.

    Optional keyword arguments, passed only if call() declares them: endpoint (this endpoint's name), model,
    max_tokens, schema (the stage's JSON schema; only when structured_output is on for the endpoint).
    Errors: raise llmwiki.llm.adapters.Blocked / RateLimited / Timeout yourself, or any exception with
    `.status_code` — it is classified by message and code (429 → rate limit, 400/403/422 with 'blocked', 'policy',
    'PII', 'content filter'… → Blocked, which makes the pool try the next endpoint)."""
    _mods: dict = {}

    def path(self) -> Path:
        from llmwiki.conf import data_dir
        return Path(self.ep.options.get("module") or os.environ.get("LLMWIKI_ADAPTER") or data_dir() / "company_adapter.py")

    def module(self):
        p = self.path()
        if str(p) not in Custom._mods:
            if not p.exists():
                raise LlmError(f"custom adapter not found: {p} (copy tools/company_adapter.example.py there)")
            spec = importlib.util.spec_from_file_location(f"company_adapter_{len(Custom._mods)}", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            Custom._mods[str(p)] = mod
        return Custom._mods[str(p)]

    def complete(self, system, prompt, timeout, max_tokens, schema=None):
        try:
            fn = getattr(self.module(), self.ep.options.get("function", "call"))
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


def _find_exe(env: str, names: list[str], extra: list[Path], explicit: str = "") -> str:
    """explicit = the endpoint's options.exe (a path set in the settings); then PATH and the usual install folders
    (the environment variable `env` is still honoured for older setups)."""
    for c in [explicit, os.environ.get(env)] + [shutil.which(n) for n in names] + [str(p) for p in extra]:
        if c and Path(c).exists():
            return c
    raise LlmError(f"{names[0]} CLI not found (install it and log in once, or set the endpoint's options.exe to its path)")


def claude_exe(explicit: str = "") -> str:
    return _find_exe("LLMWIKI_CLAUDE", ["claude"], [Path.home() / ".local" / "bin" / "claude.exe"], explicit)


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
        cmd = [claude_exe(self.ep.options.get("exe", "")), "-p", "--model", self.ep.model or "haiku", "--output-format", "json", "--tools", "",
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
        exe = _find_exe("LLMWIKI_GEMINI", ["gemini", "gemini.cmd"], [Path(os.environ.get("APPDATA", "")) / "npm" / "gemini.cmd"],
                        self.ep.options.get("exe", ""))
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
        exe = _find_exe("LLMWIKI_CODEX", ["codex", "codex.cmd"], [Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd"],
                        self.ep.options.get("exe", ""))
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
    kinds = {"custom": Custom, "http_json": HttpJson, "http_text": HttpJson, "openai_compat": OpenAICompat, "claude_cli": ClaudeCli,
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
