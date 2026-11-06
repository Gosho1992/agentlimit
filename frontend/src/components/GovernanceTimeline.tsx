import type { LedgerEntry } from "../types";

const SHOW: Record<string, (p: any) => string> = {
  containment_probe: (p) => `containment probe ${p.passed ? "passed (all mandatory checks)" : "FAILED: " + (p.failed_checks || []).join(", ")}`,
  sandbox_attached: (p) => `microVM VMM pid ${p.root_pid} + descendants verified in host cgroup (${p.members?.length} procs, ${p.stable_passes} stable scans)`,
  sandbox_attach_failed: (p) => `cgroup attachment FAILED: ${JSON.stringify(p.failures)}`,
  session_start_failed: (p) => `session refused (${p.category})`,
  state_transition: (p) => `${p.from} → ${p.to}  (${p.cause}${p.utilisation_pct ? ", " + Object.entries(p.utilisation_pct).map(([k, v]) => `${k} ${v}%`).join(" ") : ""})`,
  recommendation_issued: (p) => `recommend: ${p.rule_id} — ${p.suggestion?.slice(0, 70)}`,
  recommendation_applied: (p) => `applied ${p.action}${p.applied ? "" : " (failed)"}${p.context_chars_before ? ` prompt ${p.context_chars_before}→${p.context_chars_after} chars` : ""}`,
  throttle_applied: (p) => `throttle: ${p.action} (readback ${p.cpu_max_readback}); model: ${typeof p.model_downgrade === "string" ? p.model_downgrade : p.model_downgrade.from + " → " + p.model_downgrade.to}`,
  inference_call: (p) => (p.effective_model !== p.requested_model ? `model call #${p.request_id} used ${p.effective_model} (downgraded from ${p.requested_model})` : ""),
  pause_applied: (p) => `pause: ${p.mode}${p.verified_frozen ? " (frozen verified)" : ""}`,
  resume_applied: () => `resumed`,
  budget_extension: (p) => `operator approved +${p.cpu_usec_added / 1e6}s CPU`,
  orchestrator_terminated: () => `agent loop cancelled, inference revoked`,
  kill_verified: (p) => `KILL CONFIRMED (${p.reason})`,
  kill_failed: (p) => `KILL NOT CONFIRMED: ${JSON.stringify(p.checks)}`,
  agent_stopped: (p) => `agent stopped by ${p.stopped_by}: ${p.reason}`,
  agent_completed: (p) => `agent completed, output sha256 ${p.stdout_sha256?.slice(0, 12)}…`,
  agent_budget_exceeded: (p) => `model call refused: ${p.reason}`,
  memory_telemetry_validated: () => `host memory telemetry validated`,
  session_closed: (p) => `session closed: sandbox gone=${p.workload_gone}, cgroup removed=${p.cgroup_removed}`,
};

export default function GovernanceTimeline({ entries }: { entries: LedgerEntry[] }) {
  const seen = new Set<string>();
  const rows = entries.filter((e) => {
    if (!SHOW[e.event_type] || !SHOW[e.event_type](e.payload) || seen.has(e.entry_hash)) return false;
    seen.add(e.entry_hash);
    return true;
  });
  return (
    <div className="timeline">
      <h3>Governance timeline (signed ledger)</h3>
      {rows.length === 0 && <p className="muted">Waiting for events…</p>}
      <ul>
        {rows.map((e) => (
          <li key={e.entry_hash} className={`ev-${e.event_type}`}>
            <span className="muted mono">{new Date(e.timestamp_utc).toLocaleTimeString()}</span>{" "}
            {e.event_type === "state_transition" && (
              <span className={`state-pill state-${String(e.payload.to).toLowerCase()}`}>{e.payload.to}</span>
            )}{" "}
            {SHOW[e.event_type](e.payload)}
          </li>
        ))}
      </ul>
    </div>
  );
}
