import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.evidence.signer import Signer


# A ledger is sealed once one of these is written; nothing may follow it.
TERMINAL_EVENT_TYPES = ("session_closed", "session_start_failed", "session_interrupted")


class LedgerSealedError(RuntimeError):
    """Raised on any append after the ledger's terminal event."""


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
        self._prev_hash, self._last_type = self._load_tail()
        # the out-of-loop watchdog thread may append concurrently with the event loop
        self._lock = threading.Lock()

    def _load_tail(self) -> tuple[str, str | None]:
        if not self.path.exists():
            return "0" * 64, None
        last = ""
        for line in self.path.read_text().splitlines():
            if line.strip():
                last = line
        if not last:
            return "0" * 64, None
        try:
            e = json.loads(last)
            return e.get("entry_hash", "0" * 64), e.get("event_type")
        except Exception:
            return "0" * 64, None

    @property
    def sealed(self) -> bool:
        return self._last_type in TERMINAL_EVENT_TYPES

    def append(self, event_type: str, payload: dict[str, Any]) -> dict:
        with self._lock:
            return self._append_locked(event_type, payload)

    def _append_locked(self, event_type: str, payload: dict[str, Any]) -> dict:
        if self.sealed:
            raise LedgerSealedError(f"ledger {self.session_id} is sealed by {self._last_type}; "
                                    f"refusing to append {event_type!r}")
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
        self._last_type = event_type
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


# Integrity proves entries were not altered; it cannot prove none are missing at
# the END (a prefix of a valid chain is still a valid chain). Completeness closes
# that gap: a finished session must end with its signed terminal event, which
# only the signing key can produce. Anything else is reported, never VALID.
TERMINAL_EVENTS = ("session_closed", "session_start_failed")   # COMPLETE; session_interrupted = INTERRUPTED


def ledger_completeness(entries: list[dict], live: bool = False) -> tuple[str, str]:
    """-> (verdict, detail). COMPLETE / IN_PROGRESS / INTERRUPTED / INCOMPLETE."""
    if not entries:
        return "INCOMPLETE", "ledger is empty"
    if entries[0].get("event_type") != "session_created":
        return "INCOMPLETE", f"first entry is {entries[0].get('event_type')!r}, not session_created"
    last_type = entries[-1].get("event_type")
    if last_type in TERMINAL_EVENTS:
        return "COMPLETE", f"ends with signed {last_type} ({len(entries)} entries)"
    if live:
        return "IN_PROGRESS", "session still running"
    if last_type == "session_interrupted":
        return "INTERRUPTED", "server stopped before the session closed (recorded on restart)"
    return "INCOMPLETE", f"no signed terminal event: ledger ends at {last_type!r} (truncated or cut off)"
