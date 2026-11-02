import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { get, send } from "../api";
import { usePeek } from "../drawer";
import { RulesPanel } from "../RuleEditor";
import { Empty, Err, fmt, k, Loading, refreshMeta, Seg, useApi } from "../util";

const SUBS: [string, string][] = [["logs", "Logs"], ["pipeline", "Pipeline"], ["pool", "Pool"], ["rules", "Rules"], ["tags", "Tags"], ["settings", "Settings"], ["eval", "Eval"]];
const SEC_COLORS = ["#3B7DD8", "#16A0A8", "#C2477F", "#5FB8BE", "#D9822B", "#2E9E62", "#9B59D0", "#7A8794", "#B8A12E"];

function Bar({ sections, cap }: { sections: any[]; cap?: number }) {
  const total = Math.max(cap || 0, sections.reduce((a, s) => a + (s.sent || 0), 0), 1);
  return (
    <div className="sbar" title={sections.map((s) => `${s.label}: ${s.sent}`).join(" · ")} style={{ display: "flex", height: 10, borderRadius: 4, overflow: "hidden", background: "var(--sunken)", margin: "6px 0" }}>
      {sections.map((s, i) => <i key={i} style={{ width: `${(100 * (s.sent || 0)) / total}%`, background: SEC_COLORS[i % SEC_COLORS.length] }} />)}
    </div>
  );
}

function SecTable({ sections }: { sections: any[] }) {
  return <div className="tbl"><table className="mt secs"><thead><tr><th>Section</th><th>wanted</th><th>budget</th><th>sent</th></tr></thead><tbody>{sections.map((s, i) => (
    <tr key={i}><td><i style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: SEC_COLORS[i % SEC_COLORS.length], marginRight: 5 }} />{s.label}</td>
      <td className="num">{k(s.want)}</td><td className="num">{k(s.budget)}</td><td className={`num ${s.cut_cap ? "cutcap" : s.cut_budget ? "cut" : ""}`}>{k(s.sent)}{s.cut_budget ? " ✂" : ""}{s.cut_cap ? " ✂cap" : ""}</td></tr>))}</tbody></table></div>;
}

function Call({ c }: { c: any }) {
  const [full, setFull] = useState<any>(null);
  const stCls = c.status === "ok" ? "ok" : c.status === "invalid" || c.status === "timeout" ? "risk" : "bad";
  return (
    <div className="req">
      <div className="top"><span className="kind">{c.stage}</span><b>round {c.round}</b><span className={`pill ${stCls}`}>{c.status}</span>
        <span className="sim">{c.endpoint} · ~{k(c.prompt_tokens)} tokens · {(c.latency_ms / 1000).toFixed(1)} s{c.queue_wait_ms > 1000 ? ` · waited ${(c.queue_wait_ms / 1000).toFixed(0)} s` : ""}{c.redactions ? ` · ${c.redactions} PII items removed` : ""} · {c.at.slice(11, 19)}</span></div>
      {c.sections?.length > 0 && <Bar sections={c.sections} />}
      {c.errors?.length > 0 && <ul className="writes">{c.errors.map((e: string, i: number) => <li key={i} className="tag">✗ {e}</li>)}</ul>}
      <details onToggle={(e) => (e.target as HTMLDetailsElement).open && !full && get(`/agent/calls/${c.id}`).then(setFull)}>
        <summary className="sim">prompt sections, prompt and answer</summary>
        {c.sections?.length > 0 && <SecTable sections={c.sections} />}
        {full ? <>
          <div className="lbl">System prompt</div><pre className="json">{full.system}</pre>
          <div className="lbl">Prompt</div><pre className="json" style={{ maxHeight: 420, overflow: "auto" }}>{full.prompt}</pre>
          <div className="lbl">Answer</div><pre className="json" style={{ maxHeight: 420, overflow: "auto" }}>{full.response}</pre></> : <p className="sim">Loading…</p>}
      </details>
    </div>
  );
}

