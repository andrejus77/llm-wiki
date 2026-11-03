"""System prompts for every stage. Kept short and explicit for small models: one job, a JSON example, the labels it
may use. The first line of each user prompt is `TASK: <stage>` (the mock adapter and the logs rely on it)."""
from __future__ import annotations

from llmwiki.conf import get_settings
from llmwiki.pipeline.templates import TEMPLATES


def _lang() -> str:
    return get_settings().pipeline.language


def _levels() -> str:
    return "\n".join(f"- {lvl}: " + ", ".join(k for k, _, _ in secs) for lvl, secs in TEMPLATES.items())


NOTE_RULES = """For each email [n] write:
- note: 1–2 sentences, at most {maxc} characters, concrete: who does/decides/asks what, with numbers, codes and dates from the email
- kind: decision | issue | request | info | done
- importance: judge what a team member needs to know, not how official the mail looks.
  important: a decision; a problem, incident, exception or delay; a new deadline or a changed plan; a request or question to the team;
    a commitment (who does what by when); numbers or facts someone will need later.
  routine: human coordination with little lasting value: acknowledgements, "thanks", "noted", meeting logistics, a short status without change.
  not_important: anything a system sends as a matter of course — confirmations (order, booking, delivery, approval, receipt, "your request was
    received"), notifications (PO waiting for approval, shipment delivered, job finished, password expiry, planned maintenance), newsletters,
    out-of-office. Also when it concerns an important topic: the topic matters, the notification does not.
    Exception: a system mail that reports an error, failure, rejection or deviation someone must act on is important.
- due: a deadline written in the email as YYYY-MM-DD, else null{ai_rule}"""

AI_RULE = """
- ai: true if the email is about AI or automation work — building, buying, testing or using AI models, LLMs, chatbots, agents, prompts,
  ML / forecast / OCR / classification models, AI vendors and tools, or workflow automation such as n8n flows. Judge by meaning, not by
  keywords: "the model reads the totals wrong on scanned invoices" is AI work without any keyword; a classic RPA bot, an SAP batch job or
  an Excel macro is not — unless the email is about adding AI to it. Example terms (not a checklist): {kw}. Otherwise false."""


def _note_rules() -> str:
    s = get_settings()
    ai = AI_RULE.format(kw=", ".join(s.departments.ai_keywords)) if s.departments.ai_address else ""
    return NOTE_RULES.format(maxc=s.pipeline.note_max_chars, ai_rule=ai)


def _note_example() -> str:
    ai = ', "ai": false' if get_settings().departments.ai_address else ""
    return '{"email": 1, "note": "...", "kind": "issue", "importance": "important", "due": null' + ai + "}"


def s1_system() -> str:
    return ("You write short factual notes about emails for a team knowledge base of a logistics department.\n"
            + _note_rules() +
            f"\nRules: use only facts from the emails. Emails may be in Korean or another language; always write in {_lang()}. "
            "Quoted earlier mail is context only: note what THIS email adds.\n"
            'Return JSON only, exactly like: {"notes": [' + _note_example() + "]}")


ROUTE_RULES = """Choose the wiki pages these emails add knowledge to, ONLY from the candidate pages P1, P2, … listed below.
- Pages are about lasting topics, projects, processes or recurring tasks — never about one document number. A single PO, invoice or delivery
  belongs on the page of the topic or process it is part of. Use an existing candidate only if it is about the SAME topic —
  the same site, partner or system alone is not enough (a credit block in Venlo is not the Venlo pallet supply topic).
- One "primary" page (the main topic) and at most {sec} "secondary" pages that the emails also inform.
- Use "page": "new" only if no candidate is about this topic; then also give "title" (short, specific), "level" and "why".
  Level — by the nature of the work, not by how much mail there is yet:
    project: a time-bound effort with a goal and an end — a go-live, migration, rollout, implementation, tender, relocation, audit
      preparation — with several people and dated milestones.
    topic: an ongoing subject without an end date — a partner relationship, a recurring problem area, a process or system the team runs.
    task: one concrete piece of work or a how-to — a single change or access request, a one-off correction, a recurring routine step.
  (A script also checks the size: a page only shows as a project or topic once enough people, days and emails are on it.)
- Never create a page for automated notifications or confirmations, a question answered in the same email, thanks, or out-of-office.
{items_rule}
- status of the page, only if these emails change it, else null:
    On track = progressing as planned · At risk = a deadline or goal is threatened (delay, missing input, unresolved problem with a date) ·
    Blocked = cannot continue until someone else acts · Done = finished and confirmed · Open = ongoing work without a plan to track.
  open_add: new unresolved points (short). open_close: codes (o-N) of listed open issues now resolved.
- Routine mail can add nothing: then return {{"pages": []}}."""


