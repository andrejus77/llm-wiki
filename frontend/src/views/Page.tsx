import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { send } from "../api";
import { Cite, usePeek } from "../drawer";
import { Err, fmt, Kind, Loading, Prose, Seg, Status, TagPill, tColor, useApi } from "../util";

function History({ pid, sec, close, reload }: { pid: string; sec: any; close: () => void; reload: () => void }) {
  const h = useApi(`/pages/${pid}/history`, { key: sec.key });
  const nav = useNavigate();
  return (
    <div className="card" style={{ marginTop: 8 }}>
      <div className="sec-h">History of {sec.label}<span className="sp" /><button className="btn small" onClick={close}>Close</button></div>
      <ul className="hist">{((h.data as any[]) || []).map((x) => (
        <li key={x.id}><b>{x.accepted ? `v${x.version}` : "draft"}</b> {x.accepted ? "accepted" : "rejected by the guards"} · round {x.round} · {fmt(x.at.slice(0, 10))}
          {x.reason && <span className="sim"> · {x.reason}</span>}
          <details><summary className="sim">text and guard results</summary>
            <ul className="writes">{(x.guards || []).map((g: any, i: number) => <li key={i} className={g[1] ? "meta2" : g[2] ? "sec" : "tag"}>{g[1] ? "✓" : g[2] ? "!" : "✗"} {g[0]}</li>)}</ul>
            <Prose text={x.text} className="prose" />
            {x.call_id && <button className="linkbtn sim" onClick={() => nav(`/agent/pipeline?call=${x.call_id}`)}>the request →</button>}
            {x.accepted && x.version !== sec.version && <button className="btn small" onClick={async () => { await send("POST", `/pages/${pid}/rollback`, { key: sec.key, version: x.version }); reload(); close(); }}>Roll back to v{x.version}</button>}
          </details></li>))}</ul>
      <p className="sim" style={{ margin: 0 }}>Every draft is kept, rejected ones too. A rollback writes a new version; nothing is deleted.</p>
    </div>
  );
}

function DeptCard({ p, reload }: { p: any; reload: () => void }) {
  const meta = useApi("/meta");
  const opts: any[] = (meta.data as any)?.dept_info || [];
  const set = async (v: string) => { await send("PATCH", `/pages/${p.id}`, { dept: v }); reload(); };
  return (
    <div className="card"><h3>Department</h3>
      <p style={{ margin: "0 0 6px" }}>{p.dept_name ? <span className="dept-pill">{p.dept_name}</span> : <span className="sim">none yet</span>}
        <span className="sim"> · {p.dept_locked ? "set by a person" : "set by the script"}</span></p>
      <p className="sim" style={{ margin: "0 0 6px" }}>A topic appears in exactly one department's report. Mail by department:</p>
      <ul>{Object.entries(p.depts).map(([d, n]: any) => <li key={d}><span className="chip dept">{d.split("@")[0]}</span><span className="sim">{n} emails</span></li>)}</ul>
      <label className="sim">Move to <select value={p.dept_locked ? p.dept : ""} onChange={(e) => set(e.target.value)}>
        <option value="">automatic</option>{opts.map((o) => <option key={o.addr} value={o.addr}>{o.name}</option>)}</select></label>
    </div>
  );
}

