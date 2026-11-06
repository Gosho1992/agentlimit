import { useEffect, useRef, useState } from "react";
import { getSession, streamSession } from "../api/client";
import type { GovernanceState, LedgerEntry, SessionSnapshot } from "../types";
import ControlBar from "./ControlBar";
import EvidenceReport from "./EvidenceReport";
import GovernanceTimeline from "./GovernanceTimeline";
import RecommendationCard from "./RecommendationCard";
import ResourceGauge from "./ResourceGauge";

const LADDER: GovernanceState[] = ["NORMAL", "ALERT", "RECOMMEND", "THROTTLE", "PAUSE", "KILL"];

const CHECK_LABELS: Record<string, string> = {
  session_marked_terminated: "Session marked terminated",
  inference_revoked: "Inference revoked (no more model calls)",
  agent_task_finished: "Host-side agent loop cancelled",
  cgroup_populated_0: "cgroup.events populated 0",
  sandbox_pid_gone: "MicroVM host process gone",
  sandbox_removed_via_runtime: "Sandbox stopped & removed via Microsandbox SDK",
};

export default function SessionDashboard({ sessionId, onReset }: { sessionId: string; onReset: () => void }) {
  const [snap, setSnap] = useState<SessionSnapshot | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [done, setDone] = useState(false);
  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const push = (e: LedgerEntry) => setEntries((prev) => [...prev, e]);
    const stop = streamSession(sessionId, {
      onState: push,
      onSample: () => {},
      onRecommend: push,
      onLedger: push,
      onDone: () => setDone(true),
      onError: () => {},
    });
    stopRef.current = stop;
    const poll = async () => {
      try {
        setSnap(await getSession(sessionId));
      } catch {
        /* noop */
      }
    };
    poll();
    const id = setInterval(poll, 1000);
    return () => {
      stop();
      clearInterval(id);
    };
  }, [sessionId]);

  useEffect(() => {
    if (done) stopRef.current?.();
  }, [done]);

  const state = snap?.state ?? "NORMAL";
  const b = snap?.budget;
  const t = snap?.termination;
  const closed = !!snap?.lifecycle || !!snap?.archived;
  const c = snap?.containment;

  return (
    <div className="dashboard">
      <div className="dashboard-header">
        <div>
          <h2>
            Session <code>{sessionId}</code>
          </h2>
          <p className="muted">
            Agent: <strong>{snap?.agent_status ?? "…"}</strong> · Steps: {snap?.steps ?? 0} ·{" "}
            {snap?.runtime_type === "microsandbox" ? (
              <span className="ok">Microsandbox microVM (KVM) · VMM pid {snap?.sandbox_pid}</span>
            ) : (
              <span className="warn">{snap?.runtime_type || "runtime pending"}</span>
            )}
            {closed && " · session closed"}
            {snap?.archived && " · archived (read-only, from signed ledger)"}
          </p>
        </div>
        <button onClick={onReset}>New session</button>
      </div>

      <div className="ladder">
        {LADDER.map((s) => (
          <div
            key={s}
            className={`ladder-step state-${s.toLowerCase()} ${s === state ? "current" : ""} ${
              LADDER.indexOf(s) < LADDER.indexOf(state) ? "passed" : ""
            }`}
          >
            {s}
          </div>
        ))}
      </div>

      {t && (
        <div className={`termination ${t.verdict === "KILL_CONFIRMED" ? "confirmed" : "failed"}`}>
          <h3>{t.verdict === "KILL_CONFIRMED" ? "✓ KILL CONFIRMED" : "✗ KILL NOT CONFIRMED"}</h3>
          <p className="muted">reason: {t.reason}</p>
          <ul>
            {Object.entries(t.checks).map(([k, v]) => (
              <li key={k} className={v ? "ok" : "fail"}>
                {v ? "✓" : "✗"} {CHECK_LABELS[k] ?? k}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid">
        <div className="card">
          <h3>Observed by the governor (host-side, not self-reported)</h3>
          {b && snap && (
            <>
              <ResourceGauge label="CPU (cumulative, cgroup cpu.stat)" used={snap.cpu_usec / 1e6} limit={b.cpu_budget_usec / 1e6} unit="s" />
              <ResourceGauge
                label={`Memory peak, host-accounted microVM (memory.peak)${snap.memory_telemetry_valid ? "" : " · telemetry not yet validated"}`}
                used={snap.memory_peak_bytes / 2 ** 20}
                limit={b.memory_limit_bytes / 2 ** 20}
                unit="MB"
              />
              <ResourceGauge
                label={`Tokens (Vultr Inference, ${snap.inference.calls ?? 0} calls)`}
                used={snap.tokens}
                limit={b.token_limit}
                unit="tok"
                format={(n) => `${Math.round(n).toLocaleString()}`}
              />
              <ResourceGauge label="Wall clock" used={snap.wall_seconds} limit={b.wall_clock_limit_seconds} unit="s" />
              <p className="muted small">
                Estimated cost ${snap.inference.estimated_cost_usd.toFixed(5)} · model {snap.inference.last_model}
                {snap.inference.throttled
                  ? snap.inference.model_downgrade_available
                    ? ` (THROTTLE: downgraded to ${snap.inference.cheap_model})`
                    : " (THROTTLE: CPU throttled; no cheaper model configured)"
                  : ""}
                {snap.inference.cost_limit_usd ? ` · cap $${snap.inference.cost_limit_usd}` : ""}
                {snap.inference.in_flight ? ` · ${snap.inference.in_flight} request in flight` : ""}
                {snap.inference.uncertain_requests ? ` · ${snap.inference.uncertain_reserved_tokens} tokens uncertain (no provider usage received)` : ""}
                {snap.inference.revoked ? " · inference revoked" : ""}
              </p>
            </>
          )}
          <ControlBar sessionId={sessionId} state={state} closed={closed} />
        </div>

        <div className="card">
          <h3>Containment</h3>
          {c ? (
            <ul className="checklist">
              <li className={c.egress_blocked ? "ok" : "fail"}>{c.egress_blocked ? "✓" : "✗"} Network egress blocked inside the microVM</li>
              <li className={c.no_secrets_in_env ? "ok" : "fail"}>{c.no_secrets_in_env ? "✓" : "✗"} No API keys / secrets in guest environment</li>
              <li className={!c.host_paths_visible?.length ? "ok" : "fail"}>
                {!c.host_paths_visible?.length ? "✓" : "✗"} Host ledger, keys and cgroup tree invisible to the guest
              </li>
              <li className={c.inputs_read_only ? "ok" : "muted"}>
                {c.inputs_read_only ? "✓" : "–"} Agent runs as uid {c.uid ?? "?"}; inputs read-only to it
              </li>
              <li className="ok">✓ Guest sees only: {(c.workspace_files || []).join(", ") || "—"}</li>
              <li className={snap?.lifecycle?.workload_gone ? "ok" : "muted"}>
                {snap?.lifecycle
                  ? snap.lifecycle.sandbox_removed && snap.lifecycle.workload_gone && snap.lifecycle.cgroup_removed
                    ? "✓ microVM stopped & removed, cgroup removed"
                    : `✗ cleanup incomplete${snap.lifecycle.sandbox_stop_error ? ": " + snap.lifecycle.sandbox_stop_error : ""}`
                  : "… microVM destroyed when session ends"}
              </li>
            </ul>
          ) : (
            <p className="muted">Probe pending…</p>
          )}
        </div>

        <div className="card">
          <GovernanceTimeline entries={entries} />
        </div>

        <div className="card">
          <h3>Recommendations (deterministic, from observed metrics)</h3>
          {(snap?.recommendations ?? []).length === 0 && <p className="muted">No recommendations yet.</p>}
          {(snap?.recommendations ?? []).map((r, i) => (
            <RecommendationCard key={r.recommendation_id ?? i} rec={r} />
          ))}
        </div>
      </div>

      <div className="card">
        <h3>Prove: evidence &amp; independent verification</h3>
        <EvidenceReport sessionId={sessionId} autoLoad={closed} />
      </div>
    </div>
  );
}
