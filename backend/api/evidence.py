"""
Evidence endpoints.

  GET  /sessions/{id}/evidence          full report
  GET  /sessions/{id}/evidence/verify   the verification block only
  GET  /sessions/{id}/evidence/export   signed ledger (JSONL) for offline verification
  GET  /sessions/{id}/evidence/public-key
  POST /sessions/{id}/evidence/tamper-test   (modifies an in-memory COPY only)

Every verdict is RE-DERIVED from the signed ledger and files on disk; no
check turns green because an event merely exists. Each area gets its own
verdict: VALID / FAILED / PARTIAL / NOT_APPLICABLE / PENDING.
"""

import copy
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.config import settings
from backend.evidence.archive import read_entries, summarize
from backend.evidence.ledger import verify_entries
from backend.evidence.signer import fingerprint
from backend.evidence.verifier import DeterministicVerifier
from backend.governance.containment import evaluate_containment
from backend.services.session_manager import session_manager

router = APIRouter(prefix="/sessions", tags=["evidence"])

KILL_CHECKS = ("session_marked_terminated", "inference_revoked", "agent_task_finished",
               "cgroup_populated_0", "sandbox_pid_gone", "sandbox_removed_via_runtime")
CLEANUP_REQUIRED = ("agent_terminated", "inference_revoked", "sandbox_removed", "workload_gone", "cgroup_removed")


def _entries(session_id: str) -> list[dict]:
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


def _session_dir(session_id: str) -> Path:
    return (Path(settings.ledger_dir) / session_id).resolve()


def _safe_path(session_id: str, relative: str) -> Path | None:
    """Resolve a ledger-recorded relative path; refuse anything outside the
    session's evidence directory."""
    base = _session_dir(session_id)
    try:
        p = (base / relative).resolve()
    except Exception:
        return None
    return p if p == base or base in p.parents else None


# ---------------------------------------------------------------- inputs / artifact

def _verify_inputs(session_id: str, entries: list[dict]) -> dict:
    ev = _last(entries, "inputs_snapshotted")
    if not ev:
        return {"verdict": "FAILED", "detail": "no input manifest in ledger", "files": {}}
    files = {}
    ok = True
    for name, meta in ev["payload"].get("files", {}).items():
        p = _safe_path(session_id, meta.get("relative_path", ""))
        if p is None:
            files[name] = {"match": False, "error": "path outside session evidence directory"}
            ok = False
            continue
        actual = DeterministicVerifier.sha256_file(p) if p.exists() else None
        match = actual == meta.get("sha256") and (p.stat().st_size if p.exists() else -1) == meta.get("bytes")
        files[name] = {"recorded": meta.get("sha256"), "actual": actual, "bytes": meta.get("bytes"), "match": match}
        ok = ok and match
    return {"verdict": "VALID" if ok else "FAILED", "files": files,
            "snapshot_time_utc": ev["payload"].get("snapshot_time_utc")}


def _verify_artifact(session_id: str, entries: list[dict], inputs: dict) -> dict:
    done = _last(entries, "agent_completed")
    if not done:
        return {"integrity": "NOT_APPLICABLE", "correctness": "NOT_APPLICABLE", "level": 0,
                "verdict": "NO_ARTIFACT", "detail": "agent produced no successful output"}
    rel = done["payload"].get("artifact_relative_path") or ""
    p = _safe_path(session_id, rel) if rel else None
    if p is None or not p.exists():
        return {"integrity": "FAILED", "correctness": "NOT_APPLICABLE", "level": 0, "verdict": "FAILED",
                "detail": "artifact missing or outside the session evidence directory"}
    data = p.read_bytes()
    actual = DeterministicVerifier.sha256_bytes(data)
    recorded = done["payload"].get("stdout_sha256")
    integrity = {"recorded_in_ledger": recorded, "actual": actual, "match": actual == recorded}
    out = {"integrity_detail": integrity, "integrity": "VALID" if integrity["match"] else "FAILED"}
    report = DeterministicVerifier.parse_agent_json(data.decode("utf-8", "ignore"))
    out["agent_report"] = report
    names = set(inputs.get("files", {}))
    if report is None or not {"orders.csv", "payments.csv"} <= names:
        return {**out, "correctness": "NOT_APPLICABLE", "level": 1,
                "verdict": "INTEGRITY_ONLY" if integrity["match"] else "FAILED",
                "detail": "no Level-2 check applies (output not JSON or no reconciliation inputs)"}
    if inputs["verdict"] != "VALID":
        return {**out, "correctness": "FAILED", "level": 1, "verdict": "FAILED",
                "detail": "input snapshot does not match the signed manifest; cannot recompute"}
    base = _session_dir(session_id) / "inputs"
    correct, detail = DeterministicVerifier.level2_reconciliation(base / "orders.csv", base / "payments.csv", report)
    out["correctness_detail"] = json.loads(detail)
    ok = integrity["match"] and correct
    return {**out, "correctness": "VALID" if correct else "FAILED", "level": 2,
            "correctness_legacy": out["correctness_detail"],
            "verdict": "VERIFIED" if ok else "FAILED",
            "detail": "agent's claim matches independent recomputation" if ok
            else "agent's claim does NOT match independent recomputation"}


