import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { send } from "../api";
import { usePeek } from "../drawer";
import { Avatar, dow, Err, fmt, Kind, Loading, MON, Seg, Status, TagPill, useApi, useLocal } from "../util";

const SRC: Record<string, [string, string]> = { script: ["script", ""], wiki: ["wiki", "green"], agent: ["agent", "violet"], notes: ["S1 notes", "blue"] };
const hue = (id: number) => `hsl(${(id * 47) % 360} 55% 50%)`;
const KIND_LABEL: Record<string, string> = { daily: "Daily", weekly: "Weekly", monthly: "Monthly" };
const heat = (n: number, max: number) => (n ? { ["--p" as any]: `${Math.round(20 + (n / Math.max(1, max)) * 60)}%` } : undefined);

// One row of day cells: weekday letter + count; empty days stay visible (outlined), busy days fill with the accent.
// For a month the story cards get one cell per week (W44 …).
function DayStrip({ days, big }: { days: { day: string; n: number; important?: number; week?: number; to?: string }[]; big?: boolean }) {
  const max = Math.max(1, ...days.map((d) => d.n));
  return (
    <div className={`daystrip${big ? " big" : ""}`}>{days.map((d) => (
      <div key={d.day} className={`dcell${d.n ? " on" : ""}${d.week ? " wkc" : ""}`} style={heat(d.n, max)}
        title={d.week ? `Week ${d.week} (${fmt(d.day)} – ${fmt(d.to)}): ${d.n} email${d.n === 1 ? "" : "s"}` : `${dow(d.day)} ${fmt(d.day)}: ${d.n} email${d.n === 1 ? "" : "s"}${d.important ? `, ${d.important} important` : ""}`}>
        {big && <b>{d.n || ""}</b>}<span>{d.week ? `W${d.week}` : dow(d.day).slice(0, big ? 3 : 1)}</span>{big && <small>{fmt(d.day)}</small>}
        {big && d.important ? <i className="imp" /> : null}</div>))}</div>
  );
}

