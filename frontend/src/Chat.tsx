import { useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { get, send } from "./api";
import { usePeek } from "./drawer";
import { useLocal } from "./util";

type Msg = { id: number; question: string; status: string; answer: string; sources: any[]; steps: any[]; confidence: string; mode: string; error?: string; seconds?: number; feedback?: number };

/** What the user has open → sent with the question as the chat's view context. */
export function viewOf(path: string): { kind: string; id?: string; label: string } {
  const p = path.split("/").filter(Boolean);
  if (p[0] === "wiki" && p[1]) return { kind: "page", id: decodeURIComponent(p[1]), label: "Wiki page" };
  if (p[0] === "emails" && p[1]) return { kind: "email", id: p[1], label: "Email" };
  if (p[0] === "threads" && p[1]) return { kind: "thread", id: p[1], label: "Thread" };
  if (p[0] === "wiki") return { kind: "list", label: "Wiki list" };
  if (p[0] === "emails") return { kind: "list", label: "Email list" };
  if (p[0] === "agent") return { kind: "agent", label: "Agent › " + (p[1] || "logs") };
  const period = new URLSearchParams(location.search).get("period");
  return { kind: "report", id: period || undefined, label: "Weekly briefing" };
}

function AnswerText({ m }: { m: Msg }) {
  const peek = usePeek();
  const byN = new Map(m.sources.map((s: any) => [s.n, s]));
  const parts = m.answer.split(/(\[\d+\])/g);
  return <div>{parts.map((x, i) => {
    const mm = x.match(/^\[(\d+)\]$/);
    if (!mm) return <span key={i} style={{ whiteSpace: "pre-wrap" }}>{x}</span>;
    const s: any = byN.get(+mm[1]);
    return <button key={i} className="cite" title={s?.title} onClick={() => s && (s.page_id ? peek({ kind: "page", id: s.page_id }) : s.email_id && peek({ kind: "email", id: s.email_id }))}>{mm[1]}</button>;
  })}</div>;
}

export default function Chat() {
  const loc = useLocation();
  const peek = usePeek();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useLocal<"fast" | "deep">("llmwiki.chatmode", "fast");
  const [session, setSession] = useState<number | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const body = useRef<HTMLDivElement>(null);
  const view = viewOf(loc.pathname);
  const running = msgs.some((m) => m.status === "queued" || m.status === "running");

  // deep link: ?chat=<message id> opens the chat with that (saved) answer
  useEffect(() => {
    const id = new URLSearchParams(location.search).get("chat");
    if (id) get<Msg>(`/chat/${id}`).then((m) => { setMsgs([m]); setSession((m as any).session_id); setOpen(true); }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!running) return;
    const t = setInterval(async () => {
      const upd = await Promise.all(msgs.map((m) => (m.status === "queued" || m.status === "running" ? get<Msg>(`/chat/${m.id}`) : Promise.resolve(m))));
      setMsgs(upd);
    }, 1000);
    return () => clearInterval(t);
  }, [running, msgs]);
  useEffect(() => { body.current?.scrollTo(0, body.current.scrollHeight); }, [msgs, open]);

  async function ask(question: string) {
    if (!question.trim()) return;
    const r = await send("POST", "/chat", { question, mode, view, session_id: session });
    setSession(r.session_id);
    setMsgs((m) => [...m, { id: r.id, question, status: "queued", answer: "", sources: [], steps: [], confidence: "", mode }]);
    setQ("");
  }
  const sugg = view.kind === "page" ? ["Explain this page simply", "What is still open here?", "Who is involved?"]
    : view.kind === "email" || view.kind === "thread" ? ["Summarise this thread", "What was decided?", "What happens next?"]
    : ["What needs my attention this week?", "Which topics are at risk?", "What was decided about the customs go-live?"];

  if (!open) return <button className="cw-fab" onClick={() => setOpen(true)}>Ask the wiki</button>;
  return (
    <section className="cw" aria-label="Chat">
      <div className="cw-h">
        <div className="r1"><b>Ask the wiki</b>
          <button type="button" onClick={() => { setMsgs([]); setSession(null); }}>Clear</button>
          <button type="button" onClick={() => setOpen(false)} aria-label="Minimise chat">—</button></div>
        <div className="cw-ctx"><span>Context:</span><span className="c">{view.label}{view.id ? ` · ${view.id}` : ""}</span>
          <span className="cw-mode"><span className="seg">{(["fast", "deep"] as const).map((v) => <button key={v} aria-pressed={mode === v} onClick={() => setMode(v)}>{v === "fast" ? "Fast" : "Deep"}</button>)}</span></span></div>
      </div>
      <div className="cw-b" ref={body}>
        {!msgs.length && <div className="note" style={{ margin: 0 }}>I see what's open on the left ({view.label}). Ask about it, or about anything in the wiki. <b>Deep</b> splits the question into sub-questions researched in parallel (slower).</div>}
        {msgs.map((m) => (
          <div key={m.id}>
            <div className="msg-q">{m.question}</div>
            <div className="msg-a">
              {m.status === "done" ? <AnswerText m={m} /> : m.status === "failed" ? <span className="sim">Failed: {m.error}</span>
                : <span className="sim">{m.steps.length ? m.steps[m.steps.length - 1].label : "queued"}…</span>}
              {m.status === "done" && m.sources.length > 0 && <div className="srcs">{m.sources.map((s: any) => (
                <button key={s.n} onClick={() => s.page_id ? peek({ kind: "page", id: s.page_id }) : s.email_id && peek({ kind: "email", id: s.email_id })}>
                  [{s.n}] {s.kind === "wiki" ? "📄" : "✉"} {s.title.slice(0, 70)}</button>))}</div>}
              {m.status === "done" && <details className="trace"><summary>{m.mode} · {m.seconds ?? "?"} s · confidence {m.confidence || "?"}</summary>
                {m.steps.map((s: any, i: number) => <div key={i}>{s.label}{s.detail ? <ul>{s.detail.map((d: string, j: number) => <li key={j}>{d}</li>)}</ul> : null}</div>)}</details>}
              {m.status === "done" && <div className="btn-row">
                <button className="btn small" aria-pressed={m.feedback === 1} onClick={() => { send("POST", `/chat/${m.id}/feedback`, { value: 1 }); setMsgs(msgs.map((x) => x.id === m.id ? { ...x, feedback: 1 } : x)); }}>👍</button>
                <button className="btn small" aria-pressed={m.feedback === -1} onClick={() => { send("POST", `/chat/${m.id}/feedback`, { value: -1 }); setMsgs(msgs.map((x) => x.id === m.id ? { ...x, feedback: -1 } : x)); }}>👎</button></div>}
            </div>
          </div>
        ))}
        <div className="sugg">{sugg.map((x) => <button key={x} onClick={() => ask(x)} disabled={running}>{x}</button>)}</div>
      </div>
      <form className="cw-in" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about what's on screen…" autoComplete="off" />
        <button className="btn primary" disabled={running}>Ask</button></form>
    </section>
  );
}
