// How a mail looks, wherever it is shown: the Emails tab, a thread, the side panel.
import React, { useEffect, useState } from "react";
import { get, send } from "./api";
import { usePeek } from "./peek";
import { dow, fmt, ImpPill, Kind, Lazy, Prose, tColor, useMeta } from "./util";

export const kB = (n: number) => `${Math.max(1, Math.round((n || 0) / 1024))} kB`;

/** The first To address; the other recipients are one click away. */
export function Recipients({ to, cc }: { to: string[]; cc: string[] }) {
  const more = to.length - 1 + cc.length;
  const first = to[0] || cc[0] || "";
  if (!first) return null;
  if (!more) return <div className="r rcpt-one"><span className="lbl">To</span><span>{first}</span></div>;
  return (
    <details className="rcpt"><summary><span className="lbl">To</span><span>{first}</span><span className="sim"> + {more} more</span></summary>
      <div className="rcpt-all">
        <div><span className="lbl">To</span><span>{to.join(", ") || "—"}</span></div>
        {cc.length > 0 && <div><span className="lbl">Cc</span><span>{cc.join(", ")}</span></div>}
      </div></details>
  );
}

/** Real attachments, closed until asked for. */
export function Files({ files }: { files: any[] }) {
  if (!files?.length) return null;
  return (
    <details className="att-dd"><summary>📎 Attachments · {files.length}</summary>
      <div className="atts">{files.map((a: any) => (
        <a key={a.id} className="att" href={`/api/attachments/${a.id}`} target="_blank" rel="noreferrer"><b>{a.filename}</b><span className="x"> · {kB(a.size)}</span>
          {a.extract && <><br /><span className="x">{a.extract.slice(0, 220)}</span></>}</a>))}</div></details>
  );
}

// ---------------------------------------------------------------- translations
let trOn = false;
try { trOn = localStorage.getItem("llmwiki.translate") === "1"; } catch { /* private mode */ }
const trSubs = new Set<(v: boolean) => void>();

function useTranslateOn(): boolean {
  const [v, setV] = useState(trOn);
  useEffect(() => { trSubs.add(setV); setV(trOn); return () => { trSubs.delete(setV); }; }, []);
  return v;
}

export function TranslateToggle() {
  const on = useTranslateOn();
  const m: any = useMeta().data;
  if (!m?.translate?.enabled) return null;
  return <label className="toggle sim tr-t" title={`Mail in another language is translated when you read it, by an internal model, and kept. The original stays what is stored.`}>
    <input type="checkbox" checked={on} onChange={(e) => { trOn = e.target.checked; try { localStorage.setItem("llmwiki.translate", trOn ? "1" : "0"); } catch { /* private mode */ } trSubs.forEach((f) => f(trOn)); }} /> Show translations ({m.translate.language})</label>;
}

/** The body of a mail, translated when that is switched on and the mail is in another language. */
export function MailText({ id, lang, text, images }: { id: number; lang: string; text: string; images?: any }) {
  const on = useTranslateOn();
  const m: any = useMeta().data;
  const wanted = !!(on && m?.translate?.enabled && !(m.translate.skip || []).includes(lang));
  const [st, setSt] = useState<any>(null);
  const [orig, setOrig] = useState(false);
  const url = `/emails/${id}/translation`;
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!wanted) return;
    let alive = true, timer = 0;
    const show = (d: any) => {
      if (!alive) return;
      setSt(d);
      if (d.status === "running") timer = window.setTimeout(() => get(url).then(show).catch((e) => alive && setSt({ status: "failed", error: String(e.message || e) })), 4000);
    };
    (tick ? send("POST", url, {}) : get(url).then((d: any) => (d.status === "none" ? send("POST", url, {}) : d))).then(show).catch((e) => alive && setSt({ status: "failed", error: String(e.message || e) }));
    return () => { alive = false; clearTimeout(timer); };
  }, [wanted, url, tick]);
  const body = (t: string) => <Prose text={t} images={images} breaks />;
  if (!wanted || !st) return body(text);
  if (st.status === "done" && !orig) return <>
    <div className="tr-note">Translated from {st.from} by a model · <button className="linkbtn" onClick={() => setOrig(true)}>show the original</button>{st.subject && <div className="tr-subj"><b>{st.subject}</b></div>}</div>
    {body(st.text)}</>;
  if (st.status === "done") return <><div className="tr-note">Original · <button className="linkbtn" onClick={() => setOrig(false)}>show the translation</button></div>{body(text)}</>;
  if (st.status === "failed") return <><div className="tr-note bad">The translation failed: {st.error} <button className="linkbtn" onClick={() => setTick((n) => n + 1)}>try again</button></div>{body(text)}</>;
  return <><div className="tr-note">Translating from {st.from || "the original language"}… the original is shown until it is ready.</div>{body(text)}</>;
}

