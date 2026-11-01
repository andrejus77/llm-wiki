"""Page templates by level. Section keys are what the model writes ("plan", "decisions" ...); labels are for people."""
from __future__ import annotations

import difflib

TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "task": [("what", "What it is", "What the task is and when it comes up"),
             ("how", "How to do it", "Step by step, with t-codes, screens and who to ask"),
             ("status", "Status", "Current state, open points, next dates")],
    "topic": [("overview", "Overview", "What this topic is about and why it matters (first sentence = one-line description)"),
              ("background", "Background", "History and context a newcomer needs"),
              ("how", "How it works", "Process, systems, steps and rules"),
              ("decisions", "Decisions and reasons", "Each decision with date, who decided and why"),
              ("people", "People and roles", "One line per person: their role in this topic"),
              ("open", "Open issues", "What is unresolved, waiting on whom, by when")],
    "project": [("overview", "Overview", "Goal and current state (first sentence = one-line description)"),
                ("goals", "Goals and scope", "What is in and out of scope"),
                ("plan", "Plan and milestones", "Dated milestones and changes to them"),
                ("decisions", "Decisions and reasons", "Each decision with date, who decided and why"),
                ("risks", "Risks and open issues", "Risks, blockers and unresolved points"),
                ("people", "People and roles", "One line per person: their role in this project"),
                ("systems", "Systems involved", "Systems, interfaces, t-codes")],
}
LEVELS = list(TEMPLATES)
STATUSES = ["On track", "At risk", "Open", "Blocked", "Done"]


def keys(level: str) -> list[str]:
    return [k for k, _, _ in TEMPLATES.get(level, TEMPLATES["topic"])]


def label_of(level: str, key: str) -> str:
    for k, label, _ in TEMPLATES.get(level, TEMPLATES["topic"]):
        if k == key:
            return label
    return key


def section_label(level: str, key: str, title: str = "") -> str:
    """What people see as a section's heading: the editor's own title (free structure), else the template label."""
    if title:
        return title
    if key == "summary":
        return "Summary"
    return label_of(level, key)


def free() -> bool:
    from llmwiki.conf import get_settings
    return get_settings().pages.structure == "free"


def fuzzy_key(level: str, key: str) -> str | None:
    """Map a model-written section key (or label) to a template key; None if nothing is close."""
    k = (key or "").strip().lower().replace(" ", "_")
    ks = keys(level)
    if k in ks:
        return k
    labels = {label.lower(): kk for kk, label, _ in TEMPLATES.get(level, TEMPLATES["topic"])}
    if key and key.strip().lower() in labels:
        return labels[key.strip().lower()]
    alias = {"plan": "plan", "milestones": "plan", "status": "status", "risks": "risks" if "risks" in ks else "open",
             "open_issues": "open" if "open" in ks else "risks", "issues": "open" if "open" in ks else "risks",
             "decision": "decisions", "people": "people", "roles": "people", "summary": "overview" if "overview" in ks else "what",
             "how_to": "how", "steps": "how", "goal": "goals", "scope": "goals", "system": "systems", "background": "background"}
    if k in alias and alias[k] in ks:
        return alias[k]
    m = difflib.get_close_matches(k, ks + list(labels), n=1, cutoff=0.6)
    if m:
        return m[0] if m[0] in ks else labels[m[0]]
    return None


def describe(level: str) -> str:
    return "\n".join(f"- {k}: {label} — {hint}" for k, label, hint in TEMPLATES.get(level, TEMPLATES["topic"]))
