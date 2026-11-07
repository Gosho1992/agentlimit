#!/usr/bin/env python3
"""
Minimal AgentLimit client. Standard library only.

    export AGENTLIMIT_URL=https://64-177-10-84.sslip.io
    export AGENTLIMIT_TOKEN=<access code>
    python agentlimit_client.py "Count the rows in /workspace/orders.csv and print the number" --cpu 20 --wall 120

Or from Python:

    from agentlimit_client import AgentLimit
    al = AgentLimit(url, token)
    result = al.run("…task…", cpu_seconds=20, wall_seconds=120, token_budget=30000)
    print(result["answer"], result["outcome"], result["proof"])
"""
import argparse
import json
import os
import sys
import time
import urllib.request


class AgentLimit:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        return data if raw else json.loads(data)

    def start(self, task: str, cpu_seconds=30, memory_mb=512, wall_seconds=300, token_budget=50_000,
              cost_limit_usd=0.0) -> str:
        r = self._call("POST", "/sessions", {"task": task, "cpu_budget_seconds": cpu_seconds,
                                             "memory_budget_mb": memory_mb, "wall_clock_seconds": wall_seconds,
                                             "token_budget": token_budget, "cost_limit_usd": cost_limit_usd})
        return r["session_id"]

    def status(self, session_id: str) -> dict:
        return self._call("GET", f"/sessions/{session_id}")

    def wait(self, session_id: str, poll_s: float = 2.0, on_change=None) -> dict:
        last = None
        while True:
            s = self.status(session_id)
            if on_change and s["state"] != last:
                on_change(s)
                last = s["state"]
            if s.get("lifecycle") or s.get("archived"):
                return s
            time.sleep(poll_s)

    def stop(self, session_id: str) -> dict:
        return self._call("POST", f"/sessions/{session_id}/kill")

    def proof(self, session_id: str) -> dict:
        return self._call("GET", f"/sessions/{session_id}/evidence")

    def download_ledger(self, session_id: str, path: str) -> str:
        with open(path, "wb") as f:
            f.write(self._call("GET", f"/sessions/{session_id}/evidence/export", raw=True))
        return path

    def run(self, task: str, **limits) -> dict:
        sid = self.start(task, **limits)
        final = self.wait(sid)
        ev = self.proof(sid)
        term = final.get("termination") or {}
        return {
            "session_id": sid,
            "outcome": "stopped_by_agentlimit" if final["state"] == "KILL" else final["agent_status"],
            "answer": final.get("agent_claim"),
            "stop_verdict": term.get("verdict"),
            "stop_reason": term.get("reason"),
            "tokens": final.get("tokens"),
            "proof": ev["verification"],
            "fingerprint": ev.get("public_key_fingerprint"),
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a task under AgentLimit and print the governed result.")
    ap.add_argument("task")
    ap.add_argument("--cpu", type=int, default=30, help="CPU budget in seconds")
    ap.add_argument("--memory", type=int, default=512, help="memory budget in MB")
    ap.add_argument("--wall", type=int, default=300, help="wall-clock limit in seconds")
    ap.add_argument("--tokens", type=int, default=50_000, help="AI token budget")
    ap.add_argument("--ledger", help="also save the signed ledger to this file")
    a = ap.parse_args()
    url, token = os.environ.get("AGENTLIMIT_URL"), os.environ.get("AGENTLIMIT_TOKEN")
    if not url or not token:
        print("set AGENTLIMIT_URL and AGENTLIMIT_TOKEN", file=sys.stderr)
        return 2
    al = AgentLimit(url, token)
    sid = al.start(a.task, cpu_seconds=a.cpu, memory_mb=a.memory, wall_seconds=a.wall, token_budget=a.tokens)
    print(f"run {sid}: watch at {url}/?session={sid}")
    final = al.wait(sid, on_change=lambda s: print(f"  state → {s['state']}  (agent: {s['agent_status']})"))
    ev = al.proof(sid)
    print("answer:", final.get("agent_claim"))
    if final.get("termination"):
        print("stopped:", final["termination"]["verdict"], "reason:", final["termination"]["reason"])
    print("proof:", json.dumps(ev["verification"], indent=2))
    if a.ledger:
        print("signed ledger saved to", al.download_ledger(sid, a.ledger))
    return 0


if __name__ == "__main__":
    sys.exit(main())
