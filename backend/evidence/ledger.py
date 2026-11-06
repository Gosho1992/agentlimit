import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.evidence.signer import Signer


class EvidenceLedger:
    """
    Append-only, hash-chained, signed event log for one session.
    The agent has no filesystem access to this path.
    """

    def __init__(self, session_id: str, signer: Signer, base_dir: str):
        self.session_id = session_id
        self.signer = signer
        self.path = Path(base_dir) / f"{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev_hash = self._load_prev_hash()

    def _load_prev_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        for line in self.path.read_text().splitlines():
            if line.strip():
                last = line
        if not last:
            return "0" * 64
        try:
            return json.loads(last).get("entry_hash", "0" * 64)
        except Exception:
            return "0" * 64

    def append(self, event_type: str, payload: dict[str, Any]) -> dict:
        event = {
            "event_type": event_type,
            "session_id": self.session_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "prev_hash": self._prev_hash,
            "payload": payload,
        }
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        event["entry_hash"] = hashlib.sha256(canonical).hexdigest()
        event["signature"] = self.signer.sign(canonical)
        with open(self.path, "a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
            f.flush()
        self._prev_hash = event["entry_hash"]
        return event

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def verify_chain(self, public_key_b64: str) -> tuple[bool, str]:
        return verify_entries(self.read_all(), public_key_b64)


CANONICAL_FIELDS = ("event_type", "session_id", "timestamp_utc", "prev_hash", "payload")


def verify_entries(entries: list[dict], public_key_b64: str) -> tuple[bool, str]:
    """Pure function so the offline CLI verifier and the live tamper test use
    exactly the same check as the server."""
    prev = "0" * 64
    for i, ev in enumerate(entries):
        if ev.get("prev_hash") != prev:
            return False, f"broken link at entry {i} ({ev.get('event_type')})"
        try:
            canonical = json.dumps({k: ev[k] for k in CANONICAL_FIELDS},
                                   sort_keys=True, separators=(",", ":")).encode()
        except KeyError as e:
            return False, f"missing field {e} at entry {i}"
        if hashlib.sha256(canonical).hexdigest() != ev.get("entry_hash"):
            return False, f"hash mismatch at entry {i} ({ev.get('event_type')})"
        if not Signer.verify(public_key_b64, canonical, ev.get("signature", "")):
            return False, f"signature mismatch at entry {i} ({ev.get('event_type')})"
        prev = ev["entry_hash"]
    return True, f"ok ({len(entries)} entries)"
