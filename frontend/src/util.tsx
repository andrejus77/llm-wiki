import React, { useCallback, useEffect, useRef, useState } from "react";
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

// Answers seen recently, shown at once when the same view or filter comes back while the fresh one loads.
const seen = new Map<string, any>();
function remember(key: string, value: any) {
  seen.delete(key);
  seen.set(key, value);
  if (seen.size > 40) seen.delete(seen.keys().next().value as string);
}

export function useApi<T = any>(path: string | null, params?: any, deps: any[] = []): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [n, setN] = useState(0);
  const key = JSON.stringify([path, params]);
  useEffect(() => {
    if (!path) return;
    const ac = new AbortController();   // a request nobody waits for any more must not hold a server thread
    if (seen.has(key)) setData(seen.get(key));
    setLoading(true);
    get<T>(path, params, ac.signal).then((d) => { remember(key, d); setData(d); setError(""); })
      .catch((e) => { if (e.name !== "AbortError") setError(String(e)); }).finally(() => { if (!ac.signal.aborted) setLoading(false); });
    return () => ac.abort();
  }, [key, n, ...deps]);
  return { data, error, loading, reload: useCallback(() => setN((x) => x + 1), []) };
}

// One shared /meta for the whole page (the header and every view need it).
let metaLast: any = null, metaAt = 0, metaBusy: Promise<any> | null = null;
const metaSubs = new Set<(d: any) => void>();
function fetchMeta(force = false): Promise<any> {
  if (metaBusy) return metaBusy;
  if (!force && metaLast && Date.now() - metaAt < 20000) return Promise.resolve(metaLast);
  metaBusy = get("/meta").then((d) => { metaLast = d; metaAt = Date.now(); metaSubs.forEach((f) => f(d)); return d; }).finally(() => { metaBusy = null; });
  return metaBusy;
}
export const refreshMeta = () => fetchMeta(true).catch(() => null);
export function useMeta(): { data: any } {
  const [data, setData] = useState<any>(metaLast);
  useEffect(() => { metaSubs.add(setData); fetchMeta().then(setData).catch(() => null); return () => { metaSubs.delete(setData); }; }, []);
  return { data };
}

/** A long list, one page at a time: the rows so far, and more() for the next page. A new filter starts again from the top. */
export function usePaged<T = any>(path: string | null, params: any, size = 100) {
  const [rows, setRows] = useState<T[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [n, setN] = useState(0);
  const ctl = useRef<AbortController | null>(null);
  const key = JSON.stringify([path, params, size]);
  useEffect(() => {
    if (!path) return;
    const ac = new AbortController();
    ctl.current = ac;
    setLoading(true);
    get<any>(path, { ...params, limit: size, offset: 0 }, ac.signal).then((d) => { setRows(d.rows); setTotal(d.total); setError(""); })
      .catch((e) => { if (e.name !== "AbortError") setError(String(e)); }).finally(() => { if (!ac.signal.aborted) setLoading(false); });
    return () => ac.abort();
  }, [key, n]);
  const more = useCallback(() => {
    if (!path || loading || total === null || rows.length >= total) return;
    const ac = ctl.current;
    setLoading(true);
    get<any>(path, { ...params, limit: size, offset: rows.length }, ac?.signal).then((d) => { setRows((r) => [...r, ...d.rows]); setTotal(d.total); })
      .catch((e) => { if (e.name !== "AbortError") setError(String(e)); }).finally(() => { if (!ac?.signal.aborted) setLoading(false); });
  }, [key, loading, total, rows.length]);
  return { rows, total, loading, error, more, hasMore: total !== null && rows.length < total, reload: useCallback(() => setN((x) => x + 1), []) };
}

/** Loads the next page when it scrolls into view; the button is there for a click too. */
export function LoadMore({ has, loading, onMore, label = "Show more" }: { has: boolean; loading: boolean; onMore: () => void; label?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!has || !ref.current) return;
    // watch the list's own scroll area when it has one: its end can be below the window while the page has not scrolled
    const box = ref.current.closest(".mlist");
    const root = box && getComputedStyle(box).overflowY === "auto" ? box : null;
    const io = new IntersectionObserver((es) => { if (es[0].isIntersecting) onMore(); }, { root, rootMargin: "200px" });
    io.observe(ref.current);
    return () => io.disconnect();
  }, [has, onMore]);
  if (!has) return null;
  return <div ref={ref} className="loadmore"><button className="btn small" onClick={onMore} disabled={loading}>{loading ? "Loading…" : label}</button></div>;
}

export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

/** A <details> whose content is only built when it is opened (long threads hold hundreds of these). */
export function Lazy({ summary, className, open = false, children }: { summary: React.ReactNode; className?: string; open?: boolean; children: () => React.ReactNode }) {
  const [on, setOn] = useState(open);
  return <details className={className} open={open} onToggle={(e) => { if ((e.target as HTMLDetailsElement).open) setOn(true); }}><summary>{summary}</summary>{on ? children() : null}</details>;
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