export default function PageView() {
  const { pid = "" } = useParams();
  const r = useApi(`/pages/${pid}`);
  const nav = useNavigate();
  const peek = usePeek();
  const [win, setWin] = useState(30);
  const [hist, setHist] = useState<string | null>(null);
  const p: any = r.data;
  if (r.error) return <Err e={r.error} />;
  if (!p) return <Loading />;
  const latest = p.timeline[0]?.day;
  const shown = p.timeline.filter((e: any) => !win || !latest || e.day >= new Date(new Date(latest).getTime() - win * 864e5).toISOString().slice(0, 10));
  const section = (s: any) => {
    const nums = new Map<number, number>();
    const cite = (id: number) => { if (!nums.has(id)) nums.set(id, nums.size + 1); return <Cite key={`c${id}`} id={id} n={nums.get(id)!} />; };
    const body = s.key === "people" ? null : <Prose text={s.text} cite={cite} />;
    return (
      <section id={`sec-${s.key}`} key={s.key} className={s.key === "summary" ? "psum" : undefined}>
        <h2>{s.label}{s.version ? <span className="sim" style={{ fontSize: ".7rem", fontWeight: 400 }}> v{s.version}</span> : s.text ? <span className="chg">provisional</span> : null}
          <button className="linkbtn sim" style={{ marginLeft: 8, fontSize: ".72rem" }} onClick={() => setHist(hist === s.key ? null : s.key)}>history{s.rejected ? ` · ${s.rejected} rejected` : ""}</button></h2>
        {s.key === "people" ? <>
          <table className="ppl"><thead><tr><th>Person</th><th>Wrote</th></tr></thead><tbody>{p.people.slice(0, 12).map((x: any) => <tr key={x.addr}><td>{x.name}{!x.internal && <span className="sim"> · external</span>}</td><td className="sim">{x.n}</td></tr>)}</tbody></table>
          <p className="sim" style={{ margin: "2px 0 6px" }}>Counts from the headers; the roles below are written by the editor.</p><Prose text={s.text} cite={cite} /></> : body}
        {!s.text && s.key !== "people" && <p className="sim">Nothing written yet.</p>}
        {nums.size > 0 && <div className="srcstrip"><span className="rc-lbl">Sources</span>{[...nums.entries()].map(([eid, n]) => {
          const src = s.sources.find((x: any) => x.email_id === eid);
          return <button key={eid} onClick={() => peek({ kind: "email", id: eid })}><b>{n}</b> {src ? `${src.sender} · ${fmt(src.day)} · ${src.subject.replace(/^(re|fw|aw|회신|전달):\s*/i, "").slice(0, 38)}` : `email ${eid}`}</button>;
        })}</div>}
        {!nums.size && s.sources.length > 0 && <div className="srcstrip"><span className="rc-lbl">Sources</span>{s.sources.slice(0, 8).map((x: any) => (
          <button key={x.email_id} onClick={() => peek({ kind: "email", id: x.email_id })}>{x.sender} · {fmt(x.day)}</button>))}{s.sources.length > 8 && <span className="sim">+{s.sources.length - 8}</span>}</div>}
        {s.pending.length > 0 && <div className="pending"><b>Not yet written in · {s.pending.length} item{s.pending.length > 1 ? "s" : ""}</b>
          <ul style={{ margin: "4px 0 0" }}>{s.pending.map((it: any) => <li key={it.id}>{it.text} {it.emails.map((x: number) => <Cite key={x} id={x} n="✉" />)} {it.status === "failed" && <span className="pill bad">failed {it.attempts}×</span>}</li>)}</ul>
          <span className="sim">The editor writes these in the next run; if its rewrite fails the guards, the original text stays.</span></div>}
        {hist === s.key && <History pid={pid} sec={s} close={() => setHist(null)} reload={r.reload} />}
      </section>
    );
  };
  return (
    <div className="page-grid"><article style={{ minWidth: 0 }}>
      <div className="crumbs"><button className="linkbtn" onClick={() => nav("/wiki")}>Wiki</button><span>›</span><span>{p.level}</span></div>
      <div className="title-row"><h1>{p.title}</h1><Status s={p.status} />{p.dept_name && <span className="dept-pill">{p.dept_name}</span>}</div>
      <div className="chips">{Object.entries(p.tags || {}).flatMap(([ty, vs]: any) => (vs || []).map((v: string) => <TagPill key={ty + v} ty={ty} v={v} small={false} onClick={() => nav(`/wiki?tag=${ty}:${encodeURIComponent(v)}`)} />))}</div>
      <dl className="meta"><div><dt>Level</dt><dd title={p.level_proposed !== p.level ? `The agent proposed ${p.level_proposed}; the script shows ${p.level} until the page has enough emails, days and people (Settings › Pipeline › level_min)` : "proposed by the agent, checked by the script"}>
        {p.level}{p.level_proposed !== p.level && <span className="sim"> (→ {p.level_proposed})</span>}</dd></div><div><dt>Created</dt><dd>{fmt(p.created.slice(0, 10))}</dd></div><div><dt>Last mail</dt><dd>{fmt(p.last_day)}</dd></div>
        <div><dt>Emails</dt><dd>{p.timeline.length}</dd></div><div><dt>Updated</dt><dd>{fmt(p.updated.slice(0, 10))}</dd></div>
        {p.length && <div title={`Length budget: ${p.length.budget} words, from the ${p.length.source_words} words people wrote in this page's emails (Settings › Wiki pages). The script holds the editor to it.`}>
          <dt>Length</dt><dd className="lenbar">{p.length.words} / {p.length.budget} words<span className="bar"><i className={p.length.words > p.length.budget * 1.15 ? "over" : ""} style={{ width: `${Math.min(100, (100 * p.length.words) / Math.max(1, p.length.budget))}%` }} /></span></dd></div>}</dl>
      {p.merged_into && <p className="note">This page was merged into another page.</p>}
      <div className="card prose">{p.sections.map(section)}</div>
      {p.open_issues.length > 0 && <div className="card" style={{ marginTop: 14 }}><div className="sec-h">Open issues (tracked by code)</div>
        <ul className="elist">{p.open_issues.map((o: any) => <li key={o.code}><span className="oid sim">{o.code}</span> {o.text} {o.email_id && <Cite id={o.email_id} n="✉" />}
          <span className="sim"> · opened {fmt(o.opened)}{o.closed ? ` · closed ${fmt(o.closed)}` : ""}</span></li>)}</ul></div>}
      <div className="card" style={{ marginTop: 14 }}>
        <div className="sec-h">Timeline · one row per email <span className="sp" /><Seg value={win} options={[[7, "7 days"], [30, "30 days"], [0, "All"]]} onChange={(v) => setWin(+v)} /></div>
        <p className="sim" style={{ margin: "0 0 4px" }}>Showing {shown.length} of {p.timeline.length}. Each row is the note-taker's note for one email; the prose above is the editor's.</p>
        <ul className="timeline">{shown.map((e: any) => (
          <li className={`ev ${e.role}`} key={e.id}><span className="d">{fmt(e.day)}<br /><span style={{ fontSize: ".7rem" }}>{e.time}</span></span><div className="txt">
            <Kind kind={e.kind} /> {e.note || e.subject}{e.role === "secondary" && <span className="pill"> secondary</span>}
            <div className="row2"><span className="tn"><span className="dotc" style={{ background: tColor(e.thread_id) }} /><button className="linkbtn" onClick={() => nav(`/threads/${e.thread_id}`)}>thread</button></span>
              <span>· <button className="linkbtn" onClick={() => peek({ kind: "email", id: e.id })}>{e.from_name}</button></span>
              {e.also.length > 0 && <span>· also on {e.also.map((a: any) => <button key={a.id} className="linkbtn" onClick={() => peek({ kind: "page", id: a.id })}>{a.title}</button>)}</span>}</div></div></li>))}</ul>
      </div>
    </article>
      <aside className="rail">
        <div className="card"><h3>On this page</h3><ul className="toc">{p.sections.map((s: any) => <li key={s.key}><button className="linkbtn" onClick={() => document.getElementById(`sec-${s.key}`)?.scrollIntoView({ behavior: "smooth" })}>{s.label}</button></li>)}</ul></div>
        {p.structure === "free" && p.people.length > 0 && <div className="card"><h3>People · from the headers</h3><ul>{p.people.slice(0, 10).map((x: any) => (
          <li key={x.addr}><span>{x.name}{!x.internal && <span className="sim"> · external</span>}</span><span className="sim">{x.n}</span></li>))}</ul></div>}
        <DeptCard p={p} reload={r.reload} />
        <div className="card"><h3>Source threads</h3><ul>{p.threads.map((t: any) => <li key={t.id}><span className="tn"><span className="dotc" style={{ background: tColor(t.id) }} /><button className="linkbtn" onClick={() => nav(`/threads/${t.id}`)}>{t.subject.slice(0, 60)}</button></span><span className="sim">{t.n}</span></li>)}</ul></div>
        {p.attachments.length > 0 && <div className="card"><h3>Attachments</h3><ul>{p.attachments.map((a: any) => <li key={a.id}><a className="linkbtn" href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer">{a.filename}</a><span className="sim">{fmt(a.day)}</span></li>)}</ul></div>}
        {p.related.length > 0 && <div className="card"><h3>Related pages · by embedding</h3><ul>{p.related.map((x: any) => <li key={x.id}><button className="linkbtn" onClick={() => nav(`/wiki/${x.id}`)}>{x.title}</button><span className="sim">{x.sim.toFixed(2)}</span></li>)}</ul></div>}
        {p.status_changes.length > 0 && <div className="card"><h3>Status history</h3><ul>{p.status_changes.map((c: any, i: number) => <li key={i}><span>{c.old || "—"} → {c.new}</span><span className="sim">{fmt(c.at)}</span></li>)}</ul></div>}
      </aside></div>
  );
}
