# AGENTS.md — instructions for a coding agent working in this repository

You are probably running on the **company work laptop** (Claude Code, Codex or Gemini CLI), asked to set the app up
for real use. This file tells you what the app is, the rules you must keep, what information is needed and where it
goes, how to check your work, and when you are done. For code changes, see "Changing code" at the end.

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
   statuses (`doctor`, `config check`, `rules import`, `pst_scan`, `fetch`, `process`, `check_store`) are fine. For an
   error you may read the traceback lines of `data/logs/errors.log`; if a line contains mail text, stop reading it.
   Judging the quality of notes and pages is the human's job (in the portal).
2. **No secrets in files or in this chat.** API keys and passwords live in Windows user environment variables that
   the human sets with `setx` (see §3). Files reference only the variable *names*. If the human pastes a secret
   into the chat, tell them to rotate it.
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
| 7 | Mail source on this laptop | `sources.pst_path` (`default` = the Outlook mailbox itself) **or** `sources.pop3_enabled: true` + `sources.pop3_accounts` | POP3 entry: `{dept: "dl-ct@corp.com", host: "pop.corp.com", port: 995, ssl: true, user: "dl-ct", password_env: "LLMWIKI_POP3_PW_CT"}` — one per shared mailbox |
| 8 | POP3 passwords | env vars named in `password_env` — **the human sets them** | `setx LLMWIKI_POP3_PW_CT "…"` |
| 9 | The API call code for both internal APIs | `data/company_adapter.py`, function `call()` | port it from the old project; rules are in the file's docstring (see §3) |
| 10 | API key / URL variable names | read in `company_adapter.py` via `os.environ` — **the human sets the values** | keep the old project's names if it had them |
| 11 | API limits | `pool.endpoints` in `data/settings.yaml` (`api-b`, `api-a`) | `rpm` (API B: 3), `timeout_s` (API A: 180), `context_tokens` (API A: 32000), `structured_output` (API A: true if its backend takes a JSON schema) |
| 12 | Skip rules (newsletters, system notifications that must never be processed) | `data/rules.yaml` → `llmwiki rules import` | format in `llmwiki/management/commands/rules.py`; `sender` = glob, `subject` = regex, `action` skip / note_only |
| 13 | Automated system senders (processed but held back, never create pages) | `pipeline.automated_senders` | globs like `"*workflow*@*"`; defaults already cover noreply/notification/batch/approval |
| 14 | AI keywords used by the old project | `departments.ai_keywords` | examples for the note-taker, not a hard filter |
| 15 | Reference-number formats (PO, delivery, invoice, container …) | `store.identifier_patterns` | regexes; defaults are SAP-style (`45\d{8}`, `8\d{7}`, `INV-\d+`); they are never removed as personal data |
| 16 | Tag vocabularies (systems, processes, work types) | `tags.systems`, `tags.processes`, `tags.work_types` | short slugs, e.g. `sap-s4`, `customs-declarations` |
| 17 | Time zone, language of the wiki | `sources.time_zone`, `pipeline.language` | `Europe/Amsterdam`, `English` (mail may be Korean; pages stay English) |
| 18 | Report rhythm | `reports.daily_time`, `reports.weekly_weekday` (3 = Thursday), `report_parts.*` | defaults match the human's request |

What NOT to port from the old project: its prompts, its HTML parsing and its threading — this app has its own and
they are tested. Port values (addresses, lists, rules, patterns), not code — except the API call code (#9).

## 3. How to apply them

- `data/settings.yaml`: edit it before the first run. It is read at start; once settings have been saved (portal or
  `config` command), the saved version wins for everything except the **machine keys** (`pool.endpoints`, `.pst`/`.eml`
  paths, `sources.pop3_enabled`, `sources.pop3_accounts`, `server`, `privacy.test_data_only`) — for those the file
  always wins. Later changes to other settings: `.\llmwiki config set <group.key> <json-or-text>` or
  `.\llmwiki config apply <file.yaml>` (validated; each change becomes a version in Agent › Settings › history).
- `data/company_adapter.py`: `call(system, prompt, timeout, endpoint="api-b", model="", max_tokens=8000, schema=None)`
  returns the answer text (or a dict for structured output). Raise `Blocked` (from `llmwiki.llm.adapters`) when an API
  refuses the input — the request then fails over to the other API and is logged as a refusal; raise `RateLimited`
  on 429. Read keys/URLs with `os.environ`. Test it with `.\doctor.bat --ping` (sends one tiny request per endpoint).
- Secrets: give the human the exact `setx NAME "value"` lines to run themselves (user environment; a new terminal sees
  them). Never write the values anywhere.
- Skip rules: write `data/rules.yaml`, run `.\llmwiki rules import data\rules.yaml --dry-run`, fix errors, then import.

Commands run in PowerShell from the repo folder: `.\llmwiki <command>` (= `manage.py` with the right environment),
`.\doctor.bat`, `.\run.bat`, `.\backfill.bat`.

## 4. Procedure and checks

1. `.\setup.bat --laptop` (if `.venv` / `data\` don't exist yet). Needs Python 3.11 and pip access; if Hugging Face is
   blocked, the human sets `HF_ENDPOINT` to the internal mirror.
2. Collect §2, write `data/settings.yaml`, `data/company_adapter.py`, `data/rules.yaml`; give the human the `setx` lines.
3. Checks — all must pass:
   - `.\llmwiki config check` → "settings are valid"
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
filled in (values, not secrets), which `setx` lines they ran, what is still open, and anything from the old project
you could not map.

## 5. Changing code

- Python 3.11 compatible; facts by script, text by the model; every model call through `pool().run_structured()`;
  every new setting as a `Field` in `llmwiki/conf.py` (it appears in the portal automatically; regenerate
  `docs/CONFIG.md` with `.\llmwiki docs_config`). Details: `docs/DEVELOPMENT.md`.
- Tests: `.venv\Scripts\python -m pytest -q` (offline, mock model; ~2 min). Run them before handing over.
- Frontend changes need Node: `cd frontend; npm install; npm run build` (the built `frontend/dist` is committed).
- To bring a change to the server: `.\tools\patch.bat --note "what changed"` → `dist\llmwiki-patch-….zip`
  (code only; see `docs/OPERATIONS.md` §2b). Keep `docs/` in step with the change.
