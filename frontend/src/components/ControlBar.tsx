import { useState } from "react";
import { killSession, OPERATOR, pauseSession, resumeSession, type Access } from "../api/client";
import type { GovernanceState } from "../types";

/** Human controls. Every message reflects what the backend confirmed, not what was asked. */
export default function ControlBar({ sessionId, access = OPERATOR, state, closed, killFailed, technical }: {
  sessionId: string; access?: Access; state: GovernanceState; closed: boolean; killFailed?: boolean; technical?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; cls: string } | null>(null);
  const operator = !access.public;

  async function act(fn: () => Promise<any>, describe: (r: any) => { text: string; cls: string }) {
    setBusy(true);
    setMsg({ text: "Sending…", cls: "muted" });
    try {
      setMsg(describe(await fn()));
    } catch (e: any) {
      setMsg({ text: `Not done: ${e.message}`, cls: "fail" });
    } finally {
      setBusy(false);
    }
  }

  const stopResult = (r: any) => r.verdict === "KILL_CONFIRMED"
    ? { text: "🛑 Stopped for good. All checks confirmed.", cls: "ok" }
    : { text: `⚠️ Stop requested but NOT confirmed (${Object.entries(r.checks || {}).filter(([, v]) => !v).map(([k]) => k).join(", ") || r.verdict}). You can retry.`, cls: "fail" };

  if (closed) return null;
  if (state === "KILL") {
    if (!killFailed) return null;            // stopping, or stopped and confirmed: nothing left to control
    return (
      <div id="tour-controls" className="controls">
        <h4>The stop wasn't confirmed</h4>
        <div className="control-bar">
          <button className="danger" disabled={busy} onClick={() => act(() => killSession(sessionId, access), stopResult)}>
            🔁 Retry stop for good
          </button>
        </div>
        {msg && <p className={`small ${msg.cls}`} role="status">{msg.text}</p>}
      </div>
    );
  }
  const paused = state === "PAUSE";
  return (
    <div id="tour-controls" className="controls">
      <h4>You're in control</h4>
      <div className="control-bar">
        <button className="btn-freeze" disabled={busy || paused}
          onClick={() => act(() => pauseSession(sessionId, access), (r) =>
            !r.paused ? { text: `Freeze not applied (state ${r.state}).`, cls: "fail" }
              : r.verified_frozen ? { text: "🧊 Frozen. The kernel confirms nothing can run.", cls: "ok" }
              : { text: "⏸️ Paused before the next step (kernel freeze not available, so not confirmed by the kernel).", cls: "warn" })}>
          🧊 Freeze
        </button>
        <button className="btn-go" disabled={busy || !paused}
          onClick={() => act(() => resumeSession(sessionId, 0, access), (r) =>
            r.resumed ? { text: `▶️ Unfrozen. Now: ${r.state}.`, cls: "ok" }
              : { text: `Not unfrozen: ${r.reason || "the kernel did not confirm the unfreeze"}.`, cls: "fail" })}>
          ▶️ Unfreeze
        </button>
        {operator && (
          <button className="btn-go" disabled={busy || !paused} title="Approve 15 more seconds of computer time (recorded in the signed record)"
            onClick={() => act(() => resumeSession(sessionId, 15, access), (r) =>
              r.resumed ? { text: `+15 s approved and unfrozen. Now: ${r.state}.`, cls: "ok" }
                : { text: `Not resumed: ${r.reason || "unfreeze not confirmed"}.`, cls: "fail" })}>
            ➕ Give 15 s more &amp; unfreeze
          </button>
        )}
        <button className="danger" disabled={busy} onClick={() => act(() => killSession(sessionId, access), stopResult)}>
          🛑 Stop for good
        </button>
      </div>
      <p className="muted small">
        Freeze = pause everything where it is. Stop for good = end the agent, cut its AI access and destroy (delete) its sandbox.
        {technical && " (PAUSE = cgroup.freeze; KILL = cgroup.kill + microVM removal + 6 checks)"}
      </p>
      {msg && <p className={`small ${msg.cls}`} role="status">{msg.text}</p>}
    </div>
  );
}
