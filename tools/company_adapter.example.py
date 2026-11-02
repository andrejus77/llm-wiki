"""company_adapter.py — Python code for an internal LLM API that the settings alone can't describe (an SDK, LangChain,
the old project's client). For a plain HTTP API you don't need this file: use `kind: http_json` in data/settings.yaml
(`llmwiki api probe` finds the options). Endpoints with `kind: custom` call `call()` below.

Copy this file to <data folder>/company_adapter.py (setup.bat / install.bat do that). It lives OUTSIDE the git repo,
so internal URLs and code never end up on GitHub. Test it with:  .\\llmwiki api test api-b

Rules:
- Return the model's answer as text (the app finds the JSON in it) — or, for structured output, the parsed object
  (dict / Pydantic model); both are fine.
- Secrets: `secret("api-b")` reads the Windows Credential Manager (stored with `llmwiki secret set api-b`) or
  data/secrets.yaml. Never write a key in this file or use environment variables for it.
- `endpoint` is the endpoint name from the settings ("api-b", "api-a"), so one file can serve both APIs.
- `schema` is the JSON schema of the answer (only for endpoints with structured_output: true). Pass it on, e.g.
  `llm.with_structured_output(schema)` or as a request field.
- Errors: raise Blocked when the API refuses the input (e.g. its personal-data filter) — the request then goes to the
  other API at once and is logged as a refusal. Raise RateLimited on 429 (retry_after in seconds is honoured),
  TimeoutError on timeouts. Other exceptions are retried a few times, then failed over.
"""
import json
import urllib.error
import urllib.request

from llmwiki.keystore import get as secret
from llmwiki.llm.adapters import Blocked, RateLimited

# Not secret: the URLs (this file never leaves the machine). Replace with the real ones.
URLS = {
    "api-b": "https://internal-llm-b.example/v1/generate",   # 3 requests/min
    "api-a": "https://internal-llm-a.example/v1/generate",   # 180 s limit, LangChain backend
}


def call(system: str, prompt: str, timeout: float | None, endpoint: str = "api-b", model: str = "", max_tokens: int = 8000,
         schema: dict | None = None):
    # ------------------------------------------------------------------------------------------
    # PASTE YOUR EXISTING CALL CODE HERE (or use one of the variants below).
    # The example posts JSON and reads a "text" field.
    # ------------------------------------------------------------------------------------------
    body = {"system": system, "prompt": prompt, "max_tokens": max_tokens}
    if model:
        body["model"] = model
    if schema is not None:
        body["response_schema"] = schema          # <-- whatever field your API uses for structured output
    req = urllib.request.Request(URLS[endpoint], data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret(endpoint)}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or None) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:300]
        if e.code == 429:
            ra = e.headers.get("Retry-After")
            raise RateLimited(f"429 from {endpoint}: {text}", float(ra) if ra else None)
        if e.code in (400, 403, 422):             # e.g. "input contains personal data" → try the other API
            raise Blocked(f"{endpoint} refused the input ({e.code}): {text}", e.code)
        raise
    if isinstance(data.get("parsed"), dict):      # structured answer
        return data["parsed"]
    return data.get("text") or data.get("output") or json.dumps(data)


# --- Variant: LangChain (e.g. API A, whose backend supports with_structured_output) -----------------------------------
#
#   from langchain_openai import AzureChatOpenAI           # or ChatOpenAI(base_url=…), or the company's own chat model class
#   _llm = None
#   def call(system, prompt, timeout, endpoint="api-a", schema=None, **kw):
#       global _llm
#       if _llm is None:
#           _llm = AzureChatOpenAI(azure_endpoint=URLS[endpoint], api_key=secret(endpoint), azure_deployment="…",
#                                  api_version="…", timeout=timeout or 180)
#       msgs = [("system", system), ("user", prompt)]
#       if schema:
#           return _llm.with_structured_output(schema).invoke(msgs)      # → dict
#       return _llm.invoke(msgs).content
#
#   (langchain packages are not in requirements.lock: install them into .venv, and for the server add them to
#   requirements.txt + run tools/lock.py before packaging.)
#
# --- Variant: reuse the old project's client file as it is -------------------------------------------------------------
#   No copy needed: in data/settings.yaml give the endpoint  options: {module: "C:/old_project/llm_client.py",
#   function: "ask"}  — it is called as ask(system, prompt, timeout, endpoint=…, schema=…) with only the keyword
#   arguments it declares. Or wrap it here:
#
#   import sys
#   sys.path.insert(0, r"C:\old_project")
#   from llm_client import ask                                # the old project's function
#   def call(system, prompt, timeout, endpoint="api-a", schema=None, **kw):
#       return ask(f"{system}\n\n{prompt}")
