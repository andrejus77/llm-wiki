"""Database schema. Everything the app knows is in this one SQLite file:
raw mail (compressed), attachments (deduplicated blobs), notes, pages, history, jobs, LLM call logs and vectors.
The Qdrant folder is only a rebuildable index of `ChunkVector`.

Facts (IDs, dates, counts, participants, sources) are always written by scripts, never by the model.
"""
from __future__ import annotations

import zlib

from django.db import models


def json_has(qs, field: str, value):
    """Filter rows whose JSON list `field` contains `value` (SQLite has no JSON __contains): cast to text + exact regex."""
    import re as _re
    from django.db.models import TextField
    from django.db.models.functions import Cast
    alias = f"_{field.replace('__', '_')}_txt"
    if isinstance(value, int):
        pat = rf"(^|[\[ ,]){value}([\],])"
    else:
        pat = '"' + _re.escape(str(value)) + '"'
    return qs.annotate(**{alias: Cast(field, TextField())}).filter(**{f"{alias}__regex": pat})


class Blob(models.Model):
    """Content-addressed binary storage (attachments, images). Compressed when it helps."""
    sha256 = models.CharField(max_length=64, primary_key=True)
    size = models.BigIntegerField()
    compressed = models.BooleanField(default=False)
    data = models.BinaryField()

    @classmethod
    def put(cls, raw: bytes) -> "Blob":
        import hashlib
        h = hashlib.sha256(raw).hexdigest()
        b = cls.objects.filter(sha256=h).first()
        if b:
            return b
        z = zlib.compress(raw, 6)
        comp = len(z) < len(raw) * 0.9
        return cls.objects.create(sha256=h, size=len(raw), compressed=comp, data=z if comp else raw)

    def get(self) -> bytes:
        d = bytes(self.data)
        return zlib.decompress(d) if self.compressed else d


class Thread(models.Model):
    subject = models.CharField(max_length=500, default="")
    first_at = models.DateTimeField(null=True)
    last_at = models.DateTimeField(null=True)
    n_emails = models.IntegerField(default=0)
    participants = models.JSONField(default=list)
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"T{self.id} {self.subject[:60]}"


class ThreadKey(models.Model):
    """Union-find keys: 'mid:<message-id>', 'conv:<thread-index guid>', 'cid:<conversation id>' → thread."""
    key = models.CharField(max_length=500, unique=True)
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="keys")


class Folder(models.Model):
    """A mail folder in the portal (like Outlook's). Filing only: whether an email is processed is the rules' business."""
    name = models.CharField(max_length=120, unique=True)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class Email(models.Model):
    """The row the portal lists and filters. The heavy part (the original message with its attachments, the full body)
    lives in EmailOriginal: SQLite has to walk over a large value to reach any column stored after it."""
    STATUS = [("new", "new"), ("skipped", "skipped"), ("duplicate", "duplicate"), ("noted", "noted"),
              ("processed", "processed"), ("failed", "failed")]
    message_id = models.CharField(max_length=500, unique=True)
    sources = models.JSONField(default=list)          # ["pop3", "pst", "eml"]
    source_ref = models.CharField(max_length=500, default="", blank=True)
    subject = models.CharField(max_length=1000, default="")
    subject_norm = models.CharField(max_length=1000, default="", db_index=True)
    subject_tpl = models.CharField(max_length=1000, default="", db_index=True)
    from_addr = models.CharField(max_length=320, default="", db_index=True)
    from_name = models.CharField(max_length=320, default="")
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list)
    depts = models.JSONField(default=list)
    sent_at = models.DateTimeField(db_index=True)
    day = models.CharField(max_length=10, db_index=True)  # local date YYYY-MM-DD
    in_reply_to = models.CharField(max_length=500, default="", blank=True)
    references = models.JSONField(default=list)
    thread_index = models.CharField(max_length=200, default="", blank=True)
    conversation_id = models.CharField(max_length=200, default="", blank=True)
    headers = models.JSONField(default=dict)          # a few interesting headers
    new_text = models.TextField(default="")           # the part written in this email
    signature = models.TextField(default="", blank=True)
    lang = models.CharField(max_length=8, default="en")
    has_table = models.BooleanField(default=False)
    n_attachments = models.IntegerField(default=0)
    thread = models.ForeignKey(Thread, null=True, on_delete=models.SET_NULL, related_name="emails")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="replies")
    dup_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")
    status = models.CharField(max_length=12, choices=STATUS, default="new", db_index=True)
    skip_rule = models.CharField(max_length=300, default="", blank=True)
    skip_action = models.CharField(max_length=12, default="", blank=True)   # skip | note_only
    rule_ref = models.IntegerField(null=True, blank=True, db_index=True)    # the SkipRule that skipped it (undone with the rule)
    skip_manual = models.BooleanField(default=False)  # a person decided skip / process: rules leave it alone
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="emails")
    folder_rule = models.IntegerField(null=True, blank=True)                # the SkipRule that filed it (undone with the rule)
    folder_manual = models.BooleanField(default=False)  # a person moved it: rules leave it alone
    audit = models.BooleanField(default=False)        # processed despite a learned rule (audit sample)
    # the per-email note (S1 or fast path)
    note = models.TextField(default="", blank=True)
    kind = models.CharField(max_length=12, default="", blank=True)
    importance = models.CharField(max_length=16, default="", blank=True)
    due = models.CharField(max_length=10, default="", blank=True)
    ai_related = models.BooleanField(default=False)   # S1: about AI/automation → belongs to the AI department
    automated = models.BooleanField(default=False)    # script: sent by a system (ingest/automated.py)
    parallel_key = models.CharField(max_length=64, default="", blank=True)  # fingerprint of the earlier message it answers, when that one isn't stored
    noted_at = models.DateTimeField(null=True)
    processed_at = models.DateTimeField(null=True)
    fail_reason = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["thread", "sent_at"]), models.Index(fields=["status", "day"]), models.Index(fields=["folder", "sent_at"])]

    def raw_bytes(self) -> bytes:
        raw = EmailOriginal.objects.filter(email_id=self.id).values_list("raw", flat=True).first()
        return zlib.decompress(bytes(raw)) if raw else b""

    @property
    def is_internal(self):
        from llmwiki.conf import get_settings
        dom = self.from_addr.split("@")[-1].lower()
        return dom in [d.lower() for d in get_settings().sources.internal_domains]

    def __str__(self):
        return f"E{self.id} {self.day} {self.from_addr} {self.subject[:50]}"


