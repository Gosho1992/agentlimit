import { useEffect, useState } from "react";
import { authStatus, setViewMode } from "./api/client";
import Developers from "./components/Developers";
import Guide from "./components/Guide";
import Landing from "./components/Landing";
import Login from "./components/Login";
import NewSessionForm from "./components/NewSessionForm";
import SessionDashboard from "./components/SessionDashboard";

// URL scheme (shareable):  /              home
//                          ?page=guide | developers | console
//                          ?run=<id>      public replay or your own guest run
//                          ?session=<id>  operator view (access code)
type Route = { page: string; run?: string | null; session?: string | null };

const GUEST_KEY = (id: string) => `agentlimit-guest-${id}`;
function guestToken(id: string): string | null {
  try { return sessionStorage.getItem(GUEST_KEY(id)); } catch { return null; }
}
function rememberGuest(id: string, token: string) {
  try { sessionStorage.setItem(GUEST_KEY(id), token); } catch { /* storage unavailable: token stays in memory */ }
}

function parse(): Route {
  const q = new URLSearchParams(window.location.search);
  if (q.get("run")) return { page: "run", run: q.get("run") };
  if (q.get("session")) return { page: "console", session: q.get("session") };
  return { page: q.get("page") || "home" };
}

export default function App() {
  const [route, setRoute] = useState<Route>(parse);
  const [auth, setAuth] = useState<{ authenticated: boolean; configured: boolean } | null>(null);
  const [memToken, setMemToken] = useState<Record<string, string>>({});

  const refresh = () => authStatus().then(setAuth).catch(() => setAuth({ authenticated: false, configured: false }));
  useEffect(() => { refresh(); }, []);
  useEffect(() => {
    const onPop = () => setRoute(parse());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const nav = (r: Route, hash = "") => {
    const url = new URL(window.location.href);
    url.search = "";
    url.hash = "";
    if (r.run) url.searchParams.set("run", r.run);
    else if (r.session) url.searchParams.set("session", r.session);
    else if (r.page !== "home") url.searchParams.set("page", r.page);
    window.history.pushState(null, "", url.toString());
    setRoute(r);
    window.scrollTo(0, 0);
    if (hash) setTimeout(() => document.getElementById(hash)?.scrollIntoView({ behavior: "smooth" }), 80);
  };
  const go = (p: string) => {
    const [page, hash] = p.split("#");
    nav({ page }, hash);
  };
  const openRun = (id: string, token?: string) => {
    if (token) { rememberGuest(id, token); setMemToken((m) => ({ ...m, [id]: token })); }
    nav({ page: "run", run: id });
  };

  // the API client follows the page: public endpoints for /run, operator endpoints for the console
  if (route.page === "run" && route.run) setViewMode({ public: true, guestToken: memToken[route.run] ?? guestToken(route.run) });
  else setViewMode({ public: false });

  const link = (page: string, label: string) => (
    <a href={`?page=${page}`} className={route.page === page ? "active" : ""}
      onClick={(e) => { e.preventDefault(); go(page); }}>{label}</a>
  );

  return (
    <div className="app">
      <header className="topnav">
        <a href="/" className="brand" onClick={(e) => { e.preventDefault(); go("home"); }}>
          <span className="brand-dot" /> AgentLimit
        </a>
        <nav>
          <a href="/#try" onClick={(e) => { e.preventDefault(); go("home#try"); }}>Try it</a>
          <a href="/#replays" onClick={(e) => { e.preventDefault(); go("home#replays"); }}>Replays</a>
          {link("guide", "How it works")}
          {link("developers", "Developers")}
          {link("console", auth?.authenticated ? "Console" : "Operator login")}
        </nav>
      </header>

      {route.page === "home" && <Landing go={go} openRun={openRun} />}
      {route.page === "guide" && <Guide go={go} />}
      {route.page === "developers" && <Developers go={go} />}
      {route.page === "run" && route.run && (
        <SessionDashboard key={route.run} sessionId={route.run} operator={false} onBack={() => go("home#replays")} backLabel="← All runs" />
      )}
      {route.page === "console" && (!auth ? null : !auth.authenticated ? (
        <Login onDone={refresh} configured={auth.configured} />
      ) : route.session ? (
        <SessionDashboard key={route.session} sessionId={route.session} operator onBack={() => nav({ page: "console" })} backLabel="← Console" />
      ) : (
        <NewSessionForm onCreated={(id) => nav({ page: "console", session: id })} />
      ))}

      <footer className="foot">
        AgentLimit · external runtime governor for AI agents on Vultr ·{" "}
        <a href="https://github.com/Gosho1992/agentlimit" target="_blank" rel="noreferrer">GitHub</a>
      </footer>
    </div>
  );
}
