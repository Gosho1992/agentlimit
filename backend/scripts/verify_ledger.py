#!/usr/bin/env python3
"""
Offline, standalone AgentLimit ledger verifier.

Needs only Python 3 + PyNaCl. Does not import or trust the AgentLimit server.

  pip install pynacl
  curl -o ledger.jsonl  https://<host>/sessions/<id>/evidence/export
  curl -s https://<host>/sessions/<id>/evidence/public-key   # -> public_key_b64
  python verify_ledger.py ledger.jsonl <public_key_b64>

Checks every entry: prev_hash link, SHA-256 of the canonical entry, Ed25519
signature, then COMPLETENESS: a finished session must end with its signed
session_closed (or session_start_failed) entry, so a ledger whose tail was cut
off is never reported as complete.
Exit code 0 = valid and complete, 1 = invalid, 3 = valid but incomplete. Then prints the governance story
(state transitions, recommendations, kill verdict) straight from the signed data.
"""
import base64
import hashlib
import json
import sys

from nacl.signing import VerifyKey

FIELDS = ("event_type", "session_id", "timestamp_utc", "prev_hash", "payload")


def main(path: str, pubkey_b64: str) -> int:
    vk = VerifyKey(base64.b64decode(pubkey_b64))
    entries = [json.loads(l) for l in open(path) if l.strip()]
    prev = "0" * 64
    for i, e in enumerate(entries):
        canonical = json.dumps({k: e[k] for k in FIELDS}, sort_keys=True, separators=(",", ":")).encode()
        if e["prev_hash"] != prev:
            print(f"INVALID: broken hash link at entry {i} ({e['event_type']})"); return 1
        if hashlib.sha256(canonical).hexdigest() != e["entry_hash"]:
            print(f"INVALID: content hash mismatch at entry {i} ({e['event_type']})"); return 1
        try:
            vk.verify(canonical, base64.b64decode(e["signature"]))
        except Exception:
            print(f"INVALID: signature mismatch at entry {i} ({e['event_type']})"); return 1
        prev = e["entry_hash"]
    h = hashlib.sha256(base64.b64decode(pubkey_b64)).hexdigest()[:32]
    fp = ":".join(h[i:i + 4] for i in range(0, 32, 4))
    print(f"VALID: {len(entries)} entries, chain intact, all Ed25519 signatures verify")
    last = entries[-1]["event_type"] if entries else None
    complete = bool(entries) and entries[0]["event_type"] == "session_created" and \
        last in ("session_closed", "session_start_failed")
    print("COMPLETE: ends with signed " + str(last) if complete else
          f"INCOMPLETE: no signed terminal event (ledger ends at {last!r}); entries may have been cut off")
    print(f"Key fingerprint: {fp}  <- must match the fingerprint published in the README/submission\n")
    for e in entries:
        t, p = e["event_type"], e["payload"]
        if t == "state_transition":
            print(f"  {e['timestamp_utc'][11:19]}  {p['from']:>9} -> {p['to']:<9} cause={p.get('cause')} util={p.get('utilisation_pct')}")
        elif t == "recommendation_issued":
            print(f"  {e['timestamp_utc'][11:19]}  RECOMMEND {p['rule_id']}: {p['suggestion'][:80]}")
        elif t in ("kill_verified", "kill_failed"):
            print(f"  {e['timestamp_utc'][11:19]}  {p.get('verdict')} checks={p.get('checks')}")
        elif t == "containment_probe":
            print(f"  {e['timestamp_utc'][11:19]}  containment probe passed={p.get('passed')} egress_blocked={p.get('egress_blocked')}")
        elif t == "session_closed":
            print(f"  {e['timestamp_utc'][11:19]}  session closed {p}")
        elif t == "watchdog_kill":
            print(f"  {e['timestamp_utc'][11:19]}  WATCHDOG KILL heartbeat_age={p.get('heartbeat_age_s')}s")
    return 0 if complete else 3


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__); sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