// ---------------------------------------------------------------- Logs
function Logs() {
  const [kind, setKind] = useState("");
  const [issues, setIssues] = useState(false);
  const [pst, setPst] = useState({ since: "", until: "" });
  const r = useApi("/agent/logs", { kind, issues: issues || undefined });
  const d: any = r.data;
  useEffect(() => { const t = setInterval(() => { if (!document.hidden) r.reload(); }, 15000); return () => clearInterval(t); }, []);
  const act = async (name: string, body: any = {}) => { const x = await send("POST", `/actions/${name}`, body); alert(x.started ? `${name} started — this page refreshes every 15 s` : `${name} is already running`); r.reload(); refreshMeta(); };
  if (r.error) return <Err e={r.error} />;
  if (!d) return <Loading />;
  const s = d.schedule, h = d.health;
  return (
    <>
      <div className="card health"><div className="sec-h">Schedule <span className={`pill ${s.live ? "ok" : ""}`}>{s.live ? "live + nightly batch" : "nightly batch only"}</span>{s.paused_for_chat && <span className="pill risk">paused for chat</span>}</div>
        <div className="hl"><span>batch at <b>{s.batch_time}</b>{s.batch_enabled ? "" : " (off)"} · last batch {s.last_batch || "never"}</span>
          {s.live ? <span>· live every <b>{s.live_tick} min</b>, lint every {s.live_lint} min{s.live_failures ? ` · ${s.live_failures} failed ticks in a row` : ""}</span> : <span>· live mode off (Settings › Schedule)</span>}
          <span>· last 24 h: {d.rates_24h.calls} model calls, {d.rates_24h.units} units, {d.rates_24h.units_retried} retried, {d.rates_24h.fast_path_fallbacks} fast-path fallbacks, {d.rates_24h.rejected} rate-limited, {d.rates_24h.timeouts} timeouts, <b className={d.rates_24h.blocked ? "risk" : ""}>{d.rates_24h.blocked} refused</b> (Agent › Pool)</span></div>
        <div className="btn-row" style={{ marginTop: 8 }}>
          {d.processing && <button className="btn small" onClick={async () => { await send("POST", "/agent/stop"); r.reload(); }}>Stop processing</button>}
          {[["fetch", "Fetch mail now"], ["process", "Process waiting mail"], ["batch", "Run the nightly batch now"], ["review", "Run the reviewer"], ["rewrite_pages", "Rewrite pages over budget"], ["reindex", "Re-index"], ["backup", "Back up the DB"]].map(([a, l]) =>
            <button key={a} className="btn small" onClick={() => act(a)} disabled={d.running.includes(a)}>{l}{d.running.includes(a) ? " (running)" : ""}</button>)}</div>
        <div className="btn-row" style={{ marginTop: 8 }}><span className="sim">Backfill from the Outlook .pst:</span>
          <input type="date" value={pst.since} onChange={(e) => setPst({ ...pst, since: e.target.value })} aria-label="since" />
          <input type="date" value={pst.until} onChange={(e) => setPst({ ...pst, until: e.target.value })} aria-label="until" />
          <button className="btn small" disabled={d.running.includes("pst")} onClick={() => act("pst", pst)}>1. Import mail</button>
          <button className="btn small" disabled={d.running.includes("backfill")} onClick={() => act("backfill")}>2. Process as backfill (week windows)</button></div></div>
      <div className="card health" style={{ marginTop: 10 }}><div className="sec-h">Store health <span className={`pill ${h.ok ? "ok" : "bad"}`}>{h.ok ? "ok" : "problem"}</span></div>
        <div className="hl"><span><b>{h.total}</b> emails in {h.threads} threads</span>{Object.entries(h.by_status).map(([k2, v]: any) => <span key={k2}>· {v} {k2}</span>)}
          <span>· {h.unseen_blocks} quoted blocks with no stored original ({h.unseen_sent} already shown to the model once)</span>
          {h.checks.map((c: any) => <span key={c[0]}>· {c[0]} {c[1] ? "✓" : `✗ (${c[2]})`}</span>)}</div></div>
      <div className="filters"><div className="frow"><span className="lbl">Kind</span>
        <Seg value={kind} options={[["", "All"], ["fetch", "Fetch"], ["batch", "Batch"], ["live", "Live"], ["backfill", "Backfill"]]} onChange={setKind} />
        <label className="toggle sim"><input type="checkbox" checked={issues} onChange={() => setIssues(!issues)} /> only runs with problems</label></div></div>
      {!d.runs.length && <Empty>No runs yet.</Empty>}
      {d.runs.map((run: any, i: number) => (
        <details className="run" key={run.id} open={i === 0}><summary>
          <span className={`pill ${run.kind === "fetch" ? "" : "open"}`}>{run.kind}</span><b>{fmt(run.started.slice(0, 10))} {run.started.slice(11, 16)}</b>
          <span className="sim">{run.stats.seconds ? `${run.stats.seconds} s · ` : ""}{run.stats.units !== undefined ? `${run.stats.units} units · ${run.stats.fast} fast path · ${run.stats.pages_edited} page edits · ${run.stats.new_pages} new pages` : run.stats.fetched !== undefined ? `${run.stats.fetched} fetched · ${run.stats.stored} stored · ${run.stats.merged} merged · ${run.stats.duplicate} duplicates` : ""}</span>
          <span className={`pill ${run.status === "done" ? (run.jobs_failed ? "risk" : "ok") : run.status === "failed" || run.status === "stopped" ? "bad" : "open"}`}>{run.status}{run.jobs_failed ? ` · ${run.jobs_failed} failed jobs` : ""}</span></summary>
          <div className="req"><ul className="writes">{run.events.map((e: any, j: number) => <li key={j} className={e.level === "error" ? "tag" : e.level === "warning" ? "sec" : "meta2"}><span className="sim">{e.ts.slice(11, 19)} · {e.kind}</span> {e.message}</li>)}</ul>
            {run.note && <pre className="json">{run.note}</pre>}
            {Object.keys(run.jobs).length > 0 && <div className="sim">Jobs: {Object.entries(run.jobs).map(([st, n]: any) => `${st} ${n}`).join(" · ")}</div>}</div></details>))}
      {d.events.length > 0 && <div className="card"><div className="sec-h">Other events</div><ul className="writes">{d.events.map((e: any, i: number) => <li key={i} className={e.level === "error" ? "tag" : e.level === "warning" ? "sec" : "meta2"}><span className="sim">{e.ts.slice(0, 16).replace("T", " ")} · {e.kind}</span> {e.message}</li>)}</ul></div>}
    </>
  );
}

