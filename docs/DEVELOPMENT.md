# Development

## Setup

```bat
py install 3.11                     :: or any Python 3.11/3.12
py -V:3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock -r requirements-dev.txt
set LLMWIKI_DATA=%CD%\data
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py models --download --all
cd frontend && npm install && npm run dev      :: Vite on :5173, proxies /api to :8000
.venv\Scripts\python manage.py serve           :: Django + API + scheduler on :8000
```

After frontend changes: `npm run build` and **commit `frontend/dist`** (the company PCs have no Node).

## Tests

```bat
.venv\Scripts\python -m pytest -q tests            :: ~1.5 min, offline (mock model, hashed embeddings)
:: tests in test_pipeline.py build on each other (ingest → pipeline → search/chat/API): run the whole file, not single tests
.venv\Scripts\ruff check llmwiki --target-version py311 --select E9,F --ignore F401,F841
```

| File | Covers |
|---|---|
| `tests/test_text.py` | subjects, tokens, quote splitting (EN/KO/Gmail/'>'), HTML tables, identifiers + t-codes, lenient JSON, budgets, guards, output fixers |
| `tests/test_pool.py` | chaos endpoint (429s, broken JSON) is retried; background waits while a chat runs; rpm window |
| `tests/test_pipeline.py` | the synthetic dataset: fetch reconciliation, duplicates, threading vs ground truth, idempotent re-fetch, the whole pipeline, hybrid search, all chat engines (LangGraph/plain × fast/deep), department reports (no topic in two reports, window-only agent input, team filter, topic week) + pairing, every API endpoint, settings round-trip |
| `tests/test_scheduler.py`, `tests/test_server_mode.py` | batch once a day, live auto-off after N failures; login required in server mode |
| `tests/test_settings.py` | every setting documented, presets 70k/105k/150k, save/load |
| `tests/test_privacy.py` | PII removal (reference numbers kept), external endpoints only for allowed stages, failover on blocked input (incl. `fallback_stages`), schema to `company_adapter.call(schema=…)` + dict answers + output redaction, the Gemini/Codex CLI adapters (stub executables), error classification, machine keys vs a copied DB |
| `tests/test_departments.py` | automated-mail detection (system senders, repeated templates; a person's weekly report and replies are not), the level check by size + text moved on a template change, department assignment (AI share, majority, a person's choice kept), team members, parallel replies in their own lanes |
| `tests/test_ingest_rules.py` | skip rules vs replies/forwards (people's feedback processed, system acks and automatic replies skipped, `include_replies`, inbox-rule forwards kept); threads joined by shared quoted history (and not across subjects) |
| `tests/test_pst.py` | the `.pst` import via a fake Outlook object model: date window, department filter (personal mail never stored), Exchange recipients → SMTP, MAPI Message-ID/References without transport headers, the dry-run scan, aliases |

## The synthetic dataset (`testdata/`)

A fictional European logistics company (`devtools/world.py`: people, partners, 4 department mailboxes incl. an AI team,
the team list (`TEAM`), the expected report department per storyline (`REPORT_DEPT`), 27 storylines + 2 Korean-language
storylines in a separate slice). The two AI storylines (`ai_mail_classifier`, `n8n_eta_flow`) were written by hand;
`n8n_eta_flow` is mailed to Control Tower only but belongs in the AI report. Regenerate:

```bat
.venv\Scripts\python -m devtools.gen_content --korean       :: Sonnet via `claude -p` writes bodies, facts, golden questions → testdata/raw/*.json (cached)
.venv\Scripts\python -m devtools.build_emails               :: script builds testdata/eml/*.eml + truth.json + golden.json (+ noise mail and hard cases)
.venv\Scripts\python -m devtools.build_emails --korean      :: testdata/ko/ + truth_ko.json + golden_ko.json
```

The builder adds: Outlook-style quoting of the real previous mail, Thread-Index, signatures/disclaimers, HTML tables,
PNG screenshots, xlsx/pdf attachments, automated noise (SAP job reports, workflow approvals, track & trace, booking
confirmations from an ordinary-looking address, newsletters, out-of-office), a late-CC thread (earlier mail only in
quotes), alias duplicates, a resend under a new Message-ID, replies without References (subject fallback).

Useful helpers (run with `manage.py shell -c "import devtools.<name>"`): `summary` (counts), `dump_pages` (page texts),
`dbg_threads` (split threads vs truth), `check_truth` (`threading_scores()`; after a pipeline run `dept_scores()`:
storylines in the right department's report, AI-flag recall / false positives, automated noise held back).

## Evaluation

```bat
manage.py eval                                  :: retrieval grid (BM25 / vector / hybrid / + weights / + recency × models) + ingest metrics
manage.py eval --answers 20                     :: + 20 chat answers judged by the LLM
manage.py eval --answers 12 --modes fast,deep --engines langgraph,plain
```

Results go to `eval/results/*.json` and Agent › Eval. Relevance: a retrieved chunk is relevant if it is a gold email
or a wiki section of a page that cites a gold email. This is how the defaults (hybrid, weights, e5 vs bge, thresholds)
were chosen — rerun after changing chunking or models.

On an 8 GB laptop keep the test endpoint at `max_concurrent: 2` for answer evals: every `claude -p` call is a separate
process, and deep mode plus the judge ran the machine out of memory once at 6.

### The user guide (features + test results)

```bat
set LLMWIKI_DATA=%CD%\data_h4
manage.py shell -c "from devtools.collect_facts import main_store; main_store()"   :: run + ingest metrics → eval/facts/store.json
manage.py serve --port 8011                                                        :: then take eval/screens/*.png (headless Edge/Chrome --screenshot)
python -m devtools.collect_facts merge "35 of 35" eval\facts\facts.json            :: + eval results (from the latest note containing FINAL), Korean facts, chat example
python -m devtools.build_guide eval\facts\facts.json <out.html>                    :: one self-contained page, screenshots inlined as JPEG
```

## How to…

**Add a setting:** add a field with `Field(default, description=..., ge/le)` to the right group in `llmwiki/conf.py`.
It appears in the portal automatically; read it with `get_settings().<group>.<field>`. Regenerate `docs/CONFIG.md`
with `manage.py docs_config`.

**Add a pipeline stage / change a prompt:** prompts in `pipeline/prompts.py`; stage logic in `pipeline/stages.py` or
`pipeline/editor.py`; output repair in `pipeline/parse.py`. Always: build sections with `budget.assemble()`, call
`pool().run_structured(stage, unit, build, parse)`, write facts by script. Add the stage name to the mock
(`llm/mock.py`, keyed by the `TASK:` line) so tests keep working offline.

**Add a chat tool:** implement it in `chat/tools.run_tool()` (add evidence with `src.add()` so it gets a number) and
document it in `TOOLS_DOC`.

**Add an API endpoint / view:** `llmwiki/api.py` (Ninja), then a component under `frontend/src/views/`.

**Add a source:** a generator yielding `(raw_bytes, source_ref, conversation_id)` in `llmwiki/sources/`, wired in
`sources/fetch.py`. Everything goes through `ingest.store.add()`.

**Add a model provider (adapter):** a class in `llm/adapters.py` with `complete(system, prompt, timeout, max_tokens,
schema=None) -> str`, registered in `make_adapter()` and in the `Endpoint.kind` literal (`conf.py`). Map failures with
`classify()` so the pool sees `RateLimited` / `Timeout` / `Blocked` / `ServerError`. CLIs: subclass `_Cli` (empty temp cwd,
prompt on stdin). Test it with a `.cmd` stub like `tests/test_privacy.py::test_gemini_and_codex_cli_adapters`.
If it sends data outside the company, configure it with `external: true`.

**Add a stage that should use structured output:** add a Pydantic model to `llm/schemas.py` (`MODELS`), and either map
the stage in `pool.STAGE_SCHEMA` or pass `schema="name"` to `run_structured`. The parser stays the validator.

**Add a personal-data pattern:** `privacy.pii_patterns` (settings; name → regex, placeholder from `privacy.PLACEHOLDER`
or `[name]`). If it collides with a reference number, add that format to `store.identifier_patterns` (protected first).
Extend `tests/test_privacy.py::PII_TEXT`.

**Change the dependency set:** edit `requirements.txt`, run `python tools/lock.py` (resolves for Python 3.11.0 /
win_amd64), `pip install -r requirements.lock`, run the tests.

**Ship a change to another machine:** run the tests, build the frontend if `frontend/src` changed, then
`tools\patch.bat --note "…"` (add `--wheels` if `requirements.lock` changed). The zip holds only the files listed by
`tools/patch.py:code_files()` (DIRS + ROOT_FILES); a new top-level folder must be added there. Applying it is described in
OPERATIONS §2b; `tests/test_patch.py` covers build → apply → conflict → rollback.

**Change what a report shows:** add a bool to `ReportParts` (`conf.py`), leave the data out in `reports/briefing.build()`
when it is off (and skip any agent request for it in `generate_texts()`), and hide it in `views/Reports.tsx` via `d.parts`.

**Tune page length:** `pages.*` settings (budget, limits, `drop_phrases`); the prompt is `prompts.PAGE_RULES`, the checks
`guards.check_page()`. Measure with `manage.py rewrite_pages --all` on a copy of a store and compare words vs budget.

## Conventions

- Facts by script, text by the model; the model only writes labels it was given (email numbers, P1..Pk, section keys;
  in the free page structure its own section titles, which the script maps to keys).
- Every LLM call goes through the pool; every user-visible event through `logs.event()`; module loggers for the rest.
- No feature may break a more important one: non-P0 features have a setting to switch them off and run in their own
  try/except (see `pipeline/runner.py`).
- Python 3.11 compatible code; UTF-8 files without BOM.
