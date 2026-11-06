import os
import sys
from pathlib import Path

import pytest

# Make `backend` importable when running pytest from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture
def tmp_ledger_dir(tmp_path):
    return str(tmp_path / "ledger")


@pytest.fixture
def tmp_signer_key(tmp_path):
    return str(tmp_path / "signer.ed25519")


@pytest.fixture
def has_cgroup_privileges():
    """True only on a real cgroup v2 host where we can create child cgroups (root)."""
    root = Path("/sys/fs/cgroup")
    if not (root / "cgroup.controllers").exists():
        return False
    try:
        probe = root / "agentlimit_probe"
        probe.mkdir(exist_ok=True)
        ok = (probe / "cgroup.events").exists()
        probe.rmdir()
        return ok
    except Exception:
        return False
