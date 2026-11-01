"""All application settings, as one Pydantic model.

Rules:
- Every tunable value used anywhere in the code is a field here (tests/test_settings.py checks the groups).
- The portal's Settings page is generated from `Settings.model_json_schema()`, so a new field appears in the UI
  automatically. Use `Field(description=...)`, bounds (`ge`/`le`) and `json_schema_extra={"tier": "P1"}` where useful.
- Effective settings = defaults <- data/settings.yaml (optional, for bootstrapping a new machine) <- the latest
  row of `SettingsVersion` in the DB (what the portal saves) <- the MACHINE_KEYS that settings.yaml sets (endpoints,
  .pst/.eml paths, server, test_data_only: they belong to the machine, not to the data). API keys are never stored:
  only env var names.

Read settings with `get_settings()` (cached for a few seconds, cheap to call anywhere).
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


# ------------------------------------------------------------------ groups
class Pop3Account(_M):
    """One department mailbox read over POP3. The password is read from an environment variable."""
    dept: str = Field("", description="The department address this mailbox belongs to (one of sources.dept_addresses); its mail counts for "
                                      "that department even when the address is not in To/Cc")
    host: str = Field("", description="POP3 server host name")
    port: int = Field(995, ge=1, le=65535, description="995 for POP3 over SSL, 110 for plain")
    ssl: bool = Field(True, description="POP3 over SSL")
    user: str = Field("", description="Login of the mailbox")
    password_env: str = Field("LLMWIKI_POP3_PASSWORD", description="Environment variable with this mailbox's password (one per mailbox, e.g. "
                                                                   "LLMWIKI_POP3_PW_CT)")
    enabled: bool = True


class Sources(_M):
    """Where mail comes from. Passwords are read from environment variables."""
    pop3_enabled: bool = Field(False, description="Fetch from the department mailboxes over POP3")
    pop3_accounts: list[Pop3Account] = Field(default_factory=list, description="The department mailboxes (one entry per shared mailbox, each with "
                                                                               "its own login). Empty = the single pop3_host/pop3_user fields below")
    pop3_host: str = Field("", description="Single mailbox (older setting): POP3 server host name")
    pop3_port: int = Field(995, ge=1, le=65535, description="Single mailbox: 995 for POP3 over SSL, 110 for plain")
    pop3_ssl: bool = Field(True, description="Single mailbox: use POP3 over SSL")
    pop3_user: str = Field("", description="Single mailbox: POP3 user name")
    pop3_password_env: str = Field("LLMWIKI_POP3_PASSWORD", description="Single mailbox: environment variable that holds the POP3 password")
    pop3_max_per_fetch: int = Field(5000, ge=1, le=100000, description="Safety limit per fetch")
    eml_folder: str = Field("", description="Folder of .eml files to import on every fetch (tests, manual drops)")
    pst_path: str = Field("", description="Outlook .pst file for the backfill (e.g. Documents\\Outlook Files\\<mailbox>.pst), or \"default\" "
                                         "for the Outlook profile's own mailbox (Exchange/.ost; Outlook COM only)")
    pst_route: Literal["auto", "com", "pypff"] = Field("auto", description="auto = Outlook COM if classic Outlook is installed, else pypff")
    pst_folders: list[str] = Field(default_factory=list, description="Only these folder paths (empty = all folders)")
    dept_addresses: list[str] = Field(default_factory=lambda: ["dl-ct@corp-europe.example", "dl-cus@corp-europe.example", "dl-fin@corp-europe.example",
                                                               "dl-ai@corp-europe.example"],
                                      description="Department addresses (only ever in To/Cc); used for the department filters")
    dept_aliases: dict[str, list[str]] = Field(default_factory=dict,
                                               description="Other names of a department address, e.g. {\"dl-ct@corp.com\": [\"DL Control Tower\"]}: display names "
                                                           "Outlook shows instead of the address when a .pst message has no transport headers")
    pst_dept_only: bool = Field(True, description="The .pst is a personal mailbox: import ONLY messages with a department address in To/Cc "
                                                  "(everything else is never stored)")
    pst_match_from: bool = Field(False, description="Also import .pst messages whose sender is a department address (e.g. mail a mailbox rule forwarded)")
    internal_domains: list[str] = Field(default_factory=lambda: ["corp-europe.example"], description="Mail domains counted as internal")
    time_zone: str = Field("Europe/Amsterdam", description="Local time zone: the day boundary for thread-days and reports")


class Store(_M):
    subject_fallback_days: int = Field(21, ge=0, le=120, description="Join replies by normalised subject + shared participant within N days when headers are missing (0 = off)")
    use_thread_index: bool = Field(True, description="Use Outlook Thread-Index / ConversationID for threading")
    quote_match_threshold: float = Field(0.8, ge=0.3, le=1.0, description="Share of a quoted block's word shingles that must match a stored email to link it")
    join_shared_history: bool = Field(True, description="Merge two threads when an email quotes a message that belongs to the other thread, or quotes the "
                                                        "same earlier message (separate replies to one email whose headers were lost)")
    shared_history_min_words: int = Field(25, ge=5, le=500, description="A shared quoted block must have at least N words to join threads (no greetings or disclaimers)")
    shared_history_days: int = Field(60, ge=1, le=365, description="Only join threads whose emails are at most N days apart")
    dup_window_min: int = Field(60, ge=0, le=1440, description="Same sender + subject + body under another Message-ID within N minutes → duplicate")
    att_extract_chars: int = Field(6000, ge=0, le=100000, description="Characters of text kept per attachment extract")
    max_attachment_mb: int = Field(40, ge=1, le=500, description="Larger attachments are recorded but not stored")
    backup_keep_days: int = Field(14, ge=0, le=365, description="Nightly copies of the database kept (0 = no backups)")
    identifier_patterns: dict[str, str] = Field(default_factory=lambda: {
        "po": r"\b45\d{8}\b", "delivery": r"\b8\d{7}\b", "invoice": r"\bINV-?\d{4,8}\b",
        "container": r"\b[A-Z]{3}U\d{7}\b", "mrn": r"\b\d{2}[A-Z]{2}[A-Z0-9]{13,14}\b"},
        description="Reference number types (regex). Found by script, searchable, never tags")


class Skip(_M):
    auto_submitted: Literal["skip", "note_only", "process"] = Field("skip", description="Mail with Auto-Submitted ≠ no (out-of-office, robots)")
    bulk: Literal["skip", "note_only", "process"] = Field("skip", description="Mail with Precedence: bulk/list/junk or List-Id (newsletters)")
    auto_reply: Literal["skip", "note_only", "process"] = Field("skip", description="Automatic replies without the header: Out-of-office / Automatic reply / "
                                                                                    "자동 회신 subjects, delivery failures, no-reply senders")
    auto_reply_subjects: list[str] = Field(default_factory=lambda: [
        r"^(automatic reply|auto(matic)?[ -]?reply|autoreply|out of (the )?office|ooo)\b", r"^(undeliverable|undelivered mail|delivery status notification|mail delivery failed|returned mail)\b",
        r"^(read|not read|delivered|gelesen|gelezen):", r"^(자동 회신|자동회신|부재중|부재 중)", r"^(automatisch antwoord|afwezig|abwesenheitsnotiz|automatische antwort)\b"],
        description="Subject regexes (case-insensitive) of automatic replies; checked before the reply rule below")
    noreply_senders: list[str] = Field(default_factory=lambda: ["noreply@*", "no-reply@*", "donotreply@*", "do-not-reply@*", "mailer-daemon@*", "postmaster@*", "autoreply@*"],
                                       description="Sender globs that never write by hand: their mail (also their 'RE:' acknowledgements) is treated as automatic")
    rules_skip_replies: bool = Field(False, description="Off (default): a static/learned rule does NOT skip a reply or forward written by someone else "
                                                        "(feedback on an automated mail is processed). A rule can opt in per rule ('also replies'). "
                                                        "Replies from the rule's own sender stay skipped")
    learned_enabled: bool = Field(True, json_schema_extra={"tier": "P2"}, description="Turn a repeatedly unimportant sender + subject template into a skip rule")
    learn_min: int = Field(5, ge=2, le=100, description="not_important verdicts needed")
    learn_days: int = Field(3, ge=1, le=60, description="…on at least N different days")
    learn_clean_days: int = Field(30, ge=1, le=365, description="…and no important/routine verdict in the last N days")
    audit_every: int = Field(10, ge=2, le=1000, description="Every Nth match is still processed; an important result disables the rule")


class Schedule(_M):
    batch_enabled: bool = Field(True, json_schema_extra={"tier": "P0"}, description="Nightly batch: fetch, process everything not yet processed, then review")
    batch_time: str = Field("18:00", pattern=r"^\d{2}:\d{2}$", description="Local time of the nightly batch")
    live_enabled: bool = Field(False, json_schema_extra={"tier": "P1"}, description="Live mode: check for new mail every few minutes. The nightly batch stays the safety net")
    live_tick_minutes: int = Field(5, ge=1, le=120, description="Live: check for new mail every N minutes (does nothing when there is none)")
    live_lint_minutes: int = Field(90, ge=10, le=1440, description="Live: lint pages changed since the last lint every N minutes")
    live_max_failed_ticks: int = Field(3, ge=1, le=50, description="Live switches itself off after N failed ticks in a row (logged)")
    pause_background_on_chat: bool = Field(True, description="While anyone chats, no new background request starts (the API key is shared)")
    chat_idle_resume_s: int = Field(90, ge=0, le=3600, description="Background work resumes N seconds after the last chat request")
    report_auto: bool = Field(True, description="Write the report texts every morning at reports.daily_time (each department; the weekly one on reports.weekly_weekday)")
    worker_enabled: bool = Field(True, description="Run the scheduler inside the server process")


class Endpoint(_M):
    name: str = Field(..., description="Unique name")
    kind: Literal["custom", "http_text", "openai_compat", "claude_cli", "gemini_cli", "codex_cli", "mock", "chaos"] = Field(
        "custom", description="custom = company_adapter.py in the data folder; claude_cli / gemini_cli / codex_cli = the logged-in CLI on this PC (text mode)")
    model: str = Field("", description="Model name passed to the adapter (e.g. haiku, sonnet, gemini-2.5-pro, gpt-5)")
    external: bool = Field(False, description="Data sent here leaves the company (Gemini/Claude/Codex CLIs, public APIs). External endpoints only get the "
                                              "stages in privacy.external_stages, always with PII removed — unless privacy.test_data_only is on")
    redact_pii: bool = Field(False, description="Remove phone numbers, e-mail addresses, IBANs, ID numbers and street addresses from every prompt sent here "
                                                "(always on for external endpoints). Turn on for an API that blocks input with personal data")
    structured_output: bool = Field(False, description="Pass the stage's JSON schema to the adapter (custom: call(..., schema=...); openai_compat: "
                                                       "response_format; codex_cli: --output-schema). For APIs that accept a schema, e.g. LangChain with_structured_output")
    base_url: str = Field("", description="URL for http_text / openai_compat")
    api_key_env: str = Field("", description="Environment variable with the API key (never the key itself)")
    rpm: int = Field(3, ge=1, le=600, description="Requests per minute allowed (API B: 3 now, maybe 60 later)")
    max_concurrent: int = Field(2, ge=1, le=64, description="Requests in flight at the same time")
    timeout_s: int = Field(0, ge=0, le=3600, description="Request timeout in seconds (0 = none)")
    context_tokens: int = Field(150000, ge=4000, le=2000000, description="Largest prompt this endpoint accepts; bigger jobs are cut to it")
    max_output_tokens: int = Field(8000, ge=256, le=200000, description="Output token limit sent to the API where supported")
    stages: list[str] = Field(default_factory=lambda: ["*"], description="Stages allowed on this endpoint (* = all): chat, s1, s2, s12, s123, s3, s4, review, report, judge")
    fallback_stages: list[str] = Field(default_factory=list, description="Stages this endpoint takes ONLY when another endpoint blocked or failed the "
                                                                         "request (pool.failover_on_error), e.g. [\"*\"] on the 180 s API")
    enabled: bool = True
    priority: int = Field(0, description="Lower is tried first")
    options: dict = Field(default_factory=dict, description="Adapter options (e.g. chaos rates, http body template)")


class Pool(_M):
    endpoints: list[Endpoint] = Field(default_factory=lambda: [
        Endpoint(name="haiku-test", kind="claude_cli", model="haiku", rpm=30, max_concurrent=6, timeout_s=240, context_tokens=150000),
        Endpoint(name="mock", kind="mock", rpm=600, max_concurrent=8, enabled=False, priority=9),
    ], description="LLM endpoints; a job goes to the first free endpoint that allows its stage")
    chat_reserve_rpm: int = Field(0, ge=0, le=60, description="Requests per minute kept free for the chat on each endpoint (0: the pause-while-chatting rule already gives chat priority; 1 costs a third of the throughput at 3 rpm)")
    backoff_base_s: int = Field(20, ge=1, le=600, description="Base of the exponential backoff after a 429 (Retry-After wins)")
    auto_lower_rpm: bool = Field(True, description="Lower an endpoint's rpm after repeated 429s (logged)")
    timeout_shrink: float = Field(0.3, ge=0.0, le=0.8, description="On a timeout, retry once with every budget shrunk by this share")
    max_retries_5xx: int = Field(3, ge=0, le=10, description="Retries on server errors")
    validation_retries: int = Field(1, ge=0, le=3, description="Retries when the output is unusable (broken schema, missing note, empty section)")
    failover_on_error: bool = Field(True, description="When an endpoint blocks the input or returns an error, send the same request to the next endpoint "
                                                      "that allows the stage (privacy rules still apply) before retrying the first one")


class Privacy(_M):
    """What may leave the company, and personal data in the wiki."""
    test_data_only: bool = Field(False, description="This store holds ONLY the synthetic test dataset: external endpoints may serve every stage. "
                                                    "Never turn on for real mail")
    external_stages: list[str] = Field(default_factory=lambda: ["review"],
                                       description="Stages an external endpoint may serve on real mail. Default: only the wiki lint/review, which sees page "
                                                   "text (no emails), with PII removed. Email stages (s1, s2, s12, s123, s3, s4) and the chat stay internal")
    redact_outputs: bool = Field(True, description="Remove phone numbers, e-mail addresses, IBANs, ID numbers and street addresses from everything the model "
                                                   "writes (notes, wiki pages, reports, chat answers). The stored emails keep the original")
    pii_patterns: dict[str, str] = Field(default_factory=lambda: {
        "phone": r"(?<![\w/.-])(?:\+|00)\d{1,3}[\s./-]?(?:\(0\)[\s./-]?)?\d{1,4}(?:[\s./-]?\d{2,4}){2,4}(?![\w/-])",
        "phone_labelled": r"(?i)\b(?:tel|phone|mobile|mob|cell|fax|gsm|telefon|telefoon|전화|휴대폰|핸드폰)\.?\s*[:.]?\s*\(?\+?\d[\d\s()./-]{6,}\d",
        "phone_local": r"(?<![\w/.-])(?:0\d{1,2}[\s-]\d{3,4}[\s-]\d{4}|01[016789][\s-]?\d{3,4}[\s-]?\d{4}|06[\s-]?\d{8})(?![\w/-])",
        "email": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
        "iban": r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b",
        "kr_rrn": r"\b\d{6}-[1-4]\d{6}\b",
        "street": r"\b[A-ZÄÖÜ][\wäöüß'-]*(?:straat|weg|laan|plein|gracht|kade|singel|straße|strasse|str\.|gasse|allee|platz|street|st\.|road|rd\.|avenue|ave\.|lane|ulica|ul\.)\s+\d{1,4}[a-zA-Z]?\b",
        "postcode_nl": r"\b\d{4}\s?[A-Z]{2}\s+[A-Z][a-z]+",
        "kr_address": r"[가-힣]+(?:특별시|광역시|시|도)\s+[가-힣]+(?:구|군|시)\s+[가-힣0-9]+(?:로|길|동)\s*\d+(?:-\d+)?"},
        description="Personal data patterns (regex) → replaced by [phone], [email], … Reference numbers (store.identifier_patterns) are protected first")
    keep_internal_emails: bool = Field(True, description="Do not redact addresses of the department mailboxes and internal domains in prompts to INTERNAL endpoints")


PRESETS: dict[str, dict] = {
    "standard": {"label": "Standard · 70k", "cap": 70000, "s3_max_sections": 5, "candidates_k": 8, "chat_rounds": 4,
                 "s1": {"instr": 2000, "new": 25000, "parent": 4000, "unseen": 8000, "outline": 2000, "att": 6000},
                 "s2": {"instr": 2500, "emails": 22000, "cand": 15000, "memory": 5000},
                 "s3": {"instr": 2000, "outline": 2000, "current": 10000, "items": 6000, "sources": 22000, "related": 5000},
                 "fast": {"s12": 30000, "s123": 45000},
                 "chat": {"sys": 1500, "view": 12000, "history": 6000, "retrieved": 25000, "tool": 8000, "tools": 25000},
                 "reports": {"lead": 15000, "story": 8000}},
    "large": {"label": "Large · 105k", "cap": 105000, "s3_max_sections": 6, "candidates_k": 12, "chat_rounds": 5,
              "s1": {"instr": 2000, "new": 38000, "parent": 6000, "unseen": 12000, "outline": 3000, "att": 10000},
              "s2": {"instr": 2500, "emails": 33000, "cand": 24000, "memory": 8000},
              "s3": {"instr": 2000, "outline": 3000, "current": 15000, "items": 9000, "sources": 34000, "related": 8000},
              "fast": {"s12": 45000, "s123": 65000},
              "chat": {"sys": 1500, "view": 18000, "history": 9000, "retrieved": 40000, "tool": 12000, "tools": 40000},
              "reports": {"lead": 22000, "story": 12000}},
    "max": {"label": "Max · 150k", "cap": 150000, "s3_max_sections": 8, "candidates_k": 15, "chat_rounds": 6,
            "s1": {"instr": 2000, "new": 55000, "parent": 8000, "unseen": 18000, "outline": 4000, "att": 15000},
            "s2": {"instr": 2500, "emails": 48000, "cand": 35000, "memory": 12000},
            "s3": {"instr": 2000, "outline": 4000, "current": 22000, "items": 12000, "sources": 50000, "related": 12000},
            "fast": {"s12": 60000, "s123": 90000},
            "chat": {"sys": 1500, "view": 25000, "history": 12000, "retrieved": 60000, "tool": 15000, "tools": 60000},
            "reports": {"lead": 30000, "story": 15000}},
}


def _preset(name):
    return copy.deepcopy(PRESETS[name])


class Budgets(_M):
    """Token budgets per prompt section. A preset sets all of them; any value can then be changed."""
    preset: Literal["standard", "large", "max"] = Field("standard", description="Standard 70k (default) · Large 105k · Max 150k")
    cap: int = Field(70000, ge=8000, le=1000000, description="Hard cap per request (tokens)")
    s3_max_sections: int = Field(5, ge=1, le=20, description="Editor: max sections rewritten per request")
    candidates_k: int = Field(8, ge=2, le=40, description="S2: candidate pages shown to the router")
    chat_rounds: int = Field(4, ge=1, le=10, description="Chat: max tool rounds")
    s1: dict[str, int] = Field(default_factory=lambda: _preset("standard")["s1"], description="S1 Notes sections")
    s2: dict[str, int] = Field(default_factory=lambda: _preset("standard")["s2"], description="S2 Route sections")
    s3: dict[str, int] = Field(default_factory=lambda: _preset("standard")["s3"], description="S3 Editor sections")
    fast: dict[str, int] = Field(default_factory=lambda: _preset("standard")["fast"], description="Fast path totals")
    chat: dict[str, int] = Field(default_factory=lambda: _preset("standard")["chat"], description="Chat sections")
    reports: dict[str, int] = Field(default_factory=lambda: _preset("standard")["reports"], description="Report inputs")
    cut_order: list[str] = Field(default_factory=lambda: ["related", "att", "memory", "cand", "outline", "parent", "unseen", "sources", "emails", "new"],
                                 description="Cut first when a prompt is over the cap (new emails keep at least 1.5k each)")

    def apply_preset(self, name: str) -> "Budgets":
        p = _preset(name)
        return Budgets(preset=name, cap=p["cap"], s3_max_sections=p["s3_max_sections"], candidates_k=p["candidates_k"],
                       chat_rounds=p["chat_rounds"], s1=p["s1"], s2=p["s2"], s3=p["s3"], fast=p["fast"], chat=p["chat"],
                       reports=p["reports"], cut_order=self.cut_order)


class Pipeline(_M):
    s1_enabled: bool = Field(True, json_schema_extra={"tier": "P0"}, description="S1 Notes")
    s2_enabled: bool = Field(True, json_schema_extra={"tier": "P0"}, description="S2 Route")
    s3_enabled: bool = Field(True, json_schema_extra={"tier": "P0"}, description="S3 Editor")
    s4_enabled: bool = Field(True, json_schema_extra={"tier": "P2"}, description="S4 Tagger (off = script tags only)")
    combine: Literal["auto", "off"] = Field("auto", json_schema_extra={"tier": "P1"}, description="Combined fast path for single emails (S12 batch, S123 live); falls back to the stages on failure")
    combine_max_emails: int = Field(1, ge=1, le=3, description="Fast path: max emails in a unit")
    combine_edit_live: bool = Field(True, description="Fast path also rewrites sections in live mode (S123)")
    single_notetaker: bool = Field(False, description="Fallback: one multi-round note-taker writes notes AND routing in one request for every unit")
    window_batch: Literal["run", "day", "week"] = Field("day", description="Editor window for the nightly batch")
    window_backfill: Literal["run", "day", "week"] = Field("week", description="Editor window for the backfill")
    note_max_chars: int = Field(300, ge=80, le=2000, description="Max length of a per-email note (longer ones are trimmed)")
    language: str = Field("English", description="Language of notes and wiki pages (mail may be in any language, e.g. Korean)")
    max_pages_per_unit: int = Field(4, ge=1, le=8, description="1 primary + up to N-1 secondary pages per unit")
    new_page_threshold: float = Field(0.55, ge=0.1, le=0.99, description="A new page is allowed only if the best candidate's cosine is below this (models without an entry below)")
    new_page_threshold_by_model: dict[str, float] = Field(default_factory=lambda: {"multilingual-e5-small": 0.88, "bge-small-en-v1.5": 0.78, "hashed": 0.45},
                                                         description="Per embedding model (cosine scales differ a lot between models; chosen by the eval)")
    merge_new_titles: float = Field(0.85, ge=0.5, le=1.0, description="New page proposals this similar to an existing title are merged into it")
    automated_senders: list[str] = Field(default_factory=lambda: [
        "*noreply*@*", "*no-reply*@*", "*donotreply*@*", "*workflow*@*", "*notification*@*", "*notifications*@*", "*alert*@*", "*alerts*@*",
        "*batch*@*", "*system*@*", "*robot*@*", "*bot@*", "*mailer*@*", "*automail*@*", "*tracking*@*", "*approve*@*", "*approval*@*"],
        description="Sender globs of systems (workflow, batch jobs, track & trace, portals). Their mail is 'automated'")
    automated_template_min: int = Field(4, ge=2, le=100, description="A sender whose same subject template arrived ≥ N times in 30 days counts as automated too")
    automated_cap: Literal["not_important", "routine", "off"] = Field("not_important", description="Script override: an automated email the model "
                                                                                                    "rated 'routine' becomes this (errors it rates 'important' stay); automated mail alone never creates a page")
    level_min: dict[str, dict[str, int]] = Field(default_factory=lambda: {"project": {"emails": 6, "days": 3, "people": 3},
                                                                          "topic": {"emails": 3, "days": 2, "people": 2}},
                                                 description="Script check of the page level: a page is shown as a project/topic only when it has at least "
                                                             "this many human (non-automated) emails, active days and people; else one level lower. "
                                                             "The model's proposal is the ceiling")


class Guards(_M):
    min_keep_ratio: float = Field(0.7, ge=0.2, le=1.0, description="Reject a section shorter than this × the old length, unless the model gives a removed_reason")
    removed_reason_min_chars: int = Field(20, ge=0, le=500, description="Minimum length of a removed_reason that counts")
    warn_change: float = Field(0.15, ge=0.02, le=2.0, description="Warn (→ confirm round) if the length changes by more than this share")
    confirm: Literal["always", "on_warning", "never"] = Field("on_warning", description="Confirm round: the model sees the original, its draft and the diff")
    max_words: int = Field(450, ge=100, le=3000, description="Max words per section (over → flagged for the reviewer)")
    inline_markers: Literal["optional", "off"] = Field("optional", description="[n] source markers from the prompt's email list; unknown numbers are dropped")
    check_key_terms: bool = Field(True, description="Warn if an item's numbers, codes or names are missing from the new text")
    keep_rejected: bool = Field(True, description="Store rejected drafts in the section history")


class Pages(_M):
    """How the editor writes a wiki page. A page is read INSTEAD of the mail, so it must be shorter than the mail and say
    each fact once: a short summary + sections the editor names itself, the whole page rewritten in one request, held to
    a length budget that grows with the mail behind it. Every limit is checked by the script (guards), not only asked for."""
    structure: Literal["free", "template"] = Field("free", description="free = a summary + up to max_sections sections the editor names itself; the "
                                                                        "whole page is rewritten at once so nothing is repeated across sections. "
                                                                        "template = the fixed sections per level (Overview, Background, How it works, …), "
                                                                        "rewritten section by section")
    summary_words: int = Field(60, ge=20, le=300, description="The summary at the top: what it is, where it stands now, what happens next")
    max_sections: int = Field(4, ge=1, le=12, description="Sections after the summary (free structure); a small topic may need one or none")
    section_words: int = Field(130, ge=40, le=1500, description="Max words per section")
    budget_ratio: float = Field(0.5, ge=0.1, le=3.0, description="Page length budget = this × the words people wrote in the page's emails "
                                                                 "(new text only: no quoted history, no automated mail) …")
    budget_min: int = Field(120, ge=40, le=3000, description="… but at least this many words …")
    budget_max: int = Field(650, ge=100, le=10000, description="… and at most this many words")
    tolerance: float = Field(0.15, ge=0.0, le=1.0, description="A draft over the budget (or a section/summary over its limit) by more than this share → "
                                                               "the confirm round asks the editor to shorten it")
    over_budget: Literal["accept", "reject"] = Field("accept", description="Still over after the confirm round: accept (a warning in the history) "
                                                                           "or keep the old text and retry next run")
    repeat_check: bool = Field(True, description="Script check for repetition: a sentence that repeats another sentence of the page → confirm round")
    repeat_similarity: float = Field(0.7, ge=0.3, le=1.0, description="Share of the shorter sentence's words that must also be in the other one to "
                                                                      "count as repeated (the summary vs a section: 0.15 higher)")
    drop_phrases: list[str] = Field(default_factory=lambda: [
        r"\b(?:the )?(?:sources|emails) (?:do|does|did) not\b", r"\bnot (?:stated|mentioned|recorded|shown|given|confirmed) in the (?:sources|emails)\b",
        r"\b(?:is|are) (?:described|covered|listed|explained) (?:in|under) the [\w ]{2,40} section\b", r"\bsee the [\w ]{2,40} section\b",
        r"\bno (?:further |other )?(?:information|details?) (?:is |are )?(?:available|given|provided)\b", r"\bno next dates are set\b"],
        description="Regexes (case-insensitive): a sentence matching one is removed by the script — talk about the sources or the page itself "
                    "instead of facts")

    def budget(self, source_words: int) -> int:
        return int(min(self.budget_max, max(self.budget_min, self.budget_ratio * source_words)))


class Reviewer(_M):
    schedule: Literal["manual", "nightly"] = Field("manual", json_schema_extra={"tier": "P2"}, description="Run the reviewer by hand, or after every nightly batch")
    auto_apply: bool = Field(False, description="Apply splits and merges automatically (off: logged only)")
    dup_threshold: float = Field(0.85, ge=0.5, le=0.99, description="Page similarity that triggers a merge proposal")
    stale_words: list[str] = Field(default_factory=lambda: ["next week", "tomorrow", "this friday", "soon", "today"], description="Relative time words to rewrite as dates")
    max_pages: int = Field(30, ge=1, le=500, description="Max pages reviewed per run")


class Tags(_M):
    systems: list[str] = Field(default_factory=lambda: ["sap-s4", "sap-ewm", "tms", "customs-gateway", "rpa-orchestrator", "edi", "excel", "power-bi", "wms"], description="Allowed primary values: system")
    processes: list[str] = Field(default_factory=lambda: ["outbound-delivery", "inbound", "customs-declarations", "carrier-invoicing", "transport-procurement", "warehouse-operations", "purchasing", "master-data", "kpi-reporting", "billing"], description="Allowed primary values: process")
    work_types: list[str] = Field(default_factory=lambda: ["project", "incident", "recurring-task", "change-request", "how-to", "decision"], description="Allowed primary values: work type")
    page_min_emails: int = Field(2, ge=1, le=20, description="A site/partner/t-code tag needs N source emails on the page")
    tcode_promote_threads: int = Field(2, ge=1, le=20, description="A new t-code becomes active after N threads (or when S4 confirms it)")
    tcode_stoplist: list[str] = Field(default_factory=lambda: ["ISO9001", "EUR1", "EUR", "USD", "CMR", "PDF", "XLSX", "KPI", "ETA", "ETD", "FYI", "ASAP"], description="Never t-codes")
    lifecycle_enabled: bool = Field(False, json_schema_extra={"tier": "P2"}, description="Automatic tag lifecycle (dormant/deprecated)")
    dormant_days: int = Field(90, ge=7, le=1000, description="Unused this long → dormant")
    deprecated_days: int = Field(180, ge=30, le=2000, description="Unused this long → deprecated")


class Retrieval(_M):
    mode: Literal["hybrid", "bm25", "vector"] = Field("hybrid", description="Fallback: bm25 or vector alone")
    vector_store: Literal["qdrant", "sqlite"] = Field("qdrant", description="qdrant = local Qdrant index; sqlite = brute force over the vectors stored in SQLite")
    qdrant_url: str = Field("", description="Qdrant server URL (empty = local mode in data/qdrant)")
    embed_model: Literal["multilingual-e5-small", "bge-small-en-v1.5", "hashed"] = Field("multilingual-e5-small", description="Active embedding model (hashed = no model files, degraded)")
    eval_models: list[str] = Field(default_factory=lambda: ["multilingual-e5-small", "bge-small-en-v1.5", "hashed"], description="Models compared by the eval")
    hf_repo_overrides: dict[str, str] = Field(default_factory=dict, description="Model name → Hugging Face repo, if the mirror uses other names")
    embed_batch: int = Field(32, ge=1, le=512, description="Texts per embedding batch")
    fts_trigram: bool = Field(True, description="Second keyword index with the trigram tokenizer, used for Hangul/CJK queries")
    chunk_tokens: int = Field(350, ge=80, le=2000, description="Email chunk size (tokens)")
    chunk_overlap: int = Field(50, ge=0, le=400, description="Email chunk overlap (tokens)")
    section_max_words: int = Field(450, ge=100, le=3000, description="Wiki sections longer than this are split into chunks")
    top_n: int = Field(30, ge=5, le=200, description="Candidates per retriever")
    top_k: int = Field(8, ge=1, le=50, description="Chunks given to the model")
    rrf_k: int = Field(60, ge=1, le=500, description="Reciprocal Rank Fusion constant")
    w_wiki: float = Field(1.0, ge=0, le=3, description="Source weight: wiki sections")
    w_notes: float = Field(0.8, ge=0, le=3, description="Source weight: per-email notes")
    w_emails: float = Field(0.6, ge=0, le=3, description="Source weight: email chunks")
    w_attachments: float = Field(0.5, ge=0, le=3, description="Source weight: attachment extracts")
    recency_half_life_days: int = Field(60, ge=0, le=3650, description="Recency boost half-life (0 = off)")
    rerank: bool = Field(False, json_schema_extra={"tier": "P2"}, description="Cross-encoder reranker on the top results (only if the eval shows a gain)")
    rerank_model: str = Field("Xenova/ms-marco-MiniLM-L-6-v2", description="Reranker model repo")
    index_attachments: bool = Field(True, description="Index attachment extracts")


class Chat(_M):
    engine: Literal["langgraph", "plain"] = Field("langgraph", description="langgraph = LangGraph state graph; plain = the same tools in a Python loop")
    deep_enabled: bool = Field(True, json_schema_extra={"tier": "P1"}, description="Deep mode: planner → parallel researchers → writer")
    deep_max_subq: int = Field(3, ge=1, le=6, description="Deep mode: max sub-questions")
    deep_research_rounds: int = Field(2, ge=1, le=4, description="Deep mode: search rounds per researcher")
    wiki_first: bool = Field(True, description="Answer from wiki sections; emails for specifics")
    history_turns: int = Field(6, ge=0, le=30, description="Earlier turns kept (compressed by script)")
    log_stale: bool = Field(True, description="Log wiki/email disagreements for the reviewer")


class Departments(_M):
    """The department addresses (sources.dept_addresses) are separate teams of one bigger department: one wiki, one report each."""
    names: dict[str, str] = Field(default_factory=lambda: {"dl-ct@corp-europe.example": "Control Tower", "dl-cus@corp-europe.example": "Customs",
                                                           "dl-fin@corp-europe.example": "Finance", "dl-ai@corp-europe.example": "AI & Automation"},
                                  description="Display name per department address")
    ai_address: str = Field("dl-ai@corp-europe.example", description="The AI department's address: AI-related mail and pages belong to it, whatever "
                                                                     "address they were sent to ('' = no AI department)")
    ai_keywords: list[str] = Field(default_factory=lambda: [
        "AI", "artificial intelligence", "LLM", "GPT", "ChatGPT", "OpenAI", "Azure OpenAI", "Gemini", "Claude", "Copilot", "LangChain", "LangGraph",
        "RAG", "embedding", "vector database", "prompt", "chatbot", "agent", "machine learning", "ML model", "fine-tuning", "n8n", "Power Automate",
        "automation workflow", "OCR model", "document AI", "classification model", "forecast model"],
        description="Examples given to the note-taker (not a checklist: it decides by meaning)")
    ai_page_min_share: float = Field(0.3, ge=0.05, le=1.0, description="A page belongs to the AI department when at least this share of its emails is AI-related")
    team_members: list[str] = Field(default_factory=list, description="Addresses (or globs, e.g. *@corp.com) of the team members. A topic is only "
                                                                      "reported when one of them SENT mail on it in the report window. Empty = any internal sender")
    team_by_dept: dict[str, list[str]] = Field(default_factory=dict, description="Optional: team members per department address (added to team_members "
                                                                                  "for that department's report)")


class Reports(_M):
    stories: int = Field(6, ge=2, le=12, description="Headline stories per Briefing")
    kinds: list[Literal["daily", "weekly", "monthly"]] = Field(default_factory=lambda: ["daily", "weekly", "monthly"],
                                                              description="Report kinds offered in the Reports tab (the period menu lists only the chosen kind)")
    default_kind: Literal["daily", "weekly", "monthly"] = Field("weekly", description="Kind shown when the Reports tab opens")
    daily_time: str = Field("07:00", pattern=r"^\d{2}:\d{2}$", description="Each morning at this time: the 'past 7 days' report per department "
                                                                         "(window ends at this time); on the 1st also the monthly report of the month before")
    weekly_weekday: int = Field(3, ge=0, le=6, description="Weekday of the weekly report (0 = Monday … 3 = Thursday, the day before the CFO meeting)")
    window_days: int = Field(7, ge=1, le=31, description="Daily/weekly report window in days; the agent only sees updates from inside it "
                                                         "(monthly reports cover the calendar month)")
    keep_daily: int = Field(14, ge=1, le=90, description="Daily reports listed in the period menu")
    keep_weekly: int = Field(26, ge=1, le=260, description="Weekly reports listed in the period menu")
    keep_monthly: int = Field(12, ge=1, le=120, description="Monthly reports listed in the period menu")
    list_max_px: int = Field(0, ge=0, le=3000, description="Max height of the lists below the stories (Decisions, Deadlines, …) in pixels; they "
                                                           "scroll inside. 0 = the height of a story card")
    require_team_activity: bool = Field(True, description="Only topics on which a team member sent mail in the window (departments.team_members)")
    layout: Literal["2col", "1col"] = Field("2col", description="Two columns with height-paired cards, or one column")
    pair_diff: int = Field(2, ge=0, le=10, description="Pair cards whose estimated heights differ by at most N lines")
    key_notes: int = Field(3, ge=2, le=4, description="Key emails per card (2–4; used to even out a pair)")
    lead_min: int = Field(300, ge=50, le=2000, description="Lead: min characters (Pydantic)")
    lead_max: int = Field(900, ge=100, le=4000, description="Lead: max characters")
    story_min: int = Field(350, ge=50, le=2000, description="Story text: min characters")
    story_max: int = Field(800, ge=100, le=4000, description="Story text: max characters")
    wait_days: int = Field(2, ge=1, le=30, description="'Waiting on others' after N days without an internal reply")
    attn_days: int = Field(7, ge=1, le=60, description="An open issue needs attention after N days")


STAT_KEYS = Literal["emails", "important", "pages_active", "new_pages", "decisions", "closed", "at_risk", "threads"]


class ReportParts(_M):
    """What a report shows. Every part of a story card and every section can be switched off here; a part that needs the
    agent (lead, story text) is then not requested either."""
    lead: bool = Field(True, description="The agent's opening paragraph (off: no request)")
    mail_per_day: bool = Field(True, description="Mail per day next to the lead (a month grid for monthly reports)")
    stat_cards: list[STAT_KEYS] = Field(default_factory=lambda: ["emails", "important", "pages_active", "new_pages", "decisions", "closed", "at_risk"],
                                        description="Stat cards under the mail per day, in this order (empty = none). Also possible: threads")
    left_out: bool = Field(True, description="The fold-out list of topics left out because no team member was involved")
    story_text: bool = Field(True, description="Story card: the agent's paragraph on what happened in the window (off: no request; the card shows its key emails)")
    story_decision: bool = Field(True, description="Story card: the newest decision of the window (from the per-email notes)")
    story_key_emails: bool = Field(True, description="Story card: the key emails (notes)")
    story_tags: bool = Field(True, description="Story card: tag pills")
    story_status: bool = Field(True, description="Story card: status and the status change in the window")
    story_level: bool = Field(True, description="Story card: project / topic / task")
    story_email_count: bool = Field(True, description="Story card footer: number of emails")
    story_people: bool = Field(True, description="Story card footer: avatars of the people involved")
    story_days: bool = Field(True, description="Story card footer: the day cells (weeks for monthly reports)")
    story_open_issues: bool = Field(True, description="Story card footer: number of open issues")
    story_next_deadline: bool = Field(True, description="Story card footer: the next deadline")
    decisions: bool = Field(True, description="Section: Decisions")
    deadlines: bool = Field(True, description="Section: Deadlines (next 14 days)")
    waiting: bool = Field(True, description="Section: Waiting on others")
    attention: bool = Field(True, description="Section: Needs attention")
    other_topics: bool = Field(True, description="Section: Other topics (active but not a headline story)")
    topic_view: bool = Field(True, description="The 'Topic week' / 'Topic month' view")
    source_switch: bool = Field(True, description="The 'Show where each part comes from' switch")
    footer_note: bool = Field(True, description="The explanation at the bottom of the report")


class Portal(_M):
    title: str = Field("Control Tower Wiki", description="Name shown in the header")
    default_window_days: int = Field(7, ge=1, le=365, description="Default time window of lists, timelines and reports")
    wiki_view: Literal["cards", "list"] = Field("cards", description="Default wiki list view (each user can switch)")


class Server(_M):
    mode: Literal["local", "server"] = Field("local", description="local = 127.0.0.1 without login; server = Django login required")
    host: str = Field("127.0.0.1", description="Listen address (0.0.0.0 on a server)")
    port: int = Field(8000, ge=1, le=65535, description="Port")
    threads: int = Field(8, ge=2, le=64, description="Web server threads")
    log_level: Literal["DEBUG", "INFO", "WARNING"] = Field("INFO", description="Console log level (the log file always has DEBUG)")


class Settings(_M):
    sources: Sources = Field(default_factory=Sources)
    store: Store = Field(default_factory=Store)
    skip: Skip = Field(default_factory=Skip)
    schedule: Schedule = Field(default_factory=Schedule)
    pool: Pool = Field(default_factory=Pool)
    privacy: Privacy = Field(default_factory=Privacy)
    budgets: Budgets = Field(default_factory=Budgets)
    pipeline: Pipeline = Field(default_factory=Pipeline)
    guards: Guards = Field(default_factory=Guards)
    pages: Pages = Field(default_factory=Pages)
    reviewer: Reviewer = Field(default_factory=Reviewer)
    tags: Tags = Field(default_factory=Tags)
    retrieval: Retrieval = Field(default_factory=Retrieval)
    chat: Chat = Field(default_factory=Chat)
    departments: Departments = Field(default_factory=Departments)
    reports: Reports = Field(default_factory=Reports)
    report_parts: ReportParts = Field(default_factory=ReportParts)
    portal: Portal = Field(default_factory=Portal)
    server: Server = Field(default_factory=Server)


GROUP_LABELS = {
    "sources": "Sources", "store": "Store & threads", "skip": "Skip rules", "schedule": "Schedule",
    "pool": "Endpoints & pool", "privacy": "Privacy", "budgets": "Budgets", "pipeline": "Pipeline", "guards": "Editor guards",
    "pages": "Wiki pages", "reviewer": "Reviewer", "tags": "Tags", "retrieval": "Retrieval", "chat": "Chat", "departments": "Departments",
    "reports": "Reports", "report_parts": "Report parts", "portal": "Portal", "server": "Server",
}

# ------------------------------------------------------------------ load / save
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "settings": None, "version": None}
_override: Settings | None = None  # tests can pin settings with use_settings()


def data_dir() -> Path:
    from django.conf import settings as dj
    return Path(dj.DATA_DIR)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("identifier_patterns", "hf_repo_overrides", "options"):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _yaml_overrides() -> dict:
    p = data_dir() / "settings.yaml"
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("settings.yaml could not be read; ignoring it")
        return {}


# Machine-specific keys: when data/settings.yaml sets one of these, it wins over the settings saved in the DB. The DB
# travels with the data (work laptop → server), the endpoints, file paths and listen address do not.
MACHINE_KEYS = ["pool.endpoints", "sources.pst_path", "sources.pst_route", "sources.pst_folders", "sources.eml_folder",
                "sources.pop3_enabled", "sources.pop3_accounts", "server", "privacy.test_data_only"]


def _get_path(d: dict, path: str):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return None, False
        d = d[part]
    return d, True


def _set_path(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def pinned_keys() -> list[str]:
    """Machine keys that this machine's settings.yaml sets (shown as 'set in settings.yaml' in the portal)."""
    y = _yaml_overrides()
    return [k for k in MACHINE_KEYS if _get_path(y, k)[1]]


