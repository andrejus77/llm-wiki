"""Connect an LLM API step by step (only a tiny test prompt is sent — never mail):

    llmwiki api probe --url https://llm.corp/api/v1 --secret api-a [--model gpt-4o] [--proxy none] [--auth header --auth-header api-key]
        tries the usual request shapes (OpenAI chat, Azure OpenAI, LangServe /invoke, plain JSON, Ollama) against the URL and
        prints, for the one that answers, the endpoint block to paste into data/settings.yaml
    llmwiki api test api-a [--schema] [--raw]
        sends the test prompt through a configured endpoint exactly as the pipeline would; shows the request (key masked),
        the HTTP status and response, where the answer was found, and what to change when it fails
    llmwiki api list
        the configured endpoints and their stages

Typical problems and their fix are printed with the error: 401 → secret; 404 → URL/path; 422 → options.body;
CERTIFICATE_VERIFY_FAILED → options.ca_bundle; proxy errors → options.proxy: none; answer not found → options.response_path.
"""
import json
import time

import yaml
from django.core.management.base import BaseCommand, CommandError

SYSTEM = "You are a connection test. Answer with JSON only."
PROMPT = 'Reply with exactly this JSON and nothing else: {"ok": true, "word": "pong"}'
SCHEMA = {"title": "Pong", "type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}}, "required": ["ok", "word"]}


def _mask(headers: dict) -> dict:
    """Never print a key: keep only the scheme word (Bearer/Basic) and the length."""
    def m(v: str) -> str:
        scheme, _, rest = v.partition(" ")
        if rest and scheme.lower() in ("bearer", "basic", "token"):
            return f"{scheme} ***({len(rest)} chars)"
        return f"***({len(v)} chars)"
    return {k: (m(v) if k.lower() in ("authorization", "api-key", "x-api-key") or "key" in k.lower() or "token" in k.lower()
                or "secret" in k.lower() else v) for k, v in headers.items()}


class Command(BaseCommand):
    help = "Find and test the connection to an LLM API (sends only a tiny test prompt)"

    def add_arguments(self, p):
        p.add_argument("op", choices=["probe", "test", "list"])
        p.add_argument("endpoint", nargs="?", help="test: the endpoint name (e.g. api-a)")
        p.add_argument("--url")
        p.add_argument("--secret", default="", help="name of the stored API key (llmwiki secret set <name>)")
        p.add_argument("--model", default="")
        p.add_argument("--auth", default="", choices=["", "bearer", "header", "basic", "query", "none"])
        p.add_argument("--auth-header", default="")
        p.add_argument("--auth-prefix", default=None)
        p.add_argument("--proxy", default="", help="system | none | http://proxy:port")
        p.add_argument("--no-verify", action="store_true", help="skip TLS verification (diagnosis only)")
        p.add_argument("--ca-bundle", default="")
        p.add_argument("--timeout", type=int, default=60)
        p.add_argument("--schema", action="store_true", help="test: also send the JSON schema (structured output)")
        p.add_argument("--raw", action="store_true", help="test: print the whole raw response")

    def handle(self, *a, **o):
        from llmwiki.conf import get_settings
        if o["op"] == "list":
            for ep in get_settings().pool.endpoints:
                self.stdout.write(f"{ep.name:<12} {ep.kind:<13} {'on ' if ep.enabled else 'off'} {ep.base_url or ''} stages={ep.stages}"
                                  f"{' external' if ep.external else ''}{' key=' + ep.api_key_secret if ep.api_key_secret else ''}")
            return
        if o["op"] == "probe":
            return self.probe(o)
        if not o["endpoint"]:
            raise CommandError("usage: llmwiki api test <endpoint name>   (see: llmwiki api list)")
        ep = next((e for e in get_settings().pool.endpoints if e.name == o["endpoint"]), None)
        if not ep:
            raise CommandError(f"no endpoint {o['endpoint']!r} (see: llmwiki api list)")
        self.test(ep, o)

    # ------------------------------------------------------------------ test one configured endpoint
    def test(self, ep, o):
        from llmwiki import keystore
        from llmwiki.llm.adapters import HttpJson, LlmError, _http, auto_answer, make_adapter
        w = self.stdout.write
        w(f"Endpoint {ep.name}: kind {ep.kind}, model {ep.model or '-'}, timeout {ep.timeout_s or 'none'}, structured_output {ep.structured_output}")
        if ep.api_key_secret:
            w(f"  key: secret '{ep.api_key_secret}' → {keystore.where(ep.api_key_secret) or 'MISSING (llmwiki secret set ' + ep.api_key_secret + ')'}")
        ad = make_adapter(ep)
        schema = SCHEMA if (o["schema"] or ep.structured_output) else None
        if isinstance(ad, HttpJson):
            c = ad.config()
            url, headers, body = ad.request(SYSTEM, PROMPT, 200, schema)
            w(f"  request: {c.get('method', 'POST')} {url}")
            w(f"  headers: {_mask(headers)}")
            w("  body: " + json.dumps(body, ensure_ascii=False)[:700])
            w(f"  tls: {'NOT verified' if c.get('verify_tls') is False else 'verified'}{' with ' + c['ca_bundle'] if c.get('ca_bundle') else ''} · proxy: {c.get('proxy', 'system')}")
            t0 = time.monotonic()
            try:
                status, text = _http(url, body, headers, ep.timeout_s or o["timeout"], c, c.get("method", "POST"), raw=True)
            except LlmError as e:
                w(f"FAILED (no HTTP answer): {e}")
                return
            w(f"  HTTP {status} in {time.monotonic() - t0:.1f}s")
            w("  response: " + (text if o["raw"] else text[:1500]))
            try:
                r = json.loads(text)
            except json.JSONDecodeError:
                r = {"_text": text}
            path = c.get("response_path", "auto")
            found, _v = auto_answer(r)
            if path != "auto":
                try:
                    from llmwiki.llm.adapters import _dig
                    _dig(r, path)
                    w(f"  answer at response_path '{path}': OK")
                except Exception:
                    w(f"  answer NOT at response_path '{path}'" + (f" — it looks like '{found}': set options.response_path: \"{found}\"" if found else ""))
            else:
                w(f"  answer found at '{found}'" + (f" (set options.response_path: \"{found}\" to pin it)" if found else " — none: set options.response_path"))
            if status >= 400:
                w(self._hint(status, text))
                return
        t0 = time.monotonic()
        try:
            ans = ad.complete(SYSTEM, PROMPT, ep.timeout_s or o["timeout"], 200, schema)
        except Exception as e:
            w(f"FAILED: {type(e).__name__}: {e}")
            if ep.kind == "custom":
                w("  (custom: the error comes from data/company_adapter.py; run it alone with: .venv\\Scripts\\python -c "
                  "\"import sys; sys.path.insert(0,'data'); import company_adapter as c; print(c.call('hi','Say pong',60))\")")
            return
        w(f"ANSWER in {time.monotonic() - t0:.1f}s: {ans[:400]}")
        from llmwiki.llm.jsonparse import extract_json
        ok = extract_json(ans)[0] is not None
        w(f"  JSON in the answer: {'yes' if ok else 'NO — the pipeline needs JSON; check the model / response_path'}")
        if ok:
            w(f"OK: endpoint {ep.name} works. Next: .\\doctor.bat --ping")

    @staticmethod
    def _hint(status: int, text: str) -> str:
        return {401: "  → 401: wrong or missing key. llmwiki secret set <name>; check options.auth (bearer / header + auth_header).",
                403: "  → 403: the key has no access to this model/URL, or a filter refused the input.",
                404: "  → 404: wrong URL or path. Try llmwiki api probe --url <base URL>.",
                405: "  → 405: wrong method or path (POST to …/chat/completions, …/invoke, …).",
                415: "  → 415: wrong content type; set options.headers.",
                422: "  → 422: the body does not match what the API expects (see the response above); adjust options.body or try another preset.",
                }.get(status, f"  → HTTP {status}: see the response above.")

    # ------------------------------------------------------------------ probe a URL with the usual API shapes
    def probe(self, o):
        from llmwiki.conf import Endpoint
        from llmwiki.llm.adapters import PRESETS, HttpJson, _http, auto_answer
        if not o["url"]:
            raise CommandError("usage: llmwiki api probe --url <base URL> [--secret <name>] [--model …]")
        base = o["url"].rstrip("/")
        common = {}
        if o["auth"]:
            common["auth"] = o["auth"]
        if o["auth_header"]:
            common["auth_header"] = o["auth_header"]
        if o["auth_prefix"] is not None:
            common["auth_prefix"] = o["auth_prefix"]
        if o["proxy"]:
            common["proxy"] = o["proxy"]
        if o["no_verify"]:
            common["verify_tls"] = False
        if o["ca_bundle"]:
            common["ca_bundle"] = o["ca_bundle"]
        tries = []
        for name in ["openai", "azure_openai", "langserve", "langserve_text", "simple", "ollama"]:
            if name == "azure_openai" and "deployments" not in base:
                continue
            tries.append((name, {}))
            if PRESETS[name].get("path"):
                tries.append((name, {"path": ""}))        # the URL already ends with the path
        w = self.stdout.write
        w(f"Probing {base} with a tiny test prompt ({len(tries)} request shapes)…")
        winner = None
        for name, extra in tries:
            opts = {"preset": name, **common, **extra}
            ep = Endpoint(name="probe", kind="http_json", base_url=base, api_key_secret=o["secret"], model=o["model"], options=opts)
            ad = HttpJson(ep)
            c = ad.config()
            url, headers, body = ad.request(SYSTEM, PROMPT, 100, None)
            try:
                status, text = _http(url, body, headers, o["timeout"], c, "POST", raw=True)
            except Exception as e:
                w(f"  {name:<15} {url:<60} no answer: {str(e)[:160]}")
                if str(e).startswith("connection:") or "timed out" in str(e).lower():
                    w("  (a connection problem, not the request shape — fix it first (see the hint), then probe again;"
                      " a slow API: --timeout 180)")
                    break
                continue
            try:
                r = json.loads(text)
            except json.JSONDecodeError:
                r = {"_text": text}
            found, v = auto_answer(r) if status < 400 else (None, None)
            pong = isinstance(v, str) and "pong" in v.lower()
            w(f"  {name:<15} {url:<60} HTTP {status} {'answer at ' + found if found else text[:120].replace(chr(10), ' ')}{'  ← works' if pong else ''}")
            if pong and not winner:
                winner = (name, extra, found)
        if not winner:
            w("No shape answered with the test reply. Look at the HTTP codes above (401 = key, 404 = path, 422 = body shape);"
              " if the old project calls this API from Python (e.g. LangChain), use kind: custom with its code in data/company_adapter.py.")
            return
        name, extra, found = winner
        block = {"name": "api-a", "kind": "http_json", "base_url": base, "model": o["model"], "api_key_secret": o["secret"],
                 "options": {"preset": name, **common, **extra, "response_path": found}}
        w("\nWorks. Endpoint settings for data/settings.yaml (keep rpm, timeout_s, stages … of your entry):\n")
        w(yaml.safe_dump({"pool": {"endpoints": [block]}}, sort_keys=False, allow_unicode=True))
        w("Then: llmwiki api test api-a --schema   (does it also take a JSON schema? if so set structured_output: true;"
          " the schema goes to the 'response_format' for openai presets or to options.schema_field)")
