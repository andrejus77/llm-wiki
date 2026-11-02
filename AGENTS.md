# AGENTS.md — instructions for a coding agent working in this repository

You are probably running on the **company work laptop** (Claude Code, Codex or Gemini CLI), asked to set the app up
for real use. This file tells you what the app is, the rules you must keep, what information is needed and where it
goes, how to connect the internal LLM APIs, how to check your work, and when you are done. For code changes, see
"Changing code" at the end.

LLM Wiki turns the department mailboxes into a team wiki + reports + a RAG chat (Django + React, SQLite, local
embeddings). Docs: `README.md` (overview), `docs/SETUP_WORK_LAPTOP.md` (the human's step-by-step — same steps as
here), `docs/OPERATIONS.md` (running it), `docs/ARCHITECTURE.md` (how it works), `docs/CONFIG.md` (every setting),
`docs/DECISIONS.md` (why).

## 1. Rules (not negotiable)

1. **You are an external model. Never read real email content.** Mail holds confidential and personal data, and only
   the company's internal APIs may see it. So do NOT open or query: `data/llmwiki.sqlite3` (or any `*.sqlite3`),
   `data/backups/`, `data/logs/llmwiki.log`, `.pst`/`.ost`/`.eml`/`.msg` files, exported data zips, or portal pages
   and API responses that show emails, notes, wiki pages, reports or chat answers. In the old project, read only
   code and configuration — never its mail exports, caches, databases or logs. Command outputs that are counts and
   statuses (`doctor`, `config check`, `rules import`, `pst_scan`, `fetch`, `process`, `check_store`, `api test`,
   `api probe` — these two send only a fixed test prompt) are fine. For an error you may read the traceback lines of
   `data/logs/errors.log`; if a line contains mail text, stop reading it. Judging the quality of notes and pages is
   the human's job (in the portal).
2. **No secrets in files, in this chat, or in environment variables.** API keys and passwords are stored by the HUMAN
   with `.\llmwiki secret set <name>` (typed hidden → Windows Credential Manager entry `llmwiki/<name>`). Settings and
   code refer to them only by name (`api_key_secret: api-a`, `password_secret: pop3-ct`, `secret("api-a")` in
   `company_adapter.py`, `{secret:NAME}` in headers). Never read the Credential Manager or `data/secrets.yaml` yourself.
   If the human pastes a secret into the chat, tell them to change it.
3. **Never commit or upload** `data/`, `data_*/`, `dist/`, `backups/`, `MANIFEST.json`, `company_adapter.py`,
   `settings.local.yaml`, `.env` (they are in `.gitignore` — keep it that way). Don't push to GitHub unless asked.
4. **Real mail goes only to the internal endpoints.** Keep `privacy.test_data_only: false` and
   `privacy.external_stages: ["review"]`. Never set `external: false` on a Gemini/Claude/Codex endpoint, never add an
   external endpoint to email stages, never turn off `privacy.redact_outputs`.
5. Don't start long runs (the full backfill) without the human saying so; it can take many hours at 3 requests/min.
6. When something is unclear, ask the human instead of guessing an address, a pattern or a limit.

## 2. Information to collect

Ask the human for whatever you can't find in the old project's configuration. Put every value where the table says.
`data/settings.yaml` is created by `setup.bat --laptop` from `tools/settings.laptop.yaml`; its `<-- fill in` marks are
the minimum.

