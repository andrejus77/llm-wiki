import { useNavigate, useSearchParams } from "react-router-dom";
import { Avatar, Empty, Err, fmt, LoadMore, Loading, pagePills, Seg, Status, TagPill, useDebounced, useLocal, useMeta, usePaged } from "../util";

type F = { days: number; levels: string[]; status: string[]; depts: string[]; people: string[]; system: string; process: string; wt: string; group: string; view: string; q: string };

export default function Wiki() {
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const meta = useMeta();
  const m: any = meta.data;
  const [f, setF] = useLocal<F>("llmwiki.wf", { days: 0, levels: [], status: [], depts: [], people: [], system: "", process: "", wt: "", group: "none", view: "", q: "" });
  const set = (p: Partial<F>) => setF({ ...f, ...p });
  const tag = sp.get("tag") || "";
  const view = f.view || m?.wiki_view || "cards";
  const q = useDebounced(f.q, 300);
  const r = usePaged<any>("/pages", { days: f.days, levels: f.levels, status: f.status, depts: f.depts, people: f.people, system: f.system, process: f.process,
    work_type: f.wt, tag, q }, 120);
  const rows: any[] = r.rows;
  const toggle = (k: keyof F, v: string) => { const a = f[k] as string[]; set({ [k]: a.includes(v) ? a.filter((x) => x !== v) : [...a, v] } as any); };
  const tagFilter = (ty: string, v: string) => setSp({ tag: `${ty}:${v}` });
  const groups: [string, any[]][] = f.group === "level" ? (m?.levels || []).map((l: string) => [`${l}s`, rows.filter((x) => x.level === l)])
    : f.group === "system" ? Object.entries(rows.reduce((g: any, x: any) => { const k = (x.tags.system || [])[0] || "no system"; (g[k] = g[k] || []).push(x); return g; }, {}))
    : [["", rows]];
  const card = (x: any) => {
    const pills = pagePills(x.tags, 12), shown = pills.slice(0, 5);
    return (
      <article className="pcard" key={x.id} style={{ ["--c" as any]: `hsl(${(x.id * 47) % 360} 55% 50%)` }}>
        <div className="pc-top"><button className="linkbtn pc-t" onClick={() => nav(`/wiki/${x.slug}`)}>{x.title}</button></div>
        <div className="rc-meta"><span className="kind">{x.level}</span><Status s={x.status} /></div>
        <p className="pc-ov">{x.overview || <span className="sim">No overview yet.</span>}</p>
        <div className="rc-tags pc-tags">{shown.map(([ty, v]) => <TagPill key={ty + v} ty={ty} v={v} onClick={() => tagFilter(ty, v)} />)}{pills.length > shown.length && <span className="chip sm plain">+{pills.length - shown.length}</span>}</div>
        <div className="rc-f pc-f"><span>{x.emails} email{x.emails === 1 ? "" : "s"}</span>
          <span className="rc-av">{x.people.slice(0, 3).map((p: string) => <Avatar key={p} name={p} size={18} />)}{x.people.length > 3 && <span className="sim">+{x.people.length - 3}</span>}</span>
          <span>{fmt(x.last_day)}</span>{x.dept_name && <span className="chip dept" title="The one department whose report shows this topic">{x.dept_name}</span>}</div>
      </article>
    );
  };
  const row = (x: any) => (
    <button className="prow" key={x.id} onClick={() => nav(`/wiki/${x.slug}`)}>
      <span><span className="t">{x.title}</span><span className="tg">{pagePills(x.tags, 6).map(([ty, v]) => <span key={ty + v} className={`chip sm t-${ty}`}>{v}</span>)}</span>
        <span className="sim" style={{ display: "block", fontSize: ".8rem", marginTop: 2 }}>{x.overview?.slice(0, 140)}</span></span>
      <span className="hide-m">{x.level}</span><span><Status s={x.status} /></span><span className="sim">{fmt(x.last_day)}</span><span className="sim">{x.emails} emails</span>
      <span className="chips" style={{ margin: 0 }}>{x.dept_name && <span className="chip dept">{x.dept_name}</span>}</span>
      <span className="ppl">{x.people.slice(0, 4).join(", ")}</span></button>
  );
  const sel = (label: string, key: "system" | "process" | "wt", vals: string[]) => (
    <select value={f[key]} onChange={(e) => set({ [key]: e.target.value } as any)} aria-label={label}><option value="">{label}: any</option>{vals.map((v) => <option key={v}>{v}</option>)}</select>);
  return (
    <>
      <div className="title-row"><h1>Wiki</h1></div>
      <p className="h-sub">Pages are written and kept up to date by the pipeline from the department's email. Filters use the email headers (dates, departments, participants) and the page tags.</p>
      <div className="filters">
        <div className="frow"><span className="lbl">Active</span><Seg value={f.days} options={[[7, "7 days"], [30, "30 days"], [90, "90 days"], [0, "All"]]} onChange={(v) => set({ days: +v })} />
          <input type="search" value={f.q} onChange={(e) => set({ q: e.target.value })} placeholder="title or text…" style={{ flex: "1 1 160px" }} /></div>
        <div className="frow"><span className="lbl">Department</span>{(m?.dept_info || []).map((d: any) => <button key={d.addr} className="chip dept" aria-pressed={f.depts.includes(d.addr)} onClick={() => toggle("depts", d.addr)}>{d.name}</button>)}</div>
        <div className="frow"><span className="lbl">Team</span><div className="cbl">{(m?.people || []).map((p: any) => (
          <label key={p.addr}><input type="checkbox" checked={f.people.includes(p.addr)} onChange={() => toggle("people", p.addr)} />{p.name}<span className="n"> {p.n}</span></label>))}</div></div>
        <div className="frow"><span className="lbl">Tags</span>{sel("System", "system", m?.tag_values?.system || [])}{sel("Process", "process", m?.tag_values?.process || [])}{sel("Work type", "wt", m?.tag_values?.work_type || [])}
          {tag && <><TagPill ty={tag.split(":")[0]} v={tag.slice(tag.indexOf(":") + 1)} small={false} /><button className="btn small" onClick={() => setSp({})}>× tag</button></>}</div>
        <div className="frow"><span className="lbl">Level</span>{(m?.levels || []).map((l: string) => <button key={l} className="chip plain" aria-pressed={f.levels.includes(l)} onClick={() => toggle("levels", l)}>{l}</button>)}
          <span className="lbl" style={{ minWidth: 0, marginLeft: 8 }}>Status</span>{(m?.statuses || []).map((s: string) => <button key={s} className="chip plain" aria-pressed={f.status.includes(s)} onClick={() => toggle("status", s)}>{s}</button>)}</div>
      </div>
      <div className="count-line"><b>{r.total ?? "…"} pages</b>{r.loading && <span className="sim">loading…</span>}<span style={{ flex: 1 }} />
        <Seg value={view} options={[["cards", "Cards"], ["list", "List"]]} onChange={(v) => set({ view: v })} />
        <span className="sim">group</span><Seg value={f.group} options={[["none", "none"], ["level", "level"], ["system", "system"]]} onChange={(v) => set({ group: v })} /></div>
      {r.error ? <Err e={r.error} /> : r.loading && !rows.length ? <Loading /> : !rows.length ? <Empty>No page matches. Try a longer time range.</Empty> : view === "cards" ?
        groups.filter(([, rs]) => rs.length).map(([l, rs]) => <div key={l}>{l && <div className="grp-h">{l} · {rs.length}</div>}<div className="pcards">{rs.map(card)}</div></div>)
        : <div className="plist"><div className="prow head"><span>Page</span><span>Level</span><span>Status</span><span>Last active</span><span>In range</span><span>Departments</span><span>Active participants</span></div>
          {groups.filter(([, rs]) => rs.length).map(([l, rs]) => <div key={l}>{l && <div className="grp-h">{l} · {rs.length}</div>}{rs.map(row)}</div>)}</div>}
      <LoadMore has={r.hasMore} loading={r.loading} onMore={r.more} />
    </>
  );
}
