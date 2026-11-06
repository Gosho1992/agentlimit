"""Deterministic stand-ins so the governor can be tested end-to-end without
root, cgroup v2, KVM or network. Real kernel behaviour is covered by
test_freeze_kill.py (needs root + cgroup v2) and scripts/day1_gate.py (Vultr VM)."""
import asyncio
import json

import httpx


class FakeCgroup:
    def __init__(self):
        self.cpu = 0
        self.mem = 50 * 2**20
        self.peak = self.mem
        self.populated = True
        self.frozen = False
        self.cpu_max_val = "100000 100000"
        self.memory_max_bytes = 768 * 2**20
        self.memory_high = None
        self.base_cpu_quota_usec = 100_000
        self.kill_works = True
        self.path = "/fake/cgroup"

    def cpu_usage_usec(self): return self.cpu
    def memory_current(self): return self.mem
    def memory_peak(self): return self.peak
    def is_populated(self): return self.populated
    def is_frozen(self): return self.frozen
    def set_cpu_quota(self, q, period=100_000): self.cpu_max_val = f"{q} {period}"
    def set_memory_high(self, b): self.memory_high = b
    def cpu_max(self): return self.cpu_max_val
    def freeze(self, timeout_s=2.0): self.frozen = True; return True
    def unfreeze(self): self.frozen = False; return True
    def kill(self, timeout_s=3.0):
        if self.kill_works:
            self.populated = False
        return not self.populated
    def destroy(self): return not self.populated
    def set_memory_max(self, b): self.memory_max_bytes = b
    def set_pids_max(self, n): self.pids = n
    def attach_tree(self, pid): self.pid = pid; return 3
    def attach_tree_verified(self, pid, starttime=None):
        self.pid = pid
        ok = getattr(self, "attach_ok", True)
        return {"root_pid": pid, "root_starttime": starttime, "members": [pid] if ok else [],
                "exited_during_scan": [], "failures": [] if ok else [{"pid": pid + 1, "reason": "attach_failed:PermissionError"}],
                "passes": 2, "stable_passes": 2, "passed": ok}
    def contains(self, pid): return pid == getattr(self, "pid", None)
    def readback(self):
        return {"cpu.max": self.cpu_max_val, "memory.max": str(self.memory_max_bytes),
                "memory.high": "max" if self.memory_high is None else str(self.memory_high),
                "pids.max": str(getattr(self, "pids", "max"))}
    def capabilities(self):
        return {f: True for f in ("cgroup.procs", "cgroup.events", "cgroup.kill", "cpu.max", "cpu.stat",
                                  "memory.max", "memory.high", "memory.current", "pids.max", "cgroup.freeze")}


class FakeRuntime:
    name = "agentlimit-fake"

    def __init__(self, results=None, delay=0.0):
        self.results = list(results or [])
        self.delay = delay
        self.execs = 0
        self.stopped = False

    async def exec(self, code, timeout=60.0):
        self.execs += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.results:
            return self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return {"stdout": "ok\n", "stderr": "", "exit_code": 0}

    async def metrics(self): return None
    stop_fails = False
    async def stop(self, force=False):
        if self.stop_fails:
            return {"stop_ok": False, "removed": False, "stop_error": "boom", "remove_error": "boom"}
        self.stopped = True
        return {"stop_ok": True, "removed": True, "stop_error": None, "remove_error": None}
    async def host_pid(self): return 999_999_999   # never a live PID
    probe_override: dict = {}
    workspace_files = ["orders.csv", "payments.csv"]
    async def containment_probe(self):
        p = {"egress_blocked": True, "dns_blocked": True, "secret_env_vars": [], "no_secrets_in_env": True,
             "host_paths_visible": [], "uid": 65534, "workspace_mounted": True,
             "workspace_files": list(self.workspace_files), "inputs_readable": True,
             "writable_inputs": [], "inputs_read_only": True}
        p.update(self.probe_override)
        return p


def mock_inference_transport(prompt_tokens=400, completion_tokens=200, code="print('hi')", calls=None, delay=0.0):
    async def handler(request: httpx.Request):
        if calls is not None:
            calls.append(json.loads(request.content))
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": f"```python\n{code}\n```"}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        })
    return httpx.MockTransport(handler)