class EmailOriginal(models.Model):
    """The original message and the full body of an email. Only re-parsing and exports read this table."""
    email = models.OneToOneField(Email, primary_key=True, on_delete=models.CASCADE, related_name="original")
    raw = models.BinaryField(null=True)               # zlib-compressed original .eml
    body_text = models.TextField(default="")          # full body as markdown-ish text
    quoted_text = models.TextField(default="")


class Attachment(models.Model):
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=500)
    content_type = models.CharField(max_length=200, default="")
    size = models.BigIntegerField(default=0)
    blob = models.ForeignKey(Blob, null=True, on_delete=models.SET_NULL)
    is_image = models.BooleanField(default=False)
    inline = models.BooleanField(default=False)
    content_id = models.CharField(max_length=300, default="", blank=True)
    extract = models.TextField(default="", blank=True)
    extract_error = models.CharField(max_length=500, default="", blank=True)


class QuotedBlock(models.Model):
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="quoted_blocks")
    idx = models.IntegerField()
    header = models.TextField(default="")
    text = models.TextField()
    linked = models.ForeignKey(Email, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    match = models.FloatField(default=0)
    sent_to_model = models.BooleanField(default=False)   # unseen history goes to the model once
    fingerprint = models.CharField(max_length=64, default="", db_index=True)


class Identifier(models.Model):
    value = models.CharField(max_length=100, db_index=True)
    type = models.CharField(max_length=30)
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="identifiers")


