"""The RAG chat agent.

Fast mode (default):
  retrieve  — hybrid search on the question + the open view, BEFORE the first model call → sources [1..k]
  agent     — one LLM round: either {"action":"tool",...} or {"action":"answer",...}
  tools     — run the tool, number its evidence, back to agent (≤ chat_rounds rounds; the last round must answer)
  finalize  — map [n] markers to sources, drop unknown numbers
Deep mode (multi-agent):
  planner → researchers in parallel (each a short agent loop on its own sub-question) → writer → finalize

Two interchangeable engines (settings.chat.engine): `langgraph` (StateGraph; deep mode fans out with Send) and
`plain` (the same node functions in a Python loop / thread pool). Every LLM call goes through the pool with chat priority.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, TypedDict
import operator

from llmwiki.chat.tools import TOOLS_DOC, Sources, add_hits, run_tool
from llmwiki.conf import get_settings
from llmwiki.llm.pool import StageFailed, pool
from llmwiki.pipeline.budget import Section, assemble
from llmwiki.rag.search import hybrid

log = logging.getLogger(__name__)


def system_prompt(final_round: bool) -> str:
    c = get_settings().chat
    lang = get_settings().pipeline.language
    return ("You are the chat assistant of a logistics department's knowledge base (a wiki written from the department's email).\n"
            + ("Answer from the wiki sections first; use notes and emails for specifics (dates, numbers, who said what).\n" if c.wiki_first else "")
            + "Cite the numbered sources you used inline like [2]. If wiki and email disagree, say so and cite both. "
            "If the sources do not contain the answer, say it is not in the wiki — never guess.\n"
            + ("This is the LAST round: answer now with what you have.\n" if final_round else TOOLS_DOC + "\nIf the sources already answer the question, answer. Otherwise call ONE tool.\n")
            + f"Write in {lang}, concise (≤ 180 words), with short bullet lists where useful.\n"
            + ('Return JSON only: {"action": "answer", "answer": "...", "sources": [1, 3], "confidence": "high|medium|low|none"}'
               + ("" if final_round else ' or {"action": "tool", "tool": "search_emails", "args": {"query": "...", "days": 30}}')))


def parse_action(o, final: bool):
    if not isinstance(o, dict):
        return None, ["answer with a JSON object"]
    act = str(o.get("action") or ("answer" if o.get("answer") else "")).lower()
    if act == "tool" and not final:
        if not o.get("tool"):
            return None, ['a tool call needs "tool"']
        return {"action": "tool", "tool": str(o["tool"]), "args": o.get("args") or {}}, []
    ans = str(o.get("answer") or "").strip()
    if not ans:
        return None, ['give "answer" (or call a tool)']
    return {"action": "answer", "answer": ans, "sources": o.get("sources") or [], "confidence": str(o.get("confidence") or "medium")}, []


# ------------------------------------------------------------------ shared node functions
class ChatState(TypedDict, total=False):
    question: str
    view: str
    history: str
    sources: Any                # Sources (in-process only)
    round: int
    tool_log: list
    action: dict
    answer: str
    confidence: str
    used: list
    steps: Annotated[list, operator.add]
    progress: Any               # callback(step_label)
    max_rounds: int
    # deep mode
    subquestions: list
    subq: dict
    evidence: Annotated[list, operator.add]


def node_retrieve(st: ChatState) -> dict:
    r = get_settings().retrieval
    q = st["question"] + ("\n" + st["view"][:300] if st.get("view") else "")
    src: Sources = st["sources"]
    hits = hybrid(q, top_k=r.top_k)
    add_hits(src, hits)
    _p(st, f"pre-retrieval: {len(hits)} chunks (hybrid, wiki first)")
    return {"round": 0, "steps": [f"retrieved {len(hits)} chunks"]}


def _p(st, label):
    cb = st.get("progress")
    if cb:
        try:
            cb(label)
        except Exception:
            pass


def node_agent(st: ChatState) -> dict:
    s = get_settings()
    b = s.budgets.chat
    rnd = st.get("round", 0) + 1
    max_r = st.get("max_rounds") or s.budgets.chat_rounds
    final = rnd >= max_r
    system = system_prompt(final)
    src: Sources = st["sources"]

    def build(cap, scale):
        secs = [Section("view", "What the user has open", [st["view"]] if st.get("view") else [], b["view"]),
                Section("history", "Earlier in this conversation", [st["history"]] if st.get("history") else [], b["history"]),
                Section("tools", "Tool calls so far", ["\n".join(st.get("tool_log") or [])] if st.get("tool_log") else [], 1000),
                Section("retrieved", "Sources", [x.render() for x in src.items], b["retrieved"] + b["tools"])]
        text, meta = assemble(secs, cap, ["history", "view", "retrieved"], system, scale)
        return system, f"TASK: chat\nROUND {rnd} of {max_r}\nQuestion: {st['question']}\n\n" + text, meta

    _p(st, f"round {rnd}: thinking")
    try:
        res = pool().run_structured("chat", f"chat:{st['question'][:60]}", build, lambda o: parse_action(o, final), chat=True)
        act = res.obj
    except StageFailed as e:
        log.warning("chat round failed: %s", e)
        act = {"action": "answer", "answer": "Sorry — the model did not return a usable answer. Please try again.", "sources": [], "confidence": "none"}
    return {"round": rnd, "action": act, "steps": [f"round {rnd}: {act['action']}" + (f" {act.get('tool')}" if act["action"] == "tool" else "")]}


def node_tools(st: ChatState) -> dict:
    s = get_settings()
    act = st["action"]
    _p(st, f"tool: {act['tool']} {json.dumps(act.get('args'), ensure_ascii=False)[:80]}")
    text, new = run_tool(act["tool"], act.get("args") or {}, st["sources"], s.retrieval.top_k, s.budgets.chat["tool"])
    log_line = f"round {st['round']}: {act['tool']}({json.dumps(act.get('args'), ensure_ascii=False)[:120]}) → {len(new)} new sources"
    return {"tool_log": (st.get("tool_log") or []) + [log_line], "steps": [log_line]}


def node_finalize(st: ChatState) -> dict:
    act = st.get("action") or {}
    src: Sources = st["sources"]
    ans = act.get("answer", "")
    nums = {int(n) for n in re.findall(r"\[(\d+)\]", ans)} | {int(x) for x in act.get("sources", []) if str(x).isdigit()}
    valid = sorted(n for n in nums if src.get(n))
    ans = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if src.get(m.group(1)) else "", ans)
    conf = act.get("confidence", "medium")
    if not valid and conf not in ("none", "low"):
        conf = "low"
    return {"answer": ans.strip(), "used": valid, "confidence": conf, "steps": [f"answer with {len(valid)} sources"]}


def route_after_agent(st: ChatState) -> str:
    return "tools" if (st.get("action") or {}).get("action") == "tool" else "finalize"


# ------------------------------------------------------------------ deep mode nodes
def node_plan(st: ChatState) -> dict:
    s = get_settings()
    system = ("You plan research for a question about a logistics department's wiki and email. Split the question into at most "
              f"{s.chat.deep_max_subq} focused sub-questions, each with the best source: wiki, notes or emails.\n"
              'Return JSON only: {"subquestions": [{"q": "...", "source": "wiki"}]}')

    def build(cap, scale):
        return system, f"TASK: plan\nQuestion: {st['question']}\n\n" + (f"## What the user has open\n{st['view'][:3000]}" if st.get("view") else ""), []

    def parse(o):
        if isinstance(o, dict) and isinstance(o.get("subquestions"), list) and o["subquestions"]:
            return o, []
        return None, ['give a "subquestions" list']
    _p(st, "planner: splitting the question")
    try:
        res = pool().run_structured("chat", "deep:plan", build, parse, chat=True, schema="plan")
        subs = [{"q": str(x.get("q") or x), "source": str(x.get("source") or "wiki")} for x in res.obj["subquestions"]][: s.chat.deep_max_subq]
    except StageFailed:
        subs = [{"q": st["question"], "source": "wiki"}]
    return {"subquestions": subs, "steps": [f"planner: {len(subs)} sub-questions"]}


def research(sub: dict, shared: Sources, view: str, progress=None) -> dict:
    """One researcher: a short agent loop on its own sub-question; returns evidence notes with source numbers."""
    s = get_settings()
    local = ChatState(question=sub["q"], view="", history="", sources=shared, round=0, tool_log=[], progress=progress,
                      max_rounds=s.chat.deep_research_rounds + 1)
    coll = {"wiki": ["wiki"], "notes": ["notes"], "emails": ["emails", "attachments"]}.get(sub.get("source"), None)
    add_hits(shared, hybrid(sub["q"], {"collections": coll} if coll else None, top_k=max(4, s.retrieval.top_k // 2)))
    for _ in range(s.chat.deep_research_rounds + 1):
        local.update(node_agent(local))
        if route_after_agent(local) == "tools":
            local.update(node_tools(local))
        else:
            break
    fin = node_finalize(local)
    return {"q": sub["q"], "notes": fin["answer"], "sources": fin["used"]}


def node_research(st: ChatState) -> dict:  # used by LangGraph's Send fan-out
    ev = research(st["subq"], st["sources"], st.get("view", ""), st.get("progress"))
    return {"evidence": [ev], "steps": [f"researcher: {ev['q'][:60]} → {len(ev['sources'])} sources"]}


def node_write(st: ChatState) -> dict:
    s = get_settings()
    src: Sources = st["sources"]
    system = ("You write the final answer from the researchers' evidence only. Cite sources inline like [2]. "
              "If the evidence does not answer the question, say so. Concise (≤ 220 words), bullet lists where useful.\n"
              'Return JSON only: {"action": "answer", "answer": "...", "sources": [1, 3], "confidence": "high|medium|low|none"}')
    ev = st.get("evidence") or []

    def build(cap, scale):
        used = sorted({n for e in ev for n in e["sources"]})
        secs = [Section("ev", "Evidence from the researchers", [f"Sub-question: {e['q']}\n{e['notes']}" for e in ev], 8000),
                Section("retrieved", "Sources", [src.get(n).render(1200) for n in used if src.get(n)], s.budgets.chat["retrieved"])]
        text, meta = assemble(secs, cap, ["retrieved"], system, scale)
        return system, f"TASK: write\nQuestion: {st['question']}\n\n" + text, meta
    _p(st, "writer: composing the answer")
    try:
        res = pool().run_structured("chat", "deep:write", build, lambda o: parse_action(o, True), chat=True)
        act = res.obj
    except StageFailed:
        act = {"action": "answer", "answer": "\n\n".join(e["notes"] for e in ev) or "Not in the wiki.", "sources": [], "confidence": "low"}
    return {"action": act, "steps": ["writer done"]}


# ------------------------------------------------------------------ engines
_graphs: dict[str, Any] = {}


def langgraph_fast():
    if "fast" not in _graphs:
        from langgraph.graph import END, START, StateGraph
        g = StateGraph(ChatState)
        g.add_node("retrieve", node_retrieve)
        g.add_node("agent", node_agent)
        g.add_node("tools", node_tools)
        g.add_node("finalize", node_finalize)
        g.add_edge(START, "retrieve")
        g.add_edge("retrieve", "agent")
        g.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "finalize": "finalize"})
        g.add_edge("tools", "agent")
        g.add_edge("finalize", END)
        _graphs["fast"] = g.compile()
    return _graphs["fast"]


def langgraph_deep():
    if "deep" not in _graphs:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Send
        g = StateGraph(ChatState)
        g.add_node("plan", node_plan)
        g.add_node("research", node_research)
        g.add_node("write", node_write)
        g.add_node("finalize", node_finalize)
        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", lambda st: [Send("research", {**st, "subq": sq}) for sq in st["subquestions"]], ["research"])
        g.add_edge("research", "write")
        g.add_edge("write", "finalize")
        g.add_edge("finalize", END)
        _graphs["deep"] = g.compile()
    return _graphs["deep"]


def run_plain(st: ChatState, deep: bool) -> ChatState:
    if deep:
        st.update(node_plan(st))
        with ThreadPoolExecutor(max_workers=len(st["subquestions"])) as ex:
            evs = list(ex.map(lambda sq: research(sq, st["sources"], st.get("view", ""), st.get("progress")), st["subquestions"]))
        st["evidence"] = evs
        st.update(node_write(st))
        st.update(node_finalize(st))
        return st
    st.update(node_retrieve(st))
    while True:
        st.update(node_agent(st))
        if route_after_agent(st) == "tools":
            st.update(node_tools(st))
            continue
        break
    st.update(node_finalize(st))
    return st


def answer(question: str, view: str = "", history: str = "", mode: str = "fast", progress=None) -> dict:
    s = get_settings()
    deep = mode == "deep" and s.chat.deep_enabled
    st = ChatState(question=question, view=view, history=history, sources=Sources(), round=0, tool_log=[], steps=[], progress=progress,
                   evidence=[], max_rounds=s.budgets.chat_rounds)
    engine = s.chat.engine
    if engine == "langgraph":
        try:
            g = langgraph_deep() if deep else langgraph_fast()
            out = g.invoke(st, {"recursion_limit": 40})
        except Exception as e:
            log.exception("LangGraph engine failed (%s) — falling back to the plain loop", e)
            engine = "plain (fallback)"
            st = ChatState(question=question, view=view, history=history, sources=Sources(), round=0, tool_log=[], steps=[], progress=progress,
                           evidence=[], max_rounds=s.budgets.chat_rounds)
            out = run_plain(st, deep)
    else:
        out = run_plain(st, deep)
    src: Sources = out["sources"]
    return {"answer": out.get("answer", ""), "confidence": out.get("confidence", ""), "engine": engine, "mode": "deep" if deep else "fast",
            "sources": [x.public() for x in src.items if x.n in set(out.get("used") or [])],
            "all_sources": [x.public() for x in src.items], "steps": out.get("steps", []),
            "evidence": [x.render(2000) for x in src.items if x.n in set(out.get("used") or [])] or [x.render(1200) for x in src.items[:8]],
            "subquestions": out.get("subquestions", []), "tool_log": out.get("tool_log", [])}