// ---------------------------------------------------------------- Pipeline
function Pipeline() {
  const [sp, setSp] = useSearchParams();
  const unit = sp.get("unit") || "";
  const call = sp.get("call");
  const units = useApi("/agent/units", { limit: 300 });
  const r = useApi(unit ? "/agent/unit" : null, { unit });
  const c = useApi(call ? `/agent/calls/${call}` : null);
  const peek = usePeek();
  const d: any = r.data;
  const list: any[] = (units.data as any[]) || [];
  return (
    <>
      <div className="btn-row" style={{ marginBottom: 14 }}><span className="lbl">Unit</span>
        <select className="select-wide" value={unit} onChange={(e) => setSp({ unit: e.target.value })}><option value="">— choose a processing unit —</option>
          {list.map((u) => <option key={u.unit} value={u.unit}>{u.unit} · {u.stage} · {u.status}</option>)}</select></div>
      <p className="sim">A unit is a thread-day (<code>td:thread:day</code>), a live delta (<code>dl:…</code>) or a page edit (<code>page:id</code>). Each request shows its prompt sections with budgets (✂ = trimmed to its budget, ✂cap = cut by the priority list), the answer, and the script's checks.</p>
      {call && c.data && <><h3>Request {call}</h3><Call c={c.data} /></>}
      {unit && !d && <Loading />}
      {d && <>
        {d.emails.length > 0 && <div className="card" style={{ marginBottom: 12 }}><div className="sec-h">Emails of this unit (numbered as in the prompt)</div>
          <ul className="elist">{d.emails.map((e: any, i: number) => <li key={e.id}><b>[{i + 1}]</b> {e.time} · {e.from_name}: <button className="linkbtn" onClick={() => peek({ kind: "email", id: e.id })}>{e.subject}</button><br /><span className="sim">{e.importance} · {e.note}</span></li>)}</ul></div>}
        {d.jobs.map((j: any) => <div className="card" key={j.id} style={{ marginBottom: 10 }}>
          <div className="sec-h"><span className="kind">{j.stage}</span> job · <span className={`pill ${j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "open"}`}>{j.status}</span><span className="sim">attempts {j.attempts}</span></div>
          {j.error && <pre className="json">{j.error}</pre>}
          {Object.keys(j.result || {}).length > 0 && <details><summary className="sim">what the script wrote</summary><pre className="json">{JSON.stringify(j.result, null, 1)}</pre></details>}</div>)}
        <h3>Model requests</h3>{d.calls.map((x: any) => <Call key={x.id} c={x} />)}
        {d.editor_calls.length > 0 && <><h3>Editor requests on the pages this unit fed</h3>{d.editor_calls.map((x: any) => <Call key={x.id} c={x} />)}</>}
      </>}
    </>
  );
}

