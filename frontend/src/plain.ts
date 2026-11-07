// Plain-language layer: every technical idea gets a simple name, a colour
// and one sentence. The technical term stays visible in small text.
import type { GovernanceState, LedgerEntry } from "./types";

export type Signal = "green" | "yellow" | "orange" | "blue" | "red" | "grey";

export interface StateInfo { signal: Signal; icon: string; label: string; tech: string; say: string }

export const STATES: Record<GovernanceState, StateInfo> = {
  NORMAL: { signal: "green", icon: "✅", label: "All good", tech: "NORMAL",
    say: "Your agent is working inside its limits. AgentLimit is watching from outside." },
  ALERT: { signal: "yellow", icon: "⚠️", label: "Heads up", tech: "ALERT",
    say: "The agent has used 70% of one of its limits. AgentLimit is watching closely." },
  RECOMMEND: { signal: "yellow", icon: "💡", label: "Advice given", tech: "RECOMMEND",
    say: "AgentLimit spotted a problem and suggested (or applied) a fix." },
  THROTTLE: { signal: "orange", icon: "🐢", label: "Slowed down", tech: "THROTTLE",
    say: "AgentLimit turns down the agent's computer speed and, when one is available, switches it to a cheaper AI model." },
  PAUSE: { signal: "blue", icon: "🧊", label: "Frozen", tech: "PAUSE",
    say: "Everything is frozen in place: no code runs and no AI calls are made until a human decides." },
  KILL: { signal: "red", icon: "🛑", label: "Stopped for good", tech: "KILL",
    say: "AgentLimit shut the agent down and destroyed its sandbox (deleted it completely). " +
      "It cannot run or call the AI again, and AgentLimit checked 6 times that it is really gone." },
};

export const LADDER: GovernanceState[] = ["NORMAL", "ALERT", "RECOMMEND", "THROTTLE", "PAUSE", "KILL"];

const lastOf = (entries: LedgerEntry[], t: string) => {
  for (let i = entries.length - 1; i >= 0; i--) if (entries[i].event_type === t) return entries[i].payload || {};
  return null;
};

/** Overall signal for a session. Never more confident than the signed evidence:
 *  KILL is "stopped" only with a confirmed verdict; THROTTLE and PAUSE say only what was verified. */
export function overall(snap: any, entries: LedgerEntry[] = []): StateInfo {
  if (!snap) return { signal: "grey", icon: "⏳", label: "Starting…", tech: "", say: "Starting a fresh, sealed sandbox for the agent." };
  const state = (snap.state ?? "NORMAL") as GovernanceState;
  const closed = !!snap.lifecycle || !!snap.archived;
  if (state === "KILL") {
    const t = snap.termination;
    if (!t) return { signal: "red", icon: "⏳", label: "Stopping…", tech: "KILL · VERIFYING",
      say: "AgentLimit is cutting AI access, ending the agent and destroying its sandbox. It will now check that nothing is left." };
    if (t.verdict === "KILL_CONFIRMED") return STATES.KILL;
    const lc = snap.lifecycle;
    if (lc && lc.workload_gone && lc.sandbox_removed)
      return { signal: "yellow", icon: "⚠️", label: "Stopped, with a warning", tech: "KILL_FAILED → CLEANED",
        say: "The stop missed one of its six checks (shown below). AgentLimit's final clean-up then confirmed the sandbox is gone and deleted." };
    return { signal: "yellow", icon: "⚠️", label: "Stop not confirmed", tech: "KILL_FAILED",
      say: "AgentLimit tried to stop the agent but could not confirm every check. See which check failed below. It needs attention." };
  }
  if (closed) {
    if (snap.agent_status === "completed")
      return { signal: "green", icon: "🏁", label: "Finished", tech: "COMPLETED",
        say: "The agent finished inside its limits. The proof below shows exactly what was checked." };
    if (String(snap.agent_status).startsWith("stopped"))
      return { signal: "yellow", icon: "✋", label: "Stopped early", tech: "STOPPED",
        say: "AgentLimit stopped the agent before it wasted more time and money (for example, retrying the same broken thing)." };
    return { signal: "yellow", icon: "⏹️", label: "Ended", tech: String(snap.agent_status || "").toUpperCase(),
      say: "The run ended without a final answer. The proof below shows exactly why." };
  }
  if (snap.agent_status === "pending") return { signal: "grey", icon: "⏳", label: "Starting…", tech: "STARTING",
    say: "Starting a fresh, sealed sandbox for the agent." };
  if (state === "THROTTLE") {
    const p = lastOf(entries, "throttle_applied");
    if (!p) return { ...STATES.THROTTLE, say: "AgentLimit is slowing the agent down…" };
    const down = p.model_downgrade && typeof p.model_downgrade === "object";
    if (p.verified === false)
      return { ...STATES.THROTTLE, label: "Slow-down not confirmed", say: "AgentLimit asked the kernel to slow the sandbox down, but the kernel did not confirm it. Recorded, not hidden." };
    return { ...STATES.THROTTLE, say: down
      ? `AgentLimit halved the sandbox's computer speed (kernel confirmed) and switched the agent's next AI calls to the cheaper model ${p.model_downgrade.to}.`
      : "AgentLimit halved the sandbox's computer speed (kernel confirmed). A cheaper AI model isn't available on this server, so the model stays the same." };
  }
  if (state === "PAUSE") {
    const p = lastOf(entries, "pause_applied");
    if (!p) return { ...STATES.PAUSE, label: "Freezing…", say: "AgentLimit is freezing the sandbox…" };
    if (p.mode === "KERNEL_FREEZE" && p.verified_frozen)
      return { ...STATES.PAUSE, label: "Frozen", say: "Everything is frozen in place (the kernel confirms it): no code runs and no AI calls are made until a human decides." };
    return { ...STATES.PAUSE, label: "Paused", say: "The agent is held before its next step: no new AI calls or code runs. A kernel freeze wasn't available, so code already running may finish its current step." };
  }
  return STATES[state] ?? STATES.NORMAL;
}

