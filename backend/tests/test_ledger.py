from backend.evidence.ledger import EvidenceLedger
from backend.evidence.signer import Signer


def test_ledger_chain_is_valid_after_appends(tmp_ledger_dir, tmp_signer_key):
    signer = Signer(tmp_signer_key)
    ledger = EvidenceLedger("sess-test", signer, tmp_ledger_dir)

    ledger.append("start", {"budget": 30})
    ledger.append("sample", {"cpu_usec": 1000})
    ledger.append("sample", {"cpu_usec": 2000})

    ok, detail = ledger.verify_chain(signer.public_key_b64())
    assert ok, detail
    entries = ledger.read_all()
    assert len(entries) == 3
    assert entries[0]["prev_hash"] == "0" * 64
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]


def test_ledger_tampering_is_detected(tmp_ledger_dir, tmp_signer_key):
    signer = Signer(tmp_signer_key)
    ledger = EvidenceLedger("sess-tamper", signer, tmp_ledger_dir)
    ledger.append("start", {"budget": 30})
    ledger.append("sample", {"cpu_usec": 1000})

    # Rewrite a payload byte in place
    path = ledger.path
    content = path.read_text()
    tampered = content.replace('"cpu_usec":1000', '"cpu_usec":9999')
    path.write_text(tampered)

    ok, detail = ledger.verify_chain(signer.public_key_b64())
    assert not ok
    assert "hash mismatch" in detail or "signature" in detail


def test_ledger_survives_reload(tmp_ledger_dir, tmp_signer_key):
    signer = Signer(tmp_signer_key)
    l1 = EvidenceLedger("sess-reload", signer, tmp_ledger_dir)
    l1.append("start", {})
    l1.append("sample", {"cpu_usec": 100})

    # Simulate a fresh process restart
    l2 = EvidenceLedger("sess-reload", signer, tmp_ledger_dir)
    l2.append("sample", {"cpu_usec": 200})

    ok, detail = l2.verify_chain(signer.public_key_b64())
    assert ok, detail
    assert len(l2.read_all()) == 3