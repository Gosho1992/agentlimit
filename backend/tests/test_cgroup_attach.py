"""attach_tree_verified() against REAL processes; only cgroup membership is
emulated (in-memory, with fork inheritance), so this runs without root."""
import os
import subprocess
import sys
import time

import pytest

from backend.sandbox.cgroup_controller import CgroupController, CgroupSetupError, proc_starttime

TICK = os.sysconf("SC_CLK_TCK")


class MemCgroup(CgroupController):
    def __init__(self, blocked=()):
        self.members: dict[int, float] = {}
        self.blocked = set(blocked)

    def attach(self, pid):
        if pid in self.blocked:
            raise PermissionError("cannot migrate")
        if proc_starttime(pid) is None:
            raise ProcessLookupError
        self.members[pid] = time.monotonic()

    def _ppid(self, pid):
        try:
            st = open(f"/proc/{pid}/stat").read()
            return int(st[st.rfind(")") + 2:].split()[1])
        except Exception:
            return None

    def contains(self, pid):
        if pid in self.members:
            return True
        # inheritance: a process forked after an ancestor joined is a member
        st = proc_starttime(pid)
        p = self._ppid(pid)
        while p and p > 1:
            if p in self.members and st is not None and st / TICK >= self.members[p] - 0.02:
                return True
            p = self._ppid(p)
        return False


def spawn(code):
    return subprocess.Popen([sys.executable, "-c", code])


def test_parent_and_existing_children_attached():
    parent = spawn("import subprocess,sys,time\n"
                   "c=[subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']) for _ in range(2)]\n"
                   "time.sleep(30)")
    try:
        time.sleep(0.5)
        cg = MemCgroup()
        r = cg.attach_tree_verified(parent.pid, proc_starttime(parent.pid))
        assert r["passed"], r
        assert parent.pid in r["members"] and len(r["members"]) >= 3   # existing children moved
    finally:
        parent.kill()


def test_grandchild_created_during_attachment_is_captured():
    parent = spawn("import subprocess,sys,time\ntime.sleep(0.15)\n"
                   "subprocess.Popen([sys.executable,'-c','import subprocess,sys,time;"
                   "subprocess.Popen([sys.executable,\"-c\",\"import time; time.sleep(30)\"]);time.sleep(30)'])\n"
                   "time.sleep(30)")
    try:
        cg = MemCgroup()
        r = cg.attach_tree_verified(parent.pid, proc_starttime(parent.pid), max_passes=40, pass_delay_s=0.05)
        time.sleep(0.6)
        r = cg.attach_tree_verified(parent.pid, proc_starttime(parent.pid))
        assert r["passed"], r
        assert len(r["members"]) >= 3
    finally:
        parent.kill()


def test_live_unmovable_descendant_fails():
    parent = spawn("import subprocess,sys,time\n"
                   "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
                   "print(c.pid, flush=True)\ntime.sleep(30)")
    try:
        time.sleep(0.5)
        children = [int(p) for p in os.listdir("/proc") if p.isdigit()
                    and MemCgroup()._ppid(int(p)) == parent.pid]
        cg = MemCgroup(blocked=children)
        r = cg.attach_tree_verified(parent.pid, proc_starttime(parent.pid))
        assert not r["passed"] and r["failures"]
    finally:
        parent.kill()


def test_process_exiting_during_scan_is_not_a_failure():
    parent = spawn("import subprocess,sys,time\n"
                   "c=subprocess.Popen([sys.executable,'-c','pass'])\ntime.sleep(30)")
    try:
        time.sleep(0.5)
        r = MemCgroup().attach_tree_verified(parent.pid, proc_starttime(parent.pid))
        assert r["passed"] and not r["failures"], r
    finally:
        parent.kill()


def test_pid_reuse_detected():
    p = spawn("import time; time.sleep(30)")
    try:
        time.sleep(0.2)
        r = MemCgroup().attach_tree_verified(p.pid, root_starttime=proc_starttime(p.pid) - 1)
        assert not r["passed"] and r["failures"][0]["reason"] == "pid_reused"
    finally:
        p.kill()


def test_missing_controller_files_fail_session_setup(tmp_path):
    with pytest.raises(CgroupSetupError):
        CgroupController("s1", root=str(tmp_path / "agentlimit"))
