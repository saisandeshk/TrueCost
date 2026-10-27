"""The ledger: one SQLite file that every source writes to and both faces read from.

Sources are the gateway (agents we run), and the collectors (Claude Code and Codex sessions on a
subscription, read from their local logs). Token classes follow Artificial Analysis' "Cost per Task"
split: input (uncached), cache read, cache write, output, and the reasoning share of output.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  workload   TEXT,
  condition  TEXT,
  config     TEXT,
  started    REAL,
  ended      REAL,
  notes      TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  task_key   TEXT PRIMARY KEY,
  run_id     TEXT,
  task_id    TEXT,
  source     TEXT,
  workload   TEXT,
  title      TEXT,
  started    REAL,
  ended      REAL,
  outcome    TEXT,
  reward     REAL,
  partial    REAL,
  violations INTEGER,
  meta       TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  call_id            TEXT PRIMARY KEY,
  ts_start           REAL,
  ts_end             REAL,
  source             TEXT,
  run_id             TEXT,
  task_key           TEXT,
  session_id         TEXT,
  role               TEXT,
  tier               TEXT,
  model              TEXT,
  api                TEXT,
  stream             INTEGER,
  input_tokens       INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  output_tokens      INTEGER,
  reasoning_tokens   INTEGER,
  n_messages         INTEGER,
  n_tool_calls       INTEGER,
  tool_names         TEXT,
  finish_reason      TEXT,
  status             INTEGER,
  error              TEXT,
  latency_ms         REAL,
  ttft_ms            REAL,
  gpu                INTEGER,
  energy_j           REAL,
  cost_usd           REAL,
  saver              TEXT,
  decision           TEXT,
  meta               TEXT
);
CREATE INDEX IF NOT EXISTS calls_task ON calls(task_key);
CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts_start);
CREATE TABLE IF NOT EXISTS energy (
  run_id       TEXT,
  gpu          INTEGER,
  window_j     REAL,
  attributed_j REAL,
  idle_j       REAL,
  PRIMARY KEY (run_id, gpu)
);
CREATE TABLE IF NOT EXISTS limits (
  ts        REAL,
  source    TEXT,
  window    TEXT,
  used_pct  REAL,
  resets_at REAL,
  PRIMARY KEY (ts, source, window)
);
"""

CALL_COLUMNS = [
    "call_id", "ts_start", "ts_end", "source", "run_id", "task_key", "session_id", "role", "tier",
    "model", "api", "stream", "input_tokens", "cache_read_tokens", "cache_write_tokens",
    "output_tokens", "reasoning_tokens", "n_messages", "n_tool_calls", "tool_names", "finish_reason",
    "status", "error", "latency_ms", "ttft_ms", "gpu", "energy_j", "cost_usd", "saver", "decision",
    "meta",
]
TASK_COLUMNS = ["task_key", "run_id", "task_id", "source", "workload", "title", "started", "ended",
                "outcome", "reward", "partial", "violations", "meta"]
RUN_COLUMNS = ["run_id", "workload", "condition", "config", "started", "ended", "notes"]

JSON_FIELDS = {"tool_names", "saver", "decision", "meta", "config"}

# Task outcomes. "verified" means an independent check passed (benchmark grader, test run).
VERIFIED, FAILED, UNVERIFIED, RUNNING = "verified", "failed", "unverified", "running"


def _encode(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if k in JSON_FIELDS and v is not None and not isinstance(v, str):
            v = json.dumps(v, separators=(",", ":"))
        out[k] = v
    return out


class Ledger:
    """Thread-safe access to the ledger file. One connection per thread, WAL mode."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    def _upsert(self, table: str, columns: list[str], row: dict[str, Any], replace: bool = True) -> None:
        row = _encode({k: v for k, v in row.items() if k in columns})
        cols = ",".join(row)
        marks = ",".join("?" for _ in row)
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self.conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", list(row.values()))

    def add_call(self, row: dict[str, Any]) -> None:
        self._upsert("calls", CALL_COLUMNS, row)

    def add_calls(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        c = self.conn
        c.execute("BEGIN")
        try:
            for row in rows:
                self._upsert("calls", CALL_COLUMNS, row)
                n += 1
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return n

    def upsert_task(self, row: dict[str, Any], merge: bool = True) -> None:
        """Insert or update a task. With merge, fields not given keep their stored values."""
        if merge:
            old = self.conn.execute("SELECT * FROM tasks WHERE task_key=?", (row["task_key"],)).fetchone()
            if old is not None:
                row = {**dict(old), **{k: v for k, v in row.items() if v is not None}}
        self._upsert("tasks", TASK_COLUMNS, row)

    def upsert_run(self, row: dict[str, Any], merge: bool = True) -> None:
        if merge:
            old = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            if old is not None:
                row = {**dict(old), **{k: v for k, v in row.items() if v is not None}}
        self._upsert("runs", RUN_COLUMNS, row)

    def set_energy(self, run_id: str, gpu: int, window_j: float, attributed_j: float, idle_j: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO energy VALUES (?,?,?,?,?)",
                          (run_id, gpu, window_j, attributed_j, idle_j))

    def add_limit(self, source: str, window: str, used_pct: float, resets_at: float | None,
                  ts: float | None = None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO limits VALUES (?,?,?,?,?)",
                          (ts or time.time(), source, window, used_pct, resets_at))

    def purge_run(self, run_id: str) -> None:
        """Delete everything booked to a run (used before re-running a failed or partial run)."""
        c = self.conn
        for t in ("calls", "tasks", "energy", "runs"):
            c.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))

    def query(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r is not None else None