class SkipRule(models.Model):
    """A mail rule: conditions (sender, subject, recipient; all that are set must match) and actions (skip / note only /
    keep processing, and optionally file into a folder)."""
    KIND = [("static", "static"), ("learned", "learned"), ("candidate", "candidate")]
    kind = models.CharField(max_length=10, choices=KIND, default="static")
    sender = models.CharField(max_length=300, default="", blank=True, help_text="glob on the sender, e.g. news@*")
    subject = models.CharField(max_length=500, default="", blank=True, help_text="regex on the subject")
    recipient = models.CharField(max_length=300, default="", blank=True, help_text="glob on To/Cc, e.g. dl-ct@*")
    template = models.CharField(max_length=500, default="", blank=True, help_text="learned: normalised subject template")
    action = models.CharField(max_length=12, default="skip")  # skip | note_only | keep (only files the mail)
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="rules")
    include_replies = models.BooleanField(default=False, help_text="also skip replies/forwards written by other senders")
    enabled = models.BooleanField(default=True)
    hits = models.IntegerField(default=0)
    why = models.CharField(max_length=500, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Page(models.Model):
    LEVELS = [("task", "task"), ("topic", "topic"), ("project", "project")]
    slug = models.SlugField(max_length=200, unique=True)
    title = models.CharField(max_length=300)
    level = models.CharField(max_length=10, choices=LEVELS, default="topic")       # effective level (script-checked)
    level_proposed = models.CharField(max_length=10, default="", blank=True)       # the model's level = the ceiling
    dept = models.CharField(max_length=320, default="", blank=True, db_index=True)  # the ONE department whose report shows it
    dept_locked = models.BooleanField(default=False)                               # set by a person: not reassigned by script
    status = models.CharField(max_length=30, default="Open")
    tags = models.JSONField(default=dict)              # {"system": [...], "process": [...], "site": [...], "tcode": [...]}
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_email_at = models.DateTimeField(null=True)
    archived = models.BooleanField(default=False)
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_reason = models.TextField(default="", blank=True)
    tagged_at = models.DateTimeField(null=True)
    reviewed_at = models.DateTimeField(null=True)
    needs_rewrite = models.BooleanField(default=False)  # a rule took source emails away: the next run rewrites the page

    def __str__(self):
        return self.title


class Section(models.Model):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="sections")
    key = models.CharField(max_length=60)                 # template key, "summary", or a slug of the editor's own title
    title = models.CharField(max_length=200, default="", blank=True)   # the editor's title (free structure); "" = the template label
    position = models.IntegerField(default=0)             # order on the page (free structure)
    text = models.TextField(default="")
    version = models.IntegerField(default=0)
    sources = models.JSONField(default=list)            # email ids recorded by the script (provenance)
    updated_at = models.DateTimeField(null=True)

    class Meta:
        unique_together = [("page", "key")]
        ordering = ["position", "id"]


class SectionHistory(models.Model):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="history")
    key = models.CharField(max_length=60)
    title = models.CharField(max_length=200, default="", blank=True)
    version = models.IntegerField()                       # the version it became (rejected drafts keep the current)
    text = models.TextField()
    accepted = models.BooleanField(default=True)
    round = models.IntegerField(default=1)
    guards = models.JSONField(default=list)               # [[label, ok, warn_only], ...]
    reason = models.TextField(default="", blank=True)     # removed_reason / rollback note
    sources = models.JSONField(default=list)
    call = models.ForeignKey("LlmCall", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class Item(models.Model):
    """A knowledge item routed to a page section (S2), waiting for the editor (S3)."""
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="items")
    section_key = models.CharField(max_length=60)        # template key; "page" in the free structure (the editor places it)
    text = models.TextField()
    emails = models.JSONField(default=list)              # email ids (mapped from [n] by the script)
    unit_key = models.CharField(max_length=200, default="")
    window = models.CharField(max_length=40, default="", db_index=True)
    status = models.CharField(max_length=10, default="pending", db_index=True)   # pending | written | failed
    attempts = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    written_version = models.IntegerField(null=True)


class PageEmail(models.Model):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="page_emails")
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="page_links")
    role = models.CharField(max_length=10, default="primary")

    class Meta:
        unique_together = [("page", "email")]


class OpenIssue(models.Model):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="open_issues")
    code = models.CharField(max_length=20)
    text = models.TextField()
    email = models.ForeignKey(Email, null=True, on_delete=models.SET_NULL, related_name="+")
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True)
    closed_by = models.ForeignKey(Email, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")


class StatusChange(models.Model):
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="status_changes")
    old = models.CharField(max_length=30, default="")
    new = models.CharField(max_length=30)
    email = models.ForeignKey(Email, null=True, on_delete=models.SET_NULL, related_name="+")
    at = models.DateTimeField()


class Tag(models.Model):
    """Tag registry: values per type with lifecycle state."""
    type = models.CharField(max_length=20)               # system, process, work_type, site, partner, tcode
    value = models.CharField(max_length=100)
    state = models.CharField(max_length=12, default="active")   # candidate | active | dormant | deprecated | rejected
    threads = models.IntegerField(default=0)
    first_seen = models.DateTimeField(null=True)
    last_seen = models.DateTimeField(null=True)
    aliases = models.JSONField(default=list)
    why = models.CharField(max_length=500, default="", blank=True)
    contexts = models.JSONField(default=list)            # t-code candidates: a few context snippets

    class Meta:
        unique_together = [("type", "value")]


