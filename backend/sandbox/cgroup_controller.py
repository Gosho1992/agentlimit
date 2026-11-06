"""
Cgroup v2 controller for a single agent session.

Fixes applied in this version:
  - collect_tree() no longer depends on /proc/<pid>/task/<tid>/children
    (which is missing on many kernels); it walks /proc/*/stat PPIDs.
  - freeze() waits for cgroup.events 'frozen 1', not just cgroup.freeze readback.
  - kill() waits for cgroup.events 'populated 0', confirming the tree is gone.
  - contains(pid) verifies actual cgroup membership (used for fail-closed).
"""

import time
from pathlib import Path


class CgroupController:
    def __init__(self, session_id: str, root: str = "/sys/fs/cgroup/agentlimit"):
        self.session_id = session_id
        self.path = Path(root) / session_id
        self.memory_max_bytes: int = 0
        self.base_cpu_quota_usec: int = 100_000
        self._ensure()

    # ---------- setup ----------

    REQUIRED_FILES = ("cgroup.procs", "cgroup.events", "cgroup.kill", "cpu.max", "cpu.stat",
                      "memory.max", "memory.high", "memory.current", "pids.max")

    def _ensure(self) -> None:
        """Create the session cgroup and FAIL if a required controller or
        control file is unavailable (no silent degradation)."""
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        chain = [p for p in reversed(parent.parents) if (p / "cgroup.subtree_control").exists()]
        chain.append(parent)
        errors = []
        for ancestor in chain:
            for ctrl in ("cpu", "memory", "pids"):
                try:
                    enabled = (ancestor / "cgroup.subtree_control").read_text().split()
                    if ctrl not in enabled:
                        (ancestor / "cgroup.subtree_control").write_text(f"+{ctrl}\n")
                except (PermissionError, FileNotFoundError, OSError) as e:
                    errors.append(f"{ancestor}:{ctrl}:{e}")
        self.path.mkdir(exist_ok=True)
        missing = [f for f in self.REQUIRED_FILES if not (self.path / f).exists()]
        if missing:
            raise CgroupSetupError(f"cgroup {self.path} lacks required control files {missing}; "
                                   f"controller errors: {errors[:3]}")
        self.freeze_supported = (self.path / "cgroup.freeze").exists()

    def capabilities(self) -> dict:
        return {f: (self.path / f).exists() for f in self.REQUIRED_FILES + ("cgroup.freeze", "memory.peak")}

    # ---------- attach ----------

    def attach(self, pid: int) -> None:
        (self.path / "cgroup.procs").write_text(str(pid))

    def attach_tree(self, root_pid: int) -> int:
        """Back-compat helper: returns the number of verified members."""
        return len(self.attach_tree_verified(root_pid)["members"])

    def attach_tree_verified(self, root_pid: int, root_starttime: int | None = None,
                             max_passes: int = 10, stable_passes_required: int = 2,
                             pass_delay_s: float = 0.05) -> dict:
        """
        1. attach the VMM root first (new descendants inherit its cgroup),
        2. repeatedly scan descendants and migrate any outside the cgroup,
        3. distinguish processes that EXITED during the scan (ignored) from
           live processes that could not be migrated (failure),
        4. pass only after `stable_passes_required` consecutive scans in which
           every live descendant was already a member,
        5. guard against PID reuse with /proc/<pid>/stat starttime.
        """
        report = {"root_pid": root_pid, "root_starttime": root_starttime, "members": [],
                  "exited_during_scan": [], "failures": [], "passes": 0, "stable_passes": 0,
                  "passed": False}
        start = proc_starttime(root_pid)
        if start is None:
            report["failures"].append({"pid": root_pid, "reason": "root_not_running"})
            return report
        if root_starttime is not None and start != root_starttime:
            report["failures"].append({"pid": root_pid, "reason": "pid_reused"})
            return report
        report["root_starttime"] = start
        try:
            self.attach(root_pid)
        except Exception as e:
            report["failures"].append({"pid": root_pid, "reason": f"attach_failed:{type(e).__name__}"})
            return report

        members: dict[int, int] = {}
        stable = 0
        for _ in range(max_passes):
            report["passes"] += 1
            moved_any = False
            failures_this_pass = []
            for pid in collect_tree(root_pid):
                st = proc_starttime(pid)
                if st is None:
                    report["exited_during_scan"].append(pid)
                    continue
                if self.contains(pid):
                    members[pid] = st
                    continue
                moved_any = True
                try:
                    self.attach(pid)
                except ProcessLookupError:
                    report["exited_during_scan"].append(pid)
                    continue
                except Exception as e:
                    if proc_starttime(pid) is None:
                        report["exited_during_scan"].append(pid)
                    else:
                        failures_this_pass.append({"pid": pid, "reason": f"attach_failed:{type(e).__name__}"})
                    continue
                if self.contains(pid):
                    members[pid] = st
                elif proc_starttime(pid) is not None:
                    failures_this_pass.append({"pid": pid, "reason": "membership_not_confirmed"})
            if failures_this_pass:
                report["failures"] = failures_this_pass
                stable = 0
            elif not moved_any:
                stable += 1
                if stable >= stable_passes_required:
                    break
            else:
                stable = 0
            time.sleep(pass_delay_s)
        # final verification of every member still alive
        for pid, st in list(members.items()):
            if proc_starttime(pid) != st:
                members.pop(pid)          # exited (or reused) after attach
            elif not self.contains(pid):
                report["failures"].append({"pid": pid, "reason": "left_cgroup"})
        report["members"] = sorted(members)
        report["stable_passes"] = stable
        report["exited_during_scan"] = sorted(set(report["exited_during_scan"]))
        report["passed"] = (not report["failures"] and stable >= stable_passes_required
                            and root_pid in members and proc_starttime(root_pid) == start)
        return report

    def contains(self, pid: int) -> bool:
        """True if this pid is actually in our cgroup right now."""
        try:
            return str(pid) in (self.path / "cgroup.procs").read_text().split()
        except FileNotFoundError:
            return False

    def is_populated(self) -> bool:
        """Fail-closed: if the state cannot be read while the cgroup exists,
        report populated (so a kill is never 'confirmed' on missing evidence).
        A removed cgroup directory implies it was empty (rmdir requires that)."""
        if not self.path.exists():
            return False
        try:
            for line in (self.path / "cgroup.events").read_text().splitlines():
                if line.startswith("populated "):
                    return line.split()[1] == "1"
        except (FileNotFoundError, PermissionError):
            pass
        return True

    # ---------- limits ----------

    def set_cpu_quota(self, quota_usec: int, period_usec: int = 100_000) -> None:
        (self.path / "cpu.max").write_text(f"{quota_usec} {period_usec}\n")

    def set_memory_max(self, bytes_: int) -> None:
        (self.path / "memory.max").write_text(str(bytes_))
        self.memory_max_bytes = bytes_

    def readback(self) -> dict:
        """Read the limits back from the kernel (what is actually enforced)."""
        out = {}
        for f in ("cpu.max", "memory.max", "memory.high", "pids.max"):
            try:
                out[f] = (self.path / f).read_text().strip()
            except FileNotFoundError:
                out[f] = None
        return out

    def cpu_max(self) -> str:
        try:
            return (self.path / "cpu.max").read_text().strip()
        except FileNotFoundError:
            return ""

    def set_memory_high(self, bytes_: int) -> None:
        (self.path / "memory.high").write_text(str(bytes_))

    def set_pids_max(self, n: int) -> None:
        (self.path / "pids.max").write_text(str(n))

    # ---------- observe ----------

    def cpu_usage_usec(self) -> int:
        try:
            for line in (self.path / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec "):
                    return int(line.split()[1])
        except FileNotFoundError:
            pass
        return 0

    def memory_current(self) -> int:
        try:
            return int((self.path / "memory.current").read_text())
        except (FileNotFoundError, ValueError):
            return 0

    def memory_peak(self) -> int:
        try:
            return int((self.path / "memory.peak").read_text())
        except (FileNotFoundError, ValueError):
            return 0

    # ---------- freeze / unfreeze ----------

    def is_frozen(self) -> bool:
        try:
            for line in (self.path / "cgroup.events").read_text().splitlines():
                if line.startswith("frozen "):
                    return line.split()[1] == "1"
        except FileNotFoundError:
            pass
        return False

    def freeze(self, timeout_s: float = 2.0) -> bool:
        """Write 1 to cgroup.freeze, then WAIT for cgroup.events 'frozen 1'."""
        try:
            (self.path / "cgroup.freeze").write_text("1")
        except (FileNotFoundError, PermissionError):
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_frozen():
                return True
            time.sleep(0.05)
        return False

    def unfreeze(self) -> bool:
        try:
            (self.path / "cgroup.freeze").write_text("0")
        except (FileNotFoundError, PermissionError):
            return False
        # Wait for frozen 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not self.is_frozen():
                return True
            time.sleep(0.05)
        return False

    # ---------- kill ----------

    def kill(self, timeout_s: float = 3.0) -> bool:
        """Write 1 to cgroup.kill, then WAIT for cgroup.events 'populated 0'."""
        # Unfreeze first (writing cgroup.kill to a frozen cgroup works on modern
        # kernels, but unfreezing is more portable).
        if not self.is_populated():
            return True  # nothing left to kill
        if self.is_frozen():
            self.unfreeze()
        try:
            (self.path / "cgroup.kill").write_text("1")
        except (FileNotFoundError, PermissionError):
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.is_populated():
                return True
            time.sleep(0.1)
        return False

    # ---------- cleanup ----------

    def destroy(self) -> bool:
        """Remove the (empty) session cgroup. Returns True if it is gone."""
        try:
            self.path.rmdir()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return not self.path.exists()


class CgroupSetupError(RuntimeError):
    """A required cgroup v2 controller or control file is unavailable."""


def proc_starttime(pid: int) -> int | None:
    """Process start time (clock ticks since boot), field 22 of /proc/<pid>/stat.
    None if the process does not exist or is a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    rest = stat[stat.rfind(")") + 2:].split()
    if not rest or rest[0] == "Z":
        return None
    try:
        return int(rest[19])
    except (IndexError, ValueError):
        return None


# ---------- process tree walk ----------

def collect_tree(root_pid: int) -> list[int]:
    """
    Return root_pid and all descendants, walked via /proc/*/stat PPID fields.
    Robust to missing /proc/<pid>/children.
    """
    children: dict[int, list[int]] = {}
    proc = Path("/proc")
    if not proc.exists():
        return [root_pid]

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except (FileNotFoundError, PermissionError):
            continue
        # Format: pid (comm) state ppid ...
        # comm may contain spaces or parens — split on the LAST ')'
        rp = stat.rfind(")")
        if rp == -1:
            continue
        rest = stat[rp + 2:].split()
        if len(rest) < 2:
            continue
        try:
            pid = int(entry.name)
            ppid = int(rest[1])  # rest[0] is state, rest[1] is ppid
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)

    result = [root_pid]
    stack = [root_pid]
    seen = {root_pid}
    while stack:
        p = stack.pop()
        for c in children.get(p, []):
            if c in seen:
                continue
            seen.add(c)
            result.append(c)
            stack.append(c)
    return result