import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { get } from "./api";
import { Files, MailText, Recipients, ThreadList, ThreadMail, threadLine, TranslateToggle } from "./mail";
import { Peek, PeekCtx, usePeek } from "./peek";
import { fmt, dow, Kind, Prose, Status, TagPill, tColor } from "./util";

export { usePeek };

/** A clickable source marker that opens an email in the drawer. */
export function Cite({ id, n }: { id: number; n: number | string }) {
  const peek = usePeek();
  return <button className="cite" onClick={(e) => { e.preventDefault(); peek({ kind: "email", id }); }}>{n}</button>;
}

export function numberedCite() {
  const nums = new Map<number, number>();
  const cite = (id: number) => { if (!nums.has(id)) nums.set(id, nums.size + 1); return <Cite key={`c${id}`} id={id} n={nums.get(id)!} />; };
  return { cite, nums };
}

function EmailPeek({ id, close }: { id: number; close: () => void }) {
  const [d, setD] = useState<any>(null);
  const [all, setAll] = useState(false);
  const nav = useNavigate();
  const peek = usePeek();
  useEffect(() => { setD(null); setAll(false); get(`/emails/${id}`).then(setD).catch(() => setD({ error: true })); }, [id]);
  if (!d) return <p className="sim">Loading…</p>;
  if (d.error) return <p className="sim">Could not load this email.</p>;
  const earlier = all ? d.earlier : d.earlier.slice(0, 8);
  return (
    <>
      <div className="pk-h"><span className="pill vio">email</span><span className="sp" /><TranslateToggle />
        {d.thread && <button className="btn small" onClick={() => { close(); nav(`/threads/${d.thread.id}`); }}>Full thread</button>}
        <button className="btn small" onClick={() => { close(); nav(`/emails/${id}`); }}>Open in Emails</button>
        <button className="btn small" onClick={close} aria-label="Close">✕</button></div>
      <h2>{d.thread ? <button className="linkbtn pk-title" title="Open the full thread" onClick={() => { close(); nav(`/threads/${d.thread.id}`); }}>{d.subject}</button> : d.subject}</h2>
      <div className="mhead"><div className="r"><b>{d.from_name}</b><span className="sim">&lt;{d.from}&gt;</span></div>
        <div className="r sim">{dow(d.day)} {fmt(d.day)} {d.time}</div>
        <Recipients to={d.to} cc={d.cc} /></div>
      {d.note && <div className="anote"><span className="k">agent note</span><span><Kind kind={d.kind} /> {d.note}</span></div>}
      {d.pages.length > 0 && <div className="wiki-links"><span className="lbl">Wiki</span>{d.pages.map((p: any) => <button key={p.id} className="chip plain" onClick={() => peek({ kind: "page", id: p.id })}>{p.title}</button>)}</div>}
      <div className="pk-body mailbox"><MailText id={d.id} lang={d.lang} text={d.body.new_text} images={d.body.images} />
        {d.body.after.map((a: any) => <img key={a.id} className="mimg" loading="lazy" src={`/api/attachments/${a.id}?inline=true`} alt={a.filename} />)}
        {!d.body.new_text && <p className="sim">No new text in this email.</p>}</div>
      <Files files={d.body.attachments} />
      {d.branch?.parallel && <p className="note"><span className="branch">⑂ parallel reply {d.branch.parallel.k} of {d.branch.parallel.n}</span> A separate answer to {d.branch.parallel.to} — not a reply to the other answers.</p>}
      <div className="rc-lbl" style={{ marginTop: 14 }}>Earlier in this thread · {d.earlier.length}</div>
      {d.earlier.length === 0 && <p className="sim">First email of this thread.</p>}
      {earlier.map((x: any) => (
        <div className="pk-prev" key={x.id}><ThreadMail e={x}
          head={d.parent_id === x.id ? <span className="pill par">answered here</span> : !x.in_chain && (d.branch?.parallel || d.branch?.ghost) ? <span className="pill vio par">parallel branch</span> : null}
          foot={<button className="linkbtn sim" onClick={() => peek({ kind: "email", id: x.id })}>open this email</button>} /></div>
      ))}
      {!all && d.earlier.length > 8 && <button className="btn small" onClick={() => setAll(true)}>Show {d.earlier.length - 8} more</button>}
    </>
  );
}

