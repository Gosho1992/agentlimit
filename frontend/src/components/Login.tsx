import { useState } from "react";
import { login } from "../api/client";

export default function Login({ onDone, configured }: { onDone: () => void; configured: boolean }) {
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="card new-session"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setErr(null);
        try {
          await login(token);
          onDone();
        } catch (x: any) {
          setErr(x.message);
        } finally {
          setBusy(false);
        }
      }}
    >
      <h2>Operator login</h2>
      <p className="muted">
        <strong>Just visiting? You don't need this.</strong> Use <a className="inline-link" href="/#try">Try it</a> to run a preset live, or watch a
        recorded run. The operator console is for custom tasks and custom limits; judges get the access code privately from the team.
      </p>
      {!configured && <p className="error">The server didn't confirm an access code is configured (or couldn't be reached). Operator sessions stay disabled; <a className="inline-link" href="/#try">Try it</a> still works.</p>}
      <label>
        Code
        <input type="password" autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} autoFocus />
      </label>
      {err && <p className="error">{err}</p>}
      <button type="submit" disabled={busy || !token}>
        {busy ? "Checking…" : "Enter"}
      </button>
    </form>
  );
}
