"""Get JSON out of free text (the work APIs are text-only) and validate it with Pydantic.

Lenient by design, so small models don't burn a retry on formatting: code fences, prose around the JSON, trailing
commas, smart quotes, Python literals and raw newlines inside strings are all repaired. Only output that still
doesn't parse, or doesn't fit the schema after the stage's own script fixes, triggers a validation retry.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError


def _balanced(text: str, start: int) -> str | None:
    depth, i, in_str, esc = 0, start, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


def _repair(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = re.sub(r"(?<![\w\"])None(?![\w\"])", "null", s)
    s = re.sub(r"(?<![\w\"])True(?![\w\"])", "true", s)
    s = re.sub(r"(?<![\w\"])False(?![\w\"])", "false", s)
    # raw newlines / tabs inside strings → escaped
    out, in_str, esc = [], False, False
    for c in s:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            elif c == "\n":
                out.append("\\n")
                continue
            elif c == "\t":
                out.append("\\t")
                continue
        elif c == '"':
            in_str = True
        out.append(c)
    return "".join(out)


def extract_json(text: str) -> tuple[Any, str]:
    """Return (object, '') or (None, error message)."""
    if not text or not text.strip():
        return None, "the answer was empty"
    t = text.strip()
    t = re.sub(r"^```(?:json|JSON)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    candidates = []
    for m in re.finditer(r"[{\[]", t):
        blk = _balanced(t, m.start())
        if blk and len(blk) > 1:
            candidates.append(blk)
            if m.start() == 0 or len(blk) > len(t) * 0.5:
                break
    if not candidates:
        # unbalanced: try to close it
        i = min([x for x in (t.find("{"), t.find("[")) if x >= 0], default=-1)
        if i < 0:
            return None, "no JSON object found in the answer"
        blk = t[i:]
        blk += "}" * max(0, blk.count("{") - blk.count("}")) + "]" * max(0, blk.count("[") - blk.count("]"))
        candidates.append(blk)
    candidates.sort(key=len, reverse=True)
    last = ""
    for c in candidates[:3]:
        for variant in (c, _repair(c)):
            try:
                return json.loads(variant), ""
            except json.JSONDecodeError as e:
                last = f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}"
    return None, last or "invalid JSON"


def validate(obj: Any, model: type[BaseModel]) -> tuple[BaseModel | None, list[str]]:
    try:
        return model.model_validate(obj), []
    except ValidationError as e:
        errs = []
        for er in e.errors()[:12]:
            loc = ".".join(str(x) for x in er["loc"])
            errs.append(f"{loc}: {er['msg']}")
        return None, errs
