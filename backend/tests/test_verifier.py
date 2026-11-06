import json
from pathlib import Path

from backend.evidence.verifier import DeterministicVerifier


def _write_csv(path: Path, header: str, rows: list[str]):
    path.write_text(header + "\n" + "\n".join(rows) + "\n")


def test_level1_integrity_matches(tmp_path):
    f = tmp_path / "report.json"
    f.write_text('{"a": 1}')
    expected = DeterministicVerifier.sha256_file(f)
    ok, _ = DeterministicVerifier.level1_integrity(f, expected)
    assert ok


def test_level1_integrity_detects_change(tmp_path):
    f = tmp_path / "report.json"
    f.write_text('{"a": 1}')
    expected = DeterministicVerifier.sha256_file(f)
    f.write_text('{"a": 2}')
    ok, _ = DeterministicVerifier.level1_integrity(f, expected)
    assert not ok


def test_level2_reconciliation_passes(tmp_path):
    orders = tmp_path / "orders.csv"
    payments = tmp_path / "payments.csv"
    _write_csv(orders, "order_id,amount", ["A,10", "B,20", "C,30", "D,40"])
    _write_csv(payments, "order_id,paid_amount", ["A,10", "B,20", "C,99"])

    # D missing, C mismatched
    ok, detail = DeterministicVerifier.level2_reconciliation(
        orders, payments, {"missing_payments": 1, "amount_mismatches": 1}
    )
    assert ok, detail


def test_level2_reconciliation_fails_on_wrong_numbers(tmp_path):
    orders = tmp_path / "orders.csv"
    payments = tmp_path / "payments.csv"
    _write_csv(orders, "order_id,amount", ["A,10", "B,20"])
    _write_csv(payments, "order_id,paid_amount", ["A,10", "B,20"])

    # Agent lied: claimed 2 missing, but there are 0
    ok, detail = DeterministicVerifier.level2_reconciliation(
        orders, payments, {"missing_payments": 2, "amount_mismatches": 0}
    )
    assert not ok
    parsed = json.loads(detail)
    assert parsed["expected"]["missing_payments"] == 0
    assert parsed["actual"]["missing_payments"] == 2

def test_input_snapshot_hash_mismatch_detected(tmp_path):
    f = tmp_path / "orders.csv"
    f.write_text("order_id,amount\nA,10\n")
    manifest = {"orders.csv": {"sha256": DeterministicVerifier.sha256_file(f)}}
    assert DeterministicVerifier.verify_inputs(manifest, tmp_path)[0]
    f.write_text("order_id,amount\nA,11\n")
    assert not DeterministicVerifier.verify_inputs(manifest, tmp_path)[0]
