import { useSearchParams } from "react-router-dom";
import { usePeek } from "../drawer";
import { Empty, fmt, Loading, useApi } from "../util";

export default function Search() {
  const [sp] = useSearchParams();
  const q = sp.get("q") || "";
  const r = useApi(q ? "/search" : null, { q });
  const peek = usePeek();
  const d: any = r.data;
  return (
    <>
      <div className="title-row"><h1>Search</h1></div>
      <p className="h-sub">Hybrid search: keywords (SQLite FTS5, BM25) plus meaning (vectors), merged with weighted reciprocal rank fusion, wiki first. Reference numbers are also looked up exactly.</p>
      {!q ? <Empty>Type in the search bar at the top.</Empty> : !d ? <Loading /> : <>
        {d.identifiers.length > 0 && <div className="card" style={{ marginBottom: 12 }}><div className="sec-h">Reference {q} <span className="pill">exact</span></div>
          <ul className="elist">{d.identifiers.map((i: any, n: number) => <li key={n}><button className="linkbtn" onClick={() => peek({ kind: "email", id: i.email.id })}>{i.email.subject}</button> <span className="sim">{i.type} · {i.email.from_name} · {fmt(i.email.day)}</span><br />{i.email.note}</li>)}</ul></div>}
        <div className="count-line"><b>{d.hits.length} results</b></div>
        {d.hits.map((h: any) => (
          <div className="result" key={h.chunk_id}><div className="rt"><span className={`pill ${h.collection === "wiki" ? "open" : h.collection === "emails" ? "vio" : ""}`}>{h.collection}</span>
            <b>{h.page_id ? <button className="linkbtn" onClick={() => peek({ kind: "page", id: h.page_id })}>{h.page_title || h.header}</button>
              : <button className="linkbtn" onClick={() => h.email_id && peek({ kind: "email", id: h.email_id })}>{h.header}</button>}</b></div>
            <div className="snip">{h.text}</div>
            <div className="scores"><span>BM25 {h.bm25_rank ? "#" + h.bm25_rank : "—"}</span><span>vector {h.vec_rank ? "#" + h.vec_rank : "—"}{h.cos !== null ? ` (cos ${h.cos})` : ""}</span><span>score {h.score}</span></div></div>))}
      </>}
    </>
  );
}
