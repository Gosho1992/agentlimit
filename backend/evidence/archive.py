"""
Sessions survive a restart as their signed ledgers. Everything the evidence
report needs is derived from ledger entries, so a judge can open a session
recorded before the server restarted and get the same verified report.
"""

import json
from pathlib import Path

from backend.config import settings


def last(entries: list[dict], event_type: str) -> dict | None:
    for e in reversed(entries):
        if e["event_type"] == event_type:
            return e
    return None


def summarize(entries: list[dict]) -> dict:
    created = (last(entries, "session_created") or {}).get("payload", {})
    closed = (last(entries, "session_closed") or {}).get("payload")
    kill = last(entries, "kill_verified") or last(entries, "kill_failed")
    st = last(entries, "state_transition")
    steps = sum(1 for e in entries if e["event_type"] == "code_generated")
    return {
        "task": created.get("task", ""),
        "budget": created.get("budget"),
        "created_at": entries[0]["timestamp_utc"] if entries else None,
        "final_state": st["payload"]["to"] if st else "NORMAL",
        "termination": kill["payload"] if kill else None,
        "lifecycle": closed,
        "agent_status": (closed or {}).get("agent_status", "interrupted" if last(entries, "session_interrupted") else "unknown"),
        "steps": steps,
        "inference": (closed or {}).get("inference"),
        "interrupted": bool(last(entries, "session_interrupted")),
    }


def ledger_path(session_id: str) -> Path:
    return Path(settings.ledger_dir) / f"{session_id}.jsonl"


def read_entries(session_id: str) -> list[dict] | None:
    if not session_id.isalnum():
        return None
    p = ledger_path(session_id)
    if not p.exists():
        return None
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def list_archived() -> list[str]:
    d = Path(settings.ledger_dir)
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("*.jsonl")), key=lambda s: (d / f"{s}.jsonl").stat().st_mtime, reverse=True)
