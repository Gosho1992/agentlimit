# AgentLimit — an external runtime governor for AI agents on Vultr

**Built for:** Vultr Agent Rush (lablab.ai), Challenge 1: *Blast Radius Zero*

AgentLimit runs an autonomous coding agent that does real work. The code the agent writes runs inside a **Microsandbox microVM (libkrun/KVM) on Vultr Cloud Compute**. A **governor that runs outside the sandbox** watches the agent's actual resource use. As the session gets close to its limits, the governor steps up through
**ALERT → RECOMMEND → THROTTLE → PAUSE → KILL**. Every decision goes into an **Ed25519-signed, hash-chained ledger**, and anyone can verify that ledger offline.

> The agent never grades its own homework. The governor measures from the host, enforces from the host, and proves it afterwards.

---

## What the judge sees (3-minute demo)

| # | Scenario (one click in the UI) | What happens | Containment moment |
|---|---|---|---|
| 1 | **Useful work, verified** | The agent reconciles 20,000 orders against payments. The verifier recomputes the answer from the hashed input snapshot and shows *agent claim vs. recomputation*. | Probe: no network, no secrets, no host paths visible in the guest |
| 2 | **Runaway agent → external kill** | The agent writes a CPU burner that ignores SIGTERM. Host `cpu.stat` drives the full ladder: throttle (`cpu.max`, plus a cheaper model if one is configured), freeze (`cgroup.freeze`), then operator Kill. | **KILL CONFIRMED** with 6 verified checks: session terminated, inference revoked, agent loop cancelled, `populated 0`, VMM process gone, sandbox removed via SDK |
| 3 | **Retry loop** | The task needs a package that isn't there. The governor blocks the 4th identical attempt **before the model is called** (the agent's own limit would have been 5). | Inference spend stops |
| 4 | **Tamper test** | One button edits a recorded CPU sample in a copy of the ledger. The verifier rejects it and names the entry that was changed. | Evidence integrity |

---

## Architecture (stated precisely)

```
 Browser ──HTTPS (Let's Encrypt, when DOMAIN is set)──> nginx ──> FastAPI backend (Vultr VX1, Ubuntu 24.04, root)
                                                          (token auth on every session/evidence API)
                               │
   ┌───────────────────────────┼───────────────────────────────────────────────┐
   │ TRUSTED CONTROL PLANE     │                                               │
   │  Agent orchestrator ──────┼──> Inference adapter ──> Vultr Serverless     │
   │   (plans, retries)        │     (holds the ONLY API key,                  │
   │        │                  │      pre-call token/cost reservation,         │
   │        │ step gate <──────┼──── revocable by the governor)                │
   │        ▼                  │                                               │
   │  GOVERNOR (deterministic) │  reads cgroup v2: cpu.stat, memory.peak       │
   │   ladder + recommendations│  writes: cpu.max, memory.high, cgroup.freeze, │
   │   + verified kill         │          cgroup.kill                          │
   │        │                  │                                               │
   │  Evidence ledger (Ed25519 + SHA-256 chain) + independent verifier         │
   └────────┼──────────────────────────────────────────────────────────────────┘
            │ exec(code)                     ▲ host cgroup /sys/fs/cgroup/agentlimit/<session>
            ▼                                │
   ┌─────────────────────────────────────────┴──┐
   │ UNTRUSTED: Microsandbox microVM (KVM)      │  Network.none(), env={}, guest rlimits,
   │ runs ONLY the model-generated Python       │  read-only input snapshot in /workspace,
   │                                            │  destroyed at session end
   └────────────────────────────────────────────┘
```

**What runs where.** The agent loop and the inference adapter run in the backend. Only the generated code runs in the microVM. That split is deliberate: the component that holds the API key and pays for inference is never inside the sandbox. It also means a kill has to stop three things, not one. So **KILL** does all of the following:

1. Marks the session terminated.
2. Revokes the inference adapter. **No new model request is sent after revocation.** If a request is already in flight, it runs in its own task, shielded from the agent's cancellation. If the provider answers, its real usage is recorded and the result is thrown away; it never reaches the agent. Until then, its reservation (estimated prompt + allowed completion) counts against the budget as *uncertain* spend. If no answer ever arrives, it stays recorded as uncertain: we never claim usage the provider did not report.
3. Cancels the agent task.
4. Writes `cgroup.kill` and waits for `cgroup.events: populated 0`.
5. Stops and removes the sandbox through the Microsandbox SDK as a second path. Its *actual* result is a check of its own.
6. **Verifies each condition on its own.** The result is `KILL_CONFIRMED` only when all checks pass. Otherwise it is `KILL_FAILED`, with the failing checks named.

