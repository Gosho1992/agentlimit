"""
Out-of-loop watchdog.

The governance engine runs on the asyncio event loop. If that loop stalls
(a blocking call, a deadlock, a runaway handler), no tick, no pre-step check
and no kill can run, and the sandbox keeps executing ungoverned.

This watchdog lives on a separate OS thread. An asyncio task on the loop
writes a heartbeat every second; the thread checks it. If the heartbeat is
older than `stall_s`, the thread kills every live session's workload directly
through cgroup.kill (a plain file write that needs no event loop), records a
signed `watchdog_kill` entry, and flags the engine so that when the loop
recovers, it walks to KILL and runs the full verified-termination checks.

Engine-task death without a stall is handled elsewhere: session_manager
finalizes the session as soon as the engine task completes. A dead server
process is handled by systemd (ExecStopPost kills every session cgroup) and
by startup recovery.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable, Iterable

log = logging.getLogger("agentlimit.watchdog")


class Watchdog:
    def __init__(self, live_sessions: Callable[[], Iterable], stall_s: float = 15.0,
                 interval_s: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.live_sessions = live_sessions
        self.stall_s = stall_s
        self.interval_s = interval_s
        self.clock = clock
        self.last_beat = clock()
        self.fired: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- event-loop side ----
    def beat(self) -> None:
        self.last_beat = self.clock()

    async def heartbeat_task(self) -> None:
        while not self._stop.is_set():
            self.beat()
            await asyncio.sleep(self.interval_s)

    # ---- thread side ----
    def stalled(self) -> bool:
        return self.clock() - self.last_beat > self.stall_s

    def check(self) -> list[str]:
        """One watchdog pass. Returns the session ids it killed."""
        if not self.stalled():
            return []
        stall = round(self.clock() - self.last_beat, 1)
        killed = []
        for rt in list(self.live_sessions()):
            sid = rt.session_id
            if getattr(rt, "closed", False) or sid in self.fired:
                continue
            self.fired.add(sid)
            try:
                ok = bool(rt.cgroup.kill(3.0))
            except Exception as e:
                ok = False
                log.error("[%s] watchdog cgroup.kill failed: %s", sid, e)
            eng = rt.engine
            eng._kill_reason = "watchdog_event_loop_stall"
            eng._manual_kill = True          # the engine finishes a verified KILL once the loop runs
            try:
                rt.ledger.append("watchdog_kill", {
                    "reason": "governance event loop stalled",
                    "heartbeat_age_s": stall, "stall_threshold_s": self.stall_s,
                    "cgroup_kill_ok": ok,
                    "note": "issued from an OS thread outside the event loop; the engine runs the "
                            "full kill verification when the loop recovers",
                })
            except Exception as e:
                log.error("[%s] watchdog ledger append failed: %s", sid, e)
            log.critical("[%s] WATCHDOG KILL: event loop heartbeat %.1fs old, cgroup_kill_ok=%s", sid, stall, ok)
            killed.append(sid)
        return killed

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.check()
            except Exception as e:   # the watchdog itself must never die silently
                log.exception("watchdog pass failed: %s", e)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.beat()
        self._thread = threading.Thread(target=self._run, name="agentlimit-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"running": bool(self._thread and self._thread.is_alive()),
                "heartbeat_age_s": round(self.clock() - self.last_beat, 2),
                "stall_threshold_s": self.stall_s, "fired_sessions": sorted(self.fired)}
