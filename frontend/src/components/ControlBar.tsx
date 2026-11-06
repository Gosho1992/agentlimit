import { useState } from "react";
import { killSession, pauseSession, resumeSession } from "../api/client";
import type { GovernanceState } from "../types";

export default function ControlBar({ sessionId, state, closed }: { sessionId: string; state: GovernanceState; closed: boolean }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function act(fn: () => Promise<any>, describe: (r: any) => string) {
    setBusy(true);
    try {
      setMsg(describe(await fn()));
    } catch (e: any) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  }

  const final = state === "KILL" || closed;
  const paused = state === "PAUSE";
  return (
    <div>
      <div className="control-bar">
        <button disabled={busy || final || paused} onClick={() => act(() => pauseSession(sessionId), (r) => `paused (${r.mode}, frozen=${r.verified_frozen})`)}>
          Pause
        </button>
        <button disabled={busy || final || !paused} onClick={() => act(() => resumeSession(sessionId, 0), (r) => `resumed → ${r.state}`)}>
          Resume
        </button>
        <button
          disabled={busy || final || !paused}
          title="Operator approves +15 CPU-seconds (recorded in the ledger)"
          onClick={() => act(() => resumeSession(sessionId, 15), (r) => `budget extended → ${r.state}`)}
        >
          Approve +15s CPU &amp; resume
        </button>
        <button className="danger" disabled={busy || final} onClick={() => act(() => killSession(sessionId), (r) => `${r.verdict}`)}>
          Kill
        </button>
      </div>
      {msg && <p className="muted small">{msg}</p>}
    </div>
  );
}