// ---------------------------------------------------------------- a mail inside a thread
/** One earlier mail: who and when, the agent's note, then the mail itself under its own toggle, on its own background. */
export function ThreadMail({ e, head, foot, day = true, extra }: { e: any; head?: React.ReactNode; foot?: React.ReactNode; day?: boolean; extra?: React.ReactNode }) {
  const peek = usePeek();
  return (
    <div className="tm">
      <div className="tm-h">{day && <span className="sim">{fmt(e.day)}</span>}<span className="sim">{e.time}</span><b>{e.from_name}</b><ImpPill imp={e.importance} />{head}</div>
      {extra}
      {(e.pages || []).length > 0 && <div className="tm-pages">{e.pages.map((p: any) => <button key={p.id} className="chip plain" title={`Open in the side panel (${p.role})`} onClick={() => peek({ kind: "page", id: p.id })}>{p.title}</button>)}</div>}
      {e.note ? <div className="anote"><span className="k">agent note</span><span><Kind kind={e.kind} /> {e.note}</span></div> : <div className="anote none"><span className="k">agent note</span><span className="sim">{e.status === "skipped" ? `skipped: ${e.skip_rule}` : "none yet"}</span></div>}
      <Lazy className="tm-full" summary={<>Email text{e.has_table ? " · table" : ""}{e.n_att ? ` · 📎 ${e.n_att}` : ""}</>}>{() => <div className="tm-body">
        <MailText id={e.id} lang={e.lang} text={e.new_text} images={e.images} />
        {!e.new_text && <p className="sim">No new text in this email.</p>}
        {e.more && <p className="sim">Long email: open it to read the rest.</p>}
        <Files files={e.files} /></div>}</Lazy>
      {foot && <div className="tm-foot">{foot}</div>}
    </div>
  );
}

/** A whole thread in order, with parallel replies in their own lane. */
export function ThreadList({ t, onOpen, compact = false }: { t: any; onOpen: (id: number) => void; compact?: boolean }) {
  let lastDay = "";
  const byId: Record<string, any> = {};
  t.emails.forEach((e: any) => { byId[`e${e.id}`] = e; });
  const laneColor = (n: number) => (n === 0 ? tColor(t.id) : `hsl(${(t.id * 47 + n * 97) % 360} 55% 50%)`);
  const indent = compact ? 14 : 28;
  return (
    <div className="thread">{(t.graph?.order || t.emails.map((e: any) => `e${e.id}`)).map((key: string) => {
      if (key.startsWith("q")) {
        const g = t.graph.ghosts[key];
        return <div key={key} className="ghost"><b>{g.header}</b> — the department never received it; several people answered it separately below<br /><span className="sim">{g.raw_header}</span><br /><span className="sim">{g.text.slice(0, 200)}</span></div>;
      }
      const e = byId[key];
      const head = e.day !== lastDay ? <div className="tday">{dow(e.day)} {fmt(e.day)}</div> : null;
      lastDay = e.day;
      return <div key={e.id}>{head}<div className="mail lane" style={{ ["--c" as any]: laneColor(e.lane || 0), marginLeft: Math.min(e.lane || 0, 4) * indent }}><span className="node" /><div className="card">
        <ThreadMail e={e} day={false}
          extra={e.parallel && <div className="branch" title="Not a reply to the email above: a separate answer to the same earlier email">⑂ parallel reply {e.parallel.k} of {e.parallel.n} to {e.parallel.to}</div>}
          head={e.reply_to && !e.parallel ? <span className="replyto">↳ reply to {e.reply_to}</span> : null}
          foot={<button className="linkbtn sim" onClick={() => onOpen(e.id)}>open this email</button>} /></div></div></div>;
    })}</div>
  );
}

export const threadLine = (t: any) => `${t.total > t.emails.length ? `The latest ${t.emails.length} of ${t.total} emails` : `${t.emails.length} emails`} · one note per email · grouped by day`;
