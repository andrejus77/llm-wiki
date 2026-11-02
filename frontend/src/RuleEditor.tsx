import { useEffect, useState } from "react";
import { send } from "./api";
import { usePeek } from "./drawer";
import { fmt, useApi, useDebounced } from "./util";

const META = /[.*+?^${}()|[\]\\]/g;
const toRegex = (s: string) => s.replace(META, "\\$&");
/** The plain text of a regex that only escapes characters; null when it is a real regex. */
const plainOf = (s: string): string | null => (/^(?:[^.*+?^${}()|[\]\\]|\\[.*+?^${}()|[\]\\])*$/.test(s) ? s.replace(/\\(.)/g, "$1") : null);

const ACTIONS: [string, string, string][] = [
  ["skip", "Skip it", "Stored and listed, never sent to a model."],
  ["note_only", "Note only", "Gets a one-line note, never goes into the wiki."],
  ["keep", "Process it as usual", "Only files it into the folder below."],
];

export function newFolder(): Promise<number | null> {
  const name = prompt("Name of the new folder");
  if (!name || !name.trim()) return Promise.resolve(null);
  return send("POST", "/folders", { name }).then((f) => f.id).catch((e) => { alert(e.message); return null; });
}

function Summary({ p, folderName }: { p: any; folderName: string }) {
  const peek = usePeek();
  if (!p) return <p className="sim">Checking the stored mail…</p>;
  if (p.errors?.length) return <p className="sim">{p.errors.join(" ")}</p>;
  return (
    <>
      <p style={{ margin: "0 0 4px" }}><b>{p.matched}</b> stored email{p.matched === 1 ? "" : "s"} match.</p>
      <ul className="rule-effects">
        {p.waiting > 0 && <li><b>{p.waiting}</b> waiting: skipped before any model sees them</li>}
        {p.processed > 0 && <li><b>{p.processed}</b> already processed: skipped, taken off their wiki pages and out of search; the next run rewrites those pages</li>}
        {p.already_skipped > 0 && <li>{p.already_skipped} already skipped</li>}
        {p.replies_kept > 0 && <li>{p.replies_kept} replies by other people stay processed (tick “also replies” to skip them too)</li>}
        {p.manual > 0 && <li>{p.manual} decided by hand stay as they are</li>}
        {p.to_folder > 0 && <li><b>{p.to_folder}</b> go to {folderName || "the folder"}</li>}
      </ul>
      {p.sample?.length > 0 && <div className="rule-sample">{p.sample.map((x: any) => (
        <button key={x.id} className="linkbtn" onClick={() => peek({ kind: "email", id: x.id })}><span className="sim">{fmt(x.day)}</span> {x.from}: {x.subject}{x.kept_as_reply ? <span className="pill">reply, kept</span> : null}</button>))}</div>}
    </>
  );
}

/** Create or edit a mail rule, with a live count of what it does to the mail already stored. */
export function RuleDialog({ init, onClose, onSaved }: { init: any; onClose: () => void; onSaved: (msg: string) => void }) {
  const folders = useApi("/folders");
  const plain = plainOf(init.subject || "");
  const [sender, setSender] = useState<string>(init.sender || "");
  const [subject, setSubject] = useState<string>(plain ?? init.subject ?? "");
  const [regex, setRegex] = useState(plain === null);
  const [recipient, setRecipient] = useState<string>(init.recipient || "");
  const [action, setAction] = useState<string>(init.action || "skip");
  const [folder, setFolder] = useState<number | "">(init.folder || "");
  const [replies, setReplies] = useState<boolean>(!!init.include_replies);
  const [apply, setApply] = useState(true);
  const [pv, setPv] = useState<any>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const body = { sender, subject: regex ? subject : toRegex(subject), recipient, action, folder: folder || null, include_replies: replies };
  const wait = useDebounced(JSON.stringify(body), 350);
  useEffect(() => {
    let live = true;
    send("POST", "/agent/rules/preview", JSON.parse(wait)).then((d) => { if (live) setPv(d); }).catch((e) => { if (live) setPv({ errors: [e.message] }); });
    return () => { live = false; };
  }, [wait]);
  const list: any[] = (folders.data as any)?.folders || [];
  const folderName = list.find((f) => f.id === folder)?.name || "";
  const save = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = init.id ? await send("PATCH", `/agent/rules/${init.id}`, { ...body, apply }) : await send("POST", "/agent/rules", { ...body, apply });
      const a = r.applied;
      onSaved(a ? `Rule saved: ${a.skipped} skipped, ${a.note_only} note only, ${a.filed} filed${a.pages_changed ? `, ${a.pages_changed} pages to rewrite in the next run` : ""}${a.pages_archived ? `, ${a.pages_archived} pages archived` : ""}.` : "Rule saved: it applies to new mail.");
    } catch (e: any) { setErr(e.message); setBusy(false); }
  };
  return (
    <>
      <div className="peek-shade dlg-shade" onClick={onClose} />
      <div className="dlg" role="dialog" aria-label="Mail rule">
        <div className="pk-h"><b>{init.id ? "Edit rule" : "New rule"}</b><span className="sp" /><button className="btn small" onClick={onClose} aria-label="Close">✕</button></div>
        <div className="lbl">When an email… (all that are filled in must match)</div>
        <label className="rf"><span>comes from</span><input value={sender} onChange={(e) => setSender(e.target.value)} placeholder="news@vendor.example, or *@vendor.example" /></label>
        <label className="rf"><span>subject {regex ? "matches" : "contains"}</span><input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder={regex ? "regular expression" : "words in the subject"} /></label>
        <label className="rf sim"><span /><span><input type="checkbox" checked={regex} onChange={(e) => { setSubject(e.target.checked ? toRegex(subject) : (plainOf(subject) ?? subject)); setRegex(e.target.checked); }} /> regular expression</span></label>
        <label className="rf"><span>is sent to (To/Cc)</span><input value={recipient} onChange={(e) => setRecipient(e.target.value)} placeholder="dl-ct@corp.example (optional)" /></label>
        <div className="lbl" style={{ marginTop: 10 }}>Then…</div>
        <div className="rule-acts">{ACTIONS.map(([v, l, h]) => <label key={v}><input type="radio" name="act" checked={action === v} onChange={() => setAction(v)} /> <b>{l}</b> <span className="sim">{h}</span></label>)}</div>
        {action !== "keep" && <label className="sim rf-check"><input type="checkbox" checked={replies} onChange={(e) => setReplies(e.target.checked)} /> also replies and forwards written by other people (off: feedback on an automated mail is still processed)</label>}
        <label className="rf"><span>move to folder</span><span className="rf-row"><select value={folder} onChange={(e) => setFolder(e.target.value ? +e.target.value : "")}><option value="">{action === "keep" ? "choose a folder" : "no folder"}</option>{list.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}</select>
          <button className="btn small" onClick={async () => { const id = await newFolder(); if (id) { folders.reload(); setFolder(id); } }}>New folder</button></span></label>
        <div className="rule-pv"><Summary p={pv} folderName={folderName} /></div>
        <label className="rf-check"><input type="checkbox" checked={apply} onChange={(e) => setApply(e.target.checked)} /> Also apply to the mail already stored{init.id ? " (changing a rule first undoes what it did)" : ""}</label>
        {err && <p className="note" style={{ borderColor: "var(--red)" }}>{err}</p>}
        <div className="btn-row" style={{ marginTop: 10 }}><button className="btn primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save rule"}</button><button className="btn" onClick={onClose}>Cancel</button></div>
      </div>
    </>
  );
}

