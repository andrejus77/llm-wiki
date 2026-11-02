# Architecture

This document is written for developers and coding agents who need to fix or extend the system. It maps every
concept to the module that implements it. Read "Invariants" and "Gotchas" before changing anything.

## 1. The big picture

```
 sources                 store (SQLite)                 pipeline (per window)                         wiki + index
 ───────                 ──────────────                 ──────────────────────                        ────────────
 POP3 ──┐                Email (new text, notes)        units: thread-day (batch/backfill)            Page / Section
 .pst ──┼─► store.add() ─► EmailOriginal (raw .eml) ─►  or thread delta (live)                ─►      SectionHistory (append-only)
 .eml ──┘   dedupe,      Thread / ThreadKey (union-find)  │                                            Item (pending → written)
            threading,   Attachment → Blob (dedup)        ├─ 1 email: fast path S12 / S123            Chunk + ChunkVector (SQLite)
            skip rules   QuotedBlock (linked/unseen)      ├─ else: S1 notes → S2 route                    └─► FTS5 (chunk_fts, chunk_fts_tri)
                         Identifier, Tag (t-codes)        ├─ S3 editor per page (guards, confirm)          └─► Qdrant (rebuildable)
                                                          ├─ index → tags (script + S4) → learned rules
                                                          └─ reviewer (nightly / manual)
 all LLM calls ─► llm/pool.py (privacy rule, PII removal, rate limits, chat priority, retries, failover, schema)
                  ─► adapters (custom · http_text · openai_compat · claude_cli · gemini_cli · codex_cli · mock · chaos)
 transfer.py: export_data / import_data (work laptop → server: one consistent SQLite copy)
 chat ─► chat/agent.py (LangGraph or plain loop; deep mode) ─► rag/search.py (hybrid) + chat/tools.py
 scheduler.py (in the server process): nightly batch · live ticks · manual actions
 api.py (Django Ninja, /api) ─► frontend/ (React, built into frontend/dist, served by views.py)
```

One process (`manage.py serve`): Waitress serves Django (WSGI) with N threads; the scheduler and pipeline run in
background threads of the same process. CLI commands (`fetch`, `process`, `eval`…) run the same code in the foreground.

## 2. Module map