export const GLOSSARY: Record<string, string> = {
  "AI agent": "A program that uses an AI model to decide what to do next, for example writing code and then running it.",
  "Sandbox": "A sealed, throw-away mini computer. The agent's code runs inside it, so it can't touch anything outside.",
  "MicroVM": "The kind of sandbox AgentLimit uses: a tiny separate virtual computer with its own operating system. Stronger walls than a normal container.",
  "Governor": "AgentLimit itself. It sits outside the sandbox, measures everything and can step in. The agent can't switch it off.",
  "Budget": "The limits you set before the run: how much computer time, memory, AI usage and wall-clock time the agent may use.",
  "CPU time": "How much processor work the agent has used. Like a taxi meter that only runs while the computer is busy.",
  "Memory": "The working space (RAM) the agent is using right now.",
  "Tokens": "How AI usage is counted and billed: roughly, pieces of words sent to and from the AI model.",
  "Slow down (throttle)": "AgentLimit limits how fast the sandbox may run and switches to a cheaper AI model.",
  "Freeze (pause)": "Everything stops exactly where it is, like pressing pause on a video. It can continue later.",
  "Stop for good (kill)": "AgentLimit ends the agent, blocks any more AI calls and destroys the sandbox. This can't be undone.",
  "Destroy": "Delete the sandbox completely: its processes are ended and its files are removed, so nothing is left running.",
  "Containment checks": "Tests run inside the sandbox before the agent starts: no internet, no passwords or keys visible, can't change the input files.",
  "Signed record (ledger)": "A diary of everything that happened. Each line is sealed with a digital signature, like a wax seal.",
  "Digital signature": "A seal only AgentLimit's private key can make. If anyone changes a single character, the seal breaks.",
  "Fingerprint": "A short code that identifies AgentLimit's signing key. Compare it with the one in the README to know the seal is genuine.",
  "Tamper test": "AgentLimit edits a copy of the record on purpose to show that the change is caught instantly.",
  "Independent check": "AgentLimit re-does the agent's maths itself from the original files, instead of trusting the agent's answer.",
  "Vultr": "The cloud this runs on: a Vultr server runs the sandboxes, and Vultr Serverless Inference runs the AI model.",
};

export const CONTAINMENT_PLAIN: Record<string, string> = {
  network_egress_blocked: "No internet access",
  dns_blocked: "Can't even look up website names",
  no_secrets_in_environment: "Can't see any passwords or API keys",
  no_host_paths_visible: "Can't see AgentLimit's own files or controls",
  workspace_mounted: "Has its work folder",
  inputs_readable: "Can read the input files",
  inputs_read_only: "Can't change the input files",
  unprivileged_execution: "Runs without admin rights",
  inputs_match_manifest: "Sees exactly the files that were sealed at the start",
};

