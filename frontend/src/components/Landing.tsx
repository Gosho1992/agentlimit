import { useEffect, useState } from "react";
import { getReplays, getScenarios, tryScenario, type PublicScenario } from "../api/client";
import { LADDER, STATES, scenarioOf } from "../plain";
import Term from "./Term";

const TITLES: Record<string, string> = {
  reconcile: "Useful work, checked", runaway: "Out-of-control agent, stopped",
  retry: "Agent stuck in a loop", memory: "Agent eating memory",
};

// Badge = how the run ended. Claims about correctness come only from r.highlights (server-derived from signed evidence).
function outcome(r: any): { cls: string; text: string } {
  if (r.kill_verdict === "KILL_CONFIRMED") return { cls: "sig-red", text: "🛑 Stopped for good" };
  if (r.kill_verdict === "KILL_FAILED") return { cls: "sig-yellow", text: "⚠️ Stop not confirmed" };
  if (r.agent_status === "completed") return { cls: "sig-green", text: "🏁 Finished" };
  if (String(r.agent_status).startsWith("stopped")) return { cls: "sig-yellow", text: "✋ Stopped early" };
  return { cls: "sig-grey", text: r.final_state };
}

type Load<T> = { state: "loading" } | { state: "error"; error: string } | { state: "ok"; data: T };

