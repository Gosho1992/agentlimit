import { useState } from "react";
import { killSession, pauseSession, resumeSession } from "../api/client";
import type { GovernanceState } from "../types";

/** Human controls. Guests control only their own run; budget extensions are operator-only. */
export default function ControlBar({ sessionId, state, closed, operator, technical }: {
  sessionId: string; state: GovernanceState; closed: boolean; operator: boolean; technical?: boolean;
}) {
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
  if (final) return null;
  return (
    <div id="tour-controls" className="controls">
      <h4>You're in control</h4>
      <div className="control-bar">
        <button className="btn-freeze" disabled={busy || paused}
          onClick={() => act(() => pauseSession(sessionId), (r) => r.verified_frozen ? "🧊 Frozen. The kernel confirms nothing can run." : `Paused (${r.mode})`)}>
          🧊 Freeze
        </button>
        <button className="btn-go" disabled={busy || !paused}
          onClick={() => act(() => resumeSession(sessionId, 0), (r) => `▶️ Unfrozen (${r.state})`)}>
          ▶️ Unfreeze
        </button>
        {operator && (
          <button className="btn-go" disabled={busy || !paused} title="Approve 15 more seconds of computer time (recorded in the signed record)"
            onClick={() => act(() => resumeSession(sessionId, 15), (r) => `+15 s approved (${r.state})`)}>
            ➕ Give 15 s more &amp; unfreeze
          </button>
        )}
        <button className="danger" disabled={busy}
          onClick={() => act(() => killSession(sessionId), (r) => r.verdict === "KILL_CONFIRMED" ? "🛑 Stopped for good and confirmed." : `Stop not confirmed: ${r.verdict}`)}>
          🛑 Stop for good
        </button>
      </div>
      <p className="muted small">
        Freeze = pause everything where it is. Stop for good = end the agent, cut its AI access and destroy (delete) its sandbox.
        {technical && " (PAUSE = cgroup.freeze; KILL = cgroup.kill + microVM removal + 6 checks)"}
      </p>
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}
