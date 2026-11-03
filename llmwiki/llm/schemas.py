"""JSON schemas of every stage's answer, for endpoints with structured_output on (e.g. an API behind LangChain's
with_structured_output, OpenAI-compatible response_format, codex --output-schema).

They describe the same JSON the prompts ask for in text. The pipeline's own parsers (pipeline/parse.py, chat/agent.py)
stay the validators either way: they accept fuzzy labels, fill defaults and produce the exact error list for a retry,
so a schema-capable API simply returns fewer broken answers. Schemas are deliberately permissive (optional fields,
extra keys allowed) so a strict backend does not reject a usable answer.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _S(BaseModel):
    model_config = ConfigDict(extra="allow")


class Note(_S):
    email: int = Field(description="Email number [n] from the prompt")
    note: str = Field(description="One or two sentences: what this email adds")
    kind: Literal["decision", "issue", "request", "info", "done"] = "info"
    importance: Literal["important", "routine", "not_important"] = "routine"
    due: Optional[str] = Field(None, description="YYYY-MM-DD or null")
    ai: bool = Field(False, description="True if the email is about AI or automation work (models, LLMs, agents, prompts, n8n-style workflows)")


class Notes(_S):
    """S1: one note per email."""
    notes: list[Note]


class Item(_S):
    section: Optional[str] = Field(None, description="Section key (template structure only), e.g. plan, decisions, risks")
    text: str
    emails: list[int] = Field(default_factory=list, description="Email numbers this fact comes from")


class PageRoute(_S):
    page: str = Field(description="A candidate label P1, P2, … or 'new'")
    role: Literal["primary", "secondary"] = "primary"
    title: Optional[str] = Field(None, description="Only for page = new")
    level: Optional[Literal["task", "topic", "project"]] = None
    why: Optional[str] = None
    items: list[Item] = Field(default_factory=list)
    status: Optional[str] = None
    open_add: list[str] = Field(default_factory=list)
    open_close: list[str] = Field(default_factory=list)


class Route(_S):
    """S2: the pages these emails add knowledge to."""
    pages: list[PageRoute]


class SectionText(_S):
    section: str
    text: str
    removed_reason: str = ""


class Sections(_S):
    """S3 editor (and its confirm round): rewritten sections."""
    sections: list[SectionText]


class PageSection(_S):
    title: str = Field(description="The editor's own short section title")
    text: str


class PageOut(_S):
    """S3 editor, free structure: the whole page."""
    summary: str = Field(description="2–4 sentences: what this is, where it stands, what is next")
    sections: list[PageSection] = Field(default_factory=list)
    removed: str = Field("", description="Facts deliberately removed and why")


class Combined(_S):
    """S12 / S123 fast path: notes + routing (+ sections in live mode)."""
    notes: list[Note]
    pages: list[PageRoute] = Field(default_factory=list)
    sections: list[SectionText] = Field(default_factory=list)


class PageTags(_S):
    page: str
    system: list[str] = Field(default_factory=list)
    process: list[str] = Field(default_factory=list)
    site: list[str] = Field(default_factory=list)
    partner: list[str] = Field(default_factory=list)
    work_type: list[str] = Field(default_factory=list)


class TCode(_S):
    code: str
    is_tcode: bool


class TagsOut(_S):
    """S4 tagger."""
    pages: list[PageTags] = Field(default_factory=list)
    tcodes: list[TCode] = Field(default_factory=list)


class Finding(_S):
    section: str
    problem: str
    text: str = Field("", description="Corrected full section text")


class Review(_S):
    """Reviewer / wiki lint."""
    findings: list[Finding] = Field(default_factory=list)


class TranslatedLine(_S):
    n: int
    text: str


class Translation(_S):
    """One email, line by line."""
    subject: str = ""
    lines: list[TranslatedLine] = Field(default_factory=list)


class Lead(_S):
    lead: str


class StoryText(_S):
    text: str


class ChatAction(_S):
    """One chat round: call a tool or answer."""
    action: Literal["tool", "answer"]
    tool: Optional[str] = None
    args: dict = Field(default_factory=dict)
    answer: Optional[str] = None
    sources: list[int] = Field(default_factory=list)
    confidence: Optional[Literal["high", "medium", "low", "none"]] = None


class SubQ(_S):
    q: str
    source: str = "wiki"


class Plan(_S):
    """Deep mode planner."""
    subquestions: list[SubQ]


class Judge(_S):
    correct: int = Field(ge=0, le=1)
    faithful: int = Field(ge=0, le=1)
    reason: str = ""


class Abstain(_S):
    abstained: int = Field(ge=0, le=1)
    reason: str = ""


MODELS: dict[str, type[BaseModel]] = {
    "notes": Notes, "route": Route, "sections": Sections, "page": PageOut, "combined": Combined, "tags": TagsOut, "review": Review,
    "translation": Translation, "lead": Lead, "story": StoryText, "chat": ChatAction, "plan": Plan, "judge": Judge, "abstain": Abstain,
}


@lru_cache(maxsize=None)
def schema(name: str) -> dict:
    """JSON schema (dict) of one answer type; KeyError for unknown names."""
    m = MODELS[name]
    s = m.model_json_schema()
    s.setdefault("title", m.__name__)
    s.setdefault("description", (m.__doc__ or m.__name__).strip())
    return s
