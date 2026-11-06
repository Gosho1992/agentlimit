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

    def _ensure(self) -> None:
        # Controllers must be enabled on every ancestor's subtree_control,
        # top-down, before the leaf exposes cpu.max / memory.max / pids.max.
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        chain = [p for p in reversed(parent.parents) if (p / "cgroup.subtree_control").exists()]
        chain.append(parent)
        for ancestor in chain:
            for ctrl in ("cpu", "memory", "pids"):
                try:
                    (ancestor / "cgroup.subtree_control").write_text(f"+{ctrl}\n")
                except (PermissionError, FileNotFoundError, OSError):
                    pass
        self.path.mkdir(exist_ok=True)

    # ---------- attach ----------

    def attach(self, pid: int) -> None:
        (self.path / "cgroup.procs").write_text(str(pid))

    def attach_tree(self, root_pid: int) -> int:
        """Attach root_pid and every descendant. Returns count attached."""
        attached = 0
        for pid in collect_tree(root_pid):
            try:
                self.attach(pid)
                attached += 1
            except (ProcessLookupError, PermissionError):
                continue
        return attached

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