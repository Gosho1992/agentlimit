import { useEffect, useRef, useState } from "react";
import { getEvents, getFeatured, getSession, OPERATOR, setFeatured, streamSession, type Access } from "../api/client";
import type { GovernanceState, LedgerEntry, SessionSnapshot } from "../types";
import { CONTAINMENT_PLAIN, KILL_PLAIN, overall, plainRule, scenarioOf } from "../plain";
import ControlBar from "./ControlBar";
import EvidenceReport from "./EvidenceReport";
import GovernanceTimeline from "./GovernanceTimeline";
import Meter from "./Meter";
import RecommendationCard from "./RecommendationCard";
import StatusBanner from "./StatusBanner";
import Story from "./Story";
import Term from "./Term";
import Tour from "./Tour";

const TITLES: Record<string, string> = {
  reconcile: "Useful work, checked",
  runaway: "Out-of-control agent, stopped",
  retry: "Agent stuck in a loop",
  memory: "Agent filling its memory",
};

export default function SessionDashboard({ sessionId, onBack, access = OPERATOR, backLabel = "← Back" }: {
  sessionId: string; onBack: () => void; access?: Access; backLabel?: string;
}) {
  const operator = !access.public;
  const [snap, setSnap] = useState<(SessionSnapshot & Record<string, any>) | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [technical, setTechnical] = useState(false);
  const [tour, setTour] = useState(false);
  const [feat, setFeat] = useState<boolean | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (operator) {
      // fast path for the operator; the 1 s poll below is the source of truth for everyone
      const push = (e: LedgerEntry) => setEntries((prev) => [...prev, e]);
      stopRef.current = streamSession(sessionId, {
        onState: push, onSample: () => {}, onRecommend: push, onLedger: push,
        onDone: () => stopRef.current?.(), onError: () => {},
      });
      getFeatured(sessionId).then((r) => setFeat(r.featured)).catch(() => setFeat(null));
    }
    let closedSeen = false;
    const poll = async () => {
      try {
        const s = await getSession(sessionId, access);
        setSnap(s);
        setErr(null);
        closedSeen = !!s.lifecycle || !!s.archived;
      } catch (e: any) {
        setErr(e.message);
      }
      try {
        const evs = await getEvents(sessionId, access);
        setEntries((prev) => {
          const seen = new Set(prev.map((e) => e.entry_hash));
          const add = evs.filter((e: LedgerEntry) => !seen.has(e.entry_hash));
          return add.length ? [...prev, ...add].sort((a, b) => a.timestamp_utc.localeCompare(b.timestamp_utc)) : prev;
        });
      } catch {
        /* noop */
      }
    };
    poll();
    const id = setInterval(() => { if (!closedSeen) poll(); }, 1000);
    return () => {
      stopRef.current?.();
      clearInterval(id);
    };
  }, [sessionId, operator, access.guestToken]);

  const info = overall(snap, entries);
  useEffect(() => {
    document.body.dataset.signal = info.signal;
    return () => { delete document.body.dataset.signal; };
  }, [info.signal]);


  const state = (snap?.state ?? "NORMAL") as GovernanceState;
  const b = snap?.budget;
  const t = snap?.termination;
  const closed = !!snap?.lifecycle || !!snap?.archived;
  const c = snap?.containment;
  const canControl = operator || !!snap?.can_control;
  const sc = snap?.scenario ?? scenarioOf(snap?.task);
  const title = (sc && TITLES[sc]) || (snap ? "Custom task" : "Loading…");
  const cost = snap?.inference?.committed_cost_usd ?? snap?.inference?.estimated_cost_usd ?? 0;

  // server-computed (monotonic clock), refreshed by the 1 s poll
  const autoStopIn = state === "PAUSE" && !closed && typeof snap?.auto_stop_in_s === "number"
    ? Math.round(snap.auto_stop_in_s) : null;

  if (err && !snap) {
    return (
      <div className="card">
        <p className="error">This run isn't available: {err}</p>
        <button onClick={onBack}>{backLabel}</button>
      </div>
    );
  }

  return (
    <div className="dashboard">
      <div className="dash-top">
        <button className="btn-ghost" onClick={onBack}>{backLabel}</button>
        <div className="dash-title">
          <h1 className="dash-h1">{title}</h1>
          <span className="muted small">
            {snap?.archived ? "Recorded real run · replay from its signed record" : closed ? "Finished run" : "Live run on a Vultr server"}
            {" · "}run <code>{sessionId}</code>
          </span>
        </div>
        <div className="dash-tools">
          <button className="btn-tour" onClick={() => setTour(true)}>❔ 1-minute tour</button>
          <label className="switch">
            <input type="checkbox" checked={technical} onChange={(e) => setTechnical(e.target.checked)} />
            <span>Technical details</span>
          </label>
          {operator && feat !== null && (
            <button className="btn-ghost" title="Show this run on the public page as a replay"
              onClick={() => setFeatured(sessionId, !feat).then((r) => setFeat(r.featured))}>
              {feat ? "★ Public replay" : "☆ Make public replay"}
            </button>
          )}
        </div>
      </div>

      <StatusBanner info={info} state={state} />
      {autoStopIn !== null && (
        <div className="countdown">
          ⏱️ Waiting for a human. If nobody unfreezes it, AgentLimit will <strong>stop it for good in {autoStopIn} s</strong>.
          {canControl && " Use the buttons under Limits to decide now."}
        </div>
      )}

      {state === "KILL" && !t && (
        <div className="killbox pending" role="status">
          <h3>⏳ Stopping and verifying…</h3>
          <p className="small">Cutting AI access, ending the agent, destroying the sandbox. The six checks appear here when they are done.</p>
        </div>
      )}
      {t && (
        <div className={`killbox ${t.verdict === "KILL_CONFIRMED" ? "confirmed" : "failed"}`}>
          <h3>{t.verdict === "KILL_CONFIRMED" ? "🛑 Stopped for good, and proven" : "✗ Stop could not be fully confirmed"}</h3>
          <p className="small">
            Why: {t.reason?.startsWith("hard_limit") ? `it hit its hard limit (${t.reason.split(":")[1]?.replace("_", " ")})` : t.reason?.includes("operator") ? "a human pressed Stop for good" : t.reason}.
            AgentLimit then checked six separate things:
          </p>
          <ul className="checks">
            {Object.entries(t.checks).map(([k, v]) => (
              <li key={k} className={v ? "ok" : "fail"}>
                {v ? "✓" : "✗"} {KILL_PLAIN[k] ?? k}
                {technical && <span className="tech-line">{k}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid">
        <div id="tour-meters" className="card">
          <h3><Term k="Budget">Limits</Term> <span className="muted small">measured from outside the sandbox</span></h3>
          {b && snap ? (
            <>
              <Meter name="Computer time" term="CPU time" used={snap.cpu_usec / 1e6} limit={b.cpu_budget_usec / 1e6}
                fmt={(n) => `${n.toFixed(1)} s`} hint="processor work used so far" />
              <Meter name="Memory" term="Memory" used={snap.memory_peak_bytes / 2 ** 20} limit={b.memory_limit_bytes / 2 ** 20}
                fmt={(n) => `${Math.round(n)} MB`} hint={snap.memory_telemetry_valid ? "peak working space" : "peak working space (meter warming up)"} />
              <Meter name="AI usage" term="Tokens" used={snap.tokens} limit={b.token_limit}
                fmt={(n) => `${Math.round(n).toLocaleString()} tokens`} hint={`${snap.inference.calls ?? 0} AI call${snap.inference.calls === 1 ? "" : "s"}`} />
              <Meter name="Running time" used={snap.wall_seconds} limit={b.wall_clock_limit_seconds}
                fmt={(n) => `${Math.round(n)} s`} hint="clock time since start" />
              {(snap.inference.cost_limit_usd ?? 0) > 0 && (
                <Meter name="AI spending (estimate)" used={cost} limit={snap.inference.cost_limit_usd!}
                  fmt={(n) => `$${n.toFixed(4)}`} hint="dollar cap, from Vultr's price list; includes calls in flight" />
              )}
              <p className="small">
                {[
                  (snap.inference.cost_limit_usd ?? 0) > 0 ? null
                    : <span key="c">AI cost so far ≈ <strong>${cost.toFixed(4)}</strong> <span className="muted">(estimate from Vultr's price list; no dollar cap on this run, the token budget applies)</span></span>,
                  snap.inference.throttled && snap.inference.model_downgrade_available
                    ? <span key="d">switched to cheaper model <code>{snap.inference.cheap_model}</code></span> : null,
                  snap.inference.throttled && !snap.inference.model_downgrade_available ? <span key="n">cheaper model not available</span> : null,
                  snap.inference.revoked ? <span key="r">AI access closed</span> : null,
                ].filter(Boolean).flatMap((x, i) => (i ? [<span key={`s${i}`}> · </span>, x] : [x]))}
              </p>
              {technical && (
                <p className="muted small tech-block">
                  cpu.stat {snap.cpu_usec} µs · memory.peak {snap.memory_peak_bytes} B · settled ${snap.inference.estimated_cost_usd.toFixed(5)} ·
                  in-flight ${(snap.inference.in_flight_reserved_cost_usd ?? 0).toFixed(5)} · uncertain ${(snap.inference.uncertain_reserved_cost_usd ?? 0).toFixed(5)} ·
                  model {snap.inference.effective_model ?? snap.inference.last_model} · runtime {snap.runtime_type} · VMM pid {snap.sandbox_pid}
                  {snap.memory_plan && ` · memory.max ${Math.round(snap.memory_plan.host_memory_max_bytes / 2 ** 20)} MB, memory.high ${Math.round(snap.memory_plan.memory_high_bytes / 2 ** 20)} MB, guest RAM ${snap.memory_plan.guest_ram_mb} MB`}
                </p>
              )}
            </>
          ) : <p className="muted">Starting…</p>}
          {canControl && <ControlBar sessionId={sessionId} access={access} state={state} closed={closed}
            killFailed={t?.verdict === "KILL_FAILED"} technical={technical} />}
          {!canControl && !closed && <p className="muted small">Only the person who started this run can freeze or stop it.</p>}
        </div>

        <div className="card">
          <Story entries={entries} technical={technical} />
        </div>

        <div className="card">
          <h3>🔒 <Term k="Sandbox">Sealed sandbox</Term></h3>
          {c ? (
            <>
              <p className={c.passed ? "ok" : "fail"}>
                {c.passed ? "Checked from inside before the agent ran:" : `Safety check failed: ${c.failed_checks.join(", ")}`}
              </p>
              <ul className="checks">
                {Object.entries(c.checks || {}).map(([k, v]) => (
                  <li key={k} className={v ? "ok" : "fail"}>
                    {v ? "✓" : "✗"} {CONTAINMENT_PLAIN[k] ?? k}{technical && <span className="tech-line">{k}</span>}
                  </li>
                ))}
                <li className={snap?.lifecycle ? (snap.lifecycle.sandbox_removed && snap.lifecycle.workload_gone ? "ok" : "fail") : "muted"}>
                  {snap?.lifecycle
                    ? snap.lifecycle.sandbox_removed && snap.lifecycle.workload_gone ? "✓ Sandbox destroyed after the run" : "✗ Clean-up incomplete"
                    : "… Sandbox will be destroyed when the run ends"}
                </li>
              </ul>
              <p className="muted small">It's a <Term k="MicroVM">microVM</Term>: a tiny separate computer, not just a folder.</p>
            </>
          ) : <p className="muted">Checking…</p>}
        </div>

        <div className="card">
          <h3>💡 Advice from AgentLimit</h3>
          {(snap?.recommendations ?? []).length === 0 && <p className="muted">No problems spotted so far.</p>}
          {(snap?.recommendations ?? []).map((r, i) =>
            technical ? <RecommendationCard key={r.recommendation_id ?? i} rec={r} /> : (
              <div key={r.recommendation_id ?? i} className="advice">
                <p>{plainRule(r.rule_id)}</p>
                <AdviceSteps rec={r} entries={entries} />
              </div>
            ))}
        </div>
      </div>

      <details className="card task-box">
        <summary>The exact task given to the agent</summary>
        <p className="mono small">{snap?.task}</p>
        <p className="muted small">The agent writes its own Python code for this, then runs it inside the sealed sandbox.</p>
      </details>

      <div id="tour-proof" className="card">
        <h3>🧾 The proof <span className="muted small">re-checked from the signed record</span></h3>
        {!closed && <p className="muted">The full proof appears when the run ends.</p>}
        <EvidenceReport key={String(closed)} sessionId={sessionId} access={access} autoLoad={closed} technical={technical} />
      </div>

      {technical && (
        <div className="card">
          <GovernanceTimeline entries={entries} />
        </div>
      )}

      <Tour open={tour} onClose={() => setTour(false)} />
    </div>
  );
}


/** Suggested → Applied → Verified, read from the signed recommendation_applied event. */
function AdviceSteps({ rec, entries }: { rec: any; entries: LedgerEntry[] }) {
  const ev = [...entries].reverse().find((e) => e.event_type === "recommendation_applied"
    && (e.payload.recommendation_id === rec.recommendation_id || (!rec.recommendation_id && e.payload.rule_id === rec.rule_id)));
  const applied = ev?.payload.applied === true;
  const verified = ev?.payload.verified === true;
  if (rec.status === "pending_operator" || rec.action === "notify_operator")
    return <p className="small muted">Suggested · needs a human decision (AgentLimit does not act on its own here).</p>;
  return (
    <p className="small steps-line">
      <span className="ok">✓ Suggested</span>
      <span className={applied ? "ok" : ev ? "fail" : "muted"}>{applied ? "✓ Applied" : ev ? "✗ Not applied" : "… Applying"}</span>
      <span className={verified ? "ok" : applied ? "warn" : "muted"}>{verified ? "✓ Verified" : applied ? "≈ Not independently verified" : ""}</span>
    </p>
  );
}