// ---------------------------------------------------------------- Refusals (blocked input) per endpoint
function Refusals({ d }: { d: any }) {
  const nav = useNavigate();
  if (!d) return null;
  const days = Object.keys(d.by_day);
  const eps = Object.keys(d.totals);
  return (
    <div className="card tbl" style={{ marginBottom: 14 }}><div className="sec-h">Refusals · requests an API blocked, last {d.days} days
      {eps.map((ep) => <span key={ep} className="pill risk">{ep}: {d.totals[ep]} ({Math.round((d.rate[ep] || 0) * 100)} % of its requests)</span>)}</div>
      {!d.recent.length ? <p className="sim" style={{ margin: 0 }}>No refusals. When an API blocks an input (e.g. its personal-data filter), it shows here with what happened next.</p> : <>
        <p className="sim" style={{ margin: "0 0 6px" }}>Per day: {days.map((dd) => `${fmt(dd)} ${Object.entries(d.by_day[dd]).map(([ep, n]: any) => `${ep} ${n}`).join(", ")}`).join(" · ")}</p>
        <table className="mt"><thead><tr><th>Time</th><th>API</th><th>Stage</th><th>Unit</th><th>Reason given</th><th>PII removed</th><th>Then</th></tr></thead><tbody>
          {d.recent.map((c: any) => <tr key={c.id}><td className="num">{fmt(c.at.slice(0, 10))} {c.at.slice(11, 16)}</td><td>{c.endpoint}{c.http_code ? ` · ${c.http_code}` : ""}</td>
            <td><span className="kind">{c.stage}</span></td><td><button className="linkbtn" onClick={() => nav(`/agent/pipeline?call=${c.id}`)}>{c.unit}</button></td>
            <td style={{ whiteSpace: "normal", maxWidth: 420 }}>{c.error}</td><td className="num">{c.redactions}</td>
            <td>{c.then ? `${c.then.endpoint}: ${c.then.status}` : <span className="pill bad">no failover</span>}</td></tr>)}</tbody></table></>}
    </div>
  );
}

// ---------------------------------------------------------------- Pool
function Pool() {
  const r = useApi("/agent/pool");
  useEffect(() => { const t = setInterval(() => { if (!document.hidden) r.reload(); }, 5000); return () => clearInterval(t); }, []);
  const d: any = r.data;
  if (!d) return <Loading />;
  return (
    <>
      <p className="sim" style={{ marginTop: 0 }}>An endpoint stops after the failed or rejected requests in a row set in Settings › Pool › breaker_failures; when no endpoint is left for a stage, the run stops.
        {d.endpoints.some((e: any) => e.stopped) && <button className="btn small" style={{ marginLeft: 8 }} onClick={async () => { await send("POST", "/agent/resume"); r.reload(); refreshMeta(); }}>Resume</button>}</p>
      <div className="tt-grid" style={{ marginBottom: 14 }}>
        <div className="card"><div className="sec-h">Endpoints</div><div className="tbl"><table className="mt"><thead><tr><th>Name</th><th>Kind · model</th><th>Limit</th><th>Last 60 s</th><th>Context</th><th>Stats</th></tr></thead><tbody>
          {d.endpoints.map((e: any) => <tr key={e.name} className={e.enabled ? "" : "off"}><td><b>{e.name}</b>{e.stopped && <span className="pill bad" title={e.stopped.reason}> stopped</span>}{e.cooling_s ? <span className="pill risk"> cooling {e.cooling_s}s</span> : null}</td>
            <td className="sim">{e.kind} · {e.model}{e.external && <span className="pill risk"> external</span>}{e.redact_pii && <span className="pill"> PII removed</span>}{e.structured && <span className="pill"> schema</span>}</td><td className="sim">{e.rpm}/min{e.rpm !== e.rpm_config ? ` (lowered from ${e.rpm_config})` : ""} · {e.max_concurrent} parallel · timeout {e.timeout_s || "none"}</td>
            <td><span className="rpm">{Array.from({ length: Math.min(e.rpm, 12) }, (_, i) => <i key={i} className={i < e.used_60s ? "on" : ""} />)}</span> {e.used_60s}/{e.rpm} · {e.in_flight} running</td>
            <td className="num">{k(e.context)}</td><td className="sim">ok {e.stats.ok} · 429 {e.stats.rejected} · timeout {e.stats.timeout} · invalid {e.stats.invalid}{e.stats.blocked ? ` · blocked ${e.stats.blocked}` : ""}{e.avg_latency_ms ? ` · avg ${(e.avg_latency_ms / 1000).toFixed(1)} s` : ""}</td></tr>)}</tbody></table></div>
          <p className="sim" style={{ margin: "8px 0 0" }}>{d.paused_for_chat ? "Background work is paused because someone is chatting." : "A job goes to the first free endpoint that allows its stage and fits its budget."}</p></div>
        <div className="card"><div className="sec-h">Backlog <span className="sp" /><span className="pill">ETA {d.eta.minutes !== null ? `≈ ${d.eta.minutes} min` : "—"}</span></div>
          <div className="qrow"><span>Emails waiting</span><b>{d.queue.emails_waiting}</b></div><div className="qrow"><span>Items for the editor</span><b>{d.queue.items_pending}</b></div>
          <div className="qrow"><span>Requests (estimate)</span><b>{d.eta.requests_est}</b></div><div className="qrow"><span>Throughput</span><b>{d.eta.per_min}/min</b></div>
          <p className="sim">Last hour: {Object.entries(d.last_hour).map(([s, n]: any) => `${s} ${n}`).join(" · ") || "no calls"}</p></div>
      </div>
      <Refusals d={d.refusals} />
      <div className="card tbl"><div className="sec-h">Recent calls</div><table className="mt"><thead><tr><th>Time</th><th>Stage</th><th>Unit</th><th>Endpoint</th><th>Status</th><th>Wait</th><th>Latency</th><th>Input</th></tr></thead><tbody>
        {d.recent.map((c: any) => <tr key={c.id}><td className="num">{c.at.slice(11, 19)}</td><td><span className="kind">{c.stage}</span></td><td>{c.unit}</td><td>{c.endpoint}</td><td>{c.status}{c.round > 1 ? ` (round ${c.round})` : ""}</td>
          <td className="num">{(c.queue_wait_ms / 1000).toFixed(0)} s</td><td className="num">{(c.latency_ms / 1000).toFixed(1)} s</td><td className="num">{k(c.prompt_tokens)}</td></tr>)}</tbody></table></div>
    </>
  );
}

