import React, { useEffect, useState } from "react";
import { get } from "./api";

export const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const fmt = (d?: string | null) => (d ? `${+d.slice(8, 10)} ${MON[+d.slice(5, 7) - 1]}` : "—");
export const dow = (d: string) => DOW[new Date(d + "T12:00:00Z").getUTCDay()];
export const addDays = (d: string, n: number) => { const x = new Date(d + "T12:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
export const PALETTE = ["#3B7DD8", "#2E9E62", "#9B59D0", "#D9822B", "#16A0A8", "#C2477F", "#7A8794", "#B8A12E", "#5FB8BE", "#E06C5A", "#4D6CC9", "#8CC5A0"];
export const tColor = (id: number | string | null | undefined) => PALETTE[Math.abs(Number(id) || 0) % PALETTE.length];
export const initials = (name: string) => name.split(/[\s.@]+/).filter(Boolean).map((x) => x[0]).join("").slice(0, 2).toUpperCase();
export const short = (name: string) => { const s = (name || "").split(" "); return s.length > 1 ? `${s[0][0]}. ${s.slice(1).join(" ")}` : name; };
export const k = (n: number) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n));

export function useApi<T = any>(path: string | null, params?: any, deps: any[] = []): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [n, setN] = useState(0);
  const key = JSON.stringify([path, params]);
  useEffect(() => {
    if (!path) return;
    let live = true;
    setLoading(true);
    get<T>(path, params).then((d) => { if (live) { setData(d); setError(""); } }).catch((e) => live && setError(String(e))).finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [key, n, ...deps]);
  return { data, error, loading, reload: () => setN((x) => x + 1) };
}

export function useLocal<T>(key: string, init: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => { try { const s = localStorage.getItem(key); return s ? JSON.parse(s) : init; } catch { return init; } });
  return [v, (nv: T) => { setV(nv); try { localStorage.setItem(key, JSON.stringify(nv)); } catch { /* private mode */ } }];
}

export function Seg({ value, options, onChange }: { value: any; options: [any, string][]; onChange: (v: any) => void }) {
  return <span className="seg">{options.map(([v, l]) => <button key={String(v)} aria-pressed={String(v) === String(value)} onClick={() => onChange(v)}>{l}</button>)}</span>;
}

export function Kind({ kind }: { kind?: string }) { return kind ? <span className={`kind ${kind}`}>{kind}</span> : null; }

export function ImpPill({ imp }: { imp?: string }) {
  if (imp === "important") return <span className="pill imp">important</span>;
  if (imp === "not_important") return <span className="pill">not important</span>;
  return null;
}

const STATUS_CLS = (s: string) => (/risk|block/i.test(s) ? "risk" : /track|done/i.test(s) ? "ok" : "open");
export function Status({ s }: { s: string }) { return <span className={`pill ${STATUS_CLS(s)}`}>{s}</span>; }

export const TAG_LABEL: Record<string, string> = { system: "System", process: "Process", work_type: "Work type", site: "Site", partner: "Partner", tcode: "T-code" };
export function TagPill({ ty, v, onClick, small = true }: { ty: string; v: string; onClick?: () => void; small?: boolean }) {
  return <button className={`chip t-${ty}`} title={TAG_LABEL[ty] || ty} onClick={onClick}>{small ? null : <span className="ty">{TAG_LABEL[ty] || ty}</span>}{v}</button>;
}

export function pagePills(tags: Record<string, string[]> = {}, n = 8): [string, string][] {
  const out: [string, string][] = [];
  ([["system", 2], ["process", 1], ["site", 1], ["partner", 1]] as [string, number][]).forEach(([ty, m]) => (tags[ty] || []).slice(0, m).forEach((v) => out.push([ty, v])));
  (tags.tcode || []).slice(0, 3).forEach((v) => out.push(["tcode", v]));
  return out.slice(0, n);
}

export function Avatar({ name, size = 20 }: { name: string; size?: number }) {
  let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) | 0;
  return <span className="avatar" title={name} style={{ background: PALETTE[Math.abs(h) % PALETTE.length], width: size, height: size, fontSize: `${size / 36}rem` }}>{initials(name)}</span>;
}

// ---------------------------------------------------------------- text rendering
type CiteFn = (emailId: number) => React.ReactNode;

function inline(text: string, cite?: CiteFn, key = 0): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  const re = /\*\*([^*]+)\*\*|\[e(\d+)\]|\[image: ([^\]]+)\]/g;
  let last = 0, m: RegExpExecArray | null, i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(<b key={`${key}-b${i}`}>{m[1]}</b>);
    else if (m[2]) out.push(cite ? <React.Fragment key={`${key}-c${i}`}>{cite(+m[2])}</React.Fragment> : null);
    else if (m[3]) out.push(<span key={`${key}-i${i}`} className="sim">[image: {m[3]}]</span>);
    last = re.lastIndex; i++;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function table(lines: string[], key: number) {
  const rows = lines.map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
  const sep = rows.length > 1 && rows[1].every((c) => /^:?-+:?$/.test(c));
  const head = rows[0], body = rows.slice(sep ? 2 : 1);
  return (
    <div className="mt-wrap" key={key}><table className="mt"><thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
      <tbody>{body.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} className={/^[\d.,%+€$ -]+$/.test(c) && c ? "num" : ""}>{c}</td>)}</tr>)}</tbody></table></div>
  );
}

/** Markdown-ish text → React: paragraphs, "- " lists, | tables |, **bold**, [eNN] source markers (via cite). */
export function Prose({ text, cite, className }: { text: string; cite?: CiteFn; className?: string }) {
  const blocks: React.ReactNode[] = [];
  const lines = (text || "").replace(/\r/g, "").split("\n");
  let para: string[] = [], list: string[] = [], tab: string[] = [], key = 0;
  const flush = () => {
    if (tab.length) { blocks.push(table(tab, key++)); tab = []; }
    if (list.length) { blocks.push(<ul key={key++}>{list.map((l, i) => <li key={i}>{inline(l, cite, i)}</li>)}</ul>); list = []; }
    if (para.length) { blocks.push(<p key={key++}>{inline(para.join(" "), cite, key)}</p>); para = []; }
  };
  for (const ln of lines) {
    const t = ln.trim();
    if (/^\|.*\|$/.test(t)) { if (para.length || list.length) { const tb = tab; tab = []; flush(); tab = tb; } tab.push(t); continue; }
    if (tab.length) flush();
    if (/^#{2,4} /.test(t)) { flush(); blocks.push(<h4 key={key++}>{t.replace(/^#+ /, "")}</h4>); continue; }
    if (/^[-*•] /.test(t)) { if (para.length) { const l = list; list = []; flush(); list = l; } list.push(t.slice(2)); continue; }
    if (!t) { flush(); continue; }
    if (list.length) flush();
    para.push(t);
  }
  flush();
  return <div className={className}>{blocks}</div>;
}

export function Empty({ children }: { children: React.ReactNode }) { return <div className="note" style={{ margin: 10 }}>{children}</div>; }
export function Loading() { return <p className="sim">Loading…</p>; }
export function Err({ e }: { e: string }) { return e ? <p className="note" style={{ borderColor: "var(--red)" }}>Error: {e}</p> : null; }
