# LLM Wiki — an agentic RAG knowledge base over a department mailbox

A Django + React app that turns a logistics department's shared mailbox into a self-maintaining wiki, and a chat agent
that answers questions from it with sources. It runs on a single PC or on a Windows server, fully offline except for
the company's own LLM API.

- **Ingest:** POP3 (one or more shared department mailboxes), an Outlook `.pst` / mailbox (backfill; only mail sent to the department addresses) or `.eml` folders →
  one stored copy per message, careful threading (also separate replies that share quoted history), skip rules that keep
  automatic replies out but let people's replies to automated mail through.
- **Pipeline:** per-email notes (importance, AI-related) → routing to wiki pages (chosen from RAG candidates) → a guarded
  page editor (append-only history, confirm round, rollback) that writes each page as a short summary + its own sections,
  held to a length budget from the mail behind it and checked by script for repetition → tags → index. Script checks
  around the model: automated mail held back, page level (task/topic/project) only with enough emails, days and people,
  one owning department per page.
- **RAG:** hybrid retrieval (SQLite FTS5 BM25 + Qdrant vectors, weighted reciprocal rank fusion, wiki first), local multilingual
  embeddings (Korean works), a LangGraph chat agent with tools, and a deep multi-agent mode (planner → parallel researchers → writer).
- **Model access:** a pool with per-endpoint rate limits (e.g. 3 requests/min), chat priority, retries, failover when an API
  blocks the input, a durable job log. Internal APIs via one adapter file; Claude Code, Gemini CLI and Codex as CLI endpoints.
  Text-in/text-out with lenient JSON parsing, or the stage's JSON schema for APIs with structured output.
- **Privacy:** endpoints marked external (the CLIs) only get the wiki lint, never emails; personal data (phone numbers,
  e-mail addresses, IBANs, ID numbers, addresses) is removed from their prompts and from everything the model writes.
- **Portal:** Reports (one per department; daily "past 7 days", weekly (Thursday) and monthly; every part switchable in
  the settings; topic week grid) ·
  Emails (list, threads with parallel branches, week grid) · Wiki (cards/list, pages with sources) ·
  Agent (logs, pipeline inspector, pool, rules, tags, settings generated from the Pydantic model, eval) · floating chat.
- **Everything is stored in one SQLite file** (raw mail, attachments, pages, history, LLM call logs, vectors). The Qdrant
  folder is only a rebuildable index.

## Quick start (a PC with internet)

```bat
setup.bat          :: venv + packages (requirements.lock) + database + embedding model + doctor
run.bat --open     :: http://127.0.0.1:8000
```

Try it on the synthetic dataset (≈420 emails over 6 weeks to 4 department mailboxes, a fictional European logistics company):

```bat
llmwiki fetch --eml testdata\eml
llmwiki process --mode backfill        :: uses the endpoint(s) in Settings › Endpoints & pool
run.bat --open
```

For testing without an API key, the default endpoint is `claude -p --model haiku` (Claude Code CLI); `setup.bat --home`
sets up Gemini CLI on the synthetic data instead (`data_gemini\`, e.g. on the work laptop at home). Offline unit tests use the `mock` endpoint:
`.venv\Scripts\python -m pytest -q tests`.

## Work laptop → server

1. **Work laptop** (Outlook, pip, the internal APIs, optionally the CLIs): `setup.bat --laptop`, fill in `data\settings.yaml`
   and `data\company_adapter.py`, import skip rules (`llmwiki rules import`), `llmwiki pst_scan --days 7` (dry run of the
   department filter), then import + process the backlog in the portal (Agent › Logs). The wiki lint can use
   Gemini/Claude/Codex (PII removed, no emails). Step by step: [docs/SETUP_WORK_LAPTOP.md](docs/SETUP_WORK_LAPTOP.md).
2. `tools\package.bat --with-data` → `dist\llmwiki-offline-<date>-win_amd64-py311-WITH-DATA.zip` (wheels + models + code +
   the processed data; company data: keep it inside the company).
3. **Server** (no internet): unzip, `install.bat` (installs offline and imports the data), fill in `data\settings.yaml`
   (POP3, department addresses), keys with `llmwiki secret set …` (Windows Credential Manager), `doctor.bat --ping`, `run.bat`; then live mode on.
4. **Later code changes** (from any machine): `tools\patch.bat --note "…"` → a code-only zip; unzip it into the install on
   the other machine and run its `apply_patch.bat` (keeps data and settings, detects local changes, backs up, migrates).

Details: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Documentation

| Document | For |
|---|---|
| [docs/SETUP_WORK_LAPTOP.md](docs/SETUP_WORK_LAPTOP.md) | Step by step: GitHub → work laptop → credentials, API adapter, settings, skip rules → first mail |
| [AGENTS.md](AGENTS.md) | For a coding agent (Claude Code / Codex / Gemini CLI) setting up or changing this app: rules, the information needed and where it goes, checks |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works: data model, pipeline stages, RAG, chat, pool, scheduler — and where each piece lives in the code |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Install, run, backfill, live mode, troubleshooting, logs, backups |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Dev setup, tests, the synthetic dataset, evaluation, how to add a feature or a setting |
| [docs/CONFIG.md](docs/CONFIG.md) | Every setting with its default (generated: `manage.py docs_config`) |
| [docs/DECISIONS.md](docs/DECISIONS.md) | The design decisions and why they were made |

## Tech

Python 3.11+ · Django 5.2 + Django Ninja · Pydantic 2 · SQLite (WAL, FTS5) · Qdrant (local) · onnxruntime + tokenizers
(multilingual-e5-small / bge-small-en-v1.5) · LangGraph · Waitress · React 18 + TypeScript + Vite.
