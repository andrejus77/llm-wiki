# LLM Wiki (work version): decisions and why

The single source of truth for what was agreed, as of **1 Nov 2026**, with the reason for each decision. Use it to start future chats.

- **The full build plan:** `llm-wiki-plans.html` (claude.ai artifact https://claude.ai/artifact/6y4odwKKUPnzCGuRz2vKJ9).
- **The interactive mockup:** `llm-wiki-mockup.html` (https://claude.ai/artifact/RS9yyoEoSzzwEPin6kHDbm).
- **Older plans and option write-ups** are in `archive/`. They are history only; where they disagree with this file, this file wins.

## 1. Context

- **What it is:** a wiki that maintains itself from a logistics department's shared mailbox, plus an **agentic RAG chat** that answers from it, for the user's current employer (NL).
- **Deadlines:**
  - **Sun 1 Nov:** first tested version, built on synthetic data.
  - **Mon–Tue:** process 1–2 months of real mail.
  - **Wed 4 Nov:** demo to colleagues and the boss.
  - **Fri 6 Nov:** the user's last day.
- **Why it matters:** it's the user's main **CV project**. They must be able to explain every library, parameter and design choice in an interview.
- **Hard constraints:**
  - **No email text leaves the company.** Email stages and the chat use only the internal APIs; embeddings run locally. External models (Gemini CLI, Claude Code, Codex) may only lint the wiki, with personal data removed (user decision, 1 Nov).
  - **Personal data (phone numbers, e-mail addresses, IBANs, ID numbers, addresses) is kept out of the wiki** and out of every prompt to an external model.
  - **Nothing may need manual curation after 6 Nov.** Everything runs automatically.
  - **Facts come from scripts, never the LLM:** IDs, dates, counts, participants, sources.
  - **API keys and passwords** live in the Windows Credential Manager (`llmwiki secret set`; or a git-ignored `data/secrets.yaml`), never in settings, environment variables or the repo.
  - **The public repo/demo** uses a fictional dataset and holds no company data.

## 2. Purpose and the priority rule (required)

**Priorities, in order:**
1. The **agentic knowledge base**: the RAG chat, with the wiki as its primary source. RAG is also used inside the pipeline.
2. The **human-facing knowledge base**: readable wiki pages, then the reports, then the emails view.
3. **Visual polish.**

**Rule: nothing less important may break something more important.**
- Every feature outside P0 has an on/off switch.
- Each feature runs per item in its own try/except.
- A feature that is unstable by **Mon 2 Nov 12:00** is switched off for the demo, not patched in a hurry.
- When two designs give the same wiki quality, the simpler one wins.

**Why:** the demo is in a few days and the user leaves right after, so robustness matters more than features.

| Tier | Features |
|---|---|
| P0 (demo fails without it) | email store + careful threading · staged pipeline S1–S3 · editor guards + history · index + hybrid search · chat agent · wiki page view · API pool + text-to-JSON parser · nightly batch · Settings · Django server + React portal · offline packaging · test harness |
| P1 (needed Wednesday) | live mode · single-email fast path · pause background work during chat · Emails tab · Briefing report · side drawer · Agent tab (Logs, Pipeline, Pool) · static skip rules + importance label · `.pst` import · script tags · reviewer run by hand · **deep (multi-agent) chat mode** |
| P2 (only if P0/P1 are stable) | wiki card view · Emails › Week view · learned skip rules · S4 tagger · reranker · attachment summaries · scheduled reviewer · tag lifecycle · Eval tab |
| Dropped | quote splitting into "recovered" emails · other report layouts · tag type growth · public static demo (for now) |

## 3. Decisions by area

### 3.1 Server and deployment

| Decision | Why |
|---|---|
| **Django 5 + Django Ninja** for the backend | Companies ask for Django. Ninja gives FastAPI-style typed endpoints, Pydantic schemas and OpenAPI docs *inside* Django. Django admin is a free edit UI for rules, tags and jobs |
| **No separate FastAPI service** | Two servers, two auth systems and models defined twice: more to break for no demo gain. Anything only Python may read (DRM) is read on the server anyway |
| **React 18 + TypeScript + Vite** frontend; the **built files are committed** | The user already knows React. The company PCs have no Node |
| **One process**: uvicorn + scheduler/pool worker threads | Works the same on a PC and a server. Qdrant local mode allows only one process per folder |
| **SQLite (WAL)** via the Django ORM; Postgres optional | Nothing to install |
| **All data lives in SQLite, not in raw files:** the raw `.eml` (compressed), attachments (as BLOBs, deduplicated by hash), extracts, notes, pages, section history, logs of LLM calls and the embedding vectors. The Qdrant folder is only a rebuildable index; deleting it loses nothing | One file to back up, copy and inspect (user decision, 31 Oct) |
| **Waitress (WSGI) instead of uvicorn**; the chat streams progress by polling, not SSE | Pure Python, robust on Windows Server, nothing to compile; Django's ORM is synchronous anyway |
| **Local mode:** `run.bat` on 127.0.0.1, no login. **Server mode:** Django login, users in admin | The same package serves both uses |
| **Windows Server 2016**, Python **3.11** (newer possible), started at boot by a scheduled task | That's the target server. `doctor` checks for the VC++ runtime that onnxruntime needs |
| **`package.bat`** on the company laptop builds an offline zip: win_amd64 / py3.11 wheels + the embedding model from the internal HF mirror, self-tested with `--no-index`; **`install.bat`** on the server | The server has no internet; the company laptop has pip and an HF mirror. One command, little user effort |
| Code comes from **GitHub as a zip**; the repo holds no company data (`data/`, `.env`, `settings.local.yaml` and `company_adapter.py` are git-ignored) | That's how code reaches the company laptop, and the repo is public for the CV |

### 3.2 Email sources and threading

| Decision | Why |
|---|---|
| Sources: **POP3** (30 days kept on the server) + the **24 GB `.pst`** in `Documents\Outlook Files\` (named after the mailbox) + `.eml` folders for tests | POP3 only holds a month; the demo needs 1–2 months |
| `.pst` read through **Outlook COM** (date filter, raw headers via `PR_TRANSPORT_MESSAGE_HEADERS`), else **pypff**, one message at a time, resumable | 24 GB can't be loaded into 16 GB of RAM. COM keeps the full headers for threading |
| **Every message is stored exactly once**: the key is the normalised Message-ID (or a content hash); duplicates are merged and the counts reconcile per day | Requirement: no duplicates and no lost context |
| Threads: union-find over Message-ID / In-Reply-To / References + Outlook **Thread-Index** + COM ConversationID; fallback = normalised subject + shared participant within 21 days; conflicting joins are refused | Gateways strip headers; a fallback join is logged so it can be checked |
| New vs quoted text is split by marker. **Quoted blocks are fingerprinted:** a block matching a stored email is linked; an unmatched block ("unseen history", e.g. a late CC) goes to the model **once** | Nothing is dropped and nothing repeats |
| `check_store` runs after every fetch (counts, one thread per email, a note per email) and shows on Logs | Problems are visible without reading code |
| Attachments are saved, with placeholders in the text and extracts for xlsx/pdf/docx/pptx; identifiers (PO, invoice, delivery, container) found by regex | Facts by script |
| 3 department addresses that only appear in CC are used for the department filters | That's how the mailbox works |

### 3.3 Skip rules and importance

| Decision | Why |
|---|---|
| **Static rules** (sender/subject/header → skip or note-only) | Automated mail wastes requests at 3 rpm |
| **Importance label** from the notes stage: important, routine or not_important. **not_important stops** after the note | Cheap early exit |
| **Learned rules (P2):** a sender + subject template judged unimportant ≥ 5 times on ≥ 3 days becomes a rule; every 10th match is still processed and the rule switches off if one is important | Requested; only if it doesn't put P0 at risk |

### 3.4 Processing pipeline

| Decision | Why |
|---|---|
| **Stages: S1 Notes → S2 Route → S3 Editor → S4 Tagger (P2)**, each skipped when not needed | Small outputs are where small models stay reliable, and a failure stays in its stage |
| **Every stage gets the full new emails** + the earlier stages' outputs + its own prompt and extras (candidates for S2, only the chosen pages for S3, tag lists for S4) | Notes always lose information; only the *output* is kept small |
| **S2 picks pages from RAG candidates** found with the S1 notes as the query; a new page only if the best candidate's cosine is < 0.55 | Clean notes are a better query than raw email, and the threshold stops duplicate pages |
| **Fast path for single emails** (P1): S12 in batch (note + route + items), S123 in live mode (+ section rewrite); it falls back to the stages on failure | Most units are one email; ~25–30 % fewer requests |
| One multi-round note-taker (the user's alternative) is kept as a **setting/fallback**, not the default | Use it if the stages prove brittle at work |
| **The model only writes labels it was given**: email numbers [1..n], candidate pages P1..Pk, template section keys | It can't reliably type IDs, paths or subjects |
| **At most 1 retry per stage**, only when the output is unusable (broken schema, a missing note, an empty section). The script fixes the rest: lenient JSON, trimming, unknown kind → info, fuzzy labels, dropping unusable items | 5 retries per email would be far too slow at 3 rpm |
| **Edit window:** live = per run, batch = per day, backfill = per week | A page touched 5 times a week costs one editor request in the backfill |
| **Tags belong to pages, not emails** | An earlier mockup mixed them up and it was confusing |

### 3.5 Schedules

| Decision | Why |
|---|---|
| **Nightly batch at 18:00 (P0)**: fetch → process → reviewer | Always works; the safety net |
| **Live mode (P1 toggle):** every 5 min **only if new mail arrived**; unit = the thread's new emails (1 → fast path, ≥ 2 → staged); lint every 90 min on changed pages | More useful for users, and hard to add later |
| The batch picks up whatever live missed (same job keys, so nothing runs twice); live switches itself off after 3 failed ticks | The batch must keep working if live doesn't |
| **Background work pauses while anyone chats** (running jobs finish) and resumes 90 s after the last chat request | The API key is shared, and the chat comes first |

### 3.6 Wiki pages and the editor

| Decision | Why |
|---|---|
| Pages are **prose for people**, kept up to date by the S3 editor. Since 1 Nov (late): a short summary + the editor's own sections within a length budget (see 3.15); the Task / Topic / Project templates remain as an option | Pages must explain things to a new colleague — and be faster to read than the mail |
| **Guards:** reject empty text; reject a section shrunk below 0.7× unless the model gives a `removed_reason`; headings and chatter fixed by script; warnings (length change > 15 %, an item's key terms missing, over the word limit) trigger a **confirm round** showing the original, the draft and a diff | The worry: blank output, or a paragraph replaced by one sentence |
| **The original is always kept:** an append-only history, rejected drafts included; rollback writes a new version; if both rounds fail the original stays and the items show as "not yet written in" | Nothing is ever lost |
| **No model-typed citations.** The script records which emails went into each section version (shown as a Sources strip). Optional `[n]` markers from the prompt's list; unknown ones are dropped; a missing one is never an error | The work model can't output IDs or paths, and strict checks would cause many retries |
| **Reviewer** (by hand first, scheduled in P2): contradictions (also against similar sections on other pages via RAG), stale wording, splits, duplicate pages | Keeps the wiki consistent; it goes through the same guards |

### 3.7 RAG

| Decision | Why |
|---|---|
| **RAG is used throughout:** chat, page routing, editor (related sections), reviewer, reports, search | Requested; it's also the CV story |
| **Wiki first:** source weights wiki 1.0 / notes 0.8 / emails 0.6; the chat answers from the wiki and uses emails for specifics | The wiki is the primary source |
| **Hybrid retrieval:** keyword (BM25) + vectors, fused by rank | IDs and t-codes need keywords; paraphrases need vectors |
| Chunking: one wiki section per chunk (≤ 450 words) with a script-built context header; email new text in ≤ 350-token chunks, tables as their own chunks | Natural units; tables keep their header row |
| **Chat fast mode** (P0): script retrieval before the first call, then a tool loop of ≤ 4 rounds. **Deep mode** (P1, user decision): planner → parallel researchers → writer, built with LangGraph | The multi-agent search the user asked for, kept optional |
| **Parameters are chosen by an evaluation:** ~80 golden questions from the synthetic world; recall@k, MRR, nDCG, faithfulness; an ablation grid | The interview answer to "how did you pick the parameters?" |
| **Library choices (user, 31 Oct):** Qdrant local mode · embeddings bge-small-en-v1.5 + multilingual-e5-small compared in the eval · SQLite FTS5 (BM25) · weighted Reciprocal Rank Fusion · **LangGraph for the chat agent** · reranker only if the eval shows a gain · own lenient JSON parser + Pydantic + 1 retry · own eval script · **deep multi-agent chat mode is P1** (must be in the demo) | Picked by the user from the options on the plan's RAG choices tab |
| **Every choice has a simpler fallback, one config line away:** vector store `qdrant` or `sqlite` (brute force over the vectors stored in SQLite) · embedding models: one or two, or hashed vectors · chat engine `langgraph` or `plain` (a Python loop) · deep mode on/off · reranker on/off · keyword tokenizer | If something breaks or is too slow at work, a setting changes instead of rebuilding a feature |
| **Korean mail is supported (work version only):** much of the real mail is Korean. Multilingual embeddings (multilingual-e5-small is the work default; bge-small-en is the English baseline) · a second FTS5 index with the trigram tokenizer for Hangul queries · Korean Outlook quote markers (보낸 사람, 받는 사람, 보낸 날짜, 제목, 원본 메시지) and subject prefixes (회신, 전달) · notes and wiki pages are written in English · a separate Korean test slice (not in the demo dataset) | No Dutch or German mail, but a lot of Korean. The home version stays English-only |

### 3.8 Model access

| Decision | Why |
|---|---|
| **API B:** no timeout, 3 requests/min now, maybe 60 later; **rpm editable 1–60**. **API A:** 180 s timeout, 32k; both run at once | The user's actual APIs |
| Both APIs take **plain text**; the user's existing call code goes into `company_adapter.py` (outside the repo); JSON is parsed from the text and validated. **API A (180 s) also accepts a JSON schema** (its backend uses LangChain): with `structured_output: true` the stage's schema is passed as `call(..., schema=…)` and a dict answer is accepted | API A turned out to support structured output (1 Nov); the same parsers stay the validators |
| **API B sometimes blocks input** (personal data); API A doesn't (it cleans data itself). A blocked or failed request **fails over to API A at once** (`fallback_stages: ["*"]`, so API A still only takes small stages normally). API B gets prompts with PII removed (`redact_pii`) so it blocks less | User's observation of the two APIs (1 Nov) |
| A **pool** with a durable SQLite job queue, sliding-window rate limits, parallel requests, Retry-After/backoff on 429, a smaller retry on a timeout, every call logged, and a backlog ETA | Rate limits and rejections must be tracked |
| **Budget presets 70k / 105k / 150k** (70k default); API A's 32k is an endpoint cap, not a preset | 32k presets were too tight |
| **The Settings page is generated from the Pydantic model**, so every setting is in the UI | Easy to change later, after the user has left |

### 3.9 Tags

| Decision | Why |
|---|---|
| **Classes (changed 3 Nov, see 3.19):** system, site, partner, process (from the catalog whenever the page is about it; a new term needs its exact words) · work_type (fixed list) · partner also from sender domains · code (t-codes; script) · references (PO etc.) are identifiers, not tags | Only a new term can be checked against the mail; whether a known term fits is a judgement |
| **T-codes keep growing:** a seed list, regex discovery in t-code context, automatic promotion after ≥ 2 threads (or S4 confirms), a stoplist, an optional TSTC import any time | New t-codes appear later |
| **Coloured pills, the same everywhere** (reports, wiki list/cards, page header). The registry is visible only under Agent › Tags | Consistency |

### 3.10 Portal

| Decision | Why |
|---|---|
| **Tabs:** Reports (front) · Emails · Wiki · Agent (Logs, Pipeline, Pool, Rules, Tags, Settings). Search in the top bar; a floating chat window that gets the current view as context | Agreed on 24–28 Oct |
| **Reports = Briefing, 2 columns.** Cards are paired by estimated height (≤ 2 lines apart; key emails and a text clamp even them out). Each card: title, status change, tag pills, a 60–120-word "this week" text (agent), 2–4 key email notes, a stats footer. No score/token numbers | Most of it comes from scripts, notes and the wiki; the agent writes only the lead + one text per story, with Pydantic length limits and a script fallback |
| **Side drawer:** emails, pages and source markers open on the right from anywhere; an email shows its **earlier emails in the thread** | Reading without losing your place; a reply needs its thread to make sense |
| **Emails:** List · Threads · Week (P2: days × hours, no agent work); sender checkboxes; importance labels; skipped mail greyed | Agreed |
| **Wiki:** cards/list toggle; filters for active date, department, participants, level, status and tags. On a card, **tags take at most 2 rows and the footer (emails, people, date) exactly 1 row**; the overflow becomes "+N" | Better use of space; consistent card heights |

### 3.11 Testing

| Decision | Why |
|---|---|
| Develop on the laptop with **synthetic data**: a fictional European logistics company, ~25 storylines, **~600 emails over 6 weeks**, with the hard cases built in. Ground truth + ~80 golden questions | No company mail or API here. Real volume is 100+ emails per working day |
| **Haiku through `claude -p` in text mode** as the stand-in model, measured at ~4 s per call and ~7 s for 6 in parallel; plus `mock` and `chaos` adapters (fault injection) | No API keys needed; it exercises the same text-to-JSON parsing |
| A Python 3.12 venv on the laptop (3.15 has no onnxruntime wheel); code checked against 3.11 syntax | The target is 3.11 |
| **`doctor`** checks the company setup and writes one report | Few round trips with a busy user |
| **Europe-only fictional names**, nothing Korea-related | User request |

### 3.12 Learned from the build and the test runs (1 Nov)

| Decision | Why |
|---|---|
| **Waitress (WSGI)**, chat progress by polling | Pure Python, robust on Windows Server; Django's ORM is synchronous |
| **onnxruntime pinned to 1.18.1** (numpy < 2); `doctor` tests the import in a subprocess | 1.19+ crashed on import on the dev laptop with an older VC++ runtime (14.34); the server may be the same |
| **requirements.lock resolved for Python 3.11.0** (`tools/lock.py`), wheels only | Some newest packages need ≥ 3.11.5; the server's patch level is unknown |
| **Own ONNX embedding runner** (Xenova ONNX exports of e5 / bge) | Packages offline cleanly; no PyTorch |
| **New-page rule:** the model's choice wins for candidates it saw; the cosine rule only guards parallel duplicates, and only together with title overlap | e5 cosines between unrelated topics of one company were 0.88–0.95; a cosine-only rule merged 25 storylines into 16 pages, this rule gives 24 |
| **Pages are about lasting topics, never one document number**; same site/partner alone is not the same topic | Early runs created pages per PO approval and mixed the Z1 credit block into the pallet page |
| **Editor writes "as of" the newest source email** | Haiku wrote "today is 1 Nov, the deadline passed" during the backfill |
| **chat_reserve_rpm default 0** | At 3 rpm a reserved slot costs a third of the throughput; the pause-while-chatting rule already gives the chat priority |
| **.pst via COM: sort by received time and filter in Python**, not `Items.Restrict` | Restrict's date strings depend on the Windows locale |
| **Unseen quoted history is deduplicated thread-wide** (normalised fingerprint) | The same never-received email is quoted in every later reply; it must reach the model once |
| **Eval judge gets the full cited sources and judges abstention itself** | With 300-character snippets, faithfulness was under-measured; keyword checks missed "the sources do not state…" |

### 3.13 Three machines, the CLIs and privacy (1 Nov, after the first build)

| Decision | Why |
|---|---|
| **Three situations, one code base:** work laptop **at home** (only Gemini CLI: install, `.pst` import, Gemini on the synthetic data) · work laptop **at the office** (processes the backlog with the internal APIs, lints the wiki, packages code + data) · **server** (POP3, nightly batch, live mode, internal APIs only) | The laptop has Outlook and the CLIs; the server has neither. The backlog is processed once on the laptop, new mail on the server |
| **Gemini CLI, Claude Code and Codex are CLI endpoints** (`gemini_cli`, `claude_cli`, `codex_cli`) like Haiku before: text mode, read-only, an empty temp folder; Gemini is the main one until the user is back at the office | The work laptop has all three, but from home only Gemini works; the user's personal PC has none of them |
| **External endpoints get only the wiki lint** (`privacy.external_stages: [review]`); email stages and the chat stay on the internal APIs. Only the synthetic dataset may be processed end-to-end by an external model (`privacy.test_data_only`) | Mail holds confidential and personal data |
| **PII removal by script:** regexes for phone numbers (international, labelled, NL/KR formats), e-mail addresses, IBANs, Korean ID numbers, street addresses (EU/KR); reference numbers are protected first; applied to external prompts, to API B's prompts, and to **everything the model writes** | "Phone numbers and personal information might need to be removed either way"; the wiki must not carry them |
| **The `.pst` is the user's personal mailbox:** only messages with a department address in To/Cc are imported; the rest is never read past its recipients. Exchange recipients are resolved to SMTP addresses, Message-ID/References come from MAPI properties when transport headers are missing; display-name aliases; `pst_scan` dry run; `pst_path: default` reads the Outlook mailbox itself | Department mail reaches the user's mailbox through the distribution lists, mixed with private mail |
| **Code + processed data ship together:** `package.bat --with-data` adds a consistent SQLite copy; `install.bat` imports it. `export_data` / `import_data` for data-only moves | The backlog processed on the laptop must arrive on the server as is |
| **Machine keys:** this machine's `settings.yaml` wins for endpoints, file paths, server and test_data_only over the settings stored in the copied DB | Otherwise the laptop's Gemini endpoint would arrive on the server |
| **Separate replies to one email are joined** when their quoted history shows it (a quoted block linked to an email in another thread, or the same quoted message with the same subject; ≥ 25 words; ≤ 60 days) | People reply separately; with headers lost (other mail systems, `.pst` copies) they became separate threads |
| **Skip rules and replies:** automatic replies (headers, out-of-office / 자동 회신 subjects, bounces, no-reply senders) are always skipped; a rule for automated mail **does not skip a person's reply or forward** to it (feedback is processed) unless the rule says "also replies"; a "reply" from the automated sender itself stays skipped; inbox-rule forwards (`Auto-Submitted: auto-forwarded`) are not automatic | The department sends a lot of automated mail; recipients' feedback on it matters, their auto-replies don't |

### 3.14 Departments, reports and script checks (1 Nov, evening)

| Decision | Why |
|---|---|
| **One wiki, one report per department.** Every page has exactly ONE owning department (`Page.dept`), so no topic appears in two reports; each report's agent texts see only that department's topics | The departments are almost fully separate; the user wants no topic in more than one report |
| **AI department:** the note-taker sets an `ai` flag per email (judged by meaning; a keyword list such as LLM, Gemini, Claude, n8n is given as examples, not a checklist). A page belongs to the AI department when ≥ 30 % of its human mail is AI-related, whatever address it was sent to | The user's earlier setup worked this way; keyword-only checks missed AI mail without the keywords |
| Otherwise the page belongs to the department address on most of its mail; a person can move a page in the portal (kept from then on) | Simple and visible; the script decides by default |
| **Reports only show topics a team member wrote about in the window** (`departments.team_members`) | Requested: only topics the team was actively involved in |
| **Two report rhythms:** a "past 7 days" report every morning at 07:00, and the Thursday one is the weekly report (before the CFO meeting on Friday). The window is the 7 full days before the report date | Requested |
| **The agent only sees the window:** the window's notes, the facts those emails added, status changes and open issues inside it; one background line marked as context only | Older facts must not reappear as news |
| **Report layout:** a mail-per-day strip next to the lead, the statistics as small coloured cards below it; day cells on topic cards are outlined when empty so the active weekdays are readable; a script-only **topic week** grid (days × hours) | The big hour heatmap took space without adding much; dark empty cells were unreadable |
| **Page level is proposed by the model and checked by script:** clear definitions in the prompt (project = time-bound with a goal and milestones; topic = ongoing subject; task = one piece of work or how-to), and a page only shows as a topic with ≥ 3 human emails, 2 days, 2 people and as a project with ≥ 6 emails, 3 days, 3 people. Promotions move section text to the new template without loss | The model alone was inconsistent; minimum sizes must be checked by script, not by the lint or the email agent |
| **Automated mail is held back by script:** system senders (globs) or a repeated subject template with near-identical text are "automated"; if the model rates such mail "routine" it becomes "not_important", and automated mail alone never creates a page. A person's weekly report (same subject, different text) is not affected | Many automated confirmations were still processed; the importance prompt now also lists them explicitly |
| **Status definitions** in the routing prompt (On track / At risk / Blocked / Done / Open) | Status updates were inconsistent |
| **Separate replies to one email are drawn as parallel branches** (own lane, "parallel reply k of n to …"; an unseen original shows as a placeholder), never as replies to each other | Requested: joined threads must look parallel |
| **Every refusal of an API is logged** (Agent › Pool › Refusals with reason and failover result, events, errors.log) | To see whether the 3 rpm API has issues |
| **PII removal also on API A** (not needed there, but one rule for all is simpler); reference numbers (PO, delivery, invoice…) are never removed and may go to any endpoint | User's call |

### 3.15 Shorter pages, report kinds and parts, HTML mail, code patches (1 Nov, late)

| Decision | Why |
|---|---|
| **A page is a short summary + sections the editor names itself** (free structure, the default): at most 4 sections, ≤ 130 words each, summary ≤ 60 words. The **whole page is rewritten in one request**, so the editor sees what is already said and says each fact once. The fixed templates stay available (`pages.structure: template`) | The Haiku pages were about **twice as long as the mail behind them** (38,872 words for 20,815), mostly the same facts repeated in every template section, plus contradictions between sections and talk about the sources. A wiki page must be faster to read than the mail |
| **Length budget from the mail:** 0.5 × the words people wrote in the page's emails, between 120 and 650 words. **Checked by script**, not only asked for: over the budget, a section or summary over its limit, repeated sentences (word overlap ≥ 0.7) or missing new facts → a confirm round to shorten; an unexplained collapse is rejected; sentences about the sources ("the sources do not show…") are removed by a regex list | More information is better than missing information, but not to the point where nobody reads it. The balance is a setting, not code |
| **Existing pages are rewritten once** (`rewrite_pages`, also a button) | On the Haiku store: 25 / 25 pages accepted, 38,872 → 9,886 words (budget 9,977); the Felixstowe checklist page went from 759 to 147 words for 302 words of mail, with no contradictions left |
| Page sections use the **full card width** (as wide as the open issues and the timeline) | The narrower prose column left empty space on the right |
| **Reports: Daily / Weekly / Monthly selector**; the menu lists only that kind. Monthly = the calendar month (a month calendar instead of the day strip, weekly cells on the story cards), written on the 1st at 07:00 | A menu of 14 daily reports was not useful |
| **The lists under the stories are at most one story card tall** and scroll inside | They took too much space |
| **Every part of a report is a setting** (Settings › Report parts): each story-card part (agent text, the newest decision, key emails, tags, status, level, people, day cells, open issues, next deadline) and each section; a switched-off agent text is not requested | So the report can be adjusted on the server without code or an agent |
| **HTML mail whitespace by tokens:** HTML source line breaks are spaces, Outlook paragraphs are lines, an empty paragraph is one blank line, never more than one blank line; zero-width characters removed; `reparse_bodies` re-cleans stored mail from the raw copy | Most POP3 mail is HTML; the old project's parser left 2–4 empty lines between rows |
| **Code patches between machines** (`tools\patch.bat` → zip; `apply_patch.bat` on the other machine): code only, never data or settings. A manifest per install detects files changed on that machine: kept if the patch doesn't touch them, a conflict (stop, or `--force`) if it does. Backups of the replaced code and the database, migrate, doctor, restart; `rollback` | Changes made with Claude on the personal PC or with another agent on the work laptop must reach the server without disturbing its data and settings |

### 3.16 Going live on the work laptop (2 Nov)

| Decision | Why |
|---|---|
| **Several POP3 mailboxes:** one `pop3_accounts` entry per shared department mailbox (own login, password as its own secret, its department). A mail in two mailboxes is stored once with both departments; a mailbox's department counts even for Bcc. Separate seen-lists per mailbox | Each department has its own shared mailbox with its own credentials; the first version had room for only one |
| **`AGENTS.md` for a coding agent on the work laptop** (+ `CLAUDE.md` / `GEMINI.md` pointing to it): the rules (it is an external model, so it never reads real mail, databases or mail logs; no secrets in files or chat), a checklist of every value needed with its place, the commands to apply and verify them, and when the setup is done | The setup should be possible without the user explaining everything; the agent must not become a privacy leak |
| **Settings and skip rules from the command line:** `llmwiki config show/set/apply/check` (validated, versioned) and `llmwiki rules import/export` (YAML) | An agent without a browser can configure everything; the old project's rules move over as a file |
| **Step-by-step guide** `docs/SETUP_WORK_LAPTOP.md`: GitHub (private first) → ZIP on the laptop → `setup.bat --laptop` → `llmwiki secret set` for keys → connect the APIs (`api probe` / `api test`) → settings → rules → `doctor --ping` → one week / one day first → backlog | The user does it alone on the company laptop |

### 3.17 Secrets and connecting the APIs (2 Nov, after the first install)

| Decision | Why |
|---|---|
| **No environment variables for secrets:** keys and passwords are stored with `llmwiki secret set <name>` (typed hidden) in the **Windows Credential Manager** (`llmwiki/<name>`, per Windows user; plain ctypes, no extra package), or in `data/secrets.yaml` with `--file`; settings hold only the name (`api_key_secret`, `password_secret`; the old `*_env` names are still read). `secret list` shows what is missing, never values | User request: env vars were awkward to set and easy to lose |
| **APIs without code where possible:** `kind: http_json` describes an HTTP API in the settings (presets openai / azure_openai / langserve / simple / ollama, or an own body template, response path, auth style, TLS CA bundle, proxy). `custom` (Python in `company_adapter.py`, or the old project's client file via `options.module`) stays for SDK / LangChain clients | API A did not connect easily, and Gemini on the laptop must be able to connect it without help |
| **`llmwiki api probe` / `api test`:** probe tries the usual request shapes against a URL and prints the settings block that works; test shows the request (key masked), status, response, where the answer is, and a hint per failure (401, 404, 422, certificate, proxy, response path). Only a fixed test prompt is sent | A step-by-step, checkable procedure for an agent, without it ever seeing mail or keys |
| **Both embedding models are downloaded** by setup (`models --download` gets every eval model; `retrieval.hf_endpoint` replaces the HF_ENDPOINT variable) | Only the active one was installed; the eval and a model switch need both |

### 3.18 First real use: stopping, speed, rules and folders (2 Nov)

| Decision | Why |
|---|---|
| **A run stops after 10 failed or rejected requests in a row** on an endpoint (`pool.breaker_failures`; also when 80 % of its last 30 failed). Other endpoints carry on; with none left for a stage the run ends with status `stopped`, the mail stays waiting, and a banner on every view says why. Per failed unit too, whatever the cause | An overnight run with a wrongly connected API made 600+ failed requests: each email was isolated, so nothing stopped it |
| **It stays stopped across restarts; after 60 min the next scheduled run may send one trial request**, and one failure stops it again. Resume or any run started by hand clears it. Chat is never held back | The user leaves on 6 Nov, so an API outage must not need a person to recover; a broken API now costs about one request per scheduled run, not one per email |
| **The stored originals moved to their own table** (`EmailOriginal`: raw mail, full body). Lists load no bodies and page: 100 emails at a time, 60 threads, 120 wiki pages, with the next page on scroll; rows off screen are not painted; stale requests are cancelled; text filters wait for typing to stop | 400 emails took 12–13 s on a 3,500-email test store with only 170 kB of originals per mail, far worse on real mail: SQLite must walk over a large value to reach any column after it, so every list and filter read the whole mailbox. Now 0.03–0.1 s. The migration took 19 s on that store (631 MB) |
| **The sender and team filters list department members only** (`departments.team_members` / `team_by_dept`); any other sender is found through the text filter | The list showed every sender, which is far too many |
| **A rule can run on the mail already stored**, with a live count before saving. Waiting mail is skipped before any model sees it. Processed mail is skipped, taken off its pages and out of search, and the next run rewrites those pages (with no mail left, the page is archived). Changing or removing a rule undoes what it did; the mail goes back in the queue | Before, rules only applied at import, so a new rule changed nothing among 3,500 stored emails. A skipped email that still fed a page would have made the reports and pages disagree |
| **Outlook-style rules and folders in the Emails tab**, behind an Admin mode tick (everybody in local mode, Django staff on the server): conditions on sender, subject and To/Cc; skip, note only or process as usual; optionally file into a folder. Create a rule from an email, tick rows or drag them onto a folder. Filing never decides processing, and a person's decision on an email always wins over rules | 3,500 emails a month need sorting, and rules should work the way people know from Outlook |
| **A wiki page linked from an email is listed at the top of the email** and opens in the side panel, as in Reports and Wiki | The link was at the bottom of the email, under the body and the thread history |

### 3.19 Reading mail in the portal, translations and tags that are true (3 Nov)

| Decision | Why |
|---|---|
| **A second process on the Qdrant folder is normal, not an error.** Local mode locks the folder to one process. Whoever comes second (a command while the portal runs, or the portal while a command runs) reads the same vectors from SQLite, says so once at INFO level and notes its writes in `vectors_behind`; the holder adds what is missing and removes what is gone | The user kept seeing "Qdrant unavailable". SQLite holds the vectors anyway, so results are the same, and the old warning was printed on every search |
| **A picture stands where its reference stands.** `[image: cid:…]` keeps the Content-ID (or the file name for the .pst route) and the portal shows the picture there. A message's parts hold every picture of its quoted history, so a picture referenced only in quoted text belongs to that history, not to the new mail. Only unreferenced pictures are attachments | All pictures of a thread collected at the bottom of the newest mail, and the side panel showed none |
| **Signature pictures are found by script and kept out of sight, not deleted:** small and below the closing, or a wide strip at the very end, or the same small picture written in ≥ 3 threads. A dropdown under the mail still shows them | They are a few lines of black text as an image. Script only, no image library, so it can be wrong; hence the dropdown and the `store.signature_*` settings |
| **The mail looks the same everywhere** (`mail.tsx`): who and when, the agent's note in its box, then the mail under its own toggle on its own background, closed at first. Earlier mail opens in place in the Emails tab. To/Cc and attachments are dropdowns | The note and the body ran together, the arrow sat beside the note, and earlier mail only opened in the side panel |
| **Threads open in the side panel outside the Emails tab**; the title and a button lead to the full thread. The panel has Back | User request: wiki links jumped to another tab |
| **A name in another script gets the user id: `김철수 (cs.kim)`,** when stored, for mail already stored, and in quoted headers. The id is removed from prompts to external endpoints | Korean names are unreadable and unsearchable for most of the team. A user id is part of an address, which must not leave the company |
| **Translation on request, for any language, kept until the text changes.** Internal models only, stage `translate`, with chat priority. Scripts keep the form: numbered lines in and out, blank lines and list markers never reach the model, tables checked cell by cell, codes and long numbers must survive; pictures, addresses and personal data are replaced by markers and put back. The thread's notes, the page summaries and the tag catalog go along as background. A line that still fails after the retry stays in the original | Nothing in it is specific to Korean except detecting the language, so it works for all. Line-by-line checks are what make "keep the formatting" true instead of hoped for |
| **A catalog tag is used whenever a page is about it, named or not; a new term needs its exact words.** A script checks the words of a new term against the page's emails and wiki text (`tags.evidence`). `tags.implies` adds what follows for certain, by script: any t-code means SAP, even if nobody writes the word | The user's rule. An abstract tag rarely appears word for word, and someone who only writes VL02N is talking about SAP. An earlier draft also demanded the words for catalog tags: dropped the same day for that reason. The fake `power-bi` came from my default list, written for the synthetic data, which the tagger had to choose from while seeing one sentence of each page; that list is gone |
| **The tag catalog grows in the lint, not per email.** `tags check` reads pages (text only, stage `review`, so Gemini may do it with PII removed), asks again for pages under `tags.page_min_tags`, and then a script gives every page the terms it mentions. A page may stay under the minimum: a tag is never invented to reach a number | User decision. A minimum makes no sense per email, and new terms must reach older pages without another model pass |
| **Stored mail is brought in line once by itself:** the portal runs `update_mail` when `MAIL_FORMAT` changes (re-parse, picture places, languages, then a tag sweep), no model requests | Nothing should need manual steps after the user leaves on 6 Nov |

## 4. Dropped, and why

| Dropped | Why |
|---|---|
| Cline/hooks-based ingest | Replaced by API + scripts + Pydantic |
| Free tags (4F), flat tag lists | Typed page-level tags instead |
| Report layouts other than Briefing; the single-column Briefing | The user preferred 2 columns with consistent heights |
| The "What it is" line on report cards | The title and the longer text are enough |
| 32k budget preset | Too tight |
| Strict citation checks | Too many retries with a weak model |
| Quote splitting into "recovered" emails | Unseen quoted blocks are handled instead |
| Separate FastAPI service | One Django server is simpler |
| GraphRAG, HyDE, LLM reranking | Too many requests at 3 rpm |

## 5. Still open

**Decided 3 Nov: Qdrant stays on, for the CV (user decision).** Measured 3 Nov with the app's own classes (random 384-dimension vectors, this PC): Qdrant's local mode is exact search in Python, not an index, and gives the same top 10 as the SQLite store, but slower: 20k vectors 50 ms vs 5 ms, 50k vectors 129 ms vs 12 ms, with a filter 1304 ms vs 46 ms, and it adds the one-process lock. A real Qdrant server has an index and no lock; `retrieval.qdrant_url` already connects to one. `retrieval.vector_store: sqlite` remains the one-line fallback.

Nothing blocking. The build started on 31 Oct evening. Test results and the feature list go in the guide (`llm-wiki-guide.html`); the code, README and technical docs are in `C:\Users\andre\Documents\llm-wiki\`.