export const KILL_PLAIN: Record<string, string> = {
  session_marked_terminated: "Run marked as stopped",
  inference_revoked: "AI access cut off: no more model calls possible",
  agent_task_finished: "The agent's decision loop has ended",
  cgroup_populated_0: "The kernel confirms zero processes left",
  sandbox_pid_gone: "The sandbox computer's process is gone",
  sandbox_removed_via_runtime: "The sandbox was destroyed (deleted)",
};

export const VERIFY_PLAIN: Record<string, string> = {
  ledger_integrity: "Nobody changed the record",
  ledger_completeness: "Nothing was cut off the end of the record",
  input_integrity: "Input files are the original ones",
  artifact_integrity: "The agent's answer file is untouched",
  artifact_correctness: "The answer is correct (re-calculated independently)",
  containment: "The sandbox was sealed",
  resource_limits: "The limits were really set in the kernel",
  termination: "The stop really happened",
  execution_cleanup: "Everything was cleaned up afterwards",
  inference_accounting: "AI usage fully accounted for",
};

export const VERDICT_PLAIN: Record<string, string> = {
  VALID: "✓ Yes", COMPLETE: "✓ Yes", FAILED: "✗ No", INCOMPLETE: "✗ No", PARTIAL: "≈ Partly",
  PENDING: "… Not yet", ERROR: "⚠ Check failed to run", IN_PROGRESS: "… Still running", INTERRUPTED: "⚠ Interrupted", NOT_APPLICABLE: "— Not needed",
};

const s = (n: number) => `${Math.round(n)}s`;

/** One plain sentence per signed event (null = not worth showing to a newcomer). */
export function narrate(e: LedgerEntry): { text: string; signal?: Signal } | null {
  const p = e.payload || {};
  switch (e.event_type) {
    case "session_created": return { text: "A new run was created with its limits (budget).", signal: "grey" };
    case "guest_session": return { text: "Started by a visitor from the public demo page." };
    case "inputs_snapshotted": return { text: "The input files were copied and sealed, so any later change would be detected." };
    case "runtime_started": return { text: "A fresh sandbox (a tiny separate computer) was started on the Vultr server." };
    case "sandbox_attached": return { text: "AgentLimit attached its meters to the sandbox from the outside." };
    case "containment_probe": return p.passed
      ? { text: "Safety checks passed: no internet, no passwords visible, inputs can't be changed.", signal: "green" }
      : { text: "Safety checks FAILED, so the agent was never allowed to run.", signal: "red" };
    case "governance_started": return { text: "AgentLimit started watching the agent.", signal: "green" };
    case "inference_call": return p.effective_model !== p.requested_model
      ? { text: `The agent asked the AI for help, using the cheaper model ${p.effective_model} (it was slowed down).`, signal: "orange" }
      : null;
    case "state_transition": {
      const st = STATES[p.to as GovernanceState];
      if (!st) return null;
      const human = /operator|manual/.test(String(p.cause));
      const why = human ? " (a human pressed the button)" : p.utilisation_pct
        ? ` (${Object.entries(p.utilisation_pct as Record<string, number>).sort((a, b) => b[1] - a[1])
            .slice(0, 1).map(([k, v]) => `${plainMetric(k)} at ${v}%`).join("")})` : "";
      if (p.to === "KILL") return { text: `Decided to stop it for good${why}. Stopping and checking…`, signal: "red" };
      return { text: `${st.icon} ${st.label}${why}.`, signal: st.signal };
    }
    case "recommendation_issued": return { text: `💡 Advice: ${plainRule(p.rule_id)}`, signal: "yellow" };
    case "recommendation_applied": return p.applied
      ? { text: p.verified === true ? "AgentLimit applied the fix itself and confirmed it took effect."
          : "AgentLimit applied the fix itself (not independently confirmed).", signal: "yellow" }
      : p.action === "notify_operator" ? { text: "This one needs a human decision; AgentLimit didn't act on its own." } : null;
    case "recommendation_delivery_failed": return { text: "Advice could not be delivered to the agent (recorded, not hidden).", signal: "yellow" };
    case "throttle_applied": return p.verified === false
      ? { text: "Tried to slow the sandbox down, but the kernel did NOT confirm it.", signal: "orange" }
      : { text: `🐢 Sandbox slowed to half speed (kernel confirmed)${p.model_downgrade && typeof p.model_downgrade === "object" ? `; next AI calls use the cheaper ${p.model_downgrade.to}` : "; no cheaper model available"}.`, signal: "orange" };
    case "pause_applied": return { text: p.mode === "KERNEL_FREEZE" && p.verified_frozen
      ? "🧊 Frozen: the kernel confirms nothing inside the sandbox can run."
      : "⏸️ Paused before the next step (kernel freeze not available).", signal: "blue" };
    case "pause_timeout_escalation": return { text: `Nobody decided within ${Math.round(p.timeout_s)} s, so AgentLimit stopped it for good.`, signal: "red" };
    case "manual_pause": return { text: "A human pressed Freeze.", signal: "blue" };
    case "resume_applied": return { text: "▶️ Unfrozen, the agent continues.", signal: "green" };
    case "budget_extension": return { text: `A human approved ${s((p.cpu_usec_added || 0) / 1e6)} of extra computer time.` };
    case "manual_kill": return { text: "A human pressed Stop for good.", signal: "red" };
    case "watchdog_kill": return { text: "🚨 AgentLimit's backup watchdog stopped the sandbox because the main controller froze.", signal: "red" };
    case "kill_requested": return { text: "Stopping: cutting AI access, ending the agent, destroying the sandbox…", signal: "red" };
    case "kill_verified": return { text: "🛑 Confirmed stopped: all 6 checks passed. Nothing is left running.", signal: "red" };
    case "kill_failed": return { text: "⚠️ Stop could NOT be fully confirmed; see the checks below.", signal: "yellow" };
    case "kill_retry": return { text: "A human asked AgentLimit to try the stop again.", signal: "red" };
    case "agent_stopped": return { text: `✋ The agent was stopped${p.stopped_by === "governor" ? " by AgentLimit" : ""}: ${plainReason(p.reason)}`, signal: "yellow" };
    case "agent_budget_exceeded": return { text: "An AI call was refused because it would go over the budget.", signal: "orange" };
    case "agent_completed": return { text: "🎉 The agent finished and handed in its answer.", signal: "green" };
    case "memory_telemetry_validated": return { text: "Memory meter confirmed working." };
    case "session_interrupted": return { text: "The server restarted during this run; recorded honestly as interrupted.", signal: "yellow" };
    case "session_closed": return { text: "🧹 Cleaned up: sandbox deleted, meters removed, AI access closed. The record is sealed.", signal: "grey" };
    case "session_start_failed": return { text: "The run was refused at the start because a safety step failed.", signal: "red" };
    default: return null;
  }
}