function ThreadPeek({ id, close }: { id: number; close: () => void }) {
  const [t, setT] = useState<any>(null);
  const nav = useNavigate();
  const peek = usePeek();
  useEffect(() => { setT(null); get(`/threads/${id}`).then(setT).catch(() => setT({ error: true })); }, [id]);
  if (!t) return <p className="sim">Loading…</p>;
  if (t.error) return <p className="sim">Could not load this thread.</p>;
  const full = () => { close(); nav(`/threads/${id}`); };
  return (
    <>
      <div className="pk-h"><span className="pill vio">thread</span><span className="sp" /><TranslateToggle />
        <button className="btn small" onClick={full}>Open in Emails</button>
        <button className="btn small" onClick={close} aria-label="Close">✕</button></div>
      <h2><button className="linkbtn pk-title" title="Open the full thread in Emails" onClick={full}>{t.subject}</button></h2>
      <p className="sim" style={{ margin: "0 0 8px" }}>{threadLine(t)}</p>
      <ThreadList t={t} compact onOpen={(eid) => peek({ kind: "email", id: eid })} />
    </>
  );
}

function PagePeek({ id, close }: { id: number | string; close: () => void }) {
  const [d, setD] = useState<any>(null);
  const [allRows, setAllRows] = useState(false);
  const nav = useNavigate();
  const peek = usePeek();
  useEffect(() => { setD(null); setAllRows(false); get(`/pages/${id}`).then(setD).catch(() => setD({ error: true })); }, [id]);
  if (!d) return <p className="sim">Loading…</p>;
  if (d.error) return <p className="sim">Could not load this page.</p>;
  const { cite, nums } = numberedCite();
  const full = () => { close(); nav(`/wiki/${d.slug}`); };
  const secs = d.sections.filter((s: any) => s.text);
  const pending = d.sections.flatMap((s: any) => s.pending || []);
  const rows = allRows ? d.timeline : d.timeline.slice(0, 8);
  const tags: [string, string][] = Object.entries(d.tags || {}).flatMap(([ty, vs]: any) => (vs || []).map((v: string) => [ty, v] as [string, string]));
  return (
    <>
      <div className="pk-h"><span className="pill">{d.level}</span><Status s={d.status} />{d.dept_name && <span className="dept-pill">{d.dept_name}</span>}<span className="sp" />
        <button className="btn small" onClick={full}>Open the page</button>
        <button className="btn small" onClick={close} aria-label="Close">✕</button></div>
      <h2><button className="linkbtn pk-title" title="Open the page" onClick={full}>{d.title}</button></h2>
      {tags.length > 0 && <div className="rc-tags">{tags.map(([ty, v]) => <TagPill key={ty + v} ty={ty} v={v} />)}</div>}
      <dl className="meta pk-meta"><div><dt>Last mail</dt><dd>{fmt(d.last_day)}</dd></div><div><dt>Emails</dt><dd>{d.timeline_total}</dd></div>
        <div><dt>Threads</dt><dd>{d.threads.length}</dd></div><div><dt>People</dt><dd>{d.people.length}</dd></div><div><dt>Updated</dt><dd>{fmt(d.updated.slice(0, 10))}</dd></div></dl>
      {d.merged_into && <p className="note">This page was merged into another page.</p>}
      <div className="pk-body prose">{secs.map((s: any) => <div key={s.key}><h3>{s.label}</h3><Prose text={s.text} cite={cite} /></div>)}</div>
      {nums.size > 0 && <div className="srcstrip"><span className="rc-lbl">Sources</span>{[...nums.entries()].map(([eid, n]) => {
        const src = d.sections.flatMap((s: any) => s.sources).find((x: any) => x.email_id === eid);
        return <Cite key={eid} id={eid} n={`${n} ${src ? src.sender + " · " + fmt(src.day) : ""}`} />;
      })}</div>}
      {d.open_issues.length > 0 && <details className="pk-more" open><summary>Open issues · {d.open_issues.length}</summary>
        <ul className="elist">{d.open_issues.map((o: any) => <li key={o.code}><span className="oid sim">{o.code}</span> {o.text} {o.email_id && <Cite id={o.email_id} n="✉" />}
          <span className="sim"> · opened {fmt(o.opened)}{o.closed ? ` · closed ${fmt(o.closed)}` : ""}</span></li>)}</ul></details>}
      {pending.length > 0 && <details className="pk-more"><summary>Not yet written in · {pending.length}</summary>
        <ul className="elist">{pending.map((it: any) => <li key={it.id}>{it.text} {it.emails.map((x: number) => <Cite key={x} id={x} n="✉" />)}</li>)}</ul></details>}
      <details className="pk-more" open><summary>Timeline · {d.timeline_total} email{d.timeline_total === 1 ? "" : "s"}</summary>
        <ul className="timeline">{rows.map((e: any) => (
          <li className={`ev ${e.role}`} key={e.id}><span className="d">{fmt(e.day)}<br /><span style={{ fontSize: ".7rem" }}>{e.time}</span></span><div className="txt">
            <Kind kind={e.kind} /> {e.note || e.subject}
            <div className="row2"><span className="tn"><span className="dotc" style={{ background: tColor(e.thread_id) }} /><button className="linkbtn" onClick={() => peek({ kind: "thread", id: e.thread_id })}>thread</button></span>
              <span>· <button className="linkbtn" onClick={() => peek({ kind: "email", id: e.id })}>{e.from_name}</button></span></div></div></li>))}</ul>
        {!allRows && d.timeline.length > 8 && <button className="btn small" onClick={() => setAllRows(true)}>Show {d.timeline.length - 8} more</button>}</details>
      <details className="pk-more rail" open><summary>Source threads · {d.threads.length}</summary>
        <ul>{d.threads.map((t: any) => <li key={t.id}><span className="tn"><span className="dotc" style={{ background: tColor(t.id) }} /><button className="linkbtn" onClick={() => peek({ kind: "thread", id: t.id })}>{t.subject.slice(0, 90)}</button></span><span className="sim">{t.n}</span></li>)}</ul></details>
      <details className="pk-more rail"><summary>People · {d.people.length}</summary>
        <ul>{d.people.slice(0, 30).map((x: any) => <li key={x.addr}><span>{x.name}{!x.internal && <span className="sim"> · external</span>}</span><span className="sim">{x.n}</span></li>)}</ul></details>
      {d.attachments.length > 0 && <details className="pk-more rail"><summary>Attachments · {d.attachments.length}</summary>
        <ul>{d.attachments.map((a: any) => <li key={a.id}><a className="linkbtn" href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer">{a.filename}</a><span className="sim">{fmt(a.day)}</span></li>)}</ul></details>}
      {d.related.length > 0 && <details className="pk-more rail"><summary>Related pages · {d.related.length}</summary>
        <ul>{d.related.map((x: any) => <li key={x.id}><button className="linkbtn" onClick={() => peek({ kind: "page", id: x.id })}>{x.title}</button><span className="sim">{x.sim.toFixed(2)}</span></li>)}</ul></details>}
      {d.status_changes.length > 0 && <details className="pk-more rail"><summary>Status history · {d.status_changes.length}</summary>
        <ul>{d.status_changes.map((c: any, i: number) => <li key={i}><span>{c.old || "—"} → {c.new}</span><span className="sim">{fmt(c.at)}</span></li>)}</ul></details>}
    </>
  );
}

export function DrawerProvider({ children }: { children: React.ReactNode }) {
  // A stack: going from a page to an email to its thread inside the panel can be walked back.
  const [stack, setStack] = useState<NonNullable<Peek>[]>(() => {
    // deep link: ?peek=email:123, ?peek=thread:12 or ?peek=page:slug opens the drawer on load
    const v = new URLSearchParams(location.search).get("peek");
    if (!v || !v.includes(":")) return [];
    const [kind, id] = [v.slice(0, v.indexOf(":")), v.slice(v.indexOf(":") + 1)];
    return kind === "email" || kind === "page" || kind === "thread" ? [{ kind, id }] : [];
  });
  useEffect(() => { const f = (e: KeyboardEvent) => e.key === "Escape" && setStack([]); window.addEventListener("keydown", f); return () => window.removeEventListener("keydown", f); }, []);
  const close = () => setStack([]);
  const p = stack[stack.length - 1];
  const panel = useRef<HTMLElement>(null);
  useEffect(() => { panel.current?.scrollTo(0, 0); }, [p]);   // new content starts at its top, not where the last one was scrolled to
  const open = (x: Peek) => setStack((s) => (x ? [...s, x] : []));
  return (
    <PeekCtx.Provider value={{ peek: open }}>
      {children}
      {p && <div className="peek-shade" onClick={close} />}
      {p && <aside ref={panel} className="peek" aria-label="Preview">
        {stack.length > 1 && <button className="linkbtn sim pk-back" onClick={() => setStack((s) => s.slice(0, -1))}>← Back</button>}
        {p.kind === "email" ? <EmailPeek id={Number(p.id)} close={close} /> : p.kind === "thread" ? <ThreadPeek id={Number(p.id)} close={close} /> : <PagePeek id={p.id} close={close} />}</aside>}
    </PeekCtx.Provider>
  );
}