// A monthly report's mail per day: a calendar (Mon–Sun columns), empty days outlined.
function MonthGrid({ days }: { days: { day: string; n: number; important?: number }[] }) {
  const max = Math.max(1, ...days.map((d) => d.n));
  const lead = days.length ? (new Date(days[0].day + "T12:00:00Z").getUTCDay() + 6) % 7 : 0;
  return (
    <div className="monthgrid">{["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((x) => <span key={x} className="mh">{x.slice(0, 2)}</span>)}
      {Array.from({ length: lead }, (_, i) => <span key={`x${i}`} />)}
      {days.map((d) => (
        <div key={d.day} className={`dcell${d.n ? " on" : ""}`} style={heat(d.n, max)} title={`${dow(d.day)} ${fmt(d.day)}: ${d.n} email${d.n === 1 ? "" : "s"}${d.important ? `, ${d.important} important` : ""}`}>
          <small>{+d.day.slice(8)}</small><b>{d.n || ""}</b>{d.important ? <i className="imp" /> : null}</div>))}</div>
  );
}

const STATS: Record<string, [string, string]> = { emails: ["emails", "blue"], important: ["important", "amber"], pages_active: ["topics", "teal"],
  new_pages: ["new topics", "violet"], decisions: ["decisions", "green"], closed: ["closed issues", "green"], at_risk: ["at risk", "red"], threads: ["threads", "blue"] };

function TopicWeek({ period, dept, kind }: { period: string; dept: string; kind: string }) {
  const r = useApi("/reports/week", { period, dept });
  const peek = usePeek();
  const d: any = r.data;
  if (!d) return <Loading />;
  const topics = new Map<number, any>();
  d.rows.forEach((x: any) => { const t = topics.get(x.page_id) || { ...x, n: 0, days: {} as Record<string, number> }; t.n++; t.days[x.day] = (t.days[x.day] || 0) + 1; topics.set(x.page_id, t); });
  if (kind === "monthly") {   // a month: one row per topic, one column per day
    const rows = [...topics.values()].sort((a, b) => b.n - a.n);
    const max = Math.max(1, ...rows.flatMap((t) => Object.values(t.days) as number[]));
    return (
      <>
        <p className="sim">Which of this team's topics got mail on which day of the month — from the receive times and the wiki links, no agent. {d.rows.length} emails on {rows.length} topics.</p>
        <div className="wk"><table className="tmonth"><thead><tr><th />{d.days.map((x: string) => <th key={x} className={["Sat", "Sun"].includes(dow(x)) ? "we" : ""}>{+x.slice(8)}</th>)}<th>Σ</th></tr></thead>
          <tbody>{rows.map((t) => <tr key={t.page_id}><th className="tt"><button className="linkbtn" onClick={() => peek({ kind: "page", id: t.page_id })}>{t.title}</button></th>
            {d.days.map((x: string) => <td key={x} className={`dcell${t.days[x] ? " on" : ""}`} style={heat(t.days[x] || 0, max)} title={`${t.title} · ${fmt(x)}: ${t.days[x] || 0}`}>{t.days[x] || ""}</td>)}
            <td className="sim">{t.n}</td></tr>)}</tbody></table></div>
      </>
    );
  }
  const hrs: (number | string)[] = ["<8", 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, ">17"];
  const hOf = (h: number) => (h < 8 ? "<8" : h > 17 ? ">17" : h);
  const cell = (day: string, h: number | string) => {
    const m = new Map<number, any>();
    d.rows.filter((x: any) => x.day === day && hOf(x.hour) === h).forEach((x: any) => {
      const c = m.get(x.page_id) || { ...x, n: 0, human: 0 }; c.n++; if (!x.automated) c.human++; m.set(x.page_id, c);
    });
    return [...m.values()];
  };
  return (
    <>
      <p className="sim">Which of this team's topics got mail on which day and hour of the window — from the receive times and the wiki links, no agent. {d.rows.length} emails on {topics.size} topics. Faded = automated mail only.</p>
      <div className="wk"><table><thead><tr><th />{d.days.map((x: string) => <th key={x}>{dow(x)} {fmt(x)}</th>)}</tr></thead><tbody>{hrs.map((h) => {
        const cells = d.days.map((x: string) => cell(x, h));
        if (typeof h === "string" && !cells.some((c: any[]) => c.length)) return null;
        return <tr key={String(h)}><th className="hr">{typeof h === "string" ? (h === "<8" ? "before 8" : "after 17") : `${h}:00`}</th>{cells.map((c: any[], i: number) => <td key={i}>{c.map((t) => (
          <button key={t.page_id} className={`wl${t.human ? "" : " dim"}`} style={{ ["--c" as any]: hue(t.page_id) }} onClick={() => peek({ kind: "page", id: t.page_id })} title={`${t.title} · ${t.n} email${t.n > 1 ? "s" : ""} · ${t.status}`}>
            <span className="wn">{t.n > 1 ? <b>{t.n}× </b> : null}{t.title}</span></button>))}</td>)}</tr>;
      })}</tbody></table></div>
    </>
  );
}

export default function Reports() {
  const [sp, setSp] = useSearchParams();
  const period = sp.get("period") || "";
  const [deptSel, setDeptSel] = useLocal("llmwiki.rdept", "");
  const dept = sp.get("dept") || deptSel;
  const [kindSel, setKindSel] = useLocal("llmwiki.rkind", "");
  const kind = sp.get("kind") || (period ? "" : kindSel);
  const [viewSel, setView] = useLocal("llmwiki.rview", "brief");
  const view = sp.get("view") || viewSel;
  const [area, setArea] = useState("");
  const [srcOn, setSrcOn] = useLocal("llmwiki.srcon", false);
  const [open, setOpen] = useState<number[]>([]);
  const meta = useApi("/meta");
  const r = useApi("/reports/briefing", { period, dept, area, kind });
  const peek = usePeek();
  const nav = useNavigate();
  const d: any = r.data;
  // the lists below the stories are as tall as a story card at most (they scroll inside)
  const gridRef = useRef<HTMLDivElement>(null);
  const [storyH, setStoryH] = useState(0);
  useEffect(() => {
    const el = gridRef.current?.querySelector(".rcard") as HTMLElement | null;
    if (!el) { setStoryH(0); return; }
    const ro = new ResizeObserver(() => setStoryH(Math.round(el.getBoundingClientRect().height)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [d?.period, d?.dept, view, d?.stories?.length]);
  if (r.error) return <Err e={r.error} />;
  if (!d) return <Loading />;
  if (d.empty) return <div className="note">No mail yet. Fetch mail and run the pipeline (Agent › Logs → actions).</div>;
  const P = d.parts || {};
  const showSrc = srcOn && P.source_switch !== false;
  const src = (k: string) => showSrc ? <span className={`pill ${SRC[k][1]} srcb`}>{SRC[k][0]}</span> : null;
  const listH = d.list_max_px || storyH || undefined;
  const listStyle = listH ? { maxHeight: listH } : undefined;
  const areas: [string, string][] = [["", "all areas"], ...((meta.data as any)?.tag_values?.system || []).map((v: string) => [`system:${v}`, `System: ${v}`]),
    ...((meta.data as any)?.tag_values?.process || []).map((v: string) => [`process:${v}`, `Process: ${v}`])];
  const gen = async () => { await send("POST", "/reports/generate", { period: d.period, dept: d.dept }); setTimeout(r.reload, 5000); };
  const setParams = (f: (n: URLSearchParams) => void) => { const n = new URLSearchParams(sp); f(n); setSp(n); };
  const pickDept = (a: string) => { setDeptSel(a); setParams((n) => n.set("dept", a)); };
  const pickPeriod = (p: string) => setParams((n) => { n.set("period", p); n.set("kind", d.kind); });
  const pickKind = (k: string) => { setKindSel(k); setParams((n) => { n.delete("period"); n.set("kind", k); }); };
  const monthly = d.kind === "monthly";
  const windowText = monthly ? `${fmt(d.from)} – ${fmt(d.to)}` : `past ${d.per_day.length} days`;

  const card = (st: any, layout: any) => {
    const ex = open.includes(st.page_id);
    const keyN = ex ? st.key_emails.length : layout.key_n;
    return (
      <article key={st.page_id} className={`rcard${ex ? " open" : ""}`} style={{ ["--c" as any]: hue(st.page_id) }}>
        <header className="rc-h"><span className="rc-n">{st.rank}</span>
          <div className="rc-t"><h3><button className="linkbtn" style={{ color: "inherit" }} onClick={() => peek({ kind: "page", id: st.page_id })}>{st.title}</button></h3>
            <div className="rc-meta">{P.story_level && <span className="kind">{st.level}</span>}{P.story_status && <><Status s={st.status} />{st.change && <span className="status-chg">{st.change}</span>}</>}{src("script")}
              {showSrc && <span className="pill srcb">≈ {layout.h} lines</span>}</div></div></header>
        {st.tags.length > 0 && <div className="rc-tags">{st.tags.map(([ty, v]: string[]) => <TagPill key={ty + v} ty={ty} v={v} onClick={() => nav(`/wiki?tag=${ty}:${encodeURIComponent(v)}`)} />)}{src("script")}</div>}
        {P.story_text && st.text && <div className="rc-main"><div className="rc-lbl">In this {monthly ? "month" : "window"} {src(st.text_source)}</div>
          <p className={`rc-week${layout.clamp && !ex ? " clamp" : ""}`} style={layout.clamp && !ex ? { WebkitLineClamp: layout.clamp } as any : undefined}>{st.text}</p></div>}
        {st.decision && <div className="rc-dec"><span className="rc-lbl">Decided {src("notes")}</span>
          <button className="linkbtn" onClick={() => peek({ kind: "email", id: st.decision.id })}>{st.decision.note}</button> <span className="sim">{fmt(st.decision.day)} · {st.decision.sender}</span></div>}
        {st.key_emails.length > 0 && <div className="rc-keys"><div className="rc-lbl">Key emails {src("notes")}</div>
          <ul className="rc-notes">{st.key_emails.slice(0, keyN).map((e: any) => (
            <li key={e.id}><button onClick={() => peek({ kind: "email", id: e.id })}><Kind kind={e.kind} /><span className="rc-nt">{e.note}</span><span className="sim">{fmt(e.day)} · {e.sender}</span></button></li>))}</ul>
          {st.n_key_available > keyN && !ex && <button className="linkbtn sim" onClick={() => peek({ kind: "page", id: st.page_id })}>+ {st.n_key_available - keyN} more on the page</button>}</div>}
        <footer className="rc-f">{P.story_email_count && <span>{st.n_emails} email{st.n_emails === 1 ? "" : "s"}</span>}
          {P.story_people && <span className="rc-av">{st.people.slice(0, 5).map((p: string) => <Avatar key={p} name={p} size={20} />)}</span>}
          {P.story_days && <DayStrip days={st.dots} />}
          {P.story_open_issues && <span>{st.open_issues} open issue{st.open_issues === 1 ? "" : "s"}</span>}
          {P.story_next_deadline && st.next_deadline && <span>next: <b>{fmt(st.next_deadline.day)}</b> {st.next_deadline.text.slice(0, 60)}</span>}{src("script")}
          <button className="linkbtn rc-more" onClick={() => setOpen(ex ? open.filter((x) => x !== st.page_id) : [...open, st.page_id])}>{ex ? "Show less" : "Show all"}</button></footer>
      </article>
    );
  };
  const lists = [
    P.decisions && <section key="dec" className="card rlist" style={listStyle}><div className="sec-h">Decisions <span className="pill">{d.decisions.length}</span>{src("notes")}</div>
      <ul className="elist">{d.decisions.map((e: any) => <li key={e.id}>{e.note} <button className="cite" onClick={() => peek({ kind: "email", id: e.id })}>✉</button><br />
        <span className="sim">{fmt(e.day)} · {e.sender}{e.page ? <> · <button className="linkbtn" onClick={() => peek({ kind: "page", id: e.page.id })}>{e.page.title}</button></> : null}</span></li>)}
        {!d.decisions.length && <li>None</li>}</ul></section>,
    P.deadlines && <section key="dl" className="card rlist" style={listStyle}><div className="sec-h">Deadlines · next 14 days {src("notes")}</div>
      <ul className="dl">{d.deadlines.map((x: any) => <li key={x.id}><span className="dd">{+x.due.slice(8)}<small>{MON[+x.due.slice(5, 7) - 1]} · {dow(x.due)}</small></span>
        <span>{x.note}<br /><span className="sim">{x.page && <button className="linkbtn" onClick={() => peek({ kind: "page", id: x.page.id })}>{x.page.title}</button>} <button className="cite" onClick={() => peek({ kind: "email", id: x.id })}>✉</button></span></span></li>)}
        {!d.deadlines.length && <li>None</li>}</ul></section>,
    P.waiting && <section key="wait" className="card rlist" style={listStyle}><div className="sec-h">Waiting on others {src("script")}</div>
      <ul className="elist">{d.waiting.map((e: any) => <li key={e.id}><b>{e.sender}</b> since {fmt(e.day)}: {e.note} <button className="cite" onClick={() => peek({ kind: "email", id: e.id })}>✉</button></li>)}
        {!d.waiting.length && <li>Nothing unanswered</li>}</ul></section>,
    P.attention && <section key="att" className="card rlist attn" style={listStyle}><div className="sec-h">Needs attention {src("script")}</div>
      <ul className="elist">{d.attention.map((a: any, i: number) => <li key={i}><button className="linkbtn" onClick={() => a.page_id ? peek({ kind: "page", id: a.page_id }) : peek({ kind: "email", id: a.email_id })}>{a.text}</button></li>)}
        {!d.attention.length && <li>Nothing flagged</li>}</ul></section>,
  ].filter(Boolean);

  return (
    <div className="rep">
      <nav className="dept-tabs" aria-label="Department">{d.depts.map((x: any) => (
        <button key={x.addr} aria-current={x.addr === d.dept ? "page" : undefined} onClick={() => pickDept(x.addr)}>{x.name}{x.ai && <span className="pill vio">AI</span>}</button>))}</nav>
      <div className="title-row"><h1>{d.dept_name}</h1><span className={`pill ${d.kind === "daily" ? "" : "vio"}`}>{d.kind === "daily" ? "daily · past 7 days" : `${d.kind} report`}</span>
        {d.in_progress && <span className="pill">in progress</span>}<span className="sp" />
        {P.source_switch !== false && <label className="sim" style={{ display: "flex", gap: 6, alignItems: "center" }}><input type="checkbox" checked={srcOn} onChange={(e) => setSrcOn(e.target.checked)} /> Show where each part comes from</label>}</div>
      <div className="filters">
        <div className="frow">{d.kinds.length > 1 && <Seg value={d.kind} options={d.kinds.map((k: string) => [k, KIND_LABEL[k]])} onChange={pickKind} />}
          <select aria-label="Report" value={d.period} onChange={(e) => pickPeriod(e.target.value)}>{d.periods.map((p: any) => <option key={p.key} value={p.key}>{p.label}</option>)}</select>
          <span className="lbl" style={{ minWidth: 0, marginLeft: 8 }}>Area</span>
          <select value={area} onChange={(e) => setArea(e.target.value)}>{areas.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          {P.topic_view && <><span className="lbl" style={{ minWidth: 0, marginLeft: 8 }}>View</span><Seg value={view} options={[["brief", "Briefing"], ["week", monthly ? "Topic month" : "Topic week"]]} onChange={(v) => { setView(v); setParams((n) => n.delete("view")); }} /></>}</div>
      </div>
      {view === "week" && P.topic_view ? <TopicWeek period={d.period} dept={d.dept} kind={d.kind} /> : <>
        <div className={`rep-head${P.mail_per_day || P.stat_cards?.length ? "" : " solo"}`}>
          <div><p className="sim" style={{ margin: "0 0 6px" }}>{fmt(d.from)} – {fmt(d.to)} · only {d.dept_name} topics with team activity</p>
            {P.lead && d.lead && <p className="lead">{d.lead}{src(d.lead_source)}</p>}
            {d.agent_texts.done < d.agent_texts.total && d.stories.length > 0 && <div className="btn-row" style={{ marginTop: 8 }}><span className="sim">{d.agent_texts.total - d.agent_texts.done} text(s) still use the script fallback.</span>
              <button className="btn small" onClick={gen}>Write the texts now</button></div>}
            {P.left_out && d.left_out.length > 0 && <details className="sim" style={{ marginTop: 8 }}><summary>{d.left_out.length} topic{d.left_out.length > 1 ? "s" : ""} with mail but no team member involved — left out</summary>
              <ul className="elist">{d.left_out.map((x: any) => <li key={x.page_id}><button className="linkbtn" onClick={() => peek({ kind: "page", id: x.page_id })}>{x.title}</button></li>)}</ul></details>}</div>
          {(P.mail_per_day || P.stat_cards?.length > 0) && <div>{P.mail_per_day && <><div className="rc-lbl">Mail per day · {windowText} {src("script")}</div>
            {monthly ? <MonthGrid days={d.per_day} /> : <DayStrip days={d.per_day} big />}</>}
            {P.stat_cards?.length > 0 && <div className="stat-cards">{P.stat_cards.map((kk: string) => <div key={kk} className={`scard ${STATS[kk][1]}`}><b>{kk === "threads" ? d.kpis.threads : d.kpis[kk]}</b><span>{STATS[kk][0]}</span></div>)}</div>}</div>}
        </div>
        <h2 className="rep-h2">Headline stories</h2>
        {!d.stories.length && <p className="note">No topic of this team had mail with a team member involved in this {monthly ? "month" : "window"}.</p>}
        <div className="rgrid" ref={gridRef}>{d.rows.map((row: any[], i: number) => <div key={i} className={`rrow${row.length === 1 ? " single" : ""}`}>{row.map((it) => card(d.stories[it.i], it))}</div>)}</div>
        {showSrc && <p className="sim">Pairing: cards within 3 ranks whose estimated heights differ by ≤ 2 lines share a row; the shorter may show one more key email; a still-taller text is clamped. Rows: {d.rows.map((x: any[]) => x.map((y) => y.h).join("/")).join(" · ")}. The lists below are at most {listH ? `${listH}px` : "one story card"} tall.</p>}
        {lists.length > 0 && <div className="rep-lists">{lists}</div>}
        {P.other_topics && d.rest.length > 0 && <section className="card rlist" style={{ marginTop: 14, ...(listStyle || {}) }}><div className="sec-h">Other topics <span className="pill">{d.rest.length}</span>{src("script")}</div>
          <div className="tbl"><table className="mt"><thead><tr><th>Page</th><th>Status</th><th>Emails</th><th>Last note</th></tr></thead><tbody>{d.rest.map((x: any) => (
            <tr key={x.page_id}><td><button className="linkbtn" onClick={() => peek({ kind: "page", id: x.page_id })}>{x.title}</button></td><td><Status s={x.status} /></td><td>{x.n}</td><td style={{ whiteSpace: "normal" }}>{x.last_note}</td></tr>))}</tbody></table></div></section>}
        {P.footer_note && <p className="sim" style={{ marginTop: 14 }}>Each department has its own report; every topic belongs to exactly one department. Agent text: the lead + one paragraph per story, written from this {monthly ? "month's" : "window's"} mail only (separate requests, lengths checked, script fallback). Written every morning at {d.daily_time}: the daily report, on Thursdays the weekly one, on the 1st the monthly report of the month before. Which parts show is set in Settings › Report parts.</p>}
      </>}
    </div>
  );
}
