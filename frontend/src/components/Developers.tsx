const HOST = typeof window !== "undefined" ? window.location.origin : "https://your-agentlimit-host";

const CURL = `# 1. Start a governed run: a task + its limits
curl -s -X POST ${HOST}/sessions \\
  -H "Authorization: Bearer $AGENTLIMIT_TOKEN" -H "Content-Type: application/json" \\
  -d '{"task": "Count the rows in /workspace/orders.csv and print the number",
       "cpu_budget_seconds": 20, "wall_clock_seconds": 120, "token_budget": 30000}'
# → {"session_id": "ab12…", "status": "running", "containment": {"passed": true, …}}

# 2. Watch it (state goes NORMAL → … ; agent_claim holds the answer)
curl -s ${HOST}/sessions/ab12… -H "Authorization: Bearer $AGENTLIMIT_TOKEN"

# 3. Stop it yourself at any time (destroys the sandbox, returns 6 checks)
curl -s -X POST ${HOST}/sessions/ab12…/kill -H "Authorization: Bearer $AGENTLIMIT_TOKEN"

# 4. Get the proof, and the signed record to verify offline
curl -s ${HOST}/sessions/ab12…/evidence -H "Authorization: Bearer $AGENTLIMIT_TOKEN"
curl -s -o run.jsonl ${HOST}/sessions/ab12…/evidence/export -H "Authorization: Bearer $AGENTLIMIT_TOKEN"`;

const PY = `from agentlimit_client import AgentLimit     # clients/agentlimit_client.py, stdlib only

al = AgentLimit("${HOST}", token)
result = al.run(
    "Count the rows in /workspace/orders.csv and print the number",
    cpu_seconds=20, wall_seconds=120, token_budget=30_000,
)
print(result["outcome"])      # "completed" or "stopped_by_agentlimit"
print(result["answer"])       # what the agent printed
print(result["proof"])        # {"ledger_integrity": "VALID", "containment": "VALID", ...}`;

const VERIFY = `pip install pynacl
python backend/scripts/verify_ledger.py run.jsonl <public_key_b64>
# VALID: 214 entries, chain intact, all Ed25519 signatures verify
# COMPLETE: ends with signed session_closed
# Key fingerprint: f70e:46c6:…  ← compare with the README`;

const SELF = `# on a Vultr Cloud Compute VM (Ubuntu 24.04, KVM available)
git clone https://github.com/Gosho1992/agentlimit && cd agentlimit
sudo DOMAIN=<your-ip-with-dashes>.sslip.io EMAIL=you@example.com bash deploy/install_vultr.sh
# then put your Vultr Serverless Inference key in backend/.env and restart`;

function Code({ children }: { children: string }) {
  return (
    <div className="code">
      <button className="copy" onClick={() => navigator.clipboard?.writeText(children)}>Copy</button>
      <pre>{children}</pre>
    </div>
  );
}

export default function Developers({ go }: { go: (p: string) => void }) {
  return (
    <div className="doc">
      <h1>For developers</h1>
      <p className="lead">
        AgentLimit is a runtime that wraps an AI coding agent. You send a <strong>task</strong> and its <strong>limits</strong>.
        The agent (on Vultr Serverless Inference) writes Python, AgentLimit runs it in a sealed microVM and governs it from
        outside, and you get back <strong>the result, what AgentLimit did, and a signed proof</strong>.
      </p>

      <div className="doc-grid">
        <div className="doc-card"><h3>You set</h3><p>CPU seconds · memory MB · AI token budget · optional $ cap · max running time</p></div>
        <div className="doc-card"><h3>AgentLimit does</h3><p>warn → advise → slow down (+ cheaper model) → freeze → stop for good, measured from outside</p></div>
        <div className="doc-card"><h3>You get back</h3><p>the answer · the stop verdict with 6 checks · a per-area proof · a signed record you can verify offline</p></div>
      </div>

      <h2>1. From the command line</h2>
      <p className="muted">Ask the operator for an access code, or self-host (step 4) and use your own.</p>
      <Code>{CURL}</Code>

      <h2>2. From Python</h2>
      <p className="muted">One file, no dependencies: <code>clients/agentlimit_client.py</code>. It also works as a CLI:
        <code> python agentlimit_client.py "your task" --cpu 20 --wall 120</code>.</p>
      <Code>{PY}</Code>

      <h2>3. Verify the proof yourself</h2>
      <p className="muted">The standalone verifier does not import or trust the AgentLimit server.</p>
      <Code>{VERIFY}</Code>

      <h2>4. Run your own AgentLimit on Vultr</h2>
      <Code>{SELF}</Code>
      <p className="muted small">The installer runs a 23-check infrastructure gate (KVM boot, cgroup attach, network blocked,
        freeze, kill of a process that ignores stop signals, cleanup) and refuses to call the host ready if any check fails.</p>

      <h2>What it does today, honestly</h2>
      <ul>
        <li>It governs AgentLimit's built-in coding agent (model writes Python → runs in the microVM). Plugging in your own agent framework through an SDK is the next step on the roadmap.</li>
        <li>The automatic answer check (re-calculating the result) exists for the reconciliation demo; for any other task you get the signed record and the containment and stop proofs.</li>
        <li>Costs are estimates from Vultr's published prices, not your invoice.</li>
      </ul>
      <p><button onClick={() => go("guide")}>How it works →</button> <button className="btn-ghost" onClick={() => go("home")}>← Home</button></p>
    </div>
  );
}