def load_settings() -> Settings:
    base = Settings().model_dump()
    y = _yaml_overrides()
    merged = _deep_merge(base, y)
    try:
        from llmwiki.models import SettingsVersion
        row = SettingsVersion.objects.order_by("-id").first()
        if row:
            merged = _deep_merge(merged, row.data)
            for k in MACHINE_KEYS:
                v, found = _get_path(y, k)
                if found:
                    _set_path(merged, k, _deep_merge(_get_path(merged, k)[0] or {}, v) if isinstance(v, dict) else v)
    except Exception:
        log.debug("settings table not ready yet")
    try:
        return Settings.model_validate(merged)
    except Exception:
        log.exception("saved settings are invalid; falling back to defaults + settings.yaml")
        return Settings.model_validate(_deep_merge(base, _yaml_overrides()))


def get_settings() -> Settings:
    if _override is not None:
        return _override
    now = time.monotonic()
    with _lock:
        if _cache["settings"] is None or now - _cache["at"] > 3:
            _cache["settings"] = load_settings()
            _cache["at"] = now
        return _cache["settings"]


def invalidate():
    with _lock:
        _cache["settings"] = None


def save_settings(new: Settings | dict, user: str = "", note: str = "") -> Settings:
    """Validate, store a new SettingsVersion (full snapshot) and refresh the cache."""
    from llmwiki.models import SettingsVersion
    s = new if isinstance(new, Settings) else Settings.model_validate(new)
    SettingsVersion.objects.create(data=json.loads(s.model_dump_json()), user=user, note=note)
    invalidate()
    log.info("settings saved by %s (%s)", user or "system", note)
    return s


class use_settings:
    """Context manager for tests: `with use_settings(s): ...` pins get_settings()."""

    def __init__(self, s: Settings):
        self.s = s

    def __enter__(self):
        global _override
        self.prev, _override = _override, self.s
        return self.s

    def __exit__(self, *a):
        global _override
        _override = self.prev