**Measurement boundary.** Host cgroups account for the VMM process tree (guest RAM plus VMM overhead, vCPU time). The **memory budget means host-accounted microVM memory**, because that is what the governor measures. The guest is given `budget − 192 MB` of RAM. `memory.max` is set to the budget, the memory recommendation fires at 80% of it, and it applies `memory.high` at 90% of it. Guest processes are not host processes. Guest process count is limited with in-guest rlimits, not host `pids.max`. Host memory is used for enforcement only after the governor has seen it respond to guest allocation (`memory_telemetry_validated`).

---

## Vultr services used

| Service | Role |
|---|---|
| **Vultr Cloud Compute (VX1, KVM)** | Hosts the backend, the governor, and all microVMs. It is the control and enforcement plane, not static hosting. |
| **Vultr Serverless Inference** (`https://api.vultrinference.com/v1`, OpenAI-compatible) | Plans and writes the agent's code and repairs it from real stderr. Configured model IDs are checked against `GET /v1/models` at startup (see `/health`). THROTTLE switches to `VULTR_MODEL_CHEAP` **only if** a distinct model is configured. Otherwise the ledger and the UI say "CPU throttling only", and no downgrade is claimed. |
| **Microsandbox on Vultr** (per Vultr's *Agent sandboxing on Cloud Compute* guide) | One fresh libkrun/KVM microVM per session, with no network and an empty env. |

Cost is shown as an **estimate** from a price table (default: Vultr's published $0.55 / $2.75 per 1M input/output tokens; override with `PRICING_OVERRIDES`). Authoritative spend is in the Vultr console Usage tab.

---

## Containment requirements → implementation

| Requirement | How it is met | Where it is proven |
|---|---|---|
| Process isolation | KVM microVM per session. The VMM PID is attached to a dedicated host cgroup, and the session is refused (**fail-closed**) if it can't be attached. | `sandbox_attached` ledger entry |
| Secret hygiene | The inference key lives only in the host adapter. Guest `env={}` and `Network.none()`. Agent code runs as the unprivileged user `nobody`, and the inputs are root-owned and read-only. An in-guest probe checks egress, DNS, secret-like env vars, host paths, uid and input writability. A failed probe blocks the session. | `containment_probe` |
| Resource limits | Session budgets for cumulative CPU (cgroup), memory, tokens (with pre-call reservation), wall clock and an optional estimated-$ cap. `memory.max` includes VMM headroom. 90-second timeout per exec. Guest NPROC/FSIZE rlimits. | `cgroup_configured`, `state_transition` |
| Lifecycle discipline | Every session (completed, failed, stopped or killed) goes through `finalize()`. Each result is reported separately and truthfully: agent stopped, inference revoked, in-flight requests settled, VM stopped, VM removed (actual SDK result), cgroup emptied and removed. On startup, orphaned cgroups and sandboxes are removed, and sessions a crash left open are marked `session_interrupted`. | `session_closed` |

---

## Evidence and verification ("Prove")

- **Ledger:** append-only JSONL. Each entry is `sha256(canonical(entry))`, chained by `prev_hash` and signed with Ed25519.
- **Inputs:** copied into a read-only snapshot and hashed when the session starts. The verifier re-hashes them before recomputing anything.
- **Artifact:** the agent's final stdout is written to disk and its SHA-256 is recorded in the signed `agent_completed` entry.
- **Level 2 correctness:** reconciliation is recomputed independently from the verified inputs and compared with the agent's claim.
- **Offline verification:** download the ledger and run the standalone verifier. It needs only PyNaCl and does not trust the server:
  ```bash
  curl -o ledger.jsonl http://<host>/sessions/<id>/evidence/export
  curl -s http://<host>/sessions/<id>/evidence/public-key
  python backend/scripts/verify_ledger.py ledger.jsonl <public_key_b64>
  ```
- **Persistence:** sessions are stored as their signed ledgers. After a restart, past sessions are listed and their evidence is re-verified from the ledger, read-only.
- **Trust model:** tamper-evident as long as the signing key is not compromised. Verify against the *published fingerprint*, not only the key the server serves. It is *not* immutable against someone with host root. The verifier runs host-side, isolated from the agent, but not in a separate VM.

---

## Security of the public deployment

- **Authentication:** every session, control, stream and evidence endpoint requires `ACCESS_TOKEN`, either as a `Bearer` header (curl/scripts) or as the HttpOnly, SameSite=Strict cookie issued by `POST /auth/login` (the dashboard). With no token configured, the APIs return 503: they refuse to run unprotected. `/health` shows only readiness unless you are authenticated. Login attempts are rate-limited.
- **CORS:** same-origin only by default (the dashboard is served by the backend). Extra origins must be listed in `ALLOWED_ORIGINS`.
- **TLS:** HTTPS with Let's Encrypt when the installer is given `DOMAIN` (a free `<ip>.sslip.io` name works). Cookies are then `Secure`. Without `DOMAIN`, the deployment is plain HTTP and is described that way.
- **Spend caps across all sessions:** at most `MAX_CONCURRENT_SESSIONS` running, `MAX_SESSIONS_PER_HOUR` started per hour, and `MAX_TOKENS_PER_DAY` tokens per day (in-flight reservations included). These sit on top of the per-session budgets.

**For judges:** the access code and the evidence-key fingerprint are in the lablab submission text. Compare the fingerprint shown in the dashboard (and printed by the offline verifier) with that independently published value. Do not just trust the key the server hands out.

Evidence signing key fingerprint: `<filled in after deployment — see `journalctl -u agentlimit | grep fingerprint`>`

## Deploy on Vultr (recommended: native)

1. Create a **VX1 Cloud Compute** instance with Ubuntu 24.04. KVM must be available: `ls /dev/kvm`.
2. Clone the repo and run the installer, replacing the dashed IP with your VM's IP:
   ```bash
   git clone <this repo> agentlimit && cd agentlimit
   sudo DOMAIN=203-0-113-7.sslip.io EMAIL=you@example.com bash deploy/install_vultr.sh
   ```
   The installer sets up the venv, the Microsandbox SDK (which bundles `msb` and libkrunfw), a generated `ACCESS_TOKEN`, demo data, the dashboard build, a systemd service on 127.0.0.1:8000, nginx with SSE unbuffered, and the Let's Encrypt certificate. It then runs the infrastructure gate.
3. In `backend/.env`, set `VULTR_INFERENCE_KEY`, `VULTR_MODEL_DEFAULT`, and optionally a genuinely cheaper `VULTR_MODEL_CHEAP`. Use IDs exactly as `/health` lists them. Then run `sudo systemctl restart agentlimit`.
4. Check readiness: `curl -s -H "Authorization: Bearer <ACCESS_TOKEN>" http://127.0.0.1:8000/health` should show `"ready_for_governed_sessions": true`.
5. Open `https://<DOMAIN>/` and enter the access code.

**Infrastructure gate.** Run this before every demo: `sudo venv/bin/python -m backend.scripts.day1_gate`. It checks:
- KVM, cgroup v2, VM boot and exec
- host PID discovery, cgroup attach, CPU and memory accounting
- egress blocked, no secrets in the guest, unprivileged agent, read-only inputs, host paths invisible
- freeze/unfreeze, killing a **SIGTERM-resistant** workload, SDK removal after the kill, and cleanup

Its output is saved to `/var/lib/agentlimit/day1_gate.txt`. Claim only what passes.

**Docker Compose** (alternative): `cp backend/.env.example backend/.env`, set the token and key, then `docker compose up --build`. This needs `/dev/kvm`, `privileged` and `cgroup: host`, which are already in the compose file. The Docker *sandbox* fallback (`ALLOW_DOCKER_FALLBACK=true`) exists only for machines without KVM. It does not provide the microVM isolation guarantees, it is not part of the demonstrated claims, it is never selected silently, and the dashboard labels it.

## Develop and test

```bash
pip install -r backend/requirements.txt
python -m pytest backend/tests -q        # 72 pass anywhere; 3 kernel tests run on cgroup v2 as root
cd frontend && npm install && npm run dev
```

The tests use the real budget objects, governor, interventions, orchestrator, inference adapter and evidence API. Only the kernel, VM and HTTP model endpoint are faked, so the suite runs anywhere. The kernel paths are covered by `test_freeze_kill.py` and the infrastructure gate on the VM.

## API

`POST /auth/login` · `GET /auth/status` · `POST /sessions` · `GET /sessions` · `GET /sessions/{id}` · `GET /sessions/{id}/stream` (SSE) · `POST /sessions/{id}/pause|resume|kill` · `GET /sessions/{id}/evidence` · `GET …/evidence/verify` · `GET …/evidence/export` · `GET …/evidence/public-key` · `POST …/evidence/tamper-test` · `GET /health`

## What this project does not claim

- It does not claim to have invented sandboxing, cgroups or budget alerts. The contribution is the *combination*: host-side measurement, graduated enforcement that also covers the inference spend, verified termination, and signed proof.
- No guaranteed savings. A recommendation is reported as effective only when its before and after measurements (for example, prompt characters before and after compaction) show it.
- No exact guest-process CPU accounting. The numbers are host-side VMM figures.
- Dollar figures are estimates. An optional estimated-cost cap is enforced through the same pre-call reservation.
- AgentLimit does not claim that cancelling a request locally cancels it at the provider. It claims that no *new* request is sent after revocation, and it accounts for in-flight requests as described above.

## License

MIT