# ---------------------------------------------------------------- containment / limits / lifecycle

def _verify_containment(entries: list[dict]) -> dict:
    ev = _last(entries, "containment_probe")
    if not ev:
        return {"verdict": "FAILED", "passed": False, "checks": {}, "failed_checks": ["no_probe_recorded"]}
    raw = ev["payload"].get("raw")
    expected = sorted((_last(entries, "inputs_snapshotted") or {}).get("payload", {}).get("files", {}))
    re_eval = evaluate_containment(raw, expected_files=expected)       # independent re-evaluation
    recorded_passed = ev["payload"].get("passed")
    consistent = recorded_passed == re_eval["passed"]
    attach = _last(entries, "sandbox_attached")
    rt_ev = (_last(entries, "runtime_started") or {}).get("payload", {})
    isolation = bool(attach) and attach["payload"].get("passed") is True and rt_ev.get("runtime_type") == "microsandbox"
    checks = {"process_isolation_microvm_verified_in_cgroup": isolation, **re_eval["checks"]}
    failed = [k for k, v in checks.items() if not v]
    if not consistent:
        failed.append("recorded_verdict_disagrees_with_reevaluation")
    return {"verdict": "VALID" if not failed else "FAILED", "passed": not failed, "checks": checks,
            "failed_checks": failed, "runtime_type": rt_ev.get("runtime_type"),
            "attachment": attach["payload"] if attach else None}


def _verify_limits(entries: list[dict]) -> dict:
    cg = (_last(entries, "cgroup_configured") or {}).get("payload")
    created = (_last(entries, "session_created") or {}).get("payload", {})
    plan = created.get("memory_plan") or {}
    if not cg:
        return {"verdict": "FAILED", "detail": "no cgroup configuration recorded"}
    rb, exp = cg.get("readback", {}), cg.get("expected", {})
    checks = {
        "cpu.max_readback_matches": rb.get("cpu.max") == exp.get("cpu.max"),
        "memory.max_equals_memory_budget": rb.get("memory.max") == exp.get("memory.max")
                                           == str(plan.get("host_memory_max_bytes")),
        "pids.max_readback_matches": rb.get("pids.max") == exp.get("pids.max"),
        "required_control_files_present": all((cg.get("capabilities") or {}).get(f) for f in
                                              ("cgroup.kill", "cpu.stat", "memory.current", "cgroup.events")),
    }
    return {"verdict": "VALID" if all(checks.values()) else "FAILED", "checks": checks,
            "readback": rb, "memory_plan": plan,
            "semantics": {"cpu.max": "CPU bandwidth limit (throttling)", "cpu.stat": "cumulative CPU vs budget",
                          "memory.high": "soft reclaim pressure", "memory.max": "hard ceiling (OOM)",
                          "pids.max": "host VMM tasks only; guest processes limited by guest rlimits"}}


def _verify_cleanup(entries: list[dict]) -> dict:
    closed = (_last(entries, "session_closed") or {}).get("payload")
    if not closed:
        state = "FAILED" if _last(entries, "session_interrupted") else "PENDING"
        return {"verdict": state, "detail": "session interrupted by restart" if state == "FAILED" else "session open"}
    checks = {k: closed.get(k) is True for k in CLEANUP_REQUIRED}
    return {"verdict": "VALID" if all(checks.values()) else "FAILED", "checks": checks,
            "sandbox_stop_error": closed.get("sandbox_stop_error")}


def _verify_termination(entries: list[dict]) -> dict:
    kill = _last(entries, "kill_verified") or _last(entries, "kill_failed")
    if not kill:
        return {"verdict": "NOT_APPLICABLE", "detail": "session was not killed"}
    checks = {k: (kill["payload"].get("checks") or {}).get(k) is True for k in KILL_CHECKS}
    ok = all(checks.values())
    recorded = kill["payload"].get("verdict")
    return {"verdict": "VALID" if ok and recorded == "KILL_CONFIRMED" else "FAILED",
            "recorded_verdict": recorded, "checks": checks, "reason": kill["payload"].get("reason")}