// ---------------------------------------------------------------- Rules
function Rules() {
  const r = useApi("/agent/rules");
  const d: any = r.data;
  if (!d) return <Loading />;
  const b = d.builtin;
  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}><div className="sec-h">How mail is skipped</div>
        <ol className="flow"><li><b>Headers</b>: Auto-Submitted → {b.auto_submitted}; Precedence bulk / List-Id → {b.bulk}.</li>
          <li><b>Automatic replies</b> → {b.auto_reply}: out-of-office / automatic-reply / 자동 회신 subjects, delivery failures, no-reply senders ({(b.noreply_senders || []).slice(0, 3).join(", ")}…). They stay skipped even when they answer a thread.</li>
          <li><b>Mail rules</b> below (sender, subject, recipient): <i>skip</i> = stored and shown greyed, never sent to the model; <i>note only</i> = noted but never routed; any rule can also file the mail into a folder. A new rule can also run on the stored mail; changing or removing it undoes what it did.
            {b.rules_skip_replies ? " Rules also skip replies and forwards." : " A reply or forward written by someone else (e.g. feedback on an automated mail) is still processed, unless the rule has “also replies”."}</li>
          <li><b>Importance label</b> from the note-taker: <code>not_important</code> stops after the note.</li>
          <li><b>Learned rules</b> ({b.learned_enabled ? "on" : "off"}): a sender + subject template judged unimportant ≥ {b.learn_min} times on ≥ {b.learn_days} days becomes a rule; every {b.audit_every}th match is still processed and an important result disables the rule.</li></ol>
        <p className="sim">{d.skipped_30d} emails skipped in the last 30 days.</p></div>
      <RulesPanel onChanged={r.reload} />
    </>
  );
}

