"""
Durable global usage accounting (SQLite, survives restarts).

reserve() is ONE immediate transaction: it counts active sessions, sessions
started in the last hour, and tokens committed in the last 24 h, applies the
new session's full token budget as a reservation, and either inserts the row
or raises LimitExceeded. Two simultaneous requests cannot both take the last
allowance.

Token commitment per session:
  open (starting/running)   -> its whole token budget (reserved)
  closed / failed           -> actual + uncertain tokens
  interrupted (restart)     -> max(reserved, actual + uncertain): spend is
                               never forgotten because the server restarted.
"""

import sqlite3
import threading
import time
from pathlib import Path

from backend.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_usage (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    closed_at REAL,
    reserved_tokens INTEGER NOT NULL,
    actual_tokens INTEGER NOT NULL DEFAULT 0,
    uncertain_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    reserved_cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL
)
"""

COMMITTED_SQL = """
SELECT COALESCE(SUM(CASE
    WHEN status IN ('starting','running') THEN reserved_tokens
    WHEN status = 'interrupted' THEN MAX(reserved_tokens, actual_tokens + uncertain_tokens)
    ELSE actual_tokens + uncertain_tokens END), 0)
FROM session_usage WHERE created_at > ?
"""


class LimitExceeded(RuntimeError):
    """A global abuse/spend limit would be exceeded."""


class UsageStore:
    def __init__(self, path: str | None = None):
        self.path = path or settings.usage_db_path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def reserve(self, session_id: str, token_budget: int, cost_limit_usd: float = 0.0) -> dict:
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                c.execute("BEGIN IMMEDIATE")
                active = c.execute("SELECT COUNT(*) FROM session_usage WHERE status IN ('starting','running')").fetchone()[0]
                hourly = c.execute("SELECT COUNT(*) FROM session_usage WHERE created_at > ?", (now - 3600,)).fetchone()[0]
                day = c.execute(COMMITTED_SQL, (now - 86400,)).fetchone()[0]
                if active >= settings.max_concurrent_sessions:
                    raise LimitExceeded(f"{active} sessions already running (limit {settings.max_concurrent_sessions})")
                if hourly >= settings.max_sessions_per_hour:
                    raise LimitExceeded(f"hourly session limit reached ({settings.max_sessions_per_hour}/h)")
                if day + token_budget > settings.max_tokens_per_day:
                    raise LimitExceeded(f"daily token cap: {day:,} committed + {token_budget:,} requested "
                                        f"> {settings.max_tokens_per_day:,}")
                c.execute("INSERT INTO session_usage (session_id, created_at, reserved_tokens, reserved_cost_usd, status)"
                          " VALUES (?, ?, ?, ?, 'starting')", (session_id, now, token_budget, cost_limit_usd))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            finally:
                c.close()
        return {"active_before": active, "sessions_last_hour": hourly + 1,
                "tokens_committed_24h": day + token_budget}

    def set_status(self, session_id: str, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE session_usage SET status=? WHERE session_id=?", (status, session_id))

    def close(self, session_id: str, status: str, actual_tokens: int = 0, uncertain_tokens: int = 0,
              estimated_cost_usd: float = 0.0, reserved_cost_usd: float = 0.0) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE session_usage SET status=?, closed_at=?, actual_tokens=?, uncertain_tokens=?,"
                      " estimated_cost_usd=?, reserved_cost_usd=? WHERE session_id=?",
                      (status, time.time(), actual_tokens, uncertain_tokens, estimated_cost_usd,
                       reserved_cost_usd, session_id))

    def mark_interrupted(self) -> list[str]:
        with self._lock, self._conn() as c:
            rows = [r[0] for r in c.execute(
                "SELECT session_id FROM session_usage WHERE status IN ('starting','running')")]
            c.execute("UPDATE session_usage SET status='interrupted', closed_at=? "
                      "WHERE status IN ('starting','running')", (time.time(),))
        return rows

    def summary(self) -> dict:
        now = time.time()
        with self._lock, self._conn() as c:
            return {
                "active_sessions": c.execute("SELECT COUNT(*) FROM session_usage WHERE status IN ('starting','running')").fetchone()[0],
                "sessions_last_hour": c.execute("SELECT COUNT(*) FROM session_usage WHERE created_at > ?", (now - 3600,)).fetchone()[0],
                "tokens_committed_24h": c.execute(COMMITTED_SQL, (now - 86400,)).fetchone()[0],
                "limits": {"max_concurrent_sessions": settings.max_concurrent_sessions,
                           "max_sessions_per_hour": settings.max_sessions_per_hour,
                           "max_tokens_per_day": settings.max_tokens_per_day},
            }