ITEMS_TEMPLATE = """- items: self-contained facts in {lang} (who, what, when, why, numbers), each for one section key of that page's level:
{levels}
  and "emails": the numbers [n] of the emails it comes from."""
ITEMS_FREE = """- items: the NEW facts these emails add to that page, in {lang}: self-contained (who, what, when, why, numbers), one fact per item,
  only what a colleague needs later (decisions and reasons, changes, numbers, dates, how something works) — not greetings, thanks or
  "X asked Y to confirm" once the answer is in the same emails. "emails": the numbers [n] it comes from. The editor places them on the page."""


def _route_rules() -> str:
    from llmwiki.pipeline.templates import free
    p = get_settings().pipeline
    items = ITEMS_FREE.format(lang=_lang()) if free() else ITEMS_TEMPLATE.format(lang=_lang(), levels=_levels())
    return ROUTE_RULES.format(sec=p.max_pages_per_unit - 1, items_rule=items)


def _item_example() -> str:
    from llmwiki.pipeline.templates import free
    return '{"text": "...", "emails": [1, 2]}' if free() else '{"section": "plan", "text": "...", "emails": [1, 2]}'


def s2_system() -> str:
    return ("You route emails into a team wiki of a logistics department.\n" + _route_rules() +
            '\nReturn JSON only, like: {"pages": [{"page": "P1", "role": "primary", "items": [' + _item_example() + '], '
            '"status": "At risk", "open_add": ["..."], "open_close": ["o-3"]}, {"page": "new", "title": "...", "level": "topic", "why": "...", "role": "secondary", "items": [...]}]}')


def s12_system(edit: bool) -> str:
    base = ("You process emails for a team wiki of a logistics department: first write a note per email, then route them to wiki pages.\n"
            + _note_rules() + "\n\nThen:\n" + _route_rules()
            + "\nIf every email is not_important, return no pages.")
    if edit:
        base += ("\n\nFinally, for the PRIMARY page only: rewrite the sections shown under 'Current sections' that your items belong to, "
                 "as complete new section texts that keep everything still true and add the new facts (explain for a new colleague; absolute dates; no headings). "
                 'Give them in "sections": [{"section": "plan", "text": "...", "removed_reason": ""}]. If a section gets much shorter, say why in removed_reason.')
    ex = ('{"notes": [' + _note_example() + '], '
          '"pages": [{"page": "P1", "role": "primary", "items": [' + _item_example() + '], "status": null, "open_add": [], "open_close": []}]'
          + (', "sections": [{"section": "plan", "text": "...", "removed_reason": ""}]' if edit else "") + "}")
    return base + f"\nRules: use only facts from the emails; always write in {_lang()} even if the email is Korean.\nReturn JSON only, like: {ex}"


