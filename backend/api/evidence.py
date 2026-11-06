"""
Evidence endpoints.

  GET  /sessions/{id}/evidence          full report
  GET  /sessions/{id}/evidence/verify   re-run every check now
  GET  /sessions/{id}/evidence/export   signed ledger (JSONL) + public key, for offline verification
  POST /sessions/{id}/evidence/tamper-test
        copies the ledger, flips one recorded value in the COPY, and shows the
        verifier rejecting it (the real ledger is never modified)

Everything here is re-derived from the signed ledger and files on disk, not
from in-memory agent state:
  - chain:     hash links + Ed25519 signature on every entry
  - inputs:    snapshot files re-hashed against `inputs_snapshotted`
  - artifact:  final stdout file re-hashed against `agent_completed`
  - level 2:   reconciliation recomputed independently from the verified inputs
"""

import copy
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.evidence.archive import read_entries, summarize
from backend.evidence.ledger import verify_entries
from backend.evidence.signer import fingerprint
from backend.evidence.verifier import DeterministicVerifier
from backend.services.session_manager import session_manager

router = APIRouter(prefix="/sessions", tags=["evidence"])


def _entries(session_id: str) -> list[dict]:
    """Live or archived — the report is always derived from the signed ledger."""
    rt = session_manager.get(session_id)
    entries = rt.ledger.read_all() if rt else read_entries(session_id)
    if entries is None:
        raise HTTPException(404, "session not found")
    return entries


def _last(entries: list[dict], event_type: str) -> dict | None:
    for e in reversed(entries):
        if e["event_type"] == event_type:
            return e
    return None


def _verify_artifact(entries: list[dict]) -> dict:
    inputs_ev = _last(entries, "inputs_snapshotted")
    done_ev = _last(entries, "agent_completed")
    result: dict = {}

    inputs_ok, inputs_detail = (False, {})
    if inputs_ev:
        inputs_ok, inputs_detail = DeterministicVerifier.verify_inputs(
            inputs_ev["payload"]["files"], inputs_ev["payload"]["snapshot_dir"])
    result["inputs"] = {"verified": inputs_ok, "files": inputs_detail}

    if not done_ev:
        return {**result, "level": 0, "verdict": "NO_ARTIFACT", "detail": "agent produced no successful output"}

    path = done_ev["payload"].get("artifact_path")
    recorded = done_ev["payload"].get("stdout_sha256")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return {**result, "level": 0, "verdict": "FAILED", "detail": f"artifact missing: {path}"}
    actual = DeterministicVerifier.sha256_bytes(data)
    integrity = {"recorded_in_ledger": recorded, "actual": actual, "match": actual == recorded}
    result["integrity"] = integrity

    agent_report = DeterministicVerifier.parse_agent_json(data.decode("utf-8", "ignore"))
    result["agent_report"] = agent_report
    snap = inputs_ev["payload"]["snapshot_dir"] if inputs_ev else None
    files = (inputs_ev or {}).get("payload", {}).get("files", {})
    if agent_report is None or not snap or "orders.csv" not in files or "payments.csv" not in files:
        verdict = "INTEGRITY_ONLY" if integrity["match"] else "FAILED"
        return {**result, "level": 1, "verdict": verdict,
                "detail": "no Level-2 check applies (output not JSON or no reconciliation inputs)"}
    if not inputs_ok:
        return {**result, "level": 1, "verdict": "FAILED", "detail": "input snapshot hashes do not match ledger"}

    correct, detail = DeterministicVerifier.level2_reconciliation(
        f"{snap}/orders.csv", f"{snap}/payments.csv", agent_report)
    result["correctness"] = json.loads(detail)
    ok = integrity["match"] and correct
    return {**result, "level": 2, "verdict": "VERIFIED" if ok else "FAILED",
            "detail": "agent's claim matches independent recomputation" if ok
            else "agent's claim does NOT match independent recomputation"}