/** The rule list with edit, on/off and delete. Used in Emails (admin mode) and in Agent › Rules. */
export function RulesPanel({ onChanged }: { onChanged?: () => void }) {
  const r = useApi("/agent/rules");
  const [edit, setEdit] = useState<any>(null);
  const [msg, setMsg] = useState("");
  const d: any = r.data;
  if (!d) return <p className="sim">Loading…</p>;
  const admin = d.admin;
  const done = (m: string) => { setMsg(m); setEdit(null); r.reload(); onChanged?.(); };
  const toggle = async (x: any) => {
    if (x.enabled && x.hits && !confirm(`Turn this rule off? The ${x.hits} emails it skipped or limited go back to normal, and skipped ones are processed in the next run.`)) return;
    try { await send("PATCH", `/agent/rules/${x.id}`, { enabled: !x.enabled }); done(x.enabled ? "Rule turned off and undone." : "Rule turned on and applied."); } catch (e: any) { setMsg(e.message); }
  };
  const del = async (x: any) => {
    if (!confirm(`Delete this rule? ${x.hits ? `The ${x.hits} emails it skipped or limited go back to normal, and skipped ones are processed in the next run.` : ""}`)) return;
    try { await send("DELETE", `/agent/rules/${x.id}`); done("Rule deleted and undone."); } catch (e: any) { setMsg(e.message); }
  };
  const all = async () => {
    if (!confirm("Run every enabled rule on all stored mail now?")) return;
    try { const a = await send("POST", "/agent/rules/apply_all"); done(`Applied ${a.rules} rules: ${a.skipped} skipped, ${a.note_only} note only, ${a.filed} filed, ${a.pages_changed} pages to rewrite.`); } catch (e: any) { setMsg(e.message); }
  };
  const label = (a: string) => (a === "skip" ? "skip" : a === "note_only" ? "note only" : "process");
  return (
    <div className="card tbl">
      <div className="sec-h">Mail rules <span className="sp" />{admin && <><button className="btn small primary" onClick={() => setEdit({})}>New rule</button><button className="btn small" onClick={all}>Apply all to stored mail</button></>}</div>
      {msg && <p className="note" style={{ margin: "0 0 8px" }}>{msg}</p>}
      {!d.rules.length ? <p className="sim" style={{ margin: 0 }}>No rules yet.{admin ? " Open an email and choose “Create rule from this email”, or press New rule." : ""}</p> :
        <table className="mt"><thead><tr><th>When</th><th>Kind</th><th>Then</th><th>Folder</th><th>Also replies</th><th>Mail</th><th>On</th><th>Why</th><th /></tr></thead><tbody>
          {d.rules.map((x: any) => <tr key={x.id} className={x.enabled ? "" : "off"}>
            <td style={{ whiteSpace: "normal", maxWidth: 340 }}><code>{[x.sender, x.subject || x.template, x.recipient && `to ${x.recipient}`].filter(Boolean).join(" · ")}</code></td><td>{x.kind}</td><td>{label(x.action)}</td><td>{x.folder_name}</td>
            <td>{x.include_replies ? "yes" : ""}</td><td className="num">{x.hits}</td>
            <td><input type="checkbox" disabled={!admin} checked={x.enabled} onChange={() => toggle(x)} aria-label="Rule on" /></td>
            <td className="sim" style={{ whiteSpace: "normal" }}>{x.why}</td>
            <td>{admin && <>{x.kind === "static" && <button className="linkbtn sim" onClick={() => setEdit(x)}>edit</button>} <button className="linkbtn sim" onClick={() => del(x)}>delete</button></>}</td></tr>)}</tbody></table>}
      {edit && <RuleDialog key={edit.id || "new"} init={edit} onClose={() => setEdit(null)} onSaved={done} />}
    </div>
  );
}
