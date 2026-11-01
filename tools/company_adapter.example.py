"""company_adapter.py — the bridge to the company's internal LLM APIs.

Copy this file to <data folder>/company_adapter.py (install.bat does that) and paste your existing API call code
into `call()`. It lives OUTSIDE the git repo, so internal URLs and code never end up on GitHub.

Rules:
- Return the model's answer as text (the app finds the JSON in it) — or, for an API with structured output, the
  parsed object (dict / Pydantic model); the app accepts both.
- Read keys from environment variables (os.environ), never write them in this file.
- `endpoint` is the endpoint name from Settings › Endpoints & pool ("api-b", "api-a"), so one file serves both APIs.
- `schema` is the JSON schema of the answer for this stage (only for endpoints with structured_output: true, e.g. the
  180 s API whose backend uses LangChain). Pass it on, e.g. `llm.with_structured_output(schema)` or as a request field.
- Errors: raise Blocked when the API refuses the input (e.g. its personal-data filter): the request then goes to the
  other API at once. Raise RateLimited (or any exception with status_code = 429; retry_after in seconds is honoured)
  on rate limits, TimeoutError on timeouts. Other exceptions are retried a few times, then failed over.
"""
import json
import os
import urllib.error
import urllib.request

from llmwiki.llm.adapters import Blocked, RateLimited

# Example settings — replace with your real values (or read them from environment variables).
URLS = {
    "api-b": os.environ.get("LLMWIKI_API_B_URL", "https://internal-llm-b.example/v1/generate"),   # 3 requests/min
    "api-a": os.environ.get("LLMWIKI_API_A_URL", "https://internal-llm-a.example/v1/generate"),   # 180 s limit, LangChain
}
KEYS = {"api-b": "LLMWIKI_API_B_KEY", "api-a": "LLMWIKI_API_A_KEY"}


def call(system: str, prompt: str, timeout: float | None, endpoint: str = "api-b", model: str = "", max_tokens: int = 8000,
         schema: dict | None = None):
    # ------------------------------------------------------------------------------------------
    # PASTE YOUR EXISTING CALL CODE HERE. The example below posts JSON and reads a "text" field.
    # ------------------------------------------------------------------------------------------
    body = {"system": system, "prompt": prompt, "max_tokens": max_tokens}
    if model:
        body["model"] = model
    if schema is not None:
        body["response_schema"] = schema          # <-- whatever field your API uses for structured output
    req = urllib.request.Request(URLS[endpoint], data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get(KEYS[endpoint], '')}"})
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


# The LangChain way, if you call the 180 s model from Python directly (sketch):
#
#   from langchain_openai import AzureChatOpenAI           # or the company's own chat model class
#   llm = AzureChatOpenAI(azure_deployment="...", timeout=180)
#   def call(system, prompt, timeout, endpoint="api-a", schema=None, **kw):
#       if schema:
#           return llm.with_structured_output(schema).invoke([("system", system), ("user", prompt)])   # → dict
#       return llm.invoke([("system", system), ("user", prompt)]).content