def _containment_checklist(entries: list[dict]) -> dict:
    probe = (_last(entries, "containment_probe") or {}).get("payload", {})
    rt_ev = (_last(entries, "runtime_started") or {}).get("payload", {})
    cg_ev = (_last(entries, "cgroup_configured") or {}).get("payload", {})
    attach = _last(entries, "sandbox_attached")
    closed = (_last(entries, "session_closed") or {}).get("payload")
    lifecycle_ok = bool(closed) and all(closed.get(k) for k in ("sandbox_removed", "workload_gone", "cgroup_removed"))
    return {
        "process_isolation": {
            "ok": bool(attach) and rt_ev.get("runtime_type") == "microsandbox",
            "detail": f"generated code runs in a {rt_ev.get('runtime_type')} guest; "
                      f"VMM pid {attach['payload']['pid'] if attach else '?'} attached to host cgroup",
        },
        "secret_hygiene": {
            "ok": bool(probe.get("no_secrets_in_env")) and bool(probe.get("egress_blocked")),
            "detail": f"guest env secret-like vars={probe.get('secret_env_vars')}; "
                      f"egress_blocked={probe.get('egress_blocked')}; agent uid={probe.get('uid')}; "
                      f"inputs read-only to agent={probe.get('inputs_read_only')}; "
                      "inference key held only by host adapter",
        },
        "resource_limits": {
            "ok": bool(cg_ev),
            "detail": f"cgroup memory.max={cg_ev.get('memory.max_bytes')} cpu.max={cg_ev.get('cpu.max')} "
                      f"pids.max={cg_ev.get('pids.max')}; per-exec timeout 90s; session CPU/token/wall budgets",
        },
        "lifecycle_discipline": {
            "ok": lifecycle_ok,
            "detail": ({k: closed.get(k) for k in ("sandbox_stopped", "sandbox_removed", "sandbox_stop_error",
                                                   "workload_gone", "cgroup_removed", "inference_revoked")}
                       if closed else "session still open"),
        },
    }


def _report(session_id: str) -> dict:
    entries = _entries(session_id)
    rt = session_manager.get(session_id)
    pubkey = session_manager.signer.public_key_b64()
    chain_ok, chain_detail = verify_entries(entries, pubkey)
    summary = summarize(entries)
    inference = rt.orchestrator.inference.snapshot() if rt else (summary["inference"] or {})
    recs = [e["payload"] for e in entries if e["event_type"] == "recommendation_issued"]
    return {
        "session_id": session_id,
        "task": summary["task"],
        "chain_valid": chain_ok,
        "chain_detail": chain_detail,
        "artifact_verification": _verify_artifact(entries),
        "containment": _containment_checklist(entries),
        "termination": summary["termination"],
        "lifecycle": summary["lifecycle"],
        "public_key_b64": pubkey,
        "public_key_fingerprint": fingerprint(pubkey),
        "final_state": summary["final_state"],
        "agent_status": rt.orchestrator.status if rt else summary["agent_status"],
        "steps_completed": summary["steps"],
        "inference": inference,
        "recommendations": recs,
        "archived": rt is None,
        "entry_count": len(entries),
        "entries": entries,
    }


@router.get("/{session_id}/evidence")
async def get_evidence(session_id: str):
    return JSONResponse(_report(session_id))


@router.get("/{session_id}/evidence/verify")
async def verify_evidence(session_id: str):
    r = _report(session_id)
    return {k: r[k] for k in ("chain_valid", "chain_detail", "artifact_verification", "containment")}


@router.get("/{session_id}/evidence/export")
async def export_ledger(session_id: str):
    body = "".join(__import__("json").dumps(e, separators=(",", ":")) + "\n" for e in _entries(session_id))
    return PlainTextResponse(body, media_type="application/x-ndjson", headers={
        "Content-Disposition": f'attachment; filename="agentlimit_{session_id}.jsonl"',
        "X-AgentLimit-Public-Key": session_manager.signer.public_key_b64(),
    })


@router.get("/{session_id}/evidence/public-key")
async def public_key(session_id: str):
    _entries(session_id)
    pk = session_manager.signer.public_key_b64()
    return {"public_key_b64": pk, "fingerprint": fingerprint(pk), "algorithm": "Ed25519",
            "note": "Compare the fingerprint with the one published in the project submission/README."}


@router.post("/{session_id}/evidence/tamper-test")
async def tamper_test(session_id: str):
    entries = _entries(session_id)
    pubkey = session_manager.signer.public_key_b64()
    original_ok, original_detail = verify_entries(entries, pubkey)

    tampered = copy.deepcopy(entries)
    target_idx, field, before, after = None, None, None, None
    for i, e in enumerate(tampered):
        p = e.get("payload", {})
        if e["event_type"] == "resource_sample" and isinstance(p.get("cpu_usage_usec"), int):
            target_idx, field, before = i, "payload.cpu_usage_usec", p["cpu_usage_usec"]
            # "make the agent look cheaper" (or, for an idle sample, forge usage)
            p["cpu_usage_usec"] = before // 10 if before >= 10 else before + 1_000_000
            after = p["cpu_usage_usec"]
            break
    if target_idx is None and tampered:
        target_idx, field, before = 0, "event_type", tampered[0]["event_type"]
        tampered[0]["event_type"] = after = "forged"
    tampered_ok, tampered_detail = verify_entries(tampered, pubkey)
    return {
        "original": {"valid": original_ok, "detail": original_detail},
        "tampered_copy": {"entry_index": target_idx, "field": field, "before": before, "after": after,
                          "valid": tampered_ok, "detail": tampered_detail},
        "tamper_detected": original_ok and not tampered_ok,
        "note": "The real ledger is unchanged; only an in-memory copy was modified.",
    }