| # | Information | Where it goes | Example / notes |
|---|---|---|---|
| 1 | Department mailbox addresses (all, incl. the AI department's) | `sources.dept_addresses` in `data/settings.yaml` | `["dl-ct@corp.com", "dl-cus@corp.com", "dl-fin@corp.com", "dl-ai@corp.com"]`, lower case |
| 2 | Display name of each department | `departments.names` | `{"dl-ct@corp.com": "Control Tower", …}` — one report tab each |
| 3 | The AI department's address | `departments.ai_address` | AI-related mail and pages go to its report, whatever address they were sent to |
| 4 | Team members (people whose activity makes a topic reportable) | `departments.team_members` (+ optional `team_by_dept`) | addresses or globs, e.g. `["*@corp.com"]` only if every internal sender counts |
| 5 | Company mail domains | `sources.internal_domains` | `["corp.com", "corp.co.kr"]` |
| 6 | Outlook display names of the department addresses | `sources.dept_aliases` | `{"dl-ct@corp.com": ["DL Control Tower"]}` — needed when `.pst` mail shows names, not addresses |
| 7 | Mail source on this laptop | `sources.pst_path` (`default` = the Outlook mailbox itself) **or** `sources.pop3_enabled: true` + `sources.pop3_accounts` | POP3 entry: `{dept: "dl-ct@corp.com", host: "pop.corp.com", port: 995, ssl: true, user: "dl-ct", password_secret: "pop3-ct"}` — one per shared mailbox |
| 8 | POP3 passwords | secrets named in `password_secret` — **the human runs** `.\llmwiki secret set pop3-ct` | |
| 9 | How each internal API is called (URL, auth, request/response shape — or the old project's client code) | `pool.endpoints` entries `api-b` / `api-a`: kind `http_json` (no code) or `custom` (`data/company_adapter.py`) | see §3 "Connecting an API" |
| 10 | API keys | secrets named in `api_key_secret` — **the human runs** `.\llmwiki secret set api-a` | |
| 11 | API limits | the same endpoint entries | `rpm` (API B: 3), `timeout_s` (API A: 180), `context_tokens` (API A: 32000), `structured_output` (true if `api test --schema` works) |
| 12 | Mail rules (newsletters, system notifications that must never be processed; folders to file mail into) | `data/rules.yaml` → `.\llmwiki rules import` | format in `llmwiki/management/commands/rules.py`; `sender` = glob, `subject` = regex, `recipient` = glob on To/Cc, `action` skip / note_only / keep, optional `folder` |
| 13 | Automated system senders (processed but held back, never create pages) | `pipeline.automated_senders` | globs like `"*workflow*@*"`; defaults already cover noreply/notification/batch/approval |
| 14 | AI keywords used by the old project | `departments.ai_keywords` | examples for the note-taker, not a hard filter |
| 15 | Reference-number formats (PO, delivery, invoice, container …) | `store.identifier_patterns` | regexes; defaults are SAP-style (`45\d{8}`, `8\d{7}`, `INV-\d+`); they are never removed as personal data |
| 16 | Tag vocabularies (systems, processes, work types) | `tags.systems`, `tags.processes`, `tags.work_types` | short slugs, e.g. `sap-s4`, `customs-declarations` |
| 17 | Time zone, language of the wiki | `sources.time_zone`, `pipeline.language` | `Europe/Amsterdam`, `English` (mail may be Korean; pages stay English) |
| 18 | Report rhythm | `reports.daily_time`, `reports.weekly_weekday` (3 = Thursday), `report_parts.*` | defaults match the human's request |
| 19 | Hugging Face mirror (only if the models must be downloaded again) | `retrieval.hf_endpoint` | then `.\llmwiki models --download` (gets both embedding models) |

What NOT to port from the old project: its prompts, its HTML parsing and its threading — this app has its own and
they are tested. Port values (addresses, lists, rules, patterns) and how the APIs are called — not other code.

## 3. How to apply them

- `data/settings.yaml`: edit it before the first run. It is read at start; once settings have been saved (portal or
  `config` command), the saved version wins for everything except the **machine keys** (`pool.endpoints`, `.pst`/`.eml`
  paths, `sources.pop3_enabled`, `sources.pop3_accounts`, `server`, `privacy.test_data_only`) — for those the file
  always wins. Later changes to other settings: `.\llmwiki config set <group.key> <json-or-text>` or
  `.\llmwiki config apply <file.yaml>` (validated; each change becomes a version in Agent › Settings › history).
- Secrets: tell the human the exact names to store (`.\llmwiki secret list` shows which ones the settings need and
  which are missing). They run `.\llmwiki secret set <name>` themselves.
- Mail rules: write `data/rules.yaml`, run `.\llmwiki rules import data\rules.yaml --dry-run`, fix errors, then import.
  Rules only look at new mail until they are applied. If mail is already stored, show the human the effect of
  `.\llmwiki rules apply --dry-run` (counts only, no mail text) and run `.\llmwiki rules apply` only when they agree:
  it also takes processed mail off its wiki pages.
- After any API change: `.\llmwiki api test <name>` must pass before processing. A run stops by itself after
  `pool.breaker_failures` (10) failed requests in a row and shows why; never raise that limit to get past failures, and
  never set it to 0. Fix the connection; a run started by hand (`.\llmwiki process`) resumes. See Agent › Pool or
  `.\llmwiki config show pool`.

### Connecting an API (do this for api-b and api-a, one at a time)

Only a fixed test prompt is sent by these commands — no mail.

1. **Find out how the old project calls it** (code/config only): URL, HTTP method, auth header (Bearer / `api-key` /
   other), request body shape, where the answer is in the response, whether it uses an SDK (openai, LangChain …) and
   whether it sends a JSON schema. Ask the human for anything missing. Have them store the key:
   `.\llmwiki secret set api-a`.
2. **Plain HTTP API → kind `http_json`** (preferred: no code). Let the app find the request shape:
   ```
   .\llmwiki api probe --url https://<host>/<base path> --secret api-a [--model <model or deployment>] [--proxy none]
                       [--auth header --auth-header api-key]      (a key in its own header, e.g. api-key: <key>)
   ```
   It tries OpenAI chat (`…/chat/completions`), Azure OpenAI, LangServe (`…/invoke`, typical for a LangChain backend),
   plain `{system, prompt}` JSON and Ollama, and prints the `pool.endpoints` entry to paste into `data/settings.yaml`
   (keep `rpm`, `timeout_s`, `context_tokens`, `stages`, `fallback_stages`, `priority`, `redact_pii` from the template).
   If no shape works, read the HTTP codes it printed: 401 → key/auth style; 404/405 → URL or path; 422 → body shape
   (write `options.body` yourself, see below); certificate / proxy errors → fix those first.
3. **API that needs Python (SDK, LangChain client, the old project's client) → kind `custom`**: put the call code into
   `call()` in `data/company_adapter.py` (template with a LangChain variant: `tools/company_adapter.example.py`), keys
   via `secret("api-a")`. Or point the endpoint at the old project's file without copying it:
   `options: {module: "C:/old_project/client.py", function: "ask"}` (called as `ask(system, prompt, timeout, …)`).
   Packages it needs that are not in `requirements.lock`: install them into `.venv` and note them for the human.
4. **Test**: `.\llmwiki api test api-a` shows the request (key masked), HTTP status, the raw response, where the
   answer was found, and a hint on failure. Fix and repeat until it ends with `OK: endpoint api-a works`.
   Then `.\llmwiki api test api-a --schema`: if the API accepts the JSON schema and still answers, set
   `structured_output: true` (for openai-style presets the schema goes to `response_format`; elsewhere set
   `options.schema_field`, e.g. `"schema"`, or put `"{schema}"` into `options.body`).
5. Finally `.\doctor.bat --ping` (one tiny request per endpoint).

`http_json` options (all optional; a preset fills most of them):

| Option | Meaning |
|---|---|
| `preset` | `openai`, `azure_openai`, `langserve`, `langserve_text`, `simple`, `ollama` |
| `path` | appended to `base_url` (`/chat/completions`, `/invoke`, `""`) |
| `method` | `POST` (default) |
| `body` | the request body as a YAML mapping; placeholders `{system}`, `{prompt}`, `{system_prompt}` (both joined), `{messages}` (OpenAI-style list), `{lc_messages}` (LangChain-style list), `{model}`, `{max_tokens}`, `{schema}`; a value that is exactly a placeholder keeps its type (list, number) |
| `response_path` | where the answer text is, dotted (`choices.0.message.content`, `output.content`, `data.text`); `auto` tries the usual places |
| `schema_field` | where the JSON schema goes when `structured_output: true` (`openai` = response_format, or a dotted body path) |
| `auth` | `bearer` (default when a key is set), `header`, `basic`, `query`, `none`; with `auth_header`, `auth_prefix`, `auth_user`, `auth_param` |
| `headers` / `query` | extra headers / URL parameters; values may contain `{secret:NAME}` |
| `verify_tls` / `ca_bundle` | TLS check (the Windows certificate store is used, which usually has the company CA); a `.pem` file for another CA; `verify_tls: false` only to diagnose |
| `proxy` | `system` (default), `none` (usual for internal APIs when the PC has an internet proxy), or `http://proxy:port` |

## 4. Procedure and checks

1. `.\setup.bat --laptop` (if `.venv` / `data\` don't exist yet). Needs Python 3.11 and pip access; models via
   `retrieval.hf_endpoint` if Hugging Face is blocked. `.\llmwiki models` must list BOTH embedding models as present.
2. Collect §2; write `data/settings.yaml`, `data/rules.yaml` (and `data/company_adapter.py` if a custom endpoint is used);
   give the human the list of secret names to store.
3. Checks — all must pass:
   - `.\llmwiki config check` → "settings are valid"
   - `.\llmwiki secret list` → no MISSING
   - `.\llmwiki api test api-b` and `.\llmwiki api test api-a` → OK
   - `.\llmwiki rules import data\rules.yaml` → no invalid rules
   - `.\doctor.bat --ping` → no FAIL lines (APIs answer, POP3 logins work, Outlook/.pst found, privacy summary shows
     external endpoints limited to `review`). The report is in `data\doctor-report.txt`. The Gemini / Claude / Codex
     endpoints are optional (wiki lint only): one that isn't installed and logged in gets `enabled: false` in
     `data/settings.yaml` instead of staying red.
   - `.\llmwiki pst_scan --days 7` (if `.pst`) → department messages found; if 0, fix `dept_aliases` / `pst_match_from`.
4. With the human's go-ahead: import one week (`.\llmwiki fetch --pst --since <date>` or `.\llmwiki fetch --pop3`) and
   process one day (`.\llmwiki process --mode batch --since <day> --until <day>`). Report the counts; the human
   looks at the results in the portal (`.\run.bat --open`). Agent › Pool › Refusals shows blocked requests.
5. The full backfill only when the human says so: `.\backfill.bat <since> <until>` (close the portal first).

**Done when:** the checks in 3 pass, one day was processed with 0 unit errors, and you have told the human: what you
filled in (values, not secrets), which secret names they stored, how each API is connected, what is still open, and
anything from the old project you could not map.

Commands run in PowerShell from the repo folder: `.\llmwiki <command>` (= `manage.py` with the right environment),
`.\doctor.bat`, `.\run.bat`, `.\backfill.bat`.

## 5. Changing code

- Python 3.11 compatible; facts by script, text by the model; every model call through `pool().run_structured()`;
  every new setting as a `Field` in `llmwiki/conf.py` (it appears in the portal automatically; regenerate
  `docs/CONFIG.md` with `.\llmwiki docs_config`). Details: `docs/DEVELOPMENT.md`.
- Tests: `.venv\Scripts\python -m pytest -q` (offline, mock model; ~2 min). Run them before handing over.
- Frontend changes need Node: `cd frontend; npm install; npm run build` (the built `frontend/dist` is committed).
- To bring a change to the server: `.\tools\patch.bat --note "what changed"` → `dist\llmwiki-patch-….zip`
  (code only; see `docs/OPERATIONS.md` §2b). Keep `docs/` in step with the change.