// ---------------------------------------------------------------- Tags
function Tags() {
  const r = useApi("/agent/tags");
  const [ty, setTy] = useState("");
  const d: any = r.data;
  if (!d) return <Loading />;
  const rows = d.rows.filter((x: any) => !ty || x.type === ty);
  return (
    <>
      <div className="card" style={{ marginBottom: 14 }}><div className="sec-h">Tags belong to pages</div>
        <p className="sim" style={{ margin: 0 }}>System / process / work type come from fixed lists (Settings › Tags), assigned by the S4 tagger. Site, partner and t-code tags are aggregated by script from a page's emails. New t-codes are discovered in t-code context and promoted automatically. Reference numbers (POs, invoices, deliveries) are identifiers, not tags.</p></div>
      <div className="card tbl"><div className="sec-h">Values <span className="sp" /><Seg value={ty} options={[["", "All"], ["system", "System"], ["process", "Process"], ["work_type", "Work type"], ["site", "Site"], ["partner", "Partner"], ["tcode", "T-code"]]} onChange={setTy} /></div>
        <table className="tag-table"><thead><tr><th>Value</th><th>Type</th><th>State</th><th>Pages</th><th>Threads</th><th>Why / context</th></tr></thead><tbody>
          {rows.map((x: any, i: number) => <tr key={i}><td><span className={`chip sm t-${x.type}`}>{x.value}</span></td><td className="sim">{x.type}</td><td><span className={`life ${x.state}`}>{x.state}</span></td>
            <td className="sim">{x.pages}</td><td className="sim">{x.threads ?? ""}</td><td className="sim" style={{ whiteSpace: "normal" }}>{x.why}{x.contexts?.length ? ` · “${x.contexts[0]}”` : ""}</td></tr>)}</tbody></table></div>
      {d.log.length > 0 && <div className="card" style={{ marginTop: 14 }}><div className="sec-h">Registry log</div><ul className="writes">{d.log.map((e: any, i: number) => <li key={i} className="meta2"><b>{fmt(e.ts.slice(0, 10))}</b> · {e.message}</li>)}</ul></div>}
    </>
  );
}

// ---------------------------------------------------------------- Settings (generated from the Pydantic schema)
function resolve(schema: any, node: any): any {
  if (node?.$ref) return schema.$defs[node.$ref.split("/").pop()];
  if (node?.allOf?.length === 1) return resolve(schema, node.allOf[0]);
  return node;
}

function Field({ name, spec, value, def, onChange }: { name: string; spec: any; value: any; def: any; onChange: (v: any) => void }) {
  const changed = JSON.stringify(value) !== JSON.stringify(def);
  const t = spec.type || (spec.enum ? "string" : spec.anyOf ? "string" : "object");
  let input;
  if (spec.enum) input = <select value={value} onChange={(e) => onChange(e.target.value)}>{spec.enum.map((o: string) => <option key={o}>{o}</option>)}</select>;
  else if (t === "boolean") input = <label className="toggle"><input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /> {value ? "on" : "off"}</label>;
  else if (t === "integer" || t === "number") input = <input type="number" value={value} min={spec.minimum} max={spec.maximum} step={t === "integer" ? 1 : 0.01} onChange={(e) => onChange(t === "integer" ? parseInt(e.target.value || "0") : parseFloat(e.target.value || "0"))} />;
  else if (t === "array" && (spec.items?.type === "string" || !spec.items?.$ref)) input = <textarea rows={Math.min(6, Math.max(2, (value || []).length))} value={(value || []).join("\n")} onChange={(e) => onChange(e.target.value.split("\n").map((x) => x.trim()).filter(Boolean))} />;
  else if (t === "object" && value && Object.values(value).every((x) => typeof x === "number")) input = <div className="dictnum">{Object.entries(value).map(([kk, v]: any) => (
    <label key={kk}><span className="sim">{kk}</span><input type="number" value={v} onChange={(e) => onChange({ ...value, [kk]: parseInt(e.target.value || "0") })} /></label>))}</div>;
  else if (t === "object" || t === "array") input = <textarea rows={4} defaultValue={JSON.stringify(value, null, 1)} onBlur={(e) => { try { onChange(JSON.parse(e.target.value)); } catch { alert("Not valid JSON"); } }} />;
  else input = <input type="text" value={value ?? ""} onChange={(e) => onChange(e.target.value)} />;
  return (
    <div className="fld"><span>{spec.title || name}<span className="hint"><code>{name}</code>{spec.description ? ` · ${spec.description}` : ""}{spec.minimum !== undefined ? ` · ${spec.minimum}–${spec.maximum}` : ""}{spec.tier ? ` · ${spec.tier}` : ""}</span>
      {changed && <><span className="chg">changed</span><button className="linkbtn sim" onClick={() => onChange(def)}>reset</button></>}</span>{input}</div>
  );
}

