import pytest

from backend.evidence.ledger import EvidenceLedger, verify_entries
from backend.evidence.signer import Signer


def test_tampered_copy_rejected_and_original_valid(tmp_path):
    s = Signer(str(tmp_path / "k"))
    led = EvidenceLedger("x", s, str(tmp_path))
    led.append("resource_sample", {"cpu_usage_usec": 1_000_000})
    led.append("state_transition", {"from": "NORMAL", "to": "ALERT"})
    entries = led.read_all()
    assert verify_entries(entries, s.public_key_b64())[0]
    entries[0]["payload"]["cpu_usage_usec"] = 100
    ok, detail = verify_entries(entries, s.public_key_b64())
    assert not ok and "entry 0" in detail


def test_wrong_public_key_rejected(tmp_path):
    s, other = Signer(str(tmp_path / "k")), Signer(str(tmp_path / "k2"))
    led = EvidenceLedger("x", s, str(tmp_path))
    led.append("a", {})
    assert not verify_entries(led.read_all(), other.public_key_b64())[0]


def test_offline_cli_verifier(tmp_path, capsys):
    from backend.scripts.verify_ledger import main
    s = Signer(str(tmp_path / "k"))
    led = EvidenceLedger("x", s, str(tmp_path))
    led.append("session_created", {"task": "t"})
    led.append("state_transition", {"from": "NORMAL", "to": "ALERT", "cause": "metrics"})
    led.append("session_closed", {"clean": True})
    assert main(str(led.path), s.public_key_b64()) == 0
    txt = led.path.read_text().replace('"ALERT"', '"NORMAL"', 1)
    led.path.write_text(txt)
    assert main(str(led.path), s.public_key_b64()) == 1