def s3_system() -> str:
    g = get_settings().guards
    markers = ("You may mark where a fact comes from with the email number in brackets, like [2] (optional). " if g.inline_markers == "optional" else "")
    return ("You maintain one page of a team wiki for a logistics department. Rewrite each listed section so it includes the new items.\n"
            "- Keep everything that is still true; replace what the new items make outdated (say what changed and when).\n"
            "- Write for a new colleague: explain what it is, how it works, who does what, and WHY decisions were made.\n"
            "- Say each fact once: do not repeat what the other sections of the page (outline) already say. Never write about the sources "
            "themselves ('the sources do not show …').\n"
            f"- Absolute dates (e.g. 17 Nov 2026), no 'next week'. No headings inside a section. Plain prose, short bullet lists where useful. At most {g.max_words} words per section.\n"
            f"- {markers}Write in {_lang()}.\n"
            "- People and roles: only real people (name: role); department mailboxes and distribution lists are not people.\n"
            "- Write as of the date of the newest source email; do not guess what happened after it or what 'today' is.\n"
            "- If a section becomes much shorter than before, explain why in removed_reason (otherwise leave it empty).\n"
            'Return JSON only, like: {"sections": [{"section": "plan", "text": "...", "removed_reason": ""}]}')


PAGE_RULES = """You maintain one page of a team wiki for a logistics department. Busy colleagues read the page INSTEAD of the emails,
so it must be much shorter than the mail and say every fact once. Write the whole page anew from the current page and the new facts.
- summary: 2–4 sentences, at most {sw} words: what this is, where it stands now, what happens next and who owns it.
- sections: at most {k}, each with your own short title that fits THIS topic (e.g. "How the check works", "Decisions", "Costs",
  "Plan to go-live", "Open questions"), at most {secw} words each. Use only as many as the facts need: a small topic may need one
  section or none. Keep the titles of the current page unless the content changed.
- The whole page at most {budget} words (summary + sections).
- Each fact appears ONCE. The sections must not repeat the summary or each other. Put a fact where a reader would look for it.
- Newest state wins: replace an outdated statement (say in a few words what changed and when) instead of keeping old and new side by side.
- Keep what people need later: decisions and why, numbers, dates, codes, names with roles, how things work. Drop narration of who asked
  whom once the outcome is known, greetings, and the open issues listed below (the page shows them already).
- Never write about the sources or the page itself ("the sources do not show …", "see the How section", "no information is given",
  "the result is not recorded"). If something is unknown, leave it out.
- Full, plain sentences a colleague reads quickly — short, but not note-style fragments. Short bullet lists for steps or lists.
  Absolute dates (e.g. 17 Nov 2026). No headings inside a text. Write in {lang}.
- Write as of the date of the newest source email; do not guess what happened after it.
- {markers}If you remove facts that are still true, say which and why in "removed" (e.g. "merged repeated facts", "old step-by-step detail").
Return JSON only, like: {{"summary": "...", "sections": [{{"title": "...", "text": "..."}}], "removed": ""}}"""


def page_system(budget: int) -> str:
    s = get_settings()
    markers = ("Mark where a fact comes from with the email number in brackets, like [2], where useful (optional). "
               if s.guards.inline_markers == "optional" else "")
    return PAGE_RULES.format(sw=s.pages.summary_words, k=s.pages.max_sections, secw=s.pages.section_words, budget=budget, lang=_lang(), markers=markers)


def page_confirm_system() -> str:
    return ("You are checking your own rewrite of a wiki page. You get the ORIGINAL page, your DRAFT, the checker's notes and the new facts.\n"
            "Produce the FINAL page: fix every checker note — shorten to the limits given by merging repeated facts and dropping the least useful "
            "detail; keep every new fact and every decision, number and date that is still true. Each fact once. Same rules as before: a summary, "
            "your own section titles, no talk about the sources. If you deliberately removed something that is still true, say what in \"removed\".\n"
            'Return JSON only, like: {"summary": "...", "sections": [{"title": "...", "text": "..."}], "removed": ""}')


def s3_confirm_system() -> str:
    return ("You are checking your own edit of a wiki section. You get the ORIGINAL text, your DRAFT, the checker's notes and the new items.\n"
            "Produce the FINAL text: it must keep every fact of the original that is still true, include the new items, and fix the checker's notes. "
            "If you deliberately removed something, say why in removed_reason.\n"
            'Return JSON only, like: {"sections": [{"section": "plan", "text": "...", "removed_reason": ""}]}')


