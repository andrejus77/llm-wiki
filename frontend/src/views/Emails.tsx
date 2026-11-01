import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { usePeek } from "../drawer";
import { addDays, Avatar, dow, Empty, Err, fmt, ImpPill, Kind, Loading, Prose, Seg, tColor, useApi, useLocal } from "../util";

type F = { mode: string; days: number; imp: string; depts: string[]; senders: string[]; att: boolean; tbl: boolean; status: string; q: string; wk: string; wkend: boolean; wkg: boolean };
const DEF: F = { mode: "messages", days: 7, imp: "any", depts: [], senders: [], att: false, tbl: false, status: "processed", q: "", wk: "", wkend: false, wkg: false };

function EmailPane({ id }: { id: number }) {
  const r = useApi(`/emails/${id}`);
  const nav = useNavigate();
  const peek = usePeek();
  const e: any = r.data;
  if (r.error) return <Err e={r.error} />;
  if (!e) return <Loading />;
  const inline = e.body.attachments.filter((a: any) => a.is_image);
  const files = e.body.attachments.filter((a: any) => !a.inline && !a.is_image);
  return (
    <div className="card" style={{ borderLeft: `4px solid ${tColor(e.thread_id)}` }}>
      <div className="crumbs">{e.thread && <button className="linkbtn" onClick={() => nav(`/threads/${e.thread.id}`)}>Thread: {e.thread.subject} ({e.thread.n})</button>}</div>
      <h2 style={{ fontSize: "1.3rem", margin: "4px 0 10px" }}>{e.subject}</h2>
      {e.note && <div className="anote"><span className="k">agent note</span><span><Kind kind={e.kind} /> {e.note}{e.due ? ` · due ${fmt(e.due)}` : ""}</span></div>}
      <div className="mhead">
        <div className="r"><b>{e.from_name}</b><span className="sim">&lt;{e.from}&gt;</span><ImpPill imp={e.importance} />{e.status === "skipped" && <span className="pill bad">skipped</span>}{e.lang === "ko" && <span className="pill">Korean</span>}</div>
        <div className="r"><span className="lbl">To</span><span>{e.to.join(", ")}</span></div>
        {e.cc.length > 0 && <div className="r"><span className="lbl">Cc</span><span>{e.cc.join(", ")}</span></div>}
        <div className="r"><span className="lbl">Date</span><span>{dow(e.day)} {fmt(e.day)} {e.day.slice(0, 4)}, {e.time}</span><span className="sim">via {e.sources.join(" + ")}</span></div>
      </div>
      {e.status === "skipped" && <p className="note"><b>Skipped by a rule:</b> {e.skip_rule}. Stored, never sent to the model.</p>}
      {e.fail_reason && <p className="note"><b>Last processing error:</b> {e.fail_reason.slice(0, 300)} — retried in the next run.</p>}
      <div className="mbody" style={{ marginTop: 14 }}><Prose text={e.body.new_text} /></div>
      {inline.map((a: any) => <figure className="shot" key={a.id} style={{ margin: "0 0 12px" }}><div className="bar"><span>{a.filename}</span></div><img src={`/api/attachments/${a.id}?inline=true`} alt={a.filename} style={{ maxWidth: "100%" }} /></figure>)}
      {files.length > 0 && <><div className="lbl">Attachments</div><div className="atts">{files.map((a: any) => (
        <a key={a.id} className="att" href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer"><b>📎 {a.filename}</b><span className="x"> · {Math.round(a.size / 1024)} kB</span>
          {a.extract && <><br /><span className="x">{a.extract.slice(0, 220)}</span></>}</a>))}</div></>}
      {e.body.signature && <details className="sig"><summary>Signature and disclaimer (cut from the new text)</summary><div>{e.body.signature}</div></details>}
      {e.body.quoted.length > 0 && <details className="quoted"><summary>Quoted history: {e.body.quoted.length} block{e.body.quoted.length > 1 ? "s" : ""}</summary>
        {e.body.quoted.map((q: any) => <div className="qblock" key={q.idx}><span className="sim">{q.header.split("\n")[0]}</span><br />{q.text.slice(0, 400)}
          {q.linked ? <button className="linkbtn" onClick={() => peek({ kind: "email", id: q.linked })}> open</button> : <span className="pill vio">only copy{q.sent_to_model ? " · sent to the model once" : ""}</span>}</div>)}</details>}
      {e.branch?.parallel && <p className="note"><span className="branch">⑂ parallel reply {e.branch.parallel.k} of {e.branch.parallel.n}</span> This is a separate answer to {e.branch.parallel.to}, not a reply to the other answers.</p>}
      {e.earlier.length > 0 && <details className="quoted" open><summary>Earlier in this thread: {e.earlier.length}</summary>
        {e.earlier.slice(0, 6).map((x: any) => <div className="qblock" key={x.id}><span className="sim">{fmt(x.day)} {x.time} · {x.from_name}</span>
          {x.in_chain ? (e.parent_id === x.id ? <span className="pill par">this email answers it</span> : null) : e.branch?.parallel || e.branch?.ghost ? <span className="pill vio par">parallel branch</span> : null}<br />{x.note || x.new_text.slice(0, 200)}
          <button className="linkbtn" onClick={() => peek({ kind: "email", id: x.id })}> open</button></div>)}</details>}
      <div className="note" style={{ marginTop: 14 }}>
        {e.pages.length ? <>This email fed: <ul className="fed">{e.pages.map((p: any) => <li key={p.id}><button className="linkbtn" onClick={() => peek({ kind: "page", id: p.id })}>{p.title}</button> <span className="sim">{p.role}</span></li>)}</ul></>
          : e.status === "skipped" ? "Not processed (skip rule)." : e.importance === "not_important" ? <>Labelled <b>not important</b>, so routing and editing were skipped.</> : "Not routed to a page (yet)."}
        {e.items.length > 0 && <div className="sim">Items: {e.items.map((it: any, i: number) => <div key={i}>{it.page} › {it.section}: {it.text} <span className="pill">{it.status}</span></div>)}</div>}
        {e.units.length > 0 && <div><button className="linkbtn" onClick={() => nav(`/agent/pipeline?unit=${encodeURIComponent(e.units[0])}`)}>see the pipeline →</button></div>}
      </div>
    </div>
  );
}

