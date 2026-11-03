# Operations

How to install, run and look after LLM Wiki. Three machines, one code base:

| Machine | What it does | Models | Settings template |
|---|---|---|---|
| **Work laptop at home** (internet; Gemini CLI works, the internal APIs don't) | install, `.pst` dry run + import (no model needed), and a Gemini test run on the **synthetic** mailbox in a separate data folder | Gemini CLI, synthetic data only | `tools\settings.home.yaml` (data_gemini\) |
| **Work laptop at the office** (company network, Outlook, pip, HF mirror, the CLIs) | processes the imported backlog, lints the wiki, then **packages code + data** for the server | internal APIs (API B 3 rpm, API A 180 s) for every email stage; Gemini / Claude Code / Codex only for the wiki lint | `tools\settings.laptop.yaml` (data\) |
| **Server** (Windows Server 2016, Python 3.11, offline) | runs the portal for the team, fetches new mail over POP3, nightly batch + live mode | internal APIs only | `tools\settings.server.yaml` |

`data\settings.yaml` is per machine: for the **machine keys** (`pool.endpoints`, `.pst`/`.eml` paths, the POP3
mailboxes, `server`, `privacy.test_data_only`) it wins over the settings saved in the database, so a database copied from the laptop never
brings the laptop's endpoints to the server (Agent › Settings shows these keys as "set in settings.yaml").

## 1. Work laptop at home: Gemini on the synthetic mailbox (and the .pst import)

Real mail is never processed with Gemini. At home the internal APIs are out of reach, so tonight is for setting up,
importing, and trying Gemini on the fictional test mailbox in its own data folder:

1. Gemini CLI logged in (`gemini` → Login with Google, or `GEMINI_API_KEY`); `gemini -p "hi"` answers.
2. `setup.bat --home` → `.venv`, models, `data_gemini\settings.yaml` from `tools\settings.home.yaml`
   (`privacy.test_data_only: true`: the external Gemini endpoint may do every stage — **only because the data is fictional**;
   `doctor` fails this check if non-test mail is in that folder).
3. `set LLMWIKI_DATA=%CD%\data_gemini`, then `llmwiki fetch --eml testdata\eml` and `llmwiki process --mode backfill`
   (~650 requests; Gemini's per-account quotas apply — lower `rpm` in the settings if you hit them).
4. `run.bat --open` → compare with the Haiku numbers in the guide.
5. The real mail can already be **imported** at home (no model involved): set up `data\` with `setup.bat --laptop` (§2),
   run `llmwiki pst_scan --days 7`, then the `.pst` import. Processing waits for the office.

## 2. Work laptop at the office: backlog from Outlook, processed with the internal models

The detailed walkthrough (GitHub → laptop → credentials → first mail) is [SETUP_WORK_LAPTOP.md](SETUP_WORK_LAPTOP.md);
an agent on the laptop follows [AGENTS.md](../AGENTS.md). In short:

**Once:**
1. Download the repository zip from GitHub, unzip it (e.g. `C:\llmwiki`). Python 3.11+, classic Outlook installed.
2. If needed: `set HF_ENDPOINT=https://<internal-hf-mirror>` (and pip's index/proxy as usual on the laptop).
3. `setup.bat --laptop` → `.venv`, models, `data\settings.yaml` from `tools\settings.laptop.yaml`, `data\company_adapter.py`.
4. Edit `data\settings.yaml`:
   - `sources.pst_path`: your `.pst` file, or `default` to read your Outlook mailbox itself (Exchange/.ost) — or, if the
     shared department mailboxes are reachable over POP3 from the laptop and still hold the backlog, `pop3_enabled: true`
     with one `pop3_accounts` entry per mailbox (more complete than your personal mailbox);
   - `sources.dept_addresses`: all department addresses (incl. the AI one); `dept_aliases` if Outlook shows them only by name;
   - `sources.internal_domains`;
   - `departments.names` (display names), `departments.ai_address` (the AI department's address) and
     `departments.team_members` (the team's addresses — reports only show topics a team member wrote about).
5. Connect the two APIs (details: [SETUP_WORK_LAPTOP.md](SETUP_WORK_LAPTOP.md) §6): a plain HTTP API as `kind: http_json`
   in the settings, or Python call code in `data\company_adapter.py` as `kind: custom` (`endpoint` is "api-b" or "api-a";
   `schema` is passed when `structured_output: true`; raise `Blocked` when API B refuses input).
   Store the API keys: `llmwiki secret set api-b` / `api-a` (Windows Credential Manager; `llmwiki secret list` shows what is missing).
   A plain HTTP API needs no code: `kind: http_json` — `llmwiki api probe --url …` finds the settings, `llmwiki api test api-a` checks them.
5b. Skip rules from the old project: `data\rules.yaml` → `llmwiki rules import data\rules.yaml` (`--dry-run` first).
   Other settings without the portal: `llmwiki config set <group.key> <value>` / `llmwiki config apply <file.yaml>`.
6. The CLIs for the wiki lint (optional, any of them): `npm i -g @google/gemini-cli` → `gemini` (log in);
   Claude Code → `claude` (log in); `npm i -g @openai/codex` → `codex` (log in). Each one only ever gets wiki page text with
   personal data removed (`privacy.external_stages: [review]`).
7. `doctor.bat --ping` → every line OK (endpoints, CLIs, Outlook COM, the `.pst` filter, privacy summary).

**The backlog:**
1. **Dry run first:** `llmwiki pst_scan --days 7` → per folder, how many messages have a department address in To/Cc
   (imported) and how many are personal (left out, never stored). If department mail is missing, add the display name under
   `dept_aliases` or set `pst_match_from: true` (mail a mailbox rule forwarded *from* the department address).
2. `run.bat --open` → Agent › Logs → ".pst import" with dates → **1. Import mail** → **2. Process as backfill**.
   Or from the command line with the server stopped: `backfill.bat 2026-09-01 2026-11-06`.
3. Agent › Logs → **Run the reviewer** (the lint, done by Gemini/Claude/Codex). With `reviewer.schedule: nightly` it also runs after each batch.
4. Check a few pages and notes. Agent › Pool shows which endpoint did what; blocked requests and failovers are in Agent › Logs.

**Ship it to the server:**
- `tools\package.bat --with-data` → `dist\llmwiki-offline-<date>-win_amd64-py311-WITH-DATA.zip`: wheels + models + code +
  `import\llmwiki-data.zip` (a consistent copy of `data\llmwiki.sqlite3` + `company_adapter.py` + a manifest).
  **It contains company mail: copy it only inside the company network.**
- Data only (code already on the server): `llmwiki export_data D:\transfer\llmwiki-data.zip`, then on the server
  `llmwiki import_data D:\transfer\llmwiki-data.zip --force` (server stopped; the old database is kept in `data\backups`).
- Code only (data and settings stay as they are on the other machine): a **patch**, see §2b.

## 2b. Code patches between machines

A change made on one machine (with Claude on the personal PC, or with another agent on the work laptop) reaches the
others as a **patch**: the code only, never `data\` (database, `settings.yaml`, `company_adapter.py`, models, logs).

1. **Build** on the machine with the change: `tools\patch.bat --note "what changed"` → `dist\llmwiki-patch-<version>.zip`
   (a few MB). If `requirements.lock` changed: `tools\patch.bat --wheels` (adds the wheels, ~300 MB; needs pip access).
   If the portal pages (`frontend\src`) changed, build them first (`npm run build` in `frontend\`); the patch tool warns.
2. **Copy** the zip to the other machine and unzip it **into the LLM Wiki folder** (the one with `manage.py` and `data\`).
3. **Apply:** run `apply_patch.bat` in the unzipped folder. First `apply_patch.bat --dry-run` lists what would change.
   It stops the portal (the `LLMWiki` scheduled task, or the process serving the port), backs up the files it replaces and
   a copy of the database to `backups\`, installs changed requirements, writes the code, deletes files the patch removed,
   runs `migrate` and `doctor`, writes `MANIFEST.json` and starts the portal again (scheduled task) — or tells you to run `run.bat`.
4. **Local changes are safe:** `MANIFEST.json` records the code each install/patch wrote. A file changed on that machine
   that the patch does not change is kept. A file changed on that machine **and** in the patch is a conflict: the patch stops
   and lists it; bring that change into the source machine first, or `apply_patch.bat --force` (the backup keeps the local version).
5. **Check / undo:** the portal header and `doctor` show the code version. `.venv\Scripts\python tools\patch.py status`
   lists files changed on this machine since the last install/patch. `... tools\patch.py rollback` restores the code before
   the last patch (database migrations are not undone automatically: the database copy is next to it in `backups\`).

The first patch on a machine set up before patches existed has no `MANIFEST.json` to compare with: it overwrites changed
files (they are in the backup). From then on local changes are detected.

## 3. Install on the server

1. Copy the zip, unzip it (e.g. `D:\llmwiki`), run `install.bat`. It creates `.venv`, installs offline, creates `data\`,
   copies the model files, `data\settings.yaml` (from `tools\settings.server.yaml`) and `data\company_adapter.py`, migrates,
   **imports `import\llmwiki-data.zip` if the package has one** (embeds anything missing and rebuilds the vector index), and
   runs `doctor --offline`.
2. Edit `data\settings.yaml`: `sources.pop3_accounts` (one entry per shared department mailbox: `dept`, `host`, `port`,
   `ssl`, `user`, `password_secret`), the department addresses, internal domains. Mail that reaches two department mailboxes
   is stored once and counts for both; a mailbox's department counts even when its address is not in To/Cc (Bcc).
3. `data\company_adapter.py` comes with the data package (or paste your call code). Secrets go into the Windows
   Credential Manager of the account that runs the service: `llmwiki secret set <name>` for each POP3 password (the
   `password_secret` names, e.g. `pop3-a`) and each API key, logged in as that account. `doctor` logs in to every mailbox.
4. `.venv\Scripts\python manage.py createsuperuser` (more users: Django admin at `/admin`).
5. `doctor.bat --ping` → every line OK; the report is in `data\doctor-report.txt`.
6. `run.bat` → `http://<server>:8000`; to start at boot: `tools\install_service.bat`.
7. The first POP3 fetch gets the whole department mailbox again: mail already imported from the `.pst` is recognised by its
   Message-ID ("merged"), so only new mail is processed. Then Agent › Settings › Schedule → **Live mode on**.

**VC++ runtime:** onnxruntime (embeddings) needs the Microsoft VC++ 2015-2022 runtime. Version 1.18.1 (pinned) works with older
runtimes (14.34); `doctor` checks the import. If it still fails: install the redistributable, or set
`retrieval.embed_model: hashed` (no model, degraded search) until it is installed.

## 4. Models, privacy and failover

- **Which endpoint gets a request:** the first free endpoint (by `priority`, then load) whose `stages` allow the stage and whose
  privacy rule allows it. `external: true` (Gemini/Claude/Codex, any public API) → only `privacy.external_stages`
  (default: `review`, the wiki lint) — unless `privacy.test_data_only` (synthetic data only).
- **Personal data:** prompts to external endpoints and to endpoints with `redact_pii: true` (API B in the templates) have phone
  numbers, e-mail addresses, IBANs, ID numbers and street addresses replaced by `[phone]`, `[email]`, … (reference numbers such
  as POs and deliveries are protected first). Everything the model writes (notes, pages, reports, chat answers) is cleaned the
  same way (`privacy.redact_outputs`). The stored emails keep the original. Patterns: Agent › Settings › Privacy.
  Each request shows how many items were removed (Agent › Pipeline).
- **Failover:** when an endpoint blocks the input (adapter raises `Blocked`, or a 400/403/422 mentioning a filter/policy/PII) or
  fails, the same request goes at once to the next endpoint that allows the stage — including endpoints that list the stage in
  `fallback_stages` (API A takes everything as a fallback, its normal `stages` stay the small ones). Privacy rules still apply:
  internal mail never fails over to an external endpoint. Logged as a warning event.
- **Structured output:** `structured_output: true` passes the stage's JSON schema (`llmwiki/llm/schemas.py`) to the adapter —
  `company_adapter.call(..., schema=...)`, `response_format` for OpenAI-compatible APIs, `--output-schema` for Codex. The answer
  may come back as a dict; the same parsers check it.

## 5. Day-to-day

| What | How |
|---|---|
| Nightly batch | automatic at `schedule.batch_time` (18:00): fetch → process → (reviewer if nightly) → DB backup |
| Live mode | Agent › Settings › Schedule › Live mode on. Every 5 min: fetch; if new mail → process (single emails take the fast path and edit pages directly). Switches itself off after 3 failed ticks; the nightly batch catches up |
| Manual actions | Agent › Logs: fetch, process, run the batch now, reviewer, re-index, backup, `.pst` import + backfill |
| Settings | Agent › Settings (every setting, with history), or `llmwiki config show / set / apply / check`. API keys are never stored there |
| Reports | Reports tab, one tab per department; **Daily / Weekly / Monthly** chooses the kind, the menu then lists only those reports. Every morning at `reports.daily_time` (07:00) the "past 7 days" report of each department gets its agent texts; the Thursday one (`weekly_weekday`) is the weekly report for the CFO meeting; on the 1st the monthly report of the month before (calendar month, mail per day as a calendar, story cards count per week). A topic is in exactly one department's report (page › Department shows why; "Move to" overrides it), and only if a team member (`departments.team_members`) wrote about it in the window. "Topic week" = which topics had mail on which day and hour ("Topic month": topics × days). The lists under the stories are at most one story card tall and scroll inside (`reports.list_max_px`) |
| What a report shows | Agent › Settings › **Report parts**: every part of a story card (agent text, newest decision, key emails, tags, status, level, people, day cells, open issues, next deadline) and every section (lead, mail per day, which stat cards, decisions, deadlines, waiting, needs attention, other topics, left-out list, topic view, footer) can be switched off. A switched-off agent text is not requested either. `reports.kinds` / `default_kind` choose the kinds offered |
| Wiki page length | Agent › Settings › **Wiki pages**: each page is a short summary + up to `max_sections` sections the editor names itself, held to a budget of `budget_ratio` × the words people wrote in its emails (between `budget_min` and `budget_max`). The page header shows "Length 180 / 200 words". Over the budget, repeated sentences or talk about the sources → the editor gets a confirm round to shorten; sentences matching `drop_phrases` are removed by the script. After changing these settings: Agent › Logs → **Rewrite pages** or `llmwiki rewrite_pages` (`--all` for every page). `structure: template` brings back the fixed sections per level |
| AI department | `departments.ai_address`: mail the note-taker marks AI-related (by meaning; `ai_keywords` are examples) and pages where ≥ `ai_page_min_share` of the mail is AI-related belong to the AI department, whatever address it was sent to |
| Page levels | the agent proposes project/topic/task; a page shows that level only when it has enough human emails, days and people (`pipeline.level_min`); page header shows "(→ proposed)" until then |
| Refusals | Agent › Pool › Refusals: every request an API blocked, per day and endpoint, the reason it gave and what happened next (failover result). Also events of kind `refusal` in Agent › Logs and a WARNING line in `errors.log` |
| Mail rules and folders | Emails › tick **Admin mode** (everybody in local mode, Django staff users on the server): a folder panel, **Create rule from this email**, **Rules**, and ticking rows or dragging them onto a folder. A rule has conditions (sender, subject, To/Cc) and actions (skip / note only / process as usual, and optionally a folder), and the dialog shows live what it would do to the stored mail. Saved with "also apply": waiting mail is skipped before any model sees it; processed mail is skipped, taken off its pages and out of search, and the next run rewrites those pages (a page left with no mail is archived). Changing, turning off or deleting a rule undoes what it did; the mail it skipped goes back in the queue. A decision made by hand on an email is never overridden. Also Agent › Rules, or as a file: `llmwiki rules export rules.yaml` / `llmwiki rules import rules.yaml [--replace] [--apply]`; `llmwiki rules apply [--dry-run]` runs all rules on the stored mail. Rules imported as a file only look at new mail until applied. Automatic replies (out-of-office, 자동 회신, bounces, no-reply senders) are always skipped; a rule for automated mail does **not** skip a person's reply or forward to it (feedback is processed) unless the rule has "also replies" |
| Stopping after failures | An endpoint is stopped after `pool.breaker_failures` (10) failed or rejected requests in a row, or when `pool.breaker_share` (80 %) of its last 30 failed; the others carry on, and when no endpoint is left for a stage the run stops (status `stopped`, mail stays waiting). A red banner on every view gives the reason. After `pool.breaker_cooldown_min` (60) the next *scheduled* run may send one trial request; a failure stops it again at once, so a broken API costs about one request per scheduled run, not one per email. **Resume** (the banner, Agent › Pool) clears it; a run started by hand (a button, `llmwiki process`) resumes too. 0 cooldown = stays stopped until someone resumes. Agent › Logs › **Stop processing** stops a running run by hand |
| Reading mail | Everywhere the same: the agent's note, then the mail under its own toggle. Pictures stand where they were written; those of earlier mail appear with that mail or in the quoted history. Signature pictures are hidden in the dropdown under the mail (`store.signature_*` settings). Earlier mail opens in place in the Emails tab. On wiki pages, threads and emails open in the side panel; its title leads to the full thread |
| Translations | Tick **Show translations** (Emails tab or side panel). Mail in another language is translated when you read it, by an internal model, and kept until its text changes; "show the original" switches back. Background processing pauses meanwhile, as for the chat. Settings › Translation: target language, languages shown as they are. Quoted history is not translated: earlier mail has its own translation |
| Tags | A term in the catalog is used whenever a page is about it, named or not. A new term must literally stand in the page's emails or text; refused ones are logged in Agent › Tags. `tags.implies` adds what follows for certain (any t-code means SAP). Pages start with few tags while the catalog is small. **Once a good amount of mail is processed: Agent › Tags › Check tags now** (or `llmwiki tags check`, `--all` for every page). It sends page text only, through the same route as the reviewer, so it may run on Gemini. Terms it finds reach all pages. It also runs with the reviewer. `llmwiki tags sweep` applies the catalog without a model; `tags reject system "Power BI"` removes a term for good; `tags alias system SAP "S/4HANA"` adds a spelling |
| Users | `/admin` (Django admin) |

**Throughput** with API B at 3 requests/min: ≈ 1 working day of mail in 20–30 min; 1 month backfill ≈ 6–8 h; with API A in
parallel roughly half. At 60 requests/min a month takes about an hour. Agent › Pool shows the live ETA.

**The chat comes first:** while anyone has a chat request waiting or running, background processing starts no new request
(running ones finish) and resumes 90 s after the last chat request.

## 6. When something goes wrong

| Symptom | Where to look / what to do |
|---|---|
| Something failed | Agent › Logs (runs, warnings), then `data\logs\errors.log` and `data\logs\llmwiki.log` |
| A red "Requests stopped" banner, a run with status `stopped` | An API kept failing, see the reason in the banner. Fix it (`llmwiki api test <name>`), press **Resume**, then process again. No mail is lost: it stays waiting and is picked up by the next run |
| Email list or filters slow | Lists load 100 rows at a time and no bodies, so this should not happen; check that the database is on a local disk and that the migrations ran (`manage.py migrate`). The file can shrink after the move of the stored originals to their own table: `llmwiki vacuum` with the portal stopped |
| A wrong or missing note/page | Agent › Pipeline → the unit: prompt sections with budgets, the raw answer, what the script wrote |
| A page lost text | Wiki page › section › history: every draft (accepted and rejected) with guard results; "Roll back" writes a new version |
| Many 429s / slow | Agent › Pool: rpm per endpoint (auto-lowered after repeated 429s), cooling, ETA |
| "no enabled endpoint allows stage …" | every endpoint for that stage is external or disabled: add an internal endpoint, or (synthetic data only) `privacy.test_data_only: true` |
| A topic is in the wrong report | page › Department → "Move to" (kept from then on); systematic: `departments.ai_page_min_share`, `ai_keywords`, `names` |
| A report is (almost) empty | `departments.team_members` must list the team's addresses; topics without a team member's mail in the window are left out (listed under "left out" on the report) |
| Automated confirmations still make pages | Agent › Rules (skip rule) or `pipeline.automated_senders`; the repeated-template rule needs `automated_template_min` mails with near-identical text |
| An API does not connect | `llmwiki api test <name>`: the request (key masked), HTTP status, response and a hint (401 key/auth · 404 path · 422 body · certificate → `options.ca_bundle` · proxy → `options.proxy: none` · answer not found → `options.response_path`). `llmwiki api probe --url …` tries the usual API shapes and prints working settings. `llmwiki secret list` shows missing keys |
| API B blocks input | Agent › Pool › Refusals lists every one with its reason; failover sends it to API A. Fewer blocks: `redact_pii: true` on API B. If API A has no `fallback_stages`, the stage fails |
| Department mail missing after a `.pst` import | `llmwiki pst_scan --days 30`; add `dept_aliases` (display names) or `pst_match_from: true`; the fetch run shows "N personal messages left out" |
| Gemini CLI errors | log in again with `gemini`; quota → lower `rpm`; `doctor` shows whether the CLI is found |
| Timeouts on API A | its jobs are cut to its 32k context; lower `budgets` or restrict API A's `stages` |
| The model's JSON is often unusable | Agent › Logs "units retried"; `structured_output: true` if the API supports a schema; `pipeline.combine: off`; `pipeline.single_notetaker` |
| Pages too long / repetitive | Agent › Settings › Wiki pages: lower `budget_ratio` / `section_words`; then `llmwiki rewrite_pages`. A page's summary › history shows each whole-page draft with the guard results (budget, repeats) |
| Email text has gaps / odd line breaks | HTML mail is converted with Outlook paragraphs as lines and at most one blank line; the portal re-parses stored mail once by itself after an update; by hand: `llmwiki reparse_bodies` (from the raw mail; `--dry-run` counts first) |
| A picture is missing or hidden as a signature | The dropdown under the mail shows hidden ones. Tune `store.signature_strip_height` (0 = off) and `store.signature_image_repeat`, then `llmwiki reparse_bodies` |
| A translation failed | The message says why (no internal endpoint allows stage `translate`, a refusal, a timeout); "try again" sends it again. Its request is in Agent › Pipeline under stage `translate` |
| A tag is wrong or missing | Wrong: Agent › Tags › reject. Missing: `llmwiki tags check`, or add a spelling with `tags alias` when the mail writes it differently |
| A patch stopped with CONFLICT | the listed file was changed on that machine and in the patch: copy the change to the source machine and build a new patch, or `apply_patch.bat --force` |
| Wrong threads | `manage.py check_store`; joins by subject fallback and by shared quoted history are logged as events; `store.subject_fallback_days: 0` / `store.join_shared_history: false` disable them |
| Search quality | `manage.py eval` (with a golden set); try `retrieval.embed_model`, `rrf_k`, weights; `retrieval.mode: bm25` as a fallback |
| Embeddings fail | `doctor`; fallback `retrieval.embed_model: hashed` |
| "The Qdrant folder is in use by another process" | Normal when a command runs beside the portal: Qdrant's local mode allows one process per folder. The second one reads the same vectors from SQLite with the same results and the index catches up by itself. `llmwiki doctor` shows who holds it. Still there with nothing else running: a Python process was left behind; end it |
| Other Qdrant errors | `retrieval.vector_store: sqlite` (vectors are in SQLite anyway); or delete `data\qdrant` — it is rebuilt from SQLite |
| LangGraph errors | the chat falls back to the plain loop automatically; `chat.engine: plain` makes it permanent |
| Import refused | "this machine already has N emails": `import_data … --force` (the old DB goes to `data\backups`) |

## 7. Data, backups, privacy

- Everything is in `data\llmwiki.sqlite3` (WAL mode): raw mail, attachments, notes, pages, history, vectors, settings, logs.
  Nightly copies in `data\backups\` (kept `store.backup_keep_days`). `data\qdrant\` is a rebuildable index. `data\models\` holds
  the embedding models. `data\logs\` the log files.
- Moving the data: `export_data` / `import_data` (above), or `package.bat --with-data`. Moving code: patches (§2b);
  each patch leaves the replaced files and a database copy in `backups\`.
- Mail content goes only to internal endpoints. External endpoints get the wiki lint with personal data removed. Embeddings run locally.
- The `.pst` is a personal mailbox: only mail with a department address in To/Cc is imported; the rest is never read past its recipients.
- The GitHub repository contains no company data (see `.gitignore`); the synthetic test data is fictional.

## 8. Logs

| Log | Content |
|---|---|
| `data\logs\llmwiki.log` | everything (DEBUG), rotated 10 × 20 MB |
| `data\logs\errors.log` | warnings and errors only |
| Agent › Logs (`Run`, `Event` tables) | runs, fetch counts (incl. personal `.pst` mail left out), guard rejections, rule changes, failovers, thread merges, imports/exports |
| Agent › Pipeline / Pool (`LlmCall`) | every model request: endpoint, prompt sections, budgets, cuts, PII items removed, raw answer, status (ok / invalid / rejected / timeout / blocked / error), latency |
