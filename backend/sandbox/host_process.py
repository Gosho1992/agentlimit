"""
Locate the host-side process that runs a sandbox's microVM.

Microsandbox (libkrun/KVM) runs each microVM inside a host `msb` process (the
VMM). Guest processes are NOT host processes; the governor controls the VMM
process tree, which is what host-side cgroups can account and kill.

Matching is deliberately strict: the sandbox name must appear in the
process command line AND the process must look like a Microsandbox/libkrun
VMM. If more than one process matches we take the top-most one (whose
parent is not itself a match) so attach_tree() captures the whole subtree.
"""

from pathlib import Path

_VMM_MARKERS = ("msb", "libkrun", "microsandbox", "krun")


def _cmdline(pid_dir: Path) -> str:
    return (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore").strip()


def _ppid(pid_dir: Path) -> int:
    stat = (pid_dir / "stat").read_text()
    return int(stat[stat.rfind(")") + 2:].split()[1])


def find_candidates(sandbox_name: str) -> list[dict]:
    out = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = _cmdline(proc)
            if not cmd or sandbox_name not in cmd:
                continue
            exe = cmd.split(" ", 1)[0]
            if not any(m in exe.lower() or m in cmd.lower() for m in _VMM_MARKERS):
                continue
            out.append({"pid": int(proc.name), "ppid": _ppid(proc), "cmdline": cmd[:300]})
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
    return out


def find_sandbox_pid(sandbox_name: str) -> int | None:
    cands = find_candidates(sandbox_name)
    if not cands:
        return None
    pids = {c["pid"] for c in cands}
    roots = [c for c in cands if c["ppid"] not in pids]
    return min(roots or cands, key=lambda c: c["pid"])["pid"]