function ThreadPane({ id }: { id: number }) {
  const r = useApi(`/threads/${id}`);
  const nav = useNavigate();
  const peek = usePeek();
  const t: any = r.data;
  if (!t) return <Loading />;
  let lastDay = "";
  const byId: Record<string, any> = {};
  t.emails.forEach((e: any) => { byId[`e${e.id}`] = e; });
  const lanes = t.graph?.lanes || 1;
  const laneColor = (n: number) => n === 0 ? tColor(id) : `hsl(${(id * 47 + n * 97) % 360} 55% 50%)`;
  return (
    <div className="card" style={{ borderLeft: `4px solid ${tColor(id)}` }}><div className="crumbs">Thread view</div>
      <h2 style={{ fontSize: "1.3rem", margin: "4px 0" }}>{t.subject}</h2>
      <p className="sim" style={{ margin: "0 0 6px" }}>{t.emails.length} emails · one note per email · grouped by day{lanes > 1 ? ` · ${lanes - 1} parallel branch${lanes > 2 ? "es" : ""}: separate replies to the same email are indented in their own lane` : ""}</p>
      <div className="thread">{(t.graph?.order || t.emails.map((e: any) => `e${e.id}`)).map((key: string) => {
        if (key.startsWith("q")) {
          const g = t.graph.ghosts[key];
          return <div key={key} className="ghost"><b>{g.header}</b> — the department never received it; several people answered it separately below<br /><span className="sim">{g.raw_header}</span><br /><span className="sim">{g.text.slice(0, 200)}</span></div>;
        }
        const e = byId[key];
        const head = e.day !== lastDay ? <div className="tday">{dow(e.day)} {fmt(e.day)}</div> : null; lastDay = e.day;
        return <div key={e.id}>{head}<div className="mail lane" style={{ ["--c" as any]: laneColor(e.lane || 0), marginLeft: Math.min(e.lane || 0, 4) * 28 }}><span className="node" /><div className="card">
          {e.parallel && <div className="branch" title="Not a reply to the email above: a separate answer to the same earlier email">⑂ parallel reply {e.parallel.k} of {e.parallel.n} to {e.parallel.to}</div>}
          <div className="mhead"><div className="r"><b>{e.from_name}</b><span className="sim">{e.time}</span><ImpPill imp={e.importance} />
            {e.reply_to && !e.parallel && <span className="replyto">↳ reply to {e.reply_to}</span>}
            {e.pages.map((p: any) => <button key={p.id} className="linkbtn sim" onClick={() => peek({ kind: "page", id: p.id })}>→ {p.title}</button>)}</div></div>
          {e.note ? <div className="anote" style={{ margin: "6px 0 4px" }}><span className="k">note</span><span><Kind kind={e.kind} /> {e.note}</span></div> : <p className="sim">{e.new_text.slice(0, 160)}…</p>}
          <details className="full"><summary>Full email{e.has_table ? " · table" : ""}{e.n_att ? ` · 📎 ${e.n_att}` : ""}</summary><div className="mbody" style={{ marginTop: 8 }}><Prose text={e.new_text} /></div></details>
          <div style={{ marginTop: 4 }}><button className="linkbtn sim" onClick={() => nav(`/emails/${e.id}`)}>open email</button></div></div></div></div>;
      })}</div></div>
  );
}

