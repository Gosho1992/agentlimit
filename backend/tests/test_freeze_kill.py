import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from backend.sandbox.cgroup_controller import CgroupController, collect_tree


def _spawn_sleep(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen(["sleep", str(seconds)])


@pytest.fixture
def session_cgroup(has_cgroup_privileges, tmp_path):
    if not has_cgroup_privileges:
        pytest.skip("no cgroup v2 privileges")
    sid = f"test-{os.getpid()}-{int(time.time())}"
    cg = CgroupController(sid, root="/sys/fs/cgroup/agentlimit_test")
    yield cg
    try:
        cg.kill()
    except Exception:
        pass
    try:
        cg.destroy()
    except Exception:
        pass


def test_cgroup_kill_terminates_process(session_cgroup):
    proc = _spawn_sleep(60)
    try:
        session_cgroup.attach(proc.pid)
        time.sleep(0.1)
        assert proc.poll() is None
        assert session_cgroup.kill()
        time.sleep(0.3)
        # The process should be gone (poll() not None)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cgroup_kill_survives_sigterm_handler(session_cgroup):
    """The workload ignores SIGTERM. cgroup.kill uses SIGKILL and must succeed."""
    proc = subprocess.Popen([
        "python3", "-c",
        "import signal, time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(60)"
    ])
    try:
        session_cgroup.attach(proc.pid)
        time.sleep(0.2)
        # Confirm SIGTERM is ignored
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        assert proc.poll() is None, "process should have ignored SIGTERM"
        # Now cgroup.kill
        assert session_cgroup.kill()
        time.sleep(0.3)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cgroup_freeze_and_unfreeze(session_cgroup):
    proc = _spawn_sleep(60)
    try:
        session_cgroup.attach(proc.pid)
        time.sleep(0.1)
        frozen = session_cgroup.freeze()
        if not frozen:
            pytest.skip("cgroup.freeze not supported on this kernel")
        # While frozen, process is alive but not scheduled
        assert proc.poll() is None
        assert session_cgroup.unfreeze()
        time.sleep(0.1)
        assert proc.poll() is None
    finally:
        session_cgroup.kill()
        if proc.poll() is None:
            proc.kill()


def test_collect_tree_includes_children():
    parent = subprocess.Popen(["bash", "-c", "sleep 30 & sleep 30 & wait"])
    try:
        time.sleep(0.3)
        tree = collect_tree(parent.pid)
        assert parent.pid in tree
        assert len(tree) >= 2
    finally:
        parent.kill()