export default function Landing({ go, openRun }: { go: (page: string) => void; openRun: (id: string, token?: string) => void }) {
  const [sc, setSc] = useState<Load<{ scenarios: PublicScenario[]; guest_mode: boolean }>>({ state: "loading" });
  const [rp, setRp] = useState<Load<any[]>>({ state: "loading" });
  const scenarios = sc.state === "ok" ? sc.data.scenarios : [];
  const guest = sc.state === "ok" ? sc.data.guest_mode : false;
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const loadScenarios = () => {
    setSc({ state: "loading" });
    getScenarios().then((d) => setSc({ state: "ok", data: d })).catch((e) => setSc({ state: "error", error: e.message }));
  };
  const loadReplays = () => {
    setRp({ state: "loading" });
    getReplays().then((d) => setRp({ state: "ok", data: d })).catch((e) => setRp({ state: "error", error: e.message }));
  };
  useEffect(() => { loadScenarios(); loadReplays(); }, []);

  async function start(id: string) {
    setBusy(id);
    setErr(null);
    try {
      const r = await tryScenario(id);
      openRun(r.session_id, r.guest_token);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  const jump = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });

  return (
    <div className="landing">
      <section className="hero">
        <div className="hero-badge">Vultr Agent Rush · Blast Radius Zero</div>
        <h1>A circuit breaker for <span className="grad">AI agents</span>.</h1>
        <p className="hero-sub">
          AI agents now write and run their own code. When one goes wrong it can loop forever, burn your money or run
          wild. <strong>AgentLimit</strong> watches the agent from <em>outside</em> its sandbox. It warns it, slows it,
          freezes it or stops it for good, and gives you <strong>proof</strong> of what happened that nobody can secretly edit.
        </p>
        <div className="hero-cta">
          <button className="btn-big" onClick={() => jump("try")}>▶ Try it now (no sign-up)</button>
          <button className="btn-big btn-ghost" onClick={() => jump("replays")}>Watch a real run</button>
          <button className="btn-big btn-ghost" onClick={() => go("guide")}>How it works</button>
        </div>
        <div className="hero-lights">
          {LADDER.map((s) => (
            <div key={s} className={`hero-light sig-${STATES[s].signal}`}>
              <span className="light-dot" /> {STATES[s].label}
            </div>
          ))}
        </div>
      </section>

      <section className="why">
        <div className="why-card"><span>🔁</span><h3>Loops cost money</h3>
          <p>An agent that retries the same broken step 200 times pays for 200 AI calls.</p></div>
        <div className="why-card"><span>🔥</span><h3>Code can run away</h3>
          <p>Generated code can spin forever, eat all the memory, and even ignore "please stop".</p></div>
        <div className="why-card"><span>🤥</span><h3>Agents grade themselves</h3>
          <p>Most tools trust the agent's own report of what it did and how much it used.</p></div>
      </section>

      <section className="how">
        <h2>How it works, in 3 steps</h2>
        <div className="steps">
          <div className="step"><div className="step-n">1</div><h3>🔒 A sealed box</h3>
            <p>The agent's code runs in a <Term k="Sandbox">sandbox</Term>: a tiny separate computer on a Vultr server.
              It has no internet, can't see any passwords, and can't change its input files.</p></div>
          <div className="step"><div className="step-n">2</div><h3>📏 Meters on the outside</h3>
            <p>AgentLimit measures computer time, memory, AI usage and time from <em>outside</em> the box.
              The agent can't fake these numbers.</p></div>
          <div className="step"><div className="step-n">3</div><h3>🚦 Step in, then prove it</h3>
            <p>As limits get close it climbs a ladder: warn → advise → slow down → freeze → stop for good.
              Every step goes into a <Term k="Signed record (ledger)">signed record</Term> you can check yourself.</p></div>
        </div>
      </section>

      <section className="legend">
        <h2>Read it like a traffic light</h2>
        <div className="legend-grid">
          {LADDER.map((s) => (
            <div key={s} className={`legend-item sig-${STATES[s].signal}`}>
              <div className="legend-head"><span className="legend-icon">{STATES[s].icon}</span><strong>{STATES[s].label}</strong></div>
              <p>{STATES[s].say}</p>
            </div>
          ))}
        </div>
      </section>

      <section id="try" className="try">
        <h2>▶ Try it yourself</h2>
        <p className="muted">
          Pick a situation. A real AI agent (running on Vultr Serverless Inference) gets the task, writes its own code and
          runs it in a fresh sandbox on a Vultr server, while AgentLimit governs it live. A run takes 1 to 4 minutes.
        </p>
        {sc.state === "loading" && <p className="muted">Loading the scenarios…</p>}
        {sc.state === "error" && (
          <p className="error" role="alert">Couldn't reach the server ({sc.error}). <button className="btn-ghost" onClick={loadScenarios}>Retry</button></p>
        )}
        {sc.state === "ok" && !guest && <p className="warn">Live runs are switched off right now; you can still watch the recorded runs below.</p>}
        <div className="try-grid">
          {scenarios.map((s) => (
            <div key={s.id} className={`try-card edge-${s.expect_color}`}>
              <h3>{s.title}</h3>
              <p>{s.plain}</p>
              <p className="small"><strong>What you'll see:</strong> {s.expect}</p>
              <button disabled={!guest || !!busy} onClick={() => start(s.id)}>
                {busy === s.id ? "Starting a fresh sandbox…" : "Start this run"}
              </button>
            </div>
          ))}
        </div>
        {err && <p className="error">{err}</p>}
      </section>

      <section id="replays" className="replays">
        <h2>🎬 Watch a recorded real run</h2>
        <p className="muted">These ran on the Vultr server and are replayed from their signed record. Each card lists only what that record proves. You can re-check the proof and run the tamper test yourself.</p>
        {rp.state === "loading" && <p className="muted">Loading recorded runs…</p>}
        {rp.state === "error" && (
          <p className="error" role="alert">Couldn't load the recorded runs ({rp.error}). This is a connection problem, not missing evidence.{" "}
            <button className="btn-ghost" onClick={loadReplays}>Retry</button></p>
        )}
        {rp.state === "ok" && rp.data.length === 0 && <p className="muted">No recorded runs published yet.</p>}
        <div className="replay-list">
          {(rp.state === "ok" ? rp.data : []).map((r) => {
            const o = outcome(r);
            const scid = r.scenario ?? scenarioOf(r.task);
            return (
              <button key={r.session_id} className="replay" onClick={() => openRun(r.session_id)}>
                <span className={`replay-badge ${o.cls}`}>{o.text}</span>
                <strong>{(scid && TITLES[scid]) || "Custom task"}</strong>
                {(r.highlights ?? []).length > 0 && (
                  <ul className="highlights">
                    {r.highlights.map((h: any, i: number) => (
                      <li key={i}><span className={`story-dot sig-${h.signal}`} />{h.text}</li>
                    ))}
                  </ul>
                )}
                <span className="muted small">{new Date(r.created_at).toLocaleString()} · watch & verify →</span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="compare">
        <h2>Without vs. with AgentLimit</h2>
        <div className="table-scroll"><table className="compare-table">
          <thead><tr><th></th><th>A normal agent setup</th><th>With AgentLimit</th></tr></thead>
          <tbody>
            <tr><td>Who decides when to stop?</td><td>The agent itself</td><td>✓ An outside referee that can force a stop</td></tr>
            <tr><td>Who measures usage?</td><td>The agent reports it</td><td>✓ Measured from outside, can't be faked</td></tr>
            <tr><td>When things go wrong</td><td>Keeps going, or is killed blindly</td><td>✓ Warn → slow down → freeze → stop, step by step</td></tr>
            <tr><td>AI spending</td><td>Seen on next month's bill</td><td>✓ Token budget and dollar cap enforced before each AI call; can switch to a cheaper model</td></tr>
            <tr><td>"Did it really stop?"</td><td>Hope so</td><td>✓ 6 separate checks, recorded</td></tr>
            <tr><td>"Is the answer right?"</td><td>Trust the agent</td><td>✓ Re-calculated independently</td></tr>
            <tr><td>Proof afterwards</td><td>Logs anyone can edit</td><td>✓ Signed record; edits are caught</td></tr>
          </tbody>
        </table></div>
      </section>

      <section className="vultr">
        <h2>Built on Vultr</h2>
        <div className="steps">
          <div className="step"><h3>🖥️ Vultr Cloud Compute</h3><p>Runs AgentLimit and starts a fresh KVM <Term k="MicroVM">microVM</Term> sandbox for every run.</p></div>
          <div className="step"><h3>🧠 Vultr Serverless Inference</h3><p>The agent's brain. AgentLimit meters every AI call and can switch to a cheaper Vultr model when slowing the agent down.</p></div>
          <div className="step"><h3>🧾 Proof you can check</h3><p>Download the signed record and verify it on your own laptop, without trusting this server.</p></div>
        </div>
      </section>

      <section className="cta-end">
        <h2>Building your own agent?</h2>
        <p className="muted">Send AgentLimit a task and its limits through a simple API, and get back the result, what AgentLimit did, and the signed proof.</p>
        <button className="btn-big" onClick={() => go("developers")}>For developers →</button>
      </section>
    </div>
  );
}
