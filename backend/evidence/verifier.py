import hashlib
import hmac
import json
from pathlib import Path


class DeterministicVerifier:
    @staticmethod
    def sha256_file(path: str | Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def sha256_string(s: str) -> str:
        return hashlib.sha256(s.encode()).hexdigest()

    @staticmethod
    def sha256_bytes(b: bytes) -> str:
        return hashlib.sha256(b).hexdigest()

    @staticmethod
    def level1_integrity(artifact_path: str | Path, expected_sha256: str) -> tuple[bool, str]:
        actual = DeterministicVerifier.sha256_file(artifact_path)
        return actual == expected_sha256, json.dumps({"expected": expected_sha256, "actual": actual})

    @staticmethod
    def verify_inputs(manifest: dict, inputs_dir: str | Path) -> tuple[bool, dict]:
        """Re-hash the session's input snapshot against the hashes recorded in
        the signed ledger at session start."""
        detail = {}
        ok = True
        for name, meta in manifest.items():
            p = Path(inputs_dir) / name
            actual = DeterministicVerifier.sha256_file(p) if p.exists() else None
            match = actual == meta.get("sha256")
            ok = ok and match
            detail[name] = {"recorded": meta.get("sha256"), "actual": actual, "match": match}
        return ok, detail

    @staticmethod
    def level2_reconciliation(orders_csv, payments_csv, agent_report: dict) -> tuple[bool, str]:
        import csv

        with open(orders_csv) as f:
            orders = {r["order_id"]: float(r["amount"]) for r in csv.DictReader(f)}
        with open(payments_csv) as f:
            payments = {r["order_id"]: float(r["paid_amount"]) for r in csv.DictReader(f)}

        missing = sorted(set(orders) - set(payments))
        mismatches = [
            oid for oid in set(orders) & set(payments)
            if not hmac.compare_digest(f"{orders[oid]:.2f}", f"{payments[oid]:.2f}")
        ]

        expected = {"missing_payments": len(missing), "amount_mismatches": len(mismatches)}
        actual = {k: agent_report.get(k) for k in expected}

        ok = expected == actual
        return ok, json.dumps({"expected": expected, "actual": actual})

    @staticmethod
    def parse_agent_json(stdout: str) -> dict | None:
        """Extract the first JSON object from agent stdout."""
        stdout = stdout.strip()
        # Fast path: whole thing is a JSON object (a bare number/list/string is not a report)
        try:
            v = json.loads(stdout)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        # Fallback: find first {...}
        lo = stdout.find("{")
        hi = stdout.rfind("}")
        if lo != -1 and hi > lo:
            try:
                v = json.loads(stdout[lo:hi + 1])
                return v if isinstance(v, dict) else None
            except Exception:
                return None
        return None