def tags_system(catalog: dict[str, list[str]], work_types: list[str], min_tags: int) -> str:
    def known(ty):
        return ", ".join(catalog.get(ty) or []) or "(none yet)"
    return ("You tag wiki pages of a company department, so that people can find them. For each page name the real things it is about.\n"
            f"- system: software, tools, platforms, IT systems. Known: {known('system')}\n"
            f"- process: business processes. Known: {known('process')}\n"
            f"- site: sites, plants, warehouses, subsidiaries, countries. Known: {known('site')}\n"
            f"- partner: outside companies, suppliers, customers. Known: {known('partner')}\n"
            f"- work_type: exactly one of {', '.join(work_types)}\n"
            "Rules:\n"
            "- A known value: spelled as above, whenever this page is about it, also when the page never writes the word. Think about what the page implies: "
            "a t-code means SAP, a module means its system, a warehouse means its site. Do not add one the page has nothing to do with.\n"
            "- Anything else is new: copy it EXACTLY as the page writes it, with no translating, expanding, abbreviating or guessing. "
            "A script checks this: if those exact words are not on the page, the new tag is thrown away.\n"
            "- Short names, not sentences.\n"
            + (f"- Name everything the page supports, at least {min_tags} tags if it has that many real things. Fewer is right when it has fewer: never invent one to reach a number.\n" if min_tags else "- Name everything the page supports.\n")
            + "For each listed t-code candidate say whether it is an SAP transaction code (is_tcode true/false) from its context.\n"
            'Return JSON only, like: {"pages": [{"page": "P1", "system": ["…"], "process": ["…"], "site": ["…"], "partner": ["…"], "work_type": ["…"]}], '
            '"tcodes": [{"code": "ZMM01", "is_tcode": true}]}')


def translate_system(language: str, source: str) -> str:
    return (f"You translate one email of a company department into {language}, for colleagues who read it instead of the original (written mostly in {source}). "
            "The background tells you what it is about; use it to choose the right words, but add nothing from it.\n"
            "Translate each numbered line into exactly one line with the same number. Translate everything said, keep the tone, summarise nothing, explain nothing.\n"
            "Keep exactly as they are: markers like ⟦3⟧, names of people, system names, codes, numbers, dates, amounts, and anything already in "
            f"{language}. A line starting with | is a table row: translate inside the cells and keep every | character.\n"
            f'Return JSON only, like: {{"subject": "translated subject", "lines": [{{"n": 1, "text": "translated line"}}, {{"n": 2, "text": "…"}}]}}')


def report_lead_system(lo: int, hi: int) -> str:
    return (f"You write the opening paragraph of one team's report (the team and the dates are given): 3–5 sentences, {lo}–{hi} characters, in {_lang()}. "
            "Say what mattered most in this window and why, using only the stories given — they are this team's topics only. "
            "No greeting, no bullet points, nothing from before the window.\n"
            'Return JSON only, like: {"lead": "..."}')


def report_story_system(lo: int, hi: int) -> str:
    return (f"You write one card of a team report: what happened on this topic INSIDE the report window and what it means, {lo}–{hi} characters "
            f"(about 60–120 words), in {_lang()}. Concrete: who, what, numbers, dates, what is still open.\n"
            "Use only the facts under 'In the window'. 'Background' only tells you what the topic is — never report it as news, "
            "and do not mention anything that happened before the window.\n"
            'Return JSON only, like: {"text": "..."}')


def review_system() -> str:
    return ("You review one wiki page of a logistics department for problems. Check: contradictions between its sections; "
            "contradictions with the similar sections from other pages shown; relative dates that should be absolute; outdated statements; "
            "the same fact repeated in several sections (keep it in the one where it belongs, remove it from the others).\n"
            "For each problem return the section key, the problem, and a corrected full section text.\n"
            'Return JSON only, like: {"findings": [{"section": "plan", "problem": "...", "text": "corrected full section text"}]} (empty list if none)')