function Week({ f, set, sel }: { f: F; set: (p: Partial<F>) => void; sel?: number }) {
  const r = useApi("/emails/week", { start: f.wk || undefined, depts: f.depts, senders: f.senders, importance: f.imp === "any" ? "" : f.imp });
  const nav = useNavigate();
  const d: any = r.data;
  if (!d) return <Loading />;
  const days = [0, 1, 2, 3, 4, 5, 6].map((i) => addDays(d.start, i)).filter((x) => f.wkend || !/Sat|Sun/.test(dow(x)));
  const hrs: (number | string)[] = ["<8", 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, ">17"];
  const hOf = (t: string) => { const h = +t.slice(0, 2); return h < 8 ? "<8" : h > 17 ? ">17" : h; };
  let items: any[] = d.rows.map((e: any) => ({ e, n: 1, last: e }));
  if (f.wkg) {
    const g: Record<string, any> = {};
    d.rows.forEach((e: any) => { const kk = e.thread_id + "|" + e.day; if (!g[kk]) g[kk] = { e, n: 0, last: e }; g[kk].n++; g[kk].last = e; });
    items = Object.values(g);
  }
  return (
    <>
      <div className="wk-nav"><button className="btn small" onClick={() => set({ wk: d.prev })}>← {fmt(d.prev)}</button><b>Week of {fmt(d.start)} – {fmt(d.end)}</b>
        <button className="btn small" onClick={() => set({ wk: d.next })}>{fmt(d.next)} →</button>
        <label className="toggle sim"><input type="checkbox" checked={f.wkg} onChange={() => set({ wkg: !f.wkg })} /> one line per thread-day</label>
        <label className="toggle sim"><input type="checkbox" checked={f.wkend} onChange={() => set({ wkend: !f.wkend })} /> weekend</label>
        <span className="sim">{d.rows.length} emails · no agent requests</span></div>
      <div className="wk"><table><thead><tr><th />{days.map((x) => <th key={x}>{dow(x)} {fmt(x)}</th>)}</tr></thead><tbody>{hrs.map((h) => {
        const cells = days.map((x) => items.filter((it) => it.e.day === x && hOf(it.e.time) === h));
        if (typeof h === "string" && !cells.some((c) => c.length)) return null;
        return <tr key={String(h)}><th className="hr">{typeof h === "string" ? (h === "<8" ? "before 8" : "after 17") : `${h}:00`}</th>{cells.map((c, i) => <td key={i}>{c.map((it) => (
          <button key={it.e.id} className={`wl${sel === it.e.id ? " on" : ""}${it.e.importance === "not_important" || it.e.status === "skipped" ? " dim" : ""}`} style={{ ["--c" as any]: tColor(it.e.thread_id) }}
            onClick={() => nav(`/emails/${it.e.id}`)} title={`${it.e.from_name} · ${it.e.subject}`}>
            <span className="wt">{it.e.time}</span><Avatar name={it.e.from_name} size={16} /><span className="wn">{it.n > 1 ? <b>{it.n}× </b> : null}{(f.wkg ? it.last.note : it.e.note) || it.e.subject}</span></button>))}</td>)}</tr>;
      })}</tbody></table></div>
    </>
  );
}

