"""
INFRASTRUCTURE GATE — run on the Vultr VM (as root) before every demo.

    sudo -E venv/bin/python -m backend.scripts.day1_gate

Prints PASS/FAIL for every assumption the governor depends on, with the
evidence (PIDs, cmdlines, byte counts) so a failure is diagnosable:

  kvm_present, cgroup_v2, sandbox_boot, exec_roundtrip, host_pid_found,
  cgroup_attach, cpu_accounting_moves, memory_telemetry_moves,
  network_egress_blocked, no_secrets_in_guest, cgroup_freeze_unfreeze,
  kill_sigterm_resistant_workload, sandbox_cleanup
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.sandbox.cgroup_controller import CgroupController  # noqa: E402
from backend.sandbox.host_process import find_candidates, find_sandbox_pid  # noqa: E402
from backend.sandbox.microsandbox_runtime import MicrosandboxRuntime  # noqa: E402

RESULTS: dict[str, tuple[bool, str]] = {}


def record(name: str, ok: bool, detail: str = ""):
    RESULTS[name] = (bool(ok), detail)
    print(f"{'PASS' if ok else 'FAIL'}  {name:34s} {detail}", flush=True)


async def main():
    os.environ.setdefault("VULTR_INFERENCE_KEY_CANARY", "should-not-leak")
    record("kvm_present", Path("/dev/kvm").exists(), "/dev/kvm")
    ctrl = Path("/sys/fs/cgroup/cgroup.controllers")
    record("cgroup_v2", ctrl.exists(), ctrl.read_text().strip() if ctrl.exists() else "not cgroup v2")
    record("running_as_root", os.geteuid() == 0, f"euid={os.geteuid()}")

    import tempfile
    ws = Path(tempfile.mkdtemp())
    (ws / "orders.csv").write_text("order_id,amount\nA,1.00\n")
    rt = MicrosandboxRuntime("gate" + str(int(time.time())), memory_mb=320)
    try:
        await rt.start(ws)
        record("sandbox_boot", True, rt.name)
    except Exception as e:
        record("sandbox_boot", False, repr(e)[:300])
        return

    cg = None
    try:
        r = await rt.exec("print(6*7)", timeout=30)
        record("exec_roundtrip", r["stdout"].strip() == "42", repr(r)[:120])

        pid = None
        for _ in range(30):
            pid = find_sandbox_pid(rt.name)
            if pid:
                break
            await asyncio.sleep(0.1)
        record("host_pid_found", pid is not None, f"pid={pid} candidates={find_candidates(rt.name)[:3]}")
        if not pid:
            return

        cg = CgroupController(rt.name, root="/sys/fs/cgroup/agentlimit_gate")
        cg.set_memory_max(512 * 2**20 + 256 * 2**20)
        cg.set_pids_max(512)
        n = cg.attach_tree(pid)
        record("cgroup_attach", n > 0 and cg.contains(pid), f"attached={n} path={cg.path}")

        c0 = cg.cpu_usage_usec()
        await rt.exec("x=0\nfor i in range(30_000_000): x+=i\nprint(x)", timeout=60)
        c1 = cg.cpu_usage_usec()
        record("cpu_accounting_moves", c1 - c0 > 500_000, f"delta_usec={c1 - c0}")

        m0 = cg.memory_current()
        await rt.exec("b=bytearray(300*1024*1024)\nimport time; time.sleep(1.5)\nprint(len(b))", timeout=30)
        m1 = cg.memory_peak()
        record("memory_telemetry_moves", (m1 - m0) > 100 * 2**20, f"baseline={m0} peak={m1}")

        probe = await rt.containment_probe()
        record("network_egress_blocked", bool(probe.get("egress_blocked")), str(probe.get("egress_error", "")))
        record("no_secrets_in_guest", bool(probe.get("no_secrets_in_env")), f"vars={probe.get('secret_env_vars')}")
        record("agent_runs_unprivileged", probe.get("uid") not in (None, 0), f"uid={probe.get('uid')}")
        record("inputs_read_only_to_agent", bool(probe.get("inputs_read_only")), f"files={probe.get('workspace_files')}")
        record("host_paths_invisible", not probe.get("host_paths_visible"), str(probe.get("host_paths_visible")))

        frozen = cg.freeze()
        unfrozen = cg.unfreeze() if frozen else False
        r2 = await rt.exec("print('alive')", timeout=20) if unfrozen else {"stdout": ""}
        record("cgroup_freeze_unfreeze", frozen and unfrozen and "alive" in r2["stdout"],
               f"frozen={frozen} unfrozen={unfrozen}")

        # SIGTERM-resistant runaway inside the guest; kill from the host.
        runaway = ("import signal,time\nsignal.signal(signal.SIGTERM, lambda *a: None)\n"
                   "while True: pass")
        task = asyncio.create_task(rt.exec(runaway, timeout=120))
        await asyncio.sleep(2.0)
        t0 = time.monotonic()
        killed = cg.kill(timeout_s=5.0)
        elapsed = time.monotonic() - t0
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            pass
        alive = Path(f"/proc/{pid}").exists()
        record("kill_sigterm_resistant_workload", killed and not cg.is_populated() and not alive,
               f"populated0={not cg.is_populated()} vmm_alive={alive} in {elapsed:.2f}s")
        stop = await rt.stop(force=True)
        record("sdk_remove_after_cgroup_kill", bool(stop.get("removed")), str(stop))
    finally:
        try:
            await rt.stop(force=True)
        except Exception:
            pass
        gone = True
        if cg is not None:
            if cg.is_populated():
                cg.kill()
            gone = cg.destroy()
        record("sandbox_cleanup", gone, "cgroup removed" if gone else "cgroup still present")
        print_report()


def print_report():
    print("\n=== INFRASTRUCTURE GATE ===")
    failed = [k for k, (ok, _) in RESULTS.items() if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("FAILED:", ", ".join(failed))
        print("Do not claim the failed properties in the demo or README.")
    else:
        print("All gates passed — the governor's containment claims are backed on this host.")


if __name__ == "__main__":
    asyncio.run(main())