function Endpoints({ value, onChange, schema }: { value: any[]; onChange: (v: any[]) => void; schema: any }) {
  const spec = schema.$defs.Endpoint.properties;
  const cols = ["name", "kind", "model", "base_url", "api_key_secret", "rpm", "max_concurrent", "timeout_s", "context_tokens", "stages", "external", "redact_pii", "structured_output", "enabled", "priority"];
  const set = (i: number, kk: string, v: any) => onChange(value.map((e, j) => (j === i ? { ...e, [kk]: v } : e)));
  return (
    <div className="fld wide"><span>Endpoints<span className="hint">Each endpoint has its own limits; a job goes to the first free one that allows its stage. <b>api_key_secret</b> is the name of a stored key (<code>llmwiki secret set &lt;name&gt;</code>), never the key itself. <b>http_json</b> = an API described in settings (<code>llmwiki api probe</code> finds the settings); <b>custom</b> = company_adapter.py in the data folder; <b>claude_cli / gemini_cli / codex_cli</b> = the logged-in CLI on this PC. <b>external</b> = data leaves the company: only the stages in Privacy › external_stages, PII always removed. <b>structured_output</b> = the stage's JSON schema is passed to the API.</span></span>
      <div className="tbl" style={{ width: "100%" }}><table className="mt"><thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}<th /></tr></thead><tbody>{value.map((e, i) => (
        <tr key={i}>{cols.map((c) => <td key={c}>{spec[c].enum ? <select value={e[c]} onChange={(x) => set(i, c, x.target.value)}>{spec[c].enum.map((o: string) => <option key={o}>{o}</option>)}</select>
          : spec[c].type === "boolean" ? <input type="checkbox" checked={e[c]} onChange={(x) => set(i, c, x.target.checked)} />
          : spec[c].type === "integer" ? <input type="number" style={{ width: 80 }} value={e[c]} onChange={(x) => set(i, c, parseInt(x.target.value || "0"))} />
          : c === "stages" ? <input style={{ width: 90 }} value={(e[c] || []).join(",")} onChange={(x) => set(i, c, x.target.value.split(",").map((y) => y.trim()).filter(Boolean))} />
          : <input style={{ width: c === "base_url" ? 180 : 110 }} value={e[c] ?? ""} onChange={(x) => set(i, c, x.target.value)} />}</td>)}
          <td><button className="linkbtn sim" onClick={() => onChange(value.filter((_, j) => j !== i))}>remove</button></td></tr>))}</tbody></table></div>
      <button className="btn small" onClick={() => onChange([...value, { name: `api-${value.length + 1}`, kind: "custom", model: "", base_url: "", api_key_secret: "", rpm: 3, max_concurrent: 2, timeout_s: 0, context_tokens: 150000, max_output_tokens: 8000, stages: ["*"], external: false, redact_pii: false, structured_output: false, enabled: true, priority: 0, options: {} }])}>Add endpoint</button></div>
  );
}

function SettingsView() {
  const r = useApi("/settings");
  const [vals, setVals] = useState<any>(null);
  const [grp, setGrp] = useState("sources");
  const [msg, setMsg] = useState("");
  const d: any = r.data;
  useEffect(() => { if (d) setVals(JSON.parse(JSON.stringify(d.values))); }, [d]);
  if (!d || !vals) return <Loading />;
  const schema = d.schema;
  const gspec = resolve(schema, schema.properties[grp]);
  const changedIn = (g: string) => JSON.stringify(vals[g]) !== JSON.stringify(d.values[g]);
  const save = async () => { try { await send("PUT", "/settings", { values: vals, note: "saved in the portal" }); setMsg("Saved."); r.reload(); } catch (e) { setMsg(String(e)); } };
  return (
    <>
      <p className="sim" style={{ marginTop: 0 }}>Generated from the Pydantic <code>Settings</code> model, so every setting is here with its description, default and bounds. Saving validates and stores a new version (history below). API keys are never stored here — only the names of the stored secrets (<code>llmwiki secret set</code>).</p>
      <div className="set-layout"><nav className="set-nav">{Object.entries(d.groups).map(([g, l]: any) => <button key={g} aria-current={grp === g} onClick={() => setGrp(g)}>{l}{changedIn(g) && <span className="chg">•</span>}</button>)}</nav>
        <div>
          {grp === "budgets" && <div className="card" style={{ marginBottom: 14 }}><div className="sec-h">Preset</div><div className="presets">{Object.entries(d.presets).map(([kk, p]: any) => (
            <button key={kk} aria-pressed={vals.budgets.preset === kk} onClick={async () => { await send("POST", "/settings/preset", { name: kk }); r.reload(); }}><b>{p.label}</b><span className="sim">cap {k(p.cap)}</span></button>))}</div>
            <p className="sim" style={{ margin: "8px 0 0" }}>A preset sets every budget below; any value can then be changed. An endpoint with a smaller context cuts its jobs in the cut order.</p></div>}
          {(d.pinned || []).some((p: string) => p.split(".")[0] === grp) && <p className="sim card" style={{ marginBottom: 10 }}>Set in this machine's <code>data\settings.yaml</code> (wins over the saved settings, so a copied database never brings another machine's endpoints or paths): <code>{d.pinned.filter((p: string) => p.split(".")[0] === grp).join(", ")}</code></p>}
          <div className="card">{Object.entries(gspec.properties).map(([name, spec0]: any) => {
            const spec = { ...resolve(schema, spec0), ...spec0 };
            if (grp === "pool" && name === "endpoints") return <Endpoints key={name} schema={schema} value={vals.pool.endpoints} onChange={(v) => setVals({ ...vals, pool: { ...vals.pool, endpoints: v } })} />;
            return <Field key={name} name={name} spec={spec} value={vals[grp][name]} def={d.defaults[grp][name]} onChange={(v) => setVals({ ...vals, [grp]: { ...vals[grp], [name]: v } })} />;
          })}</div>
          <div className="btn-row" style={{ marginTop: 14 }}><button className="btn primary" onClick={save}>Save settings</button><button className="btn" onClick={() => setVals(JSON.parse(JSON.stringify(d.values)))}>Discard changes</button><span className="sim">{msg}</span></div>
          <div className="card" style={{ marginTop: 14 }}><div className="sec-h">History</div><ul className="hist">{d.history.map((h: any) => <li key={h.id}><b>{h.at.slice(0, 16).replace("T", " ")}</b> {h.user || "system"} · {h.note}</li>)}</ul></div>
        </div></div>
    </>
  );
}

