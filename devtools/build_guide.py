"""Build the user guide (features + test results) as one HTML page: python -m devtools.build_guide <facts.json> <out.html>

facts.json is assembled by devtools/collect_facts.py from the test stores and eval results."""
from __future__ import annotations

import base64
import html
import io
import json
import sys
from pathlib import Path


def esc(x) -> str:
    return html.escape(str(x))


def img(path: str, alt: str) -> str:
    from PIL import Image
    p = Path(path)
    if not p.exists():
        return ""
    im = Image.open(p).convert("RGB")
    w = 1100
    if im.width > w:
        im = im.resize((w, int(im.height * w / im.width)))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=72, optimize=True)
    return f'<figure class="shot"><img src="data:image/jpeg;base64,{base64.b64encode(b.getvalue()).decode()}" alt="{esc(alt)}" loading="lazy"><figcaption>{esc(alt)}</figcaption></figure>'


def table(rows: list[list], head: list[str], num_cols: set[int] = frozenset(), best: dict | None = None) -> str:
    out = ["<div class='tw'><table><thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr></thead><tbody>"]
    for r in rows:
        cls = " class='hl'" if best and r[0] == best.get("name") else ""
        out.append(f"<tr{cls}>" + "".join(f"<td{' class=n' if i in num_cols else ''}>{c if isinstance(c, str) and c.startswith('<') else esc(c)}</td>" for i, c in enumerate(r)) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def chat_example(c: dict | None) -> str:
    if not c:
        return ""
    body = "".join(f"<p>{esc(line)}</p>" for line in c["answer"].split("\n") if line.strip())
    return (f"<div class='example'><p class='eyebrow'>Example from the chat (fast mode, a question with a wrong premise)</p>"
            f"<p class='q'>{esc(c['question'])}</p><div class='a'>{body}</div><p class='small'>{esc(c['meta'])}</p></div>")


def h5(r: dict | None) -> str:
    if not r:
        return ""
    return ("<h3>Departments, AI flag, automated mail and reports (Haiku, full run on the extended mailbox)</h3>"
            "<p>The mailbox was extended with an AI department, two AI storylines (one mailed only to Control Tower) and booking confirmations "
            "from an ordinary-looking address, then processed from scratch with the new prompts and script checks.</p>"
            + table(r["run"], ["Check", "Result"]) + "<p>The reports written from it:</p>" + table(r["reports"], ["Report", "Result"]))


def pages_section(r: dict | None) -> str:
    if not r:
        return ""
    out = ("<h3>Shorter wiki pages (summary + the editor's own sections, length budget, repetition check)</h3>"
           "<p>The pages of the run above were about twice as long as the mail behind them: the same facts in every template section, "
           "contradictions between sections, and sentences about the sources. Two measurements with the new page editor:</p>"
           + table(r["rewrite"], ["Existing pages rewritten once (manage.py rewrite_pages --all)", "Result"]))
    if r.get("fresh"):
        out += table(r["fresh"], ["Fresh run of the whole mailbox with the new editor (incremental, week by week)", "Result"])
    if r.get("example"):
        e = r["example"]
        out += (f"<div class='example'><p class='eyebrow'>Example: {esc(e['title'])} — before {e['before']} words, after {e['after']} "
                f"(the mail behind it: {e['source']} words)</p><div class='a'>{e['html']}</div></div>")
    return out


def recheck(r: dict | None) -> str:
    if not r:
        return ""
    return ("<h3>Re-checked after the privacy, CLI and threading changes</h3>"
            "<p>The same 371 synthetic files ingested again with the new skip and thread rules (no model needed):</p>"
            + table(r["ingest"], ["Check", "Result"]) +
            "<p>Moving the processed data to another machine (<code>export_data</code> → <code>import_data</code>):</p>"
            + table(r["transfer"], ["Step", "Result"]))


def build(f: dict) -> str:
    ret = f["retrieval"]
    best = max(ret, key=lambda r: (r["recall_1"], r["mrr"]))
    bm25 = next(r for r in ret if r["name"].startswith("BM25"))
    ing = f["ingest"]
    run = f["run"]
    ans_rows = [[a["name"], a["n"], a["correct"], a["faithful"], a["source_precision"], "—" if a["abstention"] is None else a["abstention"], a["seconds"]] for a in f.get("answers", [])]
    shots = "".join(img(p, alt) for p, alt in f.get("screenshots", []))
    css = (Path(__file__).parent / "guide.css").read_text(encoding="utf-8")
    where_table = table([["Code + docs", r"C:\Users\andre\Documents\llm-wiki (README.md, docs\)"], ["Offline zip (built here)", f["zip_path"]],
                         ["Synthetic dataset", r"testdata\ (eml, ko, raw, truth.json, golden.json)"], ["Eval results", r"eval\results\*.json (also Agent › Eval)"],
                         ["Test stores (Haiku runs)", r"data_h7\ (latest: fresh run with the new page editor — the portal runs on it), data_h6\ (data_h5 with every page rewritten), data_h5\ (4 departments, AI storylines, old templates), data_h4\ (first full run), data_ko\ (Korean slice); all git-ignored"],
                         ["Code patches", r"dist\llmwiki-patch-*.zip (tools\patch.bat); applied installs keep MANIFEST.json and a backups folder"],
                         ["Screenshots", r"eval\screens\*.png"],
                         ["Decisions", r"LLM Wiki\LLM Wiki - decisions.md (copy in docs\DECISIONS.md)"]], ["What", "Where"])
    return f"""<title>LLM Wiki Guide</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{css}</style>
<div class="page">
<header class="top">
  <p class="eyebrow">Build report · {esc(f['date'])}</p>
  <h1>LLM Wiki: what's built, how to run it, and how it tested</h1>
  <p class="lede">The agentic RAG knowledge base over the department mailbox is built and tested end to end on the synthetic dataset with Haiku as the stand-in model.
  This page has the steps for tonight (work laptop at home), tomorrow (office) and the server, every feature, and the test results.</p>
  <div class="status">
    <span class="ok">{esc(f['pytest'])} automated tests pass</span>
    <span class="ok">0 lost emails · thread purity {ing['thread_purity']}</span>
    <span class="ok">{run['emails']} emails processed, {run['unit_errors']} unit errors</span>
    <span class="ok">recall@1 {best['recall_1']} (BM25 alone {bm25['recall_1']})</span>
    <span class="ok">offline package self-tested</span>
  </div>
</header>

<nav class="toc"><a href="#start">Start here</a><a href="#features">Features</a><a href="#tests">Test results</a><a href="#screens">Screens</a><a href="#limits">Limits and next steps</a><a href="#where">Where things are</a></nav>

<section id="start">
<h2>Start here</h2>
<p>Real mail is processed only by the internal APIs. Gemini, Claude Code and Codex lint the wiki (page text, personal data removed) and may process the <i>synthetic</i> mailbox end to end.</p>
<div class="tw"><table><thead><tr><th>Where</th><th>Does</th><th>Models</th><th>Settings</th></tr></thead><tbody>
<tr><td>Work laptop (company network)</td><td>imports the backlog (.pst or the department mailboxes over POP3), processes it, lints the wiki, packages code + data</td><td>API B + API A for mail; Gemini / Claude / Codex only for the lint</td><td><code>tools\\settings.laptop.yaml</code></td></tr>
<tr><td>Server</td><td>POP3 (one login per department mailbox), nightly batch, live mode, the portal for the team</td><td>API B + API A</td><td><code>tools\\settings.server.yaml</code></td></tr>
</tbody></table></div>
<p>The full walkthrough is in the repo: <code>docs\\SETUP_WORK_LAPTOP.md</code>. A coding agent on the laptop (Claude Code / Codex / Gemini CLI, started in the repo folder) can do the configuration by itself from <code>AGENTS.md</code>: it asks for each missing value, never reads real mail, and never handles secrets.</p>

<h3>1. GitHub (this PC)</h3>
<ol class="steps">
  <li>New PowerShell window → <code>git config --global user.name "…"</code> and <code>user.email</code> (GitHub's noreply address). On github.com: <b>New repository</b> <code>llm-wiki</code>, Private, empty.</li>
  <li>In the code folder: <code>git init -b main</code> → <code>git add .</code> → <code>git status</code> (about 670 files, no <code>data*</code>, <code>dist</code>, <code>backups</code>, <code>*.sqlite3</code>, <code>company_adapter.py</code>) → <code>git commit -m "LLM Wiki"</code> → <code>git remote add origin https://github.com/&lt;you&gt;/llm-wiki.git</code> → <code>git push -u origin main</code> (sign in with the browser).</li>
</ol>

<h3>2. Work laptop</h3>
<ol class="steps">
  <li>github.com → Code › <b>Download ZIP</b> → unzip to <code>C:\\llmwiki</code>. Python 3.11, pip, classic Outlook; <code>HF_ENDPOINT</code> if Hugging Face is blocked.</li>
  <li><code>.\\setup.bat --laptop</code> → <code>.venv</code>, models, <code>data\\settings.yaml</code>, <code>data\\company_adapter.py</code>.</li>
  <li>Optional: start Claude Code / Codex / Gemini in <code>C:\\llmwiki</code>: <i>"Read AGENTS.md and set up this machine. My old project is in …"</i>.</li>
  <li><b>Secrets</b> with <code>setx</code> (API keys, POP3 passwords), then a new window. <b>API code</b> into <code>data\\company_adapter.py</code> (raise <code>Blocked</code> when API B refuses input). <b>Settings</b>: the <code>&lt;-- fill in</code> lines of <code>data\\settings.yaml</code>. <b>Skip rules</b>: <code>data\\rules.yaml</code> → <code>.\\llmwiki rules import data\\rules.yaml</code>.</li>
  <li><code>.\\doctor.bat --ping</code> → every line OK. Then small first: <code>.\\llmwiki pst_scan --days 7</code>, import one week, process one day, look at it in the portal; then the backlog (<code>.\\backfill.bat &lt;since&gt; &lt;until&gt;</code>, 6–8 h per month at 3 rpm) and the reviewer.</li>
  <li><b>Package code + data:</b> <code>.\\tools\\package.bat --with-data</code> → <code>dist\\llmwiki-offline-…-WITH-DATA.zip</code>. It holds company mail: copy it only inside the company network.</li>
</ol>
<h3>3. On the server</h3>
<ol class="steps">
  <li>Unzip, run <code>install.bat</code>: offline install, then it imports the processed data (vector index rebuilt). The server's own <code>data\\settings.yaml</code> keeps the server's endpoints: the laptop's Gemini/Claude/Codex never arrive there.</li>
  <li>Edit <code>data\\settings.yaml</code> (<code>pop3_accounts</code>: one entry per department mailbox, department addresses); API keys and one POP3 password per mailbox as environment variables; <code>.venv\\Scripts\\python manage.py createsuperuser</code>; <code>doctor.bat --ping</code>.</li>
  <li><code>run.bat</code> → <code>http://&lt;server&gt;:8000</code>. The first POP3 fetch recognises mail already imported from the .pst by its Message-ID, so only new mail is processed. Then Agent › Settings › Schedule → <b>Live mode on</b>; <code>tools\\install_service.bat</code> starts it at boot.</li>
  <li>New data from the laptop later: <code>llmwiki export_data x.zip</code> there, <code>llmwiki import_data x.zip --force</code> here (server stopped; the old DB is kept in <code>data\\backups</code>).</li>
  <li><b>Code changes later</b> (made with Claude here or another agent on the laptop): <code>tools\\patch.bat --note "…"</code> on that machine → copy <code>dist\\llmwiki-patch-….zip</code>, unzip it into the server's llmwiki folder → <code>apply_patch.bat --dry-run</code>, then <code>apply_patch.bat</code>. Data and settings stay; a file also changed on the server stops it with a CONFLICT line.</li>
</ol>
</section>

<section id="features">
<h2>Features</h2>
<div class="grid">
<div class="feat"><h3>Mail in, one copy each</h3><ul>
<li>POP3 (seen-list, never deletes), the Outlook <b>.pst</b> or your Outlook mailbox itself (Outlook COM with raw headers, or pypff; one message at a time, resumable), .eml folders</li>
<li><b>Personal mailbox filter:</b> only .pst mail with a department address in To/Cc is imported (Exchange recipients resolved to SMTP, display-name aliases); everything else is never stored. <code>pst_scan</code> counts before importing</li>
<li>Exactly one stored email per message: merges the same Message-ID from POP3 and .pst or two aliases; detects resends under a new ID</li>
<li>Threads by Message-ID / References / Outlook Thread-Index / ConversationID (from MAPI properties when transport headers are missing), subject fallback only for replies with a shared participant, conflicting joins refused</li>
<li><b>Separate replies to one email</b> end up in one thread when their quoted history shows it, even with the headers lost</li>
<li>New text vs quoted history (English and <b>Korean</b> Outlook headers, 회신/전달 prefixes; notes and pages stay in English; the Korean parts are for the work version only), signatures and disclaimers cut, quoted mail that was never received (late CC) sent to the model <b>once</b></li>
<li><b>HTML mail without stray blank lines:</b> Outlook paragraphs become lines, HTML source line breaks spaces, at most one blank line; <code>reparse_bodies</code> cleans mail stored earlier</li>
<li>Everything in SQLite: raw mail (compressed), attachments (deduplicated), xlsx/pdf/docx/pptx extracts</li>
<li>Skip rules: headers and <b>automatic replies</b> (out-of-office, 자동 회신, bounces, no-reply senders) always skipped; static rules and <b>learned rules</b> for automated mail, which <b>don't skip people's replies or forwards</b> to it (feedback is processed) unless the rule says "also replies"</li></ul></div>
<div class="feat"><h3>Pipeline that keeps the wiki up to date</h3><ul>
<li>S1 notes (one per email, importance, kind, due date) → S2 routing to pages chosen from <b>RAG candidates</b> → S3 editor → S4 tags</li>
<li><b>Fast path</b>: one request for single emails (S12 batch, S123 live with direct section edits); falls back to the stages</li>
<li>Every stage gets the full emails; the model only writes labels it was given (email numbers, P1…Pk)</li>
<li><b>Pages that are faster to read than the mail:</b> a short summary + at most 4 sections the editor names itself, the whole page rewritten in one request so each fact is said once. A <b>length budget</b> from the mail behind the page (0.5 × the words people wrote, 120–650 words), section limits and a <b>repetition check</b> are enforced by script (confirm round to shorten); sentences about the sources ("the sources do not show…") are removed. All in Settings › Wiki pages; <code>rewrite_pages</code> re-applies them</li>
<li>Editor <b>guards</b>: empty or collapsed text rejected, a confirm round with ORIGINAL + DRAFT + the checker's notes, append-only history, rollback</li>
<li>Sources recorded by the script (no model-typed citations); optional [n] markers become clickable source chips</li>
<li><b>Script checks around the model:</b> automated mail (system senders, repeated templates) rated "routine" becomes "not important" and never creates a page; a page shows as a topic only with ≥ 3 human emails / 2 days / 2 people, as a project with ≥ 6 / 3 / 3 (the model's proposal is the ceiling; text moves along when the template changes)</li>
<li>Prompts with explicit definitions: importance (system confirmations are not important), project vs topic vs task, page status</li>
<li>Reviewer: contradictions vs similar sections on other pages, duplicate pages, stale dates</li>
<li>Batch (18:00, safety net), backfill (week windows) and <b>live mode</b> (5-min ticks, switches itself off after 3 failures)</li></ul></div>
<div class="feat"><h3>RAG and the chat agent</h3><ul>
<li>Hybrid retrieval: SQLite FTS5 BM25 (+ trigram for Hangul) and Qdrant vectors, <b>weighted reciprocal rank fusion</b> (wiki first), recency boost</li>
<li>Local embeddings: multilingual-e5-small (work default, Korean), bge-small-en-v1.5; hashed fallback without model files</li>
<li>Chat agent in <b>LangGraph</b>: pre-retrieval → tool loop (search wiki/notes/emails, read page, get email/thread, look up PO/delivery numbers, list pages) → answer with numbered sources</li>
<li><b>Deep mode</b>: planner → parallel researchers → writer (multi-agent, LangGraph Send)</li>
<li>The chat sees what's open in the portal; answers cite sources that open in a side drawer</li>
<li>Every library choice has a one-line fallback: vector store sqlite, embed model hashed, chat engine plain, deep off, mode bm25</li></ul></div>
<div class="feat"><h3>Model access</h3><ul>
<li>Endpoint pool: per-endpoint rpm (1–60), concurrency, timeout, context cap, allowed stages; API A and B together</li>
<li>Endpoints: your internal APIs (<code>company_adapter.py</code>), OpenAI-compatible or HTTP APIs, and the <b>Claude Code, Gemini CLI and Codex</b> CLIs (read-only, text mode)</li>
<li><b>Failover:</b> when API B blocks the input or fails, the same request goes to API A at once (<code>fallback_stages</code>); privacy rules still apply</li>
<li><b>Refusals log:</b> every blocked request with its reason and what happened next (Agent › Pool › Refusals, per day and API)</li>
<li><b>Structured output:</b> the stage's JSON schema goes to APIs that take one (API A via LangChain, OpenAI-compatible, Codex); dict answers accepted, the same parsers check them</li>
<li><b>Chat first</b>: background work pauses while anyone chats and resumes after a short idle time; optional reserved rpm for chat (<code>chat_reserve_rpm</code>, default 0)</li>
<li>429 → Retry-After/backoff, rpm lowered after repeated rejections; timeouts retried with smaller budgets; at most 1 validation retry</li>
<li>Budget presets 70k / 105k / 150k with per-section budgets and a cut order</li></ul></div>
<div class="feat"><h3>Privacy</h3><ul>
<li><b>External endpoints</b> (Gemini, Claude Code, Codex) only get the wiki lint: page text, never emails. Email stages and the chat stay on the internal APIs</li>
<li><b>Personal data removed by script</b>: phone numbers (international, labelled, NL/KR), e-mail addresses, IBANs, Korean ID numbers, street addresses (EU/KR) → <code>[phone]</code>, <code>[email]</code>…; PO/delivery/invoice numbers are protected first</li>
<li>Applied to every external prompt, to both internal APIs' prompts (API B blocks less; API A doesn't need it, but one rule is simpler), and to <b>everything the model writes</b>: notes, pages, reports and chat answers carry no PII. Reference numbers (PO, delivery, invoice…) are never removed. The stored emails keep the original</li>
<li>Each request shows how many items were removed; <code>doctor</code> prints the privacy summary</li>
<li>The synthetic dataset may be processed end to end by an external model (<code>test_data_only</code>), never real mail</li></ul></div>
<div class="feat"><h3>Portal (Django + React)</h3><ul>
<li><b>Reports, one per department</b> (tabs): every topic in exactly one report; only topics a team member wrote about in the window. <b>Daily / Weekly / Monthly</b>: a "past 7 days" report every morning at 07:00, the Thursday one is the weekly report (CFO), on the 1st the monthly one (calendar month). The agent sees only the window's facts. Mail-per-day strip (a month calendar for monthly) + coloured stat cards, 2-column story cards with the newest decision, then decisions, deadlines, waiting, needs attention (each at most one story card tall), and a script-only <b>topic week</b> grid (days × hours; topics × days for a month)</li>
<li><b>Every report part is a setting</b> (Settings › Report parts): story text, decision, key emails, tags, status, level, people, day cells, open issues, deadline, and each section; switched-off agent texts aren't requested</li>
<li><b>AI department</b>: the note-taker flags AI-related mail by meaning (keywords are examples); pages with ≥ 30 % AI mail belong to the AI report whatever address they were sent to; a person can move a page</li>
<li><b>Emails</b>: list / threads / week grid, sender checkboxes, importance, notes as preview, attachments and inline images</li>
<li><b>Wiki</b>: cards or list with tag pills (2 rows max), filters; pages with sources per section, open issues, timeline, history and rollback</li>
<li><b>Agent</b>: logs, pipeline inspector (every prompt section with budgets, raw answers, guard results), pool with ETA, rules, tags, <b>settings generated from the Pydantic model</b>, eval</li>
<li>Side drawer for any email (with the earlier emails of its thread) or page; floating chat</li>
<li>Threads: separate replies to one email are drawn as <b>parallel branches</b> in their own lane ("parallel reply 2 of 2 to …"), with a placeholder for an original that never reached the department</li>
<li>Local mode (no login) or server mode (Django login, admin at /admin)</li></ul></div>
<div class="feat"><h3>Operations</h3><ul>
<li>One process (Waitress + scheduler threads) on Windows Server 2016 / Python 3.11</li>
<li><code>package.bat</code> builds the offline zip on the company laptop, <b><code>--with-data</code></b> adds the processed data; <code>install.bat</code> installs and imports it; <code>export_data</code> / <code>import_data</code> for later moves</li>
<li>Per-machine settings: <code>settings.yaml</code> wins for endpoints, paths and server, so a copied database never brings the laptop's endpoints along</li>
<li><b>Code patches</b> between machines: <code>tools\\patch.bat</code> builds a code-only zip; its <code>apply_patch.bat</code> keeps data and settings, detects files changed on that machine (conflict → stop), backs up code and database, migrates, restarts; <code>rollback</code> undoes it. The version shows in the header and in <code>doctor</code></li>
<li><code>doctor</code> checks Python, packages, the VC++ runtime for onnxruntime, models, POP3, the .pst, Outlook COM, and pings each endpoint</li>
<li>Logs: rotating files, user-visible events, every LLM call stored; nightly DB backups (14 days)</li>
<li>README + ARCHITECTURE / OPERATIONS / DEVELOPMENT / CONFIG docs for whoever maintains it next</li></ul></div>
</div>
</section>

<section id="tests">
<h2>Test results</h2>
<p>The synthetic dataset: a fictional European logistics department, {esc(f['dataset']['storylines'])} storylines written by Sonnet, {esc(f['dataset']['files'])} .eml files over 6 weeks ({esc(f['dataset']['story_emails'])} storyline emails + {esc(f['dataset']['noise'])} automated mails), with tables, screenshots, xlsx/pdf attachments, a late CC, alias copies, a resend, forwards and replies without reply headers. Ground truth and {esc(f['dataset']['golden'])} golden questions come with it. The stand-in model is <b>Claude Haiku in text mode</b> (the same JSON-from-text path as the work APIs).</p>

<h3>Automated tests</h3>
<p><b>{esc(f['pytest'])} pass</b> (offline, mock model): quote splitting incl. Korean, HTML tables, identifiers and t-codes, lenient JSON, budgets, guards, output fixers, the pool under fault injection (50 % rate limits + 30 % broken JSON), chat priority, the whole pipeline on the dataset, all four chat engine/mode combinations, the Briefing, every API endpoint, settings, scheduler auto-off, login in server mode, and the .pst import against a fake Outlook object model. Added today: personal-data removal (reference numbers kept), external endpoints refused for email stages, failover from a blocking API to the fallback API, the schema reaching <code>company_adapter.call(schema=…)</code> with a dict answer and cleaned output, the Gemini and Codex adapters against stub executables, machine keys vs a copied database, skip rules vs replies and automatic replies, threads joined by shared quoted history, and the .pst department filter with Exchange addresses and MAPI message ids. Added in the last round: the page guards (budget, repeated sentences, talk about the sources, an unexplained collapse), the whole-page editor and <code>rewrite_pages</code> on the dataset, Outlook HTML whitespace (source wrapping, <code>&amp;nbsp;</code> paragraphs, <code>&lt;br&gt;</code> chains, <code>o:p</code> tags), daily/weekly/monthly periods, switched-off report parts, and a code patch built → applied (local change kept, data untouched) → conflict → rollback.</p>

<h3>Mail store vs ground truth</h3>
{table([["Emails expected / stored / lost", f"{ing['true_emails']} / {ing['stored']} / {ing['missing']}"], ["Alias copies merged", ing['alias_copies_merged']], ["Resent copy marked duplicate", ing['resent_marked_duplicate']],
        ["Thread purity (no unrelated mail joined)", ing['thread_purity']], ["Thread completeness", ing['thread_completeness']], ["Quoted-only emails (late CC) kept as unseen history", ing['quoted_only_in_truth']]], ["Check", "Result"], {1})}
<p class="small">The two "split" threads are partner emails without reply headers or RE: prefix; Outlook treats those as new threads too.</p>

<h3>First full pipeline run (Haiku, 6 weeks of mail, 25 storylines — before the department changes)</h3>
{table([["Emails processed", run['emails']], ["Units (thread-days)", f"{run['units']} ({run['fast']} via the fast path, {run['staged']} staged)"], ["Unit errors", run['unit_errors']],
        ["Time", f"{round(run['seconds'] / 60)} min at 6 parallel requests"], ["Pages", f"{ing.get('pages', '?')} for {ing['storylines']} storylines"], ["Routing accuracy", ing['routing_accuracy']],
        ["Page purity (share of a page's mail from one storyline)", ing.get('page_purity', '?')], ["Notes per processed email", ing['notes_per_processed_email']],
        ["Facts written into pages", ing['items_written']], ["Drafts rejected by the guards or replaced in a confirm round", ing['drafts_rejected_or_replaced']],
        ["Units that needed a validation retry", ing['units_retried_share']], ["Learned skip rules created", run.get('learned_rules', '?')]], ["Metric", "Value"], {1})}

<h3>Retrieval: how the parameters were chosen</h3>
<p>Golden questions with known source emails; a hit is a gold email or a wiki section of a page that cites one. The highlighted row is the configuration in use.</p>
{table([[r['name'], r['recall_1'], r['recall_5'], r['recall_10'], r['mrr'], r['ndcg_10'], r['ms']] for r in ret], ["Configuration", "recall@1", "recall@5", "recall@10", "MRR", "nDCG@10", "ms/query"], {1, 2, 3, 4, 5, 6}, best)}
<p class="small"><b>Reading it:</b> BM25 and vectors alone find the right source in the top 5 most of the time but rarely in first place; fusing them helps, and weighting the wiki first moves the answer to rank 1 (recall@1 {bm25['recall_1']} → {best['recall_1']}). The three embedding models are close on this English dataset; multilingual-e5-small stays the work default because a lot of the real mail is Korean.</p>

<h3>Chat answers (LLM judge)</h3>
{table(ans_rows, ["Engine · mode", "answerable questions", "correct", "faithful", "source precision", "abstains when not in the wiki", "s/answer"], {1, 2, 3, 4, 5, 6}) if ans_rows else "<p>Pending.</p>"}
<p class="small">Judged by Haiku against the golden answers. "Faithful" = every claim supported by the cited sources. "Abstains" = questions whose answer is not in the mail; the agent should say so instead of guessing. Deep mode (planner → parallel researchers → writer) is about 3–4× slower than fast mode; on these mostly single-topic questions it is as correct, so fast stays the default and deep is a toggle in the chat.</p>
{chat_example(f.get('chat_example'))}

{h5(f.get('h5'))}

{pages_section(f.get('pages'))}

{recheck(f.get('recheck'))}

<h3>Korean mail</h3>
{table([[k, v] for k, v in f['korean'].items()], ["Check", "Result"], {1})}

<h3>Other checks</h3>
<ul>
<li><b>Offline package</b>: wheels for win_amd64 / Python 3.11.0, models, code → a fresh venv installed with <code>--no-index</code>, migrated and passed <code>manage.py check</code>.</li>
<li><b>Live mode replay</b>: mail dripped in tick by tick; single emails took the one-request S123 path with direct section edits, multi-email deltas the staged path, 0 errors.</li>
<li><b>Found and fixed during testing</b>: onnxruntime ≥ 1.19 crashes on import with an older VC++ runtime (pinned 1.18.1; doctor checks it); some newest packages need Python ≥ 3.11.5 (lock file now resolved for 3.11.0); the new-page rule over-merged topics because e5 cosines between logistics topics are high (run 1: 16 pages for 25 storylines; now the model's judgement wins for candidates it saw, and a page created in parallel is reused only with cosine ≥ 0.88 <i>and</i> a title overlap ≥ 0.5: run 3 gave 24 pages, page purity 0.98); the model assumed "today" was the processing date (now an "as of" date in every prompt); department mailboxes were listed as people (prompt rule); the eval judge waited behind background work (chat priority now covers the judge).</li>
</ul>
</section>

<section id="screens">
<h2>Screens (Haiku-processed synthetic data)</h2>
{shots or "<p>Screenshots pending.</p>"}
</section>

<section id="limits">
<h2>Limits and next steps</h2>
<ul>
<li><b>Gemini CLI and Codex were not run against the real services</b> (they aren't on this PC). Their adapters are tested with stub executables, and the flags were checked against the installed CLIs' <code>--help</code> (Gemini 0.66 accepted every flag up to its login check). First thing on the work laptop: <code>doctor.bat --ping</code>, or the Gemini test run on the synthetic mailbox.</li>
<li><b>Codex with structured output</b> needs a strict JSON schema (every field required); the stage schemas are permissive, so the laptop template leaves <code>structured_output</code> off for Codex. API A's schema support depends on your LangChain backend: if it rejects a schema, set <code>structured_output: false</code>.</li>
<li><b>Personal-data patterns are regexes:</b> they catch the usual phone, e-mail, IBAN, ID and address formats, not names or free-form addresses. Check a few notes after the first real day; add patterns in Agent › Settings › Privacy.</li>
<li><b>Haiku is stronger than the work model.</b> Expect more retries and simpler pages at work. If the JSON is often unusable: <code>pipeline.combine: off</code> (smaller tasks) or a larger preset; check Agent › Logs "units retried".</li>
<li><b>Speed at 3 requests/min</b>: a month of backfill is a long run. Start it early, use API A in parallel, and raise rpm when the 60/min upgrade comes (Settings › Endpoints).</li>
<li><b>The .pst import via Outlook COM</b> is tested against a fake Outlook object model (date window, department filter, Exchange addresses, MAPI ids, threading, attachments) but not against your real mailbox. Run <code>pst_scan --days 7</code> and import one week first. If COM fails, set <code>sources.pst_route: pypff</code> (needs the libpff-python wheel; it only sees transport headers, so internal mail may be missed) or drag the needed months into a folder of .eml files (<code>sources.eml_folder</code>).</li>
<li><b>Qdrant local mode</b> allows one process: use the portal's actions while the server runs (the CLI falls back to SQLite vectors and the server re-syncs every 10 min).</li>
<li>P2 items not done or off by default: reranker (off until the eval shows a gain), scheduled reviewer (manual by default), tag lifecycle automation, a public static demo.</li>
</ul>
</section>

<section id="where">
<h2>Where things are</h2>
{where_table}
</section>
<footer class="foot">Built {esc(f['date'])} · Django 5.2 + Ninja · React 18 · SQLite FTS5 · Qdrant · onnxruntime · LangGraph · Waitress</footer>
</div>"""


if __name__ == "__main__":
    facts = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    Path(sys.argv[2]).write_text(build(facts), encoding="utf-8")
    print("wrote", sys.argv[2])