| Area | Module | What it does |
|---|---|---|
| Settings | `llmwiki/conf.py` | The Pydantic `Settings` model (every tunable value), presets, load/save, `MACHINE_KEYS` (settings.yaml wins for endpoints/paths/server). `get_settings()` everywhere |
| Secrets | `llmwiki/keystore.py` | `get(name)`: Windows Credential Manager (`llmwiki/<name>`, ctypes) → `data/secrets.yaml` → an environment variable of that name (older setups); `put`/`delete`/`needed()`; `{secret:NAME}` placeholders. Command `secret` |
| Privacy | `llmwiki/privacy.py` | `redact()` (PII → placeholders, reference numbers protected), `endpoint_allows()` (external endpoints → `privacy.external_stages` only) |
| Departments | `llmwiki/departments.py` | `assign()` the ONE owning department of a page (AI share → AI department, else the majority address; a person's choice is kept), `is_team()`, `refresh_pages()` (level + department after every window) |
| Automated mail | `ingest/automated.py` | `is_automated()`: system senders (globs) or a repeated subject template with near-identical bodies; never a person's reply |
| Transfer | `llmwiki/transfer.py` | `export_package()` / `import_package()`: the store as one SQLite copy + manifest; commands `export_data`, `import_data` |
| Logging | `llmwiki/logs.py` | Python logging config (files in data/logs), `event()` for user-visible events |
| Schema | `llmwiki/models.py` | All tables. `json_has()` helper for JSON-list filters on SQLite |
| Parsing | `ingest/mime.py`, `ingest/html2md.py` | RFC822 → `ParsedEmail`; HTML → text with markdown tables and a quote marker. Line breaks are tokens: source newlines are spaces, Outlook `MsoNormal` paragraphs are lines, an `&nbsp;` paragraph is one blank line, never two blank lines in a row (`clean_text()` for plain parts too); `reparse_bodies` re-derives stored mail |
| Quotes | `ingest/quotes.py` | New text vs quoted history (EN/NL/DE/SK/KO markers), signature cut, quoted blocks |
| Threads | `ingest/threads.py` | Union-find over Message-ID/References/Thread-Index/ConversationID + subject fallback + `join_shared_history()` |
| Store | `ingest/store.py` | `add()`: dedupe, save, attachments+extracts, quoted-block linking, shared-history joins, identifiers, t-codes, skip rules |
| Mail rules | `ingest/skiprules.py` | Header rules, automatic replies (`auto_reply_reason`), static + learned rules (replies by others exempt unless `include_replies`), audit samples, filing into folders; on stored mail: `preview()`, `apply_rule()` (skip, `unroute()`), `revert_rule()` |
| Health | `ingest/health.py` | `check_store()` invariants |
| Sources | `sources/pop3.py`, `sources/pst.py`, `sources/eml_folder.py`, `sources/fetch.py` | Iterators of raw messages; `fetch()` runs them and reconciles counts. `pop3.py`: one or more department mailboxes (`pop3_accounts`), a seen-list per mailbox, and the mailbox's department added to `Email.depts` (also for Bcc; the same mail in two mailboxes is stored once with both). `pst.py`: department filter (`is_dept_mail`), SMTP resolution of Exchange recipients, MAPI Message-ID/References, `scan_pst_com()` dry run |
| LLM | `llm/adapters.py`, `llm/mock.py`, `llm/jsonparse.py`, `llm/pool.py`, `llm/schemas.py` | Adapters (`HttpJson`: an HTTP API described by settings — presets, body template, response path, auth, TLS, proxy; `Custom`: company_adapter.py or any module/function; the Claude/Gemini/Codex CLIs) and error classes (`Blocked`, `RateLimited`…, `classify()`); lenient JSON; the pool (`run_structured`); JSON schemas per stage for structured output |
| Prompts | `pipeline/prompts.py` | System prompts per stage |
| Budgets | `pipeline/budget.py` | `assemble()`: sections with token budgets, fair share, cut order, meta for the UI |
| Units/context | `pipeline/context.py` | `Unit`, `build_units()`, email blocks, parent, outline, unseen quotes, candidate cards |
| Stages | `pipeline/stages.py` | `s1()`, `s2()`, `fast()` (S12/S123), `apply_route()`, the new-page rule |
| Fixers | `pipeline/parse.py` | Script repair of model output; decides when a retry is needed |
| Pages | `pipeline/pages.py` | Page creation (with dedupe), versioned `write_section()`, rejected drafts, rollback, issues, status, the length budget (`length_info`) |
| Editor | `pipeline/editor.py` | S3: `edit_whole()` (free structure: the whole page in one request) or per-section edits (template); draft → guards → confirm round → write or keep original; source markers; `rewrite_page()` |
| Guards | `pipeline/guards.py` | Hard rejects, warnings, diff summary, clean-up; `check_page()` (budget, section limits, `repeats()`), `drop_meta()` |
| Patches | `tools/patch.py` | Code-only patches between machines: build, apply (manifest-based conflict check, backups, migrate), status, rollback |
| Tags | `pipeline/tagger.py` | Script tags (site/partner/t-code) + S4 primary tags + t-code confirmation |
| Reviewer | `pipeline/reviewer.py` | Lint pages vs similar sections, duplicate pages, merges |
| Runner | `pipeline/runner.py` | `process(mode)`: windows → units (parallel per thread) → editor → index → tags → rules |
| RAG | `rag/embed.py`, `rag/vstore.py`, `rag/fts.py`, `rag/chunking.py`, `rag/index.py`, `rag/search.py`, `rag/rerank.py` | Embeddings, vector stores, keyword index, chunks, incremental indexing, hybrid search |
| Chat | `chat/tools.py`, `chat/agent.py`, `chat/service.py` | Tools + numbered sources; LangGraph/plain engines; deep mode; background runs |
| Reports | `reports/briefing.py` | One report per department: `periods(kind)` (daily = the 7 days before each date, weekly = the Thursday ones, monthly = calendar months `m:YYYY-MM`), `build(period, {dept, kind})` honouring `report_parts`, `topic_week()`, window-only agent input, cached texts, 2-column pairing |
| Scheduler | `scheduler.py` | Nightly batch, live ticks (auto-off), manual actions, DB backup |
| API | `api.py` | All `/api` endpoints for the portal |
| Eval | `evaluation.py` | Retrieval grid, answer judging, ingest metrics |
| Commands | `management/commands/*.py` | `serve`, `fetch`, `process`, `eval`, `doctor`, `models`, `check_store`, `docs_config`, `export_data`, `import_data`, `pst_scan`, `refresh_pages`, `rewrite_pages`, `reparse_bodies`, `vacuum`, `rules` (mail rules ⇄ YAML, `apply` on stored mail), `config` (show / set / apply settings from the command line), `secret` (keystore), `api` (probe / test an LLM API with a fixed test prompt) |
| Frontend | `frontend/src/*` | React portal (Reports, Emails, Wiki, Page, Agent, Search, Chat, drawer) |
| Dev tools | `devtools/*` | Synthetic dataset generator, truth checks, dumps |
| Deploy | `tools/*` | Offline packager, install/run batch files, adapter template, lock generator |

## 3. Data model essentials

- `Email.status`: `new` → `noted` (has a note) → `processed` (routed or not important); `skipped` (rule), `duplicate`
  (dup_of set), `failed` (retried next run). Notes live on the Email row (`note`, `kind`, `importance`, `due`).
- `EmailOriginal` holds the heavy part of an email (`raw` with its attachments, `body_text`, `quoted_text`) in its own
  table: SQLite has to walk over a large value to reach any column after it, so with these on the Email row every list
  and filter read the whole mailbox. Only `reparse_bodies` and `raw_bytes()` read it. **Lists use `LIST_DEFER` and pages:**
  `/emails` 100 rows (max 500), `/emails/threads` 60, `/pages` 120, week 600, a thread's latest 200, a page's newest 300.
- `Folder`, `Email.folder`: filing only, independent of processing. `rule_ref` / `folder_rule` = the rule that skipped or
  filed an email (what `revert_rule()` undoes); `skip_manual` / `folder_manual` = a person decided, rules leave it alone.
  `Page.needs_rewrite` = a rule took source emails away; the next run rewrites the page.
- Circuit breaker (`llm/pool.py`, state in `KV 'breaker'`): `pool.breaker_failures` failed requests in a row, or
  `breaker_share` of the last 30, stop an endpoint; with none left for a stage background work raises `Halted` and
  `runner.stop()` ends the run (status `stopped`). `runner` also stops after that many failed units in a row, whatever the
  cause. Chat is exempt. After `breaker_cooldown_min` one trial request; Resume or a manual start clears it.
- `ThreadKey`: the union-find. Keys `mid:`, `conv:`, `cid:` point to a Thread; merges move emails and keys to the oldest thread.
- `QuotedBlock`: quoted history split into blocks. `linked` = the stored email it matches; `linked IS NULL` = only copy
  ("unseen history"), sent to the model once (`sent_to_model`). `fingerprint` = hash of the block's words: the same
  quoted message in two replies has the same fingerprint → `join_shared_history()` merges their threads (same normalised
  subject, ≥ `shared_history_min_words`, within `shared_history_days`), for separate replies whose headers were lost.
- `SkipRule.include_replies`: by default a rule (aimed at automated mail) does not skip a reply/forward written by
  someone else; a "reply" from the rule's own sender still matches. Automatic replies are caught earlier by
  `auto_reply_reason()` (headers, subject patterns incl. Korean, delivery reports, no-reply senders). `Auto-Submitted:
  auto-forwarded` and Exchange's inbox-rule header are NOT automatic (that is forwarded department mail).
- `LlmCall.redactions`: personal-data items removed from that prompt; `status` includes `blocked` (every one is also an
  `Event` of kind `refusal`; Agent › Pool › Refusals = `api.refusals()`).
- `Email.ai_related` (the note-taker's `ai` flag, judged by meaning with example keywords), `Email.automated` (script),
  `Email.parallel_key` (fingerprint of the unseen message it answers — separate replies share it).
- `Page.level` is the script-checked level, `Page.level_proposed` the model's (the ceiling); `Page.dept` the one
  department whose report shows the page (`dept_locked` = set by a person in the portal).
- `Report`: cached agent texts; `period` = "<dept>|<date>", `part` = `story:<page id>` / `lead:<dept>`, found again by
  input hash (the same facts → the same text in the daily and the weekly report).
- `Page` → `Section` (current text, version, `sources` = email ids recorded by the script; free structure: `key`
  "summary" or a slug of the editor's `title`, `position` = order) → `SectionHistory` (every accepted and rejected draft,
  guard results, the LLM call; whole-page drafts are kept under the summary as "(whole-page draft)"). `Item` = a fact
  routed to a page (`section_key` "page" in the free structure, a template key otherwise), waiting for the editor.
- `Chunk` (content-hash id) → `ChunkVector` (one per embedding model). FTS5 tables use `rowid = Chunk.id`.
- `Job` (idempotency key per stage+unit+emails) and `LlmCall` (every request: sections, prompt, response, status).
- `Run` + `Event`: what Agent › Logs shows. `KV`: small runtime state (last batch day, live failures).

## 4. The pipeline in detail

`runner.process(mode)`:
1. Pending emails (`new`/`noted`/`failed`) are grouped into windows: batch = day, backfill = week (settings), live = one window.
2. `context.build_units()` → units (thread-day or thread delta). Units of one thread run **sequentially** (so a later
   day sees the earlier routing); different threads run **in parallel** (`_workers()` = sum of endpoint concurrency, ≤ 16).
3. `process_unit()`: single email (or `single_notetaker`) → `stages.fast()`; on failure → the staged path:
   `stages.s1()` then `stages.s2()` (skipped if every note is not important or the unit is note-only).
4. After each unit its emails are indexed (`index_emails`), so the next unit's routing can find them.
   `stages.apply_notes()` also runs the script checks on the notes: `automated.mark()`; an automated email rated
   `routine` becomes `pipeline.automated_cap` (not_important), so system confirmations stop before routing; and
   `apply_route()` never creates a page for a unit with only automated mail.
4b. `departments.refresh_pages()` on the pages this window touched: the level check (below) and the owning department.
5. `editor_phase()`: every page with pending items → `editor.edit_page()` (parallel per page).
6. Changed pages are indexed, script tags refreshed, S4 runs on new/untagged pages, learned skip rules updated.

Every stage builds its prompt with `budget.assemble()` and calls `pool().run_structured(stage, unit, build, parse)`.
`parse` is a stage fixer from `pipeline/parse.py`: it repairs what it can and only returns errors (→ one retry with the
error list) when the output is unusable.

**The model only ever writes labels the script gave it:** email numbers `[1..n]`, candidate pages `P1..Pk`, section keys.

**The new-page rule** (`stages.new_or_existing`), learned from the Haiku test runs:
- If the model saw the best-matching page among its candidates and still asked for a new page, the model wins
  (only a near-identical title, `pipeline.merge_new_titles`, is merged). Embedding cosines between different topics
  of one company are high (e5: 0.88–0.95 for unrelated logistics topics), so a cosine-only rule over-merged
  (v1: 16 pages for 25 storylines; v2 with this rule: 24).
- Pages the model could not have seen (created a moment ago by a parallel unit) are reused only if the cosine is
  above `pipeline.new_page_threshold_by_model[model]` **and** the titles share ≥ 50 % of their content words
  (`title_overlap`): the measured cosines of a correct merge (0.889) and two wrong ones (0.883, 0.885) were otherwise
  indistinguishable.
- Brand-new pages get a provisional overview and are indexed immediately, so the next unit's routing finds them.

**The level check** (`pages.refresh_level`): the model proposes task/topic/project (prompt: definitions by the nature of
the work); the script shows the highest level up to that proposal whose `pipeline.level_min` is met — counted on human
(non-automated) emails, active days and people (`pages.size_of`). New pages start at that level; as a page grows it is
promoted. `pages.change_level()` switches templates without losing text (`MOVE`: e.g. task.what → overview, status →
open/risks), re-keys pending items, and writes every move as a section version.

**The page editor, free structure** (`pages.structure: free`, the default since 1 Nov). The first Haiku runs with the
fixed template (6–7 sections per page, each rewritten on its own) produced pages about **twice as long as the mail
behind them** (38,872 words of pages for 20,815 words of mail on those pages): every section restated the overview, contradicted another
section, and talked about the sources ("the sources do not show …"). Now `editor.edit_whole()` rewrites the **whole page in
one request** — a summary (≤ `summary_words`) + up to `max_sections` sections with the editor's own titles — so it can
see and remove repetition. The prompt carries the page's **length budget** = `budget_ratio` × the words people wrote in
its emails (new text, no quotes, no automated mail), clamped to `budget_min`..`budget_max`, plus one line per email on the
page (for the summary and current state) and the open issues (which the page lists already, so they are not repeated).
`guards.check_page()` checks it by script: over budget / over a section limit / repeated sentences (`repeats()`: ≥
`repeat_similarity` word overlap, +0.15 between summary and section) / missing key terms of the new facts → confirm round;
a summary that is empty or a large unexplained shrink below min(old, budget) → reject; sentences matching
`pages.drop_phrases` are removed before the checks (`drop_meta()`). New sections are matched to old ones by title (exact or
fuzzy ≥ 0.6), so their history continues; dropped sections are emptied with the reason. The first rewrite on the h5
store: 25 / 25 pages accepted, 38,872 → 9,886 words (budget 9,977). `structure: template` keeps the old per-section path.

**Editor guards, template structure** (`guards.check`): empty → reject; shorter than `min_keep_ratio` × old without
`removed_reason` → reject; length change, missing key terms (numbers/codes from the items) → warnings → confirm round
(`editor._confirm`) with ORIGINAL, DRAFT and a diff. Rejected drafts are stored; the original stays; items stay pending
(max 3 attempts).

## 5. RAG

- Chunks (`rag/chunking.py`): one wiki section per chunk (split if > `section_max_words`), per-email notes, email new text
  (≤ `chunk_tokens`, overlap, tables whole), unseen quoted blocks, attachment extracts. Headers are script-built.
- Index (`rag/index.py`): `_sync()` makes the chunks of a ref exactly the wanted set (content hashes), `embed_missing()`
  stores vectors in `ChunkVector` and upserts to the store.
- Search (`rag/search.py`): `hybrid()` = FTS5 BM25 (+ trigram for Hangul) and vector search, RRF with `rrf_k`, source
  weights, recency boost, optional reranker. `candidate_pages()` groups wiki hits by page (S2 routing).
- Stores (`rag/vstore.py`): `QdrantStore` (local folder or server URL; all calls serialised — local mode is not
  thread-safe and allows one process) or `SqliteStore` (numpy brute force over `ChunkVector`). `get_store()` falls back
  to SQLite if Qdrant fails.
- Embeddings (`rag/embed.py`): own ONNX runner (onnxruntime + tokenizers): e5 = mean pooling + query:/passage:
  prefixes; bge = CLS pooling + query instruction. `hashed` needs no files (degraded fallback).

## 5b. Reports

`reports/briefing.py`. A report = (department, period): `d:<date>` = the `reports.window_days` full days before the date
(daily; the `weekly_weekday` one is the weekly report), `m:<YYYY-MM>` = the calendar month (story cards count mail per
week, the head shows a month calendar). `periods(kind)` lists one kind for the menu. `ReportParts` (settings) switches
every card part and section off; `build()` then leaves the data out and `generate_texts()` makes no request for it. `_scope()` takes the window's emails linked to pages with
`Page.dept == dept` (plus the department's unrouted mail), and drops pages without team activity (a team member sent a
non-automated email on it in the window). Stories are ranked by `_score()` (important human mail, people, decisions,
status changes, at-risk, project). The agent texts get `_story_input()`: only the window's notes, the facts those emails
added (Item), status changes and open issues inside the window, plus one background line marked as context; the lead
gets only that department's stories. The scheduler writes the texts every morning at `reports.daily_time`
(`scheduler.morning_reports`). `topic_week()` is the script-only days × hours grid of topic activity.

Thread drawing (`api.thread_graph`): each email's parent is its In-Reply-To email, else the stored email its first quoted
block matches, else a "ghost" for an unseen quoted message answered by two or more emails. The first answer continues its
parent's lane; every further answer opens a new lane — so separate replies show side by side, never as replies to each other.

## 6. Chat

`chat/agent.py`: node functions `node_retrieve`, `node_agent`, `node_tools`, `node_finalize` (fast mode) and
`node_plan`, `research()`, `node_write` (deep mode). `langgraph_fast()`/`langgraph_deep()` wire them into StateGraphs
(deep mode fans out with `Send`); `run_plain()` runs the same nodes in a loop / thread pool. If the LangGraph engine
raises, `answer()` falls back to the plain loop. Sources are numbered by `chat/tools.Sources`; `[n]` in the answer is
mapped back, unknown numbers dropped. `chat/service.py` runs a question in a thread and appends progress steps to
`ChatMessage.steps` (the portal polls `/api/chat/{id}`).

## 7. Pool and scheduling

`llm/pool.py`: endpoints from settings (rebuilt when settings change). `acquire(stage, chat, exclude)` waits for an
endpoint that allows the stage (`EpState.allows`: the endpoint's `stages`, plus `fallback_stages` during a failover, AND
`privacy.endpoint_allows`), has rpm headroom (sliding 60 s window; background leaves `chat_reserve_rpm`) and concurrency
headroom; background requests wait entirely while a chat is active (`chat_active()`). `run_structured()`:
1. builds the prompt for the chosen endpoint's cap; removes PII if the endpoint is external or has `redact_pii`;
2. passes the stage's JSON schema (`STAGE_SCHEMA` / the `schema=` argument → `llm/schemas.py`) if `structured_output`;
3. handles 429 (Retry-After/backoff, cooling, auto-lower rpm), timeouts (one retry with budgets × (1 − timeout_shrink)),
   `Blocked` and errors (failover: the request goes to the next eligible endpoint, `failed` set excluded; only when none
   is left the normal 5xx retries apply; a block with nowhere to go fails the stage), invalid output (validation rounds);
4. removes PII from the answer (`privacy.redact_outputs`) before parsing, and logs every attempt to `LlmCall`.

Adapters are text in → text out; `complete(system, prompt, timeout, max_tokens, schema=None)`. CLI adapters
(`_Cli` base): `claude -p` (system prompt flag), `gemini -p` (system prompt via the `GEMINI_SYSTEM_MD` file, approval
mode `plan` = read-only, `-o json`), `codex exec` (read-only sandbox, `--ephemeral`, `-o` last-message file,
`--output-schema`). Each runs in an empty temp folder. Errors go through `classify()`.

`scheduler.py`: `Scheduler.tick()` every 20 s: nightly batch once per day after `batch_time`; live ticks every
`live_tick_minutes` (fetch → process("live") only if new mail; lint every `live_lint_minutes`); after
`live_max_failed_ticks` failures live mode switches itself off (settings saved, event logged). `run_action()` runs
portal actions in named threads.

## 8. Invariants (tested in tests/)

1. Every fetched message → exactly one `Email` row, in exactly one non-merged `Thread`; counts reconcile per fetch.
2. Every processed email has a note. Nothing is processed twice (job keys, statuses).
3. A section is only replaced by a version that passed the guards; every draft is in `SectionHistory`.
4. Facts (ids, dates, counts, participants, sources) are written by scripts; the model only writes text and given labels.
5. The vector store can be deleted and rebuilt from SQLite (`manage.py shell -c "from llmwiki.rag.index import rebuild_vectors; rebuild_vectors()"`).
6. Email stages and the chat never reach an `external` endpoint unless `privacy.test_data_only`; external prompts never
   contain PII patterns; model outputs are cleaned before they are stored.
7. A `.pst` message without a department address in To/Cc is never stored (`pst_dept_only`).
8. A copied database keeps this machine's endpoints and paths (`MACHINE_KEYS`).

## 9. Gotchas

- **Python 3.11 is the target**: no backslashes inside f-string expressions, no `type` statements. Ruff with
  `--target-version py311` catches these.
- **SQLite has no JSON `__contains`**: use `models.json_has(qs, field, value)`.
- **onnxruntime ≥ 1.19 crashes on import** on Windows with an old VC++ runtime (msvcp140 < 14.40). It is pinned to
  1.18.1 (numpy < 2). `doctor` tests the import in a subprocess.
- **Qdrant local mode = one process**. Don't run `manage.py process` while `serve` runs on the same data folder — use the
  portal's actions instead, or set `retrieval.vector_store: sqlite` / a Qdrant server URL.
- **requirements.lock** is generated by `tools/lock.py` for Python 3.11.0 (some newest packages need ≥ 3.11.5).
- PowerShell `Set-Content` writes a BOM; Python files written that way fail with U+FEFF. Use the editor or UTF-8 without BOM.
- **CLI endpoints are separate processes** (~150–400 MB each). On an 8 GB machine keep `max_concurrent` at 2–3 per CLI.
- **Gemini CLI** needs a one-time interactive login (`gemini` → Login with Google) or `GEMINI_API_KEY`; error code 41 = no auth.
- **PII patterns are regexes**: a new reference-number format must be added to `store.identifier_patterns`, or a
  phone pattern may eat it (they are protected before redaction).