// ---------------------------------------------------------------- Eval
function Eval() {
  const r = useApi("/eval");
  const d: any = r.data;
  if (!d) return <Loading />;
  if (!d.retrieval) return <Empty>No evaluation results yet. Run <code>python manage.py eval</code>.</Empty>;
  return (
    <>
      <p className="sim">Run {d.created} · {d.questions} golden questions · {d.note || ""}</p>
      <div className="card tbl"><div className="sec-h">Retrieval (ablation)</div><table className="mt"><thead><tr><th>Configuration</th><th>recall@1</th><th>recall@5</th><th>recall@10</th><th>MRR</th><th>nDCG@10</th><th>ms/query</th></tr></thead><tbody>
        {d.retrieval.map((x: any) => <tr key={x.name}><td>{x.name}</td><td className="num">{x.recall_1}</td><td className="num">{x.recall_5}</td><td className="num">{x.recall_10}</td><td className="num">{x.mrr}</td><td className="num">{x.ndcg_10}</td><td className="num">{x.ms}</td></tr>)}</tbody></table></div>
      {d.answers && <div className="card tbl" style={{ marginTop: 14 }}><div className="sec-h">Answers (LLM judge)</div><table className="mt"><thead><tr><th>Engine · mode</th><th>n</th><th>correct</th><th>faithful</th><th>source precision</th><th>abstention ok</th><th>s/answer</th></tr></thead><tbody>
        {d.answers.map((x: any) => <tr key={x.name}><td>{x.name}</td><td className="num">{x.n}</td><td className="num">{x.correct}</td><td className="num">{x.faithful}</td><td className="num">{x.source_precision}</td><td className="num">{x.abstention}</td><td className="num">{x.seconds}</td></tr>)}</tbody></table></div>}
      {d.ingest && <div className="card" style={{ marginTop: 14 }}><div className="sec-h">Ingest</div><pre className="json">{JSON.stringify(d.ingest, null, 1)}</pre></div>}
    </>
  );
}

export default function Agent() {
  const { sub = "logs" } = useParams();
  const nav = useNavigate();
  return (
    <>
      <div className="title-row"><h1>Agent</h1><span className="pill risk">behind the scenes</span></div>
      <p className="h-sub">How the pipeline works: notes and routing per unit (or the combined fast path for single emails), the editor per page with guards, tags, the reviewer, the reports and the chat. Every model request goes through the pool and is logged here.</p>
      <nav className="subtabs">{SUBS.map(([v, l]) => <button key={v} aria-current={sub === v ? "page" : undefined} onClick={() => nav(`/agent/${v}`)}>{l}</button>)}</nav>
      {sub === "logs" ? <Logs /> : sub === "pipeline" ? <Pipeline /> : sub === "pool" ? <Pool /> : sub === "rules" ? <Rules /> : sub === "tags" ? <Tags /> : sub === "settings" ? <SettingsView /> : <Eval />}
    </>
  );
}