export default function Emails() {
  const { eid, tid } = useParams();
  const nav = useNavigate();
  const [f, setF] = useLocal<F>("llmwiki.ef", DEF);
  const set = (p: Partial<F>) => setF({ ...f, ...p });
  const facets = useApi("/emails/facets", { days: f.days });
  const list = useApi(f.mode === "week" ? null : "/emails", { days: f.days, depts: f.depts, senders: f.senders, importance: f.imp === "any" ? "" : f.imp,
    status: f.status === "all" ? "" : f.status, q: f.q, att: f.att || undefined, tbl: f.tbl || undefined, limit: 400 });
  const meta = useApi("/meta");
  const rows: any[] = (list.data as any)?.rows || [];
  const threads = useMemo(() => {
    const m = new Map<number, any[]>();
    rows.forEach((e) => { if (!m.has(e.thread_id)) m.set(e.thread_id, []); m.get(e.thread_id)!.push(e); });
    return [...m.entries()];
  }, [rows]);
  const sel = eid ? Number(eid) : undefined;
  const pane = eid ? <EmailPane id={Number(eid)} /> : tid ? <ThreadPane id={Number(tid)} /> :
    <div className="card"><p className="sim" style={{ margin: 0 }}>Select an email or a thread. The second line of each email is the <b>agent note</b> written for that email; each thread has its own colour.</p></div>;
  const any = f.depts.length || f.senders.length || f.att || f.tbl || f.q || f.imp !== "any" || f.status !== "processed";
  const senders: any[] = (facets.data as any)?.senders || [];
  const head = (
    <>
      <div className="title-row"><h1>Emails</h1></div>
      <p className="h-sub">Every email is stored once (raw mail, clean text with tables, attachments — all in SQLite). The note under each subject was written by the note-taker; the label says how important it is.</p>
      <div className="filters">
        <div className="frow"><span className="lbl">Show</span><Seg value={f.mode} options={[["messages", "Messages"], ["threads", "Threads"], ["week", "Week"]]} onChange={(v) => set({ mode: v })} />
          {f.mode !== "week" && <><span className="lbl" style={{ minWidth: 0, marginLeft: 8 }}>Received</span><Seg value={f.days} options={[[7, "7 days"], [30, "30 days"], [0, "All"]]} onChange={(v) => set({ days: +v })} /></>}
          <span className="lbl" style={{ minWidth: 0, marginLeft: 8 }}>Importance</span><Seg value={f.imp} options={[["any", "Any"], ["ir", "Important + routine"], ["important", "Important"]]} onChange={(v) => set({ imp: v })} /></div>
        <div className="frow"><span className="lbl">Department</span>{((meta.data as any)?.depts || []).map((x: string) => (
          <button key={x} className="chip dept" aria-pressed={f.depts.includes(x)} onClick={() => set({ depts: f.depts.includes(x) ? f.depts.filter((y) => y !== x) : [...f.depts, x] })}>{x.split("@")[0]}</button>))}</div>
        <div className="frow"><span className="lbl">Senders</span><div className="cbl">
          {[{ addr: "@ext", name: "all external" }, { addr: "@auto", name: "automated / not important" }, ...senders.slice(0, 30)].map((s: any) => (
            <label key={s.addr}><input type="checkbox" checked={f.senders.includes(s.addr)} onChange={() => set({ senders: f.senders.includes(s.addr) ? f.senders.filter((x) => x !== s.addr) : [...f.senders, s.addr] })} />
              {s.name}{s.n !== undefined && <span className="n"> {s.n}</span>}</label>))}</div></div>
        <div className="frow"><span className="lbl">Contains</span>
          <label className="toggle sim"><input type="checkbox" checked={f.att} onChange={() => set({ att: !f.att })} /> attachments</label>
          <label className="toggle sim"><input type="checkbox" checked={f.tbl} onChange={() => set({ tbl: !f.tbl })} /> tables</label>
          <span className="lbl" style={{ minWidth: 0, marginLeft: 8 }}>Status</span>
          <select value={f.status} onChange={(e) => set({ status: e.target.value })}><option value="processed">not skipped</option><option value="skipped">skipped by rules</option><option value="all">everything</option></select>
          <input type="search" value={f.q} onChange={(e) => set({ q: e.target.value })} placeholder="filter text…" style={{ flex: "1 1 140px" }} /></div>
      </div>
    </>
  );
  if (f.mode === "week") return <>{head}{any ? <div className="count-line"><button className="btn small" onClick={() => setF(DEF)}>Reset filters</button></div> : null}
    <Week f={f} set={set} sel={sel} />{(eid || tid) && <div className="pane" style={{ marginTop: 14 }}>{pane}</div>}</>;
  let lastDay = "";
  return (
    <>
      {head}
      <div className="count-line"><b>{f.mode === "threads" ? `${threads.length} threads` : `${(list.data as any)?.total ?? "…"} emails`}</b>
        {any ? <button className="btn small" onClick={() => setF({ ...DEF, mode: f.mode })}>Reset filters</button> : null}</div>
      <div className="mail-grid">
        <div className="mlist">{list.loading && !rows.length ? <Loading /> : !rows.length ? <Empty>No emails match.</Empty> : f.mode === "threads" ?
          threads.map(([t, es]) => (
            <button key={t} className="mrow" aria-current={tid === String(t)} onClick={() => nav(`/threads/${t}`)}><span className="stripe" style={{ background: tColor(t) }} /><span className="b">
              <span className="l1"><b>{es[0].subject.replace(/^(re|fw|aw|회신|전달):\s*/i, "")}</b><span className="sim">{fmt(es[0].day)}</span></span>
              <span className="l3 note-l"><Kind kind={es[0].kind} />{es[0].note}</span>
              <span className="l3">{es.length} in range · {[...new Set(es.map((e: any) => e.from_name))].slice(0, 4).join(", ")}</span></span></button>))
          : rows.map((e) => {
            const dh = e.day !== lastDay ? <div className="day-h">{dow(e.day)} {fmt(e.day)}</div> : null; lastDay = e.day;
            return <div key={e.id}>{dh}<button className={`mrow ${e.status === "skipped" ? "skipped" : ""}`} aria-current={sel === e.id} onClick={() => nav(`/emails/${e.id}`)}>
              <span className="stripe" style={{ background: tColor(e.thread_id) }} /><span className="b">
                <span className="l1"><b>{e.from_name}</b><span className="sim">{e.time}</span></span>
                <span className="l2">{e.subject}</span>
                <span className={`l3 ${e.note ? "note-l" : ""}`}>{e.status === "skipped" ? <span className="sim">skipped by rule: {e.skip_rule}</span> : e.note ? <><Kind kind={e.kind} />{e.note}</> : <span className="sim">not processed yet</span>}</span>
                <span className="l4">{e.depts.map((x: string) => <span key={x} className="chip dept">{x.split("@")[0]}</span>)}{e.n_att > 0 && <span className="pill">📎 {e.n_att}</span>}
                  {e.has_table && <span className="pill">table</span>}<ImpPill imp={e.importance} />{e.lang === "ko" && <span className="pill">KO</span>}</span></span></button></div>;
          })}</div>
        <div className="pane">{pane}</div>
      </div>
    </>
  );
}
