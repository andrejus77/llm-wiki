import React, { createContext, useContext, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { get } from "./api";
import { fmt, dow, Kind, pagePills, Prose, Status, TagPill } from "./util";

type Peek = { kind: "email" | "page"; id: number | string } | null;
const Ctx = createContext<{ peek: (p: Peek) => void }>({ peek: () => {} });
export const usePeek = () => useContext(Ctx).peek;

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
  const nav = useNavigate();
  const peek = usePeek();
  useEffect(() => { setD(null); get(`/emails/${id}`).then(setD).catch(() => setD({ error: true })); }, [id]);
  if (!d) return <p className="sim">Loading…</p>;
  if (d.error) return <p className="sim">Could not load this email.</p>;
  return (
    <>
      <div className="pk-h"><span className="pill vio">email</span><span className="sp" />
        <button className="btn small" onClick={() => { close(); nav(`/emails/${id}`); }}>Open in Emails</button>
        <button className="btn small" onClick={close} aria-label="Close">✕</button></div>
      <h2>{d.subject}</h2>
      <div className="sim">{d.from_name} &lt;{d.from}&gt; · {dow(d.day)} {fmt(d.day)} {d.time} · to {d.to.slice(0, 4).join(", ")}{d.cc.length ? ` · cc ${d.cc.slice(0, 4).join(", ")}` : ""}</div>
      {d.note && <p className="note" style={{ margin: "10px 0" }}><Kind kind={d.kind} /> {d.note} <span className="sim">· S1 note</span></p>}
      <div className="pk-body"><Prose text={d.body.new_text} /></div>
      {d.body.attachments.filter((a: any) => !a.inline).length > 0 && (
        <div className="atts">{d.body.attachments.filter((a: any) => !a.inline).map((a: any) => (
          <a key={a.id} className="att" href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer"><b>📎 {a.filename}</b><span className="x"> {Math.round(a.size / 1024)} kB</span></a>))}</div>
      )}
      {d.branch?.parallel && <p className="note"><span className="branch">⑂ parallel reply {d.branch.parallel.k} of {d.branch.parallel.n}</span> A separate answer to {d.branch.parallel.to} — not a reply to the other answers.</p>}
      <div className="rc-lbl" style={{ marginTop: 12 }}>Earlier in this thread · {d.earlier.length}</div>
      {d.earlier.length === 0 && <p className="sim">First email of this thread.</p>}
      {d.earlier.map((x: any, i: number) => (
        <details className="pk-prev" key={x.id} open={i === 0}>
          <summary><span className="sim">{fmt(x.day)} {x.time}</span> <b>{x.from_name}</b>
            {d.parent_id === x.id ? <span className="pill par">answered here</span> : !x.in_chain && (d.branch?.parallel || d.branch?.ghost) ? <span className="pill vio par">parallel branch</span> : null}
            {x.note ? ` · ${x.note}` : ""}</summary>
          <div className="pk-body"><Prose text={x.new_text} /></div>
          <button className="linkbtn sim" onClick={() => peek({ kind: "email", id: x.id })}>open this email</button>
        </details>
      ))}
      {d.pages.length > 0 && <><div className="rc-lbl" style={{ marginTop: 10 }}>Fed these pages</div>
        <div className="btn-row">{d.pages.map((p: any) => <button key={p.id} className="linkbtn" onClick={() => peek({ kind: "page", id: p.id })}>{p.title}</button>)}</div></>}
    </>
  );
}

function PagePeek({ id, close }: { id: number | string; close: () => void }) {
  const [d, setD] = useState<any>(null);
  const nav = useNavigate();
  useEffect(() => { setD(null); get(`/pages/${id}`).then(setD).catch(() => setD({ error: true })); }, [id]);
  if (!d) return <p className="sim">Loading…</p>;
  if (d.error) return <p className="sim">Could not load this page.</p>;
  const { cite, nums } = numberedCite();
  const secs = d.sections.filter((s: any) => s.text).slice(0, 3);
  return (
    <>
      <div className="pk-h"><span className="pill">{d.level}</span><Status s={d.status} />{d.dept_name && <span className="dept-pill">{d.dept_name}</span>}<span className="sp" />
        <button className="btn small" onClick={() => { close(); nav(`/wiki/${d.slug}`); }}>Open the page</button>
        <button className="btn small" onClick={close} aria-label="Close">✕</button></div>
      <h2>{d.title}</h2>
      <div className="rc-tags">{pagePills(d.tags).map(([ty, v]) => <TagPill key={ty + v} ty={ty} v={v} />)}</div>
      <div className="pk-body prose">{secs.map((s: any) => <div key={s.key}><h3>{s.label}</h3><Prose text={s.text} cite={cite} /></div>)}</div>
      {nums.size > 0 && <div className="srcstrip"><span className="rc-lbl">Sources</span>{[...nums.entries()].map(([eid, n]) => {
        const src = d.sections.flatMap((s: any) => s.sources).find((x: any) => x.email_id === eid);
        return <Cite key={eid} id={eid} n={`${n} ${src ? src.sender + " · " + fmt(src.day) : ""}`} />;
      })}</div>}
      <p className="sim">First 3 sections. Markers and sources open here too.</p>
    </>
  );
}

export function DrawerProvider({ children }: { children: React.ReactNode }) {
  const [p, setP] = useState<Peek>(() => {
    // deep link: ?peek=email:123 or ?peek=page:slug opens the drawer on load
    const v = new URLSearchParams(location.search).get("peek");
    if (!v || !v.includes(":")) return null;
    const [kind, id] = [v.slice(0, v.indexOf(":")), v.slice(v.indexOf(":") + 1)];
    return kind === "email" || kind === "page" ? { kind, id } : null;
  });
  useEffect(() => { const f = (e: KeyboardEvent) => e.key === "Escape" && setP(null); window.addEventListener("keydown", f); return () => window.removeEventListener("keydown", f); }, []);
  const close = () => setP(null);
  return (
    <Ctx.Provider value={{ peek: setP }}>
      {children}
      {p && <div className="peek-shade" onClick={close} />}
      {p && <aside className="peek" aria-label="Preview">{p.kind === "email" ? <EmailPeek id={Number(p.id)} close={close} /> : <PagePeek id={p.id} close={close} />}</aside>}
    </Ctx.Provider>
  );
}
