import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useLocation } from "react-router-dom";
import "./mockup.css";
import "./app.css";
import Chat from "./Chat";
import { send } from "./api";
import { DrawerProvider } from "./drawer";
import { refreshMeta, useMeta } from "./util";
import Reports from "./views/Reports";
import Emails from "./views/Emails";
import Wiki from "./views/Wiki";
import PageView from "./views/Page";
import Agent from "./views/Agent";
import Search from "./views/Search";

/** Why nothing is being processed: shown on every view until someone resumes or the endpoint answers again. */
function StopBanner({ m }: { m: any }) {
  const nav = useNavigate();
  const stopped: any[] = (m?.stopped || []);
  if (!stopped.length) return null;
  const when = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="stop-banner" role="alert">
      <span className="why"><b>Requests stopped.</b> {stopped.map((x) => <span key={x.endpoint}><b>{x.endpoint}</b>: {x.reason} </span>)}
        <span className="sim">Waiting mail stays waiting; nothing is lost. {stopped[0].until ? `After ${when(stopped[0].until)} the next scheduled run may try once.` : "It stays stopped until you resume."}</span></span>
      <button className="btn small" onClick={async () => { await send("POST", "/agent/resume"); refreshMeta(); }}>Resume</button>
      <button className="btn small" onClick={() => nav("/agent/pool")}>See the calls</button>
    </div>
  );
}

function Header() {
  const nav = useNavigate();
  const loc = useLocation();
  const meta = useMeta();
  const m: any = meta.data;
  const [q, setQ] = React.useState(new URLSearchParams(loc.search).get("q") || "");
  React.useEffect(() => { const t = setInterval(() => { if (!document.hidden) refreshMeta(); }, 30000); return () => clearInterval(t); }, []);
  return (
    <header className="hd"><div className="gut">
      <div className="hd-row">
        <div className="logo"><span className="sq" />{m?.title || "Control Tower Wiki"} <small>{m?.latest_day ? `latest mail ${m.latest_day}` : ""}{m?.schedule?.live ? " · live" : ""}{m?.schedule?.paused_for_chat ? " · background paused for chat" : ""}{m?.user ? ` · ${m.user}` : ""}{m?.version ? ` · v${m.version}` : ""}</small></div>
        <form className="hd-search" onSubmit={(e) => { e.preventDefault(); if (q.trim()) nav(`/search?q=${encodeURIComponent(q.trim())}`); }}>
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the wiki, emails, attachments, PO/invoice numbers…" aria-label="Search" /></form>
      </div>
      <nav className="tabs" aria-label="Views">
        <button onClick={() => nav("/")} aria-current={loc.pathname === "/" || loc.pathname === "/reports" ? "page" : undefined}>Reports</button>
        <button onClick={() => nav("/emails")} aria-current={loc.pathname.startsWith("/emails") || loc.pathname.startsWith("/threads") ? "page" : undefined}>Emails</button>
        <button onClick={() => nav("/wiki")} aria-current={loc.pathname.startsWith("/wiki") ? "page" : undefined}>Wiki</button>
        <span className="sp" />
        <button className="dbg" onClick={() => nav("/agent/logs")} aria-current={loc.pathname.startsWith("/agent") ? "page" : undefined}>Agent ⚙</button>
      </nav>
      <StopBanner m={m} />
    </div></header>
  );
}

function App() {
  return (
    <BrowserRouter>
      <DrawerProvider>
        <Header />
        <main className="gut" id="main">
          <Routes>
            <Route path="/" element={<Reports />} />
            <Route path="/reports" element={<Reports />} />
            <Route path="/emails" element={<Emails />} />
            <Route path="/emails/:eid" element={<Emails />} />
            <Route path="/threads/:tid" element={<Emails />} />
            <Route path="/wiki" element={<Wiki />} />
            <Route path="/wiki/:pid" element={<PageView />} />
            <Route path="/agent" element={<Navigate to="/agent/logs" />} />
            <Route path="/agent/:sub" element={<Agent />} />
            <Route path="/search" element={<Search />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </main>
        <Chat />
      </DrawerProvider>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
