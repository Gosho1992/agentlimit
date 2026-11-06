import { useEffect, useState } from "react";
import { authStatus } from "./api/client";
import Login from "./components/Login";
import NewSessionForm from "./components/NewSessionForm";
import SessionDashboard from "./components/SessionDashboard";

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("session")
  );
  const [auth, setAuth] = useState<{ authenticated: boolean; configured: boolean } | null>(null);

  const refresh = () => authStatus().then(setAuth).catch(() => setAuth({ authenticated: false, configured: false }));
  useEffect(() => {
    refresh();
  }, []);

  const open = (id: string | null) => {
    setSessionId(id);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("session", id);
    else url.searchParams.delete("session");
    window.history.replaceState(null, "", url.toString());
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>AgentLimit</h1>
        <p className="tagline">External runtime governor for autonomous AI agents on Vultr. Observe · Enforce · Prove.</p>
      </header>
      {!auth ? null : !auth.authenticated ? (
        <Login onDone={refresh} configured={auth.configured} />
      ) : !sessionId ? (
        <NewSessionForm onCreated={(id) => open(id)} />
      ) : (
        <SessionDashboard sessionId={sessionId} onReset={() => open(null)} />
      )}
    </div>
  );
}