export function plainMetric(k: string): string {
  return ({ cpu: "computer time", memory: "memory", tokens: "AI usage", wall_clock: "running time", cost: "cost" } as any)[k] ?? k;
}

export function plainRule(rule: string): string {
  return ({
    retry_loop: "the agent keeps repeating the same failing attempt. Stop retrying it.",
    memory_pressure: "memory is filling up. Squeeze the sandbox's memory before it runs out.",
    context_growth: "the conversation with the AI is growing fast. Shorten it to save money.",
    burn_rate: "at this speed the agent will hit its limit in under 30 seconds. A human should decide.",
  } as any)[rule] ?? rule;
}

function plainReason(r: string | undefined): string {
  if (!r) return "";
  if (r.includes("retry")) return "it kept retrying the same broken thing.";
  if (r.includes("terminated")) return "it was shut down.";
  return r.replace(/_/g, " ") + ".";
}

// Published in the README and the submission. Shown next to the live key so anyone can compare.
export const PUBLISHED_FINGERPRINT = "f70e:46c6:e95d:d675:0357:89c9:1a13:e7a3";

const SCENARIO_PREFIX: [string, string][] = [
  ["reconcile", "Read /workspace/orders.csv (order_id,amount)"],
  ["runaway", "Benchmark this sandbox"],
  ["retry", "Download today's exchange rates from https://example.com/fx/today.json"],
  ["retry", "This task REQUIRES the third-party package"],
  ["memory", "Stress-test this sandbox's memory"],
  ["memory", "Stress-test memory"],
];
export function scenarioOf(task?: string): string | null {
  if (!task) return null;
  return SCENARIO_PREFIX.find(([, p]) => task.startsWith(p))?.[0] ?? null;
}