class Job(models.Model):
    """Durable job record with an idempotency key (stage + unit + input hash)."""
    key = models.CharField(max_length=300, unique=True)
    stage = models.CharField(max_length=10, db_index=True)
    unit = models.CharField(max_length=200, default="", db_index=True)
    status = models.CharField(max_length=10, default="queued", db_index=True)  # queued|running|done|failed|skipped
    attempts = models.IntegerField(default=0)
    error = models.TextField(default="", blank=True)
    result = models.JSONField(default=dict)
    run = models.ForeignKey("Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)


class LlmCall(models.Model):
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="calls")
    stage = models.CharField(max_length=10, db_index=True)
    unit = models.CharField(max_length=200, default="", db_index=True)
    endpoint = models.CharField(max_length=60, default="")
    status = models.CharField(max_length=12, default="")   # ok | rejected | timeout | invalid | error
    http_code = models.IntegerField(null=True)
    attempt = models.IntegerField(default=1)
    round = models.IntegerField(default=1)
    queue_wait_ms = models.IntegerField(default=0)
    latency_ms = models.IntegerField(default=0)
    prompt_tokens = models.IntegerField(default=0)
    sections = models.JSONField(default=list)             # [{key,label,want,budget,sent,cut_budget,cut_cap}]
    system = models.TextField(default="")
    prompt = models.TextField(default="")
    response = models.TextField(default="")
    parsed = models.JSONField(null=True)
    errors = models.JSONField(default=list)
    redactions = models.IntegerField(default=0)           # personal data items removed from this prompt (privacy.redact)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class Run(models.Model):
    kind = models.CharField(max_length=20)                 # fetch, batch, live, backfill, process, review, report, index, eval
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=10, default="running")
    stats = models.JSONField(default=dict)
    note = models.TextField(default="", blank=True)


class Event(models.Model):
    ts = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=20, db_index=True)
    level = models.CharField(max_length=8, default="info")
    message = models.TextField()
    data = models.JSONField(default=dict)
    run = models.ForeignKey(Run, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")


class Chunk(models.Model):
    """A retrievable piece of text. id = content hash, so unchanged text is never re-embedded."""
    cid = models.CharField(max_length=40, unique=True)
    collection = models.CharField(max_length=12, db_index=True)   # wiki | notes | emails | attachments
    ref = models.CharField(max_length=120, db_index=True)         # page:<slug>:<key> | email:<id> | att:<id>
    header = models.TextField(default="")
    text = models.TextField()
    page = models.ForeignKey(Page, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    email = models.ForeignKey(Email, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    day = models.CharField(max_length=10, default="")
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class ChunkVector(models.Model):
    """Embeddings live in SQLite (the source of truth); Qdrant is a rebuildable index of these rows."""
    chunk = models.ForeignKey(Chunk, on_delete=models.CASCADE, related_name="vectors")
    model = models.CharField(max_length=60)
    dim = models.IntegerField()
    vector = models.BinaryField()                         # float32 little-endian

    class Meta:
        unique_together = [("chunk", "model")]


class ChatSession(models.Model):
    user = models.CharField(max_length=150, default="")
    created_at = models.DateTimeField(auto_now_add=True)


class ChatMessage(models.Model):
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name="messages")
    question = models.TextField()
    mode = models.CharField(max_length=8, default="fast")
    view = models.JSONField(default=dict)                  # what the user had open
    status = models.CharField(max_length=10, default="queued")   # queued | running | done | failed
    steps = models.JSONField(default=list)                 # progress shown while it runs
    answer = models.TextField(default="", blank=True)
    sources = models.JSONField(default=list)               # [{n, kind, ref, title, ...}]
    confidence = models.CharField(max_length=10, default="", blank=True)
    feedback = models.SmallIntegerField(default=0)         # 1 👍, -1 👎
    error = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)


class Report(models.Model):
    period = models.CharField(max_length=400)             # <dept address>|<report date>  (texts are reused by input hash)
    part = models.CharField(max_length=120)               # lead | story:<slug>
    input_hash = models.CharField(max_length=40)
    text = models.TextField()
    generated = models.BooleanField(default=True)         # False = script fallback
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("period", "part", "input_hash")]


class SettingsVersion(models.Model):
    data = models.JSONField()
    user = models.CharField(max_length=150, default="", blank=True)
    note = models.CharField(max_length=300, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class SeenUidl(models.Model):
    """POP3 UIDLs already downloaded, so a re-run never downloads again."""
    uidl = models.CharField(max_length=300, unique=True)
    seen_at = models.DateTimeField(auto_now_add=True)


class KV(models.Model):
    """Small runtime state (last batch day, live failure counter, fetch cursors...)."""
    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField(default=dict)

    @classmethod
    def get(cls, key, default=None):
        r = cls.objects.filter(key=key).first()
        return r.value if r else default

    @classmethod
    def set(cls, key, value):
        cls.objects.update_or_create(key=key, defaults={"value": value})
