# Setting up the work laptop, step by step

From the code on GitHub to the first processed mail on the company laptop. Steps 1–2 happen on the PC where the code
is now; the rest on the work laptop. An agent on the laptop (Claude Code, Codex or Gemini CLI) can do steps 6–9 on its
own: it reads [`AGENTS.md`](../AGENTS.md), asks you for what it needs, and never touches the mail itself.

Commands are for **PowerShell in `C:\llmwiki`** (PowerShell runs a script from the current folder only with `.\` in
front: `.\llmwiki`, `.\doctor.bat`; in cmd.exe the `.\` is optional).

**What you need at hand** (from your old project or your head) — the full list with examples is the checklist in
`AGENTS.md`:
- the department mailbox addresses, their display names, which one is the AI department, the team members' addresses
- how the two internal APIs are called (URL, key, or the old project's client code) and their keys
- the POP3 host and the login of each shared mailbox (if the laptop fetches over POP3), or your `.pst` / Outlook mailbox
- the skip rules and automated-sender lists from the old project

---

## 1. Put the code on GitHub (the PC where the code is)

1. Open a **new** PowerShell window (so Git is on the PATH) and check: `git --version`.
2. Once per PC — name and the private GitHub e-mail (GitHub › Settings › Emails › "Keep my email addresses private"
   shows it, e.g. `12345678+name@users.noreply.github.com`):
   ```
   git config --global user.name "Your Name"
   git config --global user.email "<the noreply address>"
   ```
3. On github.com (log in; forgotten password: <https://github.com/password_reset>): **New repository** → name `llm-wiki`,
   **Private** for now (you can make it public for your CV later), no README / .gitignore / licence.
4. In the code folder:
   ```
   cd C:\Users\<you>\Documents\llm-wiki
   git init -b main
   git add .
   git status
   ```
   `git status` must list about 670 files: `llmwiki/`, `frontend/`, `docs/`, `tests/`, `testdata/` (the fictional mailbox),
   `tools/`… and **no** `data`, `data_*`, `dist`, `backups`, `MANIFEST.json`, `*.sqlite3`, `company_adapter.py` or `.env`.
   If one of those shows up, stop and fix `.gitignore` first (`git rm -r --cached <path>`).
5. ```
   git commit -m "LLM Wiki"
   git remote add origin https://github.com/<your-user>/llm-wiki.git
   git push -u origin main
   ```
   The first push opens a window: **Sign in with your browser**. Windows remembers it afterwards.
6. On github.com, check the file list once more: no data folders.

Later changes from this PC: `git add .` → `git commit -m "…"` → `git push`. Or a code patch (step 11).

## 2. Get the code onto the work laptop

- **Without git** (simplest): on the laptop, open the repository on github.com (log in — the repo is private) →
  **Code › Download ZIP** → unzip to `C:\llmwiki` (rename the folder `llm-wiki-main` to `llmwiki`; avoid OneDrive folders).
- **With git** (if installed there): `git clone https://github.com/<your-user>/llm-wiki.git C:\llmwiki`.

Needed on the laptop: **Python 3.11** (`py -3.11 --version`), pip working through the company's proxy/index, classic
Outlook (for the `.pst`). If Hugging Face is blocked, put the internal mirror into `tools\settings.laptop.yaml` › `retrieval.hf_endpoint` before running setup (or into `data\settings.yaml` and run `.\llmwiki models --download` afterwards).

## 3. Install

```
cd C:\llmwiki
.\setup.bat --laptop
```
Creates `.venv`, installs the packages, downloads both embedding models, creates `data\settings.yaml` (from
`tools\settings.laptop.yaml`) and `data\company_adapter.py`, migrates the database and runs `doctor --offline`.
Everything under `data\` stays on this laptop and is never committed.

## 4. Start the agent (optional, recommended)

Open Claude Code / Codex / Gemini CLI **in `C:\llmwiki`** and say: *"Read AGENTS.md and set up this machine. My old
project is in `<path>`."* It works through steps 6–9, asks for what is missing, and reports what it did. Passwords and
API keys you store yourself (step 5) — never paste them into the chat.

## 5. Secrets: API keys and passwords (Windows Credential Manager — never in files or environment variables)

For each secret, run this **yourself** (typing is hidden; it is stored for your Windows user as `llmwiki/<name>` in the
Windows Credential Manager):
```
.\llmwiki secret set api-b        # key of the 3 requests/min API
.\llmwiki secret set api-a        # key of the 180 s API
.\llmwiki secret set pop3-ct      # one per department mailbox, only if this laptop reads them over POP3
.\llmwiki secret list             # what the settings need, and what is still MISSING (values are never shown)
```
The names must match the settings (`api_key_secret`, `password_secret`). On the server, run the same commands as the
Windows account that runs the portal. A machine without the Credential Manager: `--file` stores them in
`data\secrets.yaml` instead (never committed).

## 6. Connect the two APIs

Each API is EITHER described in `data\settings.yaml` (`kind: http_json`, no code — best for a plain HTTP API) OR
called by Python code in `data\company_adapter.py` (`kind: custom` — for an SDK, a LangChain client, or the old
project's client). The template has API B as `custom` and API A as `http_json`.

1. **Let the app find the request shape** (sends only a tiny test prompt):
   ```
   .\llmwiki api probe --url https://<api host>/<base path> --secret api-a --proxy none
   ```
   It tries OpenAI-style chat, Azure OpenAI, LangServe (`/invoke`, common for LangChain backends), plain JSON and Ollama
   and prints the endpoint entry to copy into `data\settings.yaml`. Add `--model <name>` if the API needs one, and
   `--auth header --auth-header api-key` if the key goes in its own header instead of `Authorization: Bearer`.
2. **Or use code**: put the old project's call code into `call()` in `data\company_adapter.py` (variants for
   LangChain and for reusing the old client file are at the bottom of that file; keys via `secret("api-a")`).
3. **Test until it says OK**:
   ```
   .\llmwiki api test api-a              # request (key masked), HTTP status, response, where the answer was found, hints
   .\llmwiki api test api-a --schema     # does it also take a JSON schema? then structured_output: true
   ```
   Typical fixes, printed with the error: 401 → key or auth style · 404 → URL/path · 422 → body shape ·
   certificate error → `options.ca_bundle` · proxy error → `options.proxy: none` · answer not found → `options.response_path`.
4. API B the same way (`api test api-b`). With `custom`, `raise Blocked(...)` when API B refuses the input
   (then the request moves to API A at once and shows in Agent › Pool › Refusals).

## 7. Settings — `data\settings.yaml`

Fill in every `<-- fill in`: `sources.dept_addresses` (all department addresses, incl. the AI one),
`sources.internal_domains`, `sources.pst_path` (your `.pst`, or `default` = your Outlook mailbox),
`sources.dept_aliases` (Outlook display names of the department addresses, if any), `departments.names`,
`departments.ai_address`, `departments.team_members`, and the endpoints' `rpm` / `timeout_s` / `context_tokens` if
they differ. POP3 instead of the `.pst`: `sources.pop3_enabled: true` and one `pop3_accounts` entry per mailbox
(see `tools\settings.server.yaml`). Then: `.\llmwiki config check`.

Further settings from the old project (automated sender globs, AI keywords, reference-number patterns, tag lists):
`.\llmwiki config set <group.key> <value>` or a small YAML file with `.\llmwiki config apply <file>` — see `AGENTS.md`.

## 8. Skip rules from the old project

Write them into `data\rules.yaml` (format at the top of `llmwiki\management\commands\rules.py`):
```yaml
- sender: "workflow@sap.your-company.com"
  subject: "^PO \\d+ waiting for approval"
  action: skip          # or note_only
  why: "SAP approval notifications"
```
`.\llmwiki rules import data\rules.yaml --dry-run`, then without `--dry-run`. They appear in Agent › Rules and in
Emails › Admin mode › Rules, where you can also make and edit them like Outlook rules, with a live count of what they do.
Out-of-office, 자동 회신, bounces and no-reply senders are skipped without rules; people's replies to automated mail
are processed unless a rule says `include_replies: true`.

Imported rules only look at new mail. If you already imported mail, also run them on it: `.\llmwiki rules apply --dry-run`
shows the counts, `.\llmwiki rules apply` does it.

## 9. Check everything

```
.\doctor.bat --ping
```
Every line OK: packages, models, the two APIs answer (one tiny request each), the CLIs for the lint, Outlook/.pst,
POP3 logins, privacy summary. Fix what is red (the line says what) and run it again.

## 10. First mail, small first

1. Dry run of the department filter: `.\llmwiki pst_scan --days 7` (department vs personal messages per folder).
2. Import one week: `.\llmwiki fetch --pst --since <date a week ago>` (or `.\llmwiki fetch --pop3`).
3. Process one day: `.\llmwiki process --mode batch --since <day> --until <day>`.
4. `.\run.bat --open` → read a few notes and pages; Agent › Pool (requests, refusals), Agent › Logs (warnings).
   Adjust skip rules / settings, then process the rest of the week. If an API keeps failing, the run stops by itself
   after 10 failed requests in a row and a red banner says why: fix it with `.\llmwiki api test <name>`, then process again.
5. The backlog: `.\backfill.bat <first day> <last day>` (imports and processes in week windows; at 3 requests/min a
   month takes 6–8 hours — keep the laptop plugged in and awake). Close the portal (`run.bat`) first, or run the same
   from the portal: Agent › Logs › .pst import → 1. Import mail → 2. Process as backfill. Agent › Pool shows the ETA.
6. Lint with Gemini/Claude/Codex (logged in once: `gemini`, `claude`, `codex`): Agent › Logs › **Run the reviewer**.

## 11. Later

- **To the server:** `.\tools\package.bat --with-data` → one zip with code + processed data (company mail: keep it
  inside the company network) → `install.bat` on the server. See `docs\OPERATIONS.md` §2–3.
- **Code changes between machines:** `.\tools\patch.bat --note "…"` → unzip into the other install →
  `.\apply_patch.bat` in the unzipped folder (data and settings stay). See `docs\OPERATIONS.md` §2b.
- **Changes from GitHub** on the laptop: download the new ZIP and unzip it **over** `C:\llmwiki` (data\ stays), or
  `git pull`; then `.venv\Scripts\python manage.py migrate`.