def _verify_inference(entries: list[dict]) -> dict:
    closed = (_last(entries, "session_closed") or {}).get("payload")
    calls = [e["payload"] for e in entries if e["event_type"] == "inference_call"]
    downgraded = [c for c in calls if c.get("effective_model") != c.get("requested_model")]
    if not closed:
        return {"verdict": "PENDING", "calls": len(calls)}
    acct = closed.get("inference_accounting")
    return {
        "verdict": "VALID" if closed.get("inference_revoked") is True and acct == "COMPLETE"
        else "PARTIAL" if closed.get("inference_revoked") is True else "FAILED",
        "new_calls_blocked_after_close": closed.get("inference_revoked") is True,
        "accounting": acct,
        "uncertain_reserved_tokens": closed.get("inference_uncertain_reserved_tokens"),
        "uncertain_reserved_cost_usd": closed.get("inference_uncertain_reserved_cost_usd"),
        "settled_after_revoke": closed.get("inference_settled_after_revoke"),
        "calls": len(calls),
        "downgraded_calls": [{"request_id": c.get("request_id"), "from": c.get("requested_model"),
                              "to": c.get("effective_model")} for c in downgraded],
    }


def _report(session_id: str) -> dict:
    entries = _entries(session_id)
    rt = session_manager.get(session_id)
    pubkey = session_manager.signer.public_key_b64()
    chain_ok, chain_detail = verify_entries(entries, pubkey)
    summary = summarize(entries)
    inputs = _verify_inputs(session_id, entries)
    artifact = _verify_artifact(session_id, entries, inputs)
    containment = _verify_containment(entries)
    limits = _verify_limits(entries)
    cleanup = _verify_cleanup(entries)
    termination = _verify_termination(entries)
    inference = _verify_inference(entries)
    verification = {
        "ledger_integrity": "VALID" if chain_ok else "FAILED",
        "input_integrity": inputs["verdict"],
        "artifact_integrity": artifact["integrity"],
        "artifact_correctness": artifact["correctness"],
        "containment": containment["verdict"],
        "resource_limits": limits["verdict"],
        "termination": termination["verdict"],
        "execution_cleanup": cleanup["verdict"],
        "inference_accounting": inference["verdict"],
    }
    started = not _last(entries, "session_start_failed")
    return {
        "session_id": session_id,
        "task": summary["task"],
        "verification": verification,
        "session_started": started,
        "start_failure": (_last(entries, "session_start_failed") or {}).get("payload"),
        "chain_valid": chain_ok,
        "chain_detail": chain_detail,
        "inputs": inputs,
        "artifact_verification": artifact,
        "containment": containment,
        "resource_limits": limits,
        "termination": termination,
        "cleanup": cleanup,
        "inference_verification": inference,
        "lifecycle": summary["lifecycle"],
        "public_key_b64": pubkey,
        "public_key_fingerprint": fingerprint(pubkey),
        "final_state": summary["final_state"],
        "agent_status": rt.orchestrator.status if rt else summary["agent_status"],
        "steps_completed": summary["steps"],
        "inference": rt.orchestrator.inference.snapshot() if rt else (summary["inference"] or {}),
        "recommendations": [e["payload"] for e in entries if e["event_type"] == "recommendation_issued"],
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
    return {k: r[k] for k in ("verification", "chain_detail", "containment", "resource_limits",
                              "termination", "cleanup", "inference_verification", "artifact_verification")}


@router.get("/{session_id}/evidence/export")
async def export_ledger(session_id: str):
    body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in _entries(session_id))
    pk = session_manager.signer.public_key_b64()
    return PlainTextResponse(body, media_type="application/x-ndjson", headers={
        "Content-Disposition": f'attachment; filename="agentlimit_{session_id}.jsonl"',
        "X-AgentLimit-Public-Key": pk, "X-AgentLimit-Key-Fingerprint": fingerprint(pk)})


@router.get("/{session_id}/evidence/public-key")
async def public_key(session_id: str):
    _entries(session_id)
    pk = session_manager.signer.public_key_b64()
    return {"public_key_b64": pk, "fingerprint": fingerprint(pk), "algorithm": "Ed25519",
            "note": "Compare the fingerprint with the one published in the README/submission before trusting it."}


@router.post("/{session_id}/evidence/tamper-test")
async def tamper_test(session_id: str):
    entries = _entries(session_id)
    pubkey = session_manager.signer.public_key_b64()
    original_ok, original_detail = verify_entries(entries, pubkey)
    tampered = copy.deepcopy(entries)
    target_idx = field = before = after = None
    for i, e in enumerate(tampered):
        p = e.get("payload", {})
        if e["event_type"] == "resource_sample" and isinstance(p.get("cpu_usage_usec"), int):
            target_idx, field, before = i, "payload.cpu_usage_usec", p["cpu_usage_usec"]
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
