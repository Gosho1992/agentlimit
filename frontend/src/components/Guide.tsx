import { GLOSSARY, LADDER, STATES } from "../plain";

export default function Guide({ go }: { go: (p: string) => void }) {
  return (
    <div className="doc">
      <h1>How AgentLimit works</h1>
      <p className="lead">A one-page guide for people who build with AI, no security background needed.</p>

      <h2>The problem</h2>
      <p>
        An AI agent is a program that asks an AI model what to do and then does it, for example writing code and running it.
        That's powerful, but nobody is holding the brakes. The agent decides for itself when to stop, and reports for
        itself how much it used. A bug or a confused model can loop forever, eat the whole machine, or rack up a big AI bill.
      </p>

      <h2>The idea: a referee outside the box</h2>
      <div className="diagram">
        <div className="dg-box dg-agent">🤖 AI agent<span>asks Vultr Serverless Inference what code to write</span></div>
        <div className="dg-arrow">runs code in ↓</div>
        <div className="dg-box dg-sandbox">🔒 Sealed sandbox (microVM)<span>no internet · no passwords · read-only inputs</span></div>
        <div className="dg-side">
          <div className="dg-box dg-gov">🚦 AgentLimit, outside<span>measures · steps in · stops · signs a record</span></div>
        </div>
      </div>
      <p>
        AgentLimit never asks the agent how it's doing. It reads the numbers from the server's own kernel, outside the
        sandbox, where the agent can't reach. Because it controls the sandbox and the AI access from outside, it can always stop them.
      </p>

      <h2>The safety ladder</h2>
      <p>Like a referee, AgentLimit doesn't jump straight to a red card. It climbs one step at a time as limits get close:</p>
      <div className="legend-grid">
        {LADDER.map((s, i) => (
          <div key={s} className={`legend-item sig-${STATES[s].signal}`}>
            <div className="legend-head"><span className="legend-icon">{STATES[s].icon}</span><strong>{STATES[s].label}</strong>
              <span className="muted small">{i === 0 ? "" : ["", "70%", "80%", "85%", "95%", "100%"][i]}</span></div>
            <p>{STATES[s].say}</p>
          </div>
        ))}
      </div>

      <h2>"Stop for good" really means gone</h2>
      <ol>
        <li>The agent's AI access is cut off, so it can't make any more model calls.</li>
        <li>The agent's decision loop is ended.</li>
        <li>Every process in the sandbox is killed by the kernel, even ones that ignore "please stop".</li>
        <li>The sandbox is <strong>destroyed</strong>: deleted completely, not just paused.</li>
        <li>AgentLimit then <strong>checks 6 separate things</strong> to confirm nothing is left. If any check fails, it says so instead of showing green.</li>
      </ol>

      <h2>The proof</h2>
      <p>
        Everything that happens is written into a <strong>signed record</strong>. Each line is sealed with a digital
        signature and linked to the line before it, so changing or deleting anything, even at the very end, is detected.
        Press <strong>"Try to cheat"</strong> on any run to watch it happen. You can download the record and check it on
        your own laptop with a small script, without trusting this website.
      </p>
      <p>For the reconciliation demo, AgentLimit also <strong>re-does the agent's maths itself</strong> from the original sealed files and compares answers.</p>

      <h2>Words you'll see</h2>
      <dl className="glossary">
        {Object.entries(GLOSSARY).map(([k, v]) => (
          <div key={k}><dt>{k}</dt><dd>{v}</dd></div>
        ))}
      </dl>

      <p>
        <button onClick={() => go("home#try")}>▶ Try it yourself</button>{" "}
        <button className="btn-ghost" onClick={() => go("developers")}>For developers →</button>
      </p>
    </div>
  );
}
