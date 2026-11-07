"""Durable job ledger: one sqlite row per run, with a resumable cursor.

Why sqlite replaces ``jobs.json``
---------------------------------
The JSON file was rewritten in full on every state change, which is fine for a
few dozen diagnostic records and useless for the two questions a long backfill
actually asks:

* **"Is another process writing this dataset right now?"**  Single flight lived
  in a module-level dict, so the console and the CLI could both start a
  ``bars_intraday`` run, each merge against a stale partition and drop the
  other's rows.
* **"How far did the last run get?"**  There was nowhere to put the answer.
  A minute-bar backfill is thousands of sequential fetches; a run that dies at
  80% must resume, not restart.

Both need a store that is visible to every process and atomically claimable, so
the ledger is a real table with a ``cursor`` column, an owner and a heartbeat.

What the ledger is *not*
------------------------
It is not the source of truth about coverage.  The lake is.  A cursor says "the
last run believed it got this far"; every ingester treats it as a hint and
re-checks the partitions, because a cursor written by a process that then died
mid-write can be optimistic.  The failure mode is a re-fetch, never a missing
row.

Leases, not pids
----------------
A crashed job cannot clean up after itself, so liveness is inferred from a
heartbeat refreshed by the runner's monitor thread.  ``os.kill(pid, 0)`` is not
portable (on Windows it is not a probe at all), and a pid is meaningless across
machines; a stale heartbeat is neither.  A row whose heartbeat is older than the
lease is reclaimable, which also recovers from ``kill -9`` and from a laptop
that slept through the run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import socket
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A job that stops heartbeating for this long is considered dead.  The monitor
#: refreshes every 15s, so three lost beats is unambiguous while still letting a
#: laptop survive a short sleep.
DEFAULT_LEASE_S = 120.0
HEARTBEAT_S = 15.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    dataset          TEXT NOT NULL,
    params           TEXT NOT NULL DEFAULT '{}',
    job_key          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,
    message          TEXT NOT NULL DEFAULT '',
    progress         REAL NOT NULL DEFAULT 0.0,
    cursor           TEXT,
    rows             INTEGER NOT NULL DEFAULT 0,
    done             INTEGER,
    total            INTEGER,
    owner            TEXT,
    heartbeat_at     TEXT,
    queued_at        TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    seconds          REAL,
    report           TEXT,
    error            TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_recent ON jobs(queued_at DESC);
CREATE INDEX IF NOT EXISTS jobs_live ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs(job_key);
"""

# Columns the runner owns; every write is a whole-row upsert so a partial update
# can never leave a row with a cursor from one run and a status from another.
_COLUMNS = (
    "id",
    "dataset",
    "params",
    "job_key",
    "status",
    "message",
    "progress",
    "cursor",
    "rows",
    "done",
    "total",
    "owner",
    "heartbeat_at",
    "queued_at",
    "started_at",
    "finished_at",
    "seconds",
    "report",
    "error",
    "cancel_requested",
)


class JobBusy(RuntimeError):
    """Another live job owns this dataset; the message names it."""

    def __init__(self, job_id: str, owner: str = "") -> None:
        self.job_id = job_id
        self.owner = owner
        where = f"（{owner}）" if owner else ""
        super().__init__(f"数据集正在被任务 {job_id}{where} 占用")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _owner() -> str:
    return f"{os.getpid()}@{socket.gethostname()}"


def owner_id() -> str:
    """Who is writing: ``pid@host``.  Diagnostic, not a liveness probe."""
    return _owner()


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Ledger:
    """The ``_jobs.sqlite`` table, one connection per process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- plumbing ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path), timeout=15.0, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            # WAL lets the API poll a running job while the ingester writes.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            conn = self._connect()
            return conn.execute(sql, args).fetchall()

    # -- reads ------------------------------------------------------------
    def get(self, job_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return _decode(rows[0]) if rows else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM jobs ORDER BY queued_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        )
        return [_decode(row) for row in rows]

    def live(self, dataset: str | None = None) -> list[dict[str, Any]]:
        """Jobs still owned by a process that is actually alive.

        Reconciliation happens here, not only in :meth:`claim`.  A process
        killed mid-run leaves a ``running`` row behind, and until somebody
        submitted the *same* dataset again nothing ever marked it dead — so the
        console showed a job that could never finish and polled it every 1.5
        seconds forever.  Reclaiming on read means "what is running" and "what
        the ledger believes is running" cannot drift apart.
        """
        try:
            self.expire_stale()
        except Exception as exc:  # noqa: BLE001 - a read must not fail over this
            logger.debug("lease reconciliation failed: %s", exc)
        sql = "SELECT * FROM jobs WHERE status IN ('queued','running')"
        args: tuple[Any, ...] = ()
        if dataset:
            sql += " AND dataset = ?"
            args = (dataset,)
        return [_decode(row) for row in self._rows(sql + " ORDER BY queued_at", args)]

    def cursor_of(self, job_id: str) -> dict[str, Any] | None:
        row = self.get(job_id)
        return (row or {}).get("cursor")

    def resume_cursor(self, job_key: str) -> tuple[str, dict[str, Any]] | None:
        """Cursor of the newest *unfinished* job with this key, if any.

        Only failed and cancelled runs hand a cursor forward: a ``done`` job has
        already been accounted for in the lake, and reusing its cursor on the
        next click would make a nightly job skip the new session entirely.
        """
        rows = self._rows(
            "SELECT id, cursor FROM jobs "
            "WHERE job_key = ? AND status IN ('failed','cancelled') "
            "AND cursor IS NOT NULL ORDER BY queued_at DESC, rowid DESC LIMIT 1",
            (job_key,),
        )
        if not rows:
            return None
        payload = _load_json(rows[0]["cursor"])
        if not isinstance(payload, dict) or not payload:
            return None
        return str(rows[0]["id"]), payload

    # -- writes -----------------------------------------------------------
    def save(self, job: dict[str, Any]) -> None:
        values = tuple(_encode(job, column) for column in _COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS if c != "id")
        sql = (
            f"INSERT INTO jobs ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        with self._lock:
            conn = self._connect()
            conn.execute(sql, values)
            conn.commit()

    def claim(self, job: dict[str, Any], *, lease_s: float = DEFAULT_LEASE_S) -> None:
        """Insert *job* as the owner of its dataset, or raise :class:`JobBusy`.

        The transaction is ``IMMEDIATE`` so the check and the insert are one
        step: two processes racing for ``bars_daily`` cannot both see "free".
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._expire_inner(conn, lease_s)
                row = conn.execute(
                    "SELECT id, owner FROM jobs WHERE dataset = ? "
                    "AND status IN ('queued','running') "
                    "ORDER BY queued_at DESC, rowid DESC LIMIT 1",
                    (job["dataset"],),
                ).fetchone()
                if row is not None:
                    conn.execute("ROLLBACK")
                    raise JobBusy(str(row["id"]), str(row["owner"] or ""))
                self._insert(conn, job)
                conn.commit()
            except JobBusy:
                raise
            except Exception:
                conn.rollback()
                raise

    def _insert(self, conn: sqlite3.Connection, job: dict[str, Any]) -> None:
        values = tuple(_encode(job, column) for column in _COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        conn.execute(
            f"INSERT INTO jobs ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    def expire_stale(self, lease_s: float = DEFAULT_LEASE_S) -> list[str]:
        """Mark runs whose heartbeat stopped as failed; returns their ids."""
        with self._lock:
            conn = self._connect()
            expired = self._expire_inner(conn, lease_s)
            conn.commit()
            return expired

    def _expire_inner(self, conn: sqlite3.Connection, lease_s: float) -> list[str]:
        cutoff = _shift(-lease_s)
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','running') "
            "AND COALESCE(heartbeat_at, started_at, queued_at) < ?",
            (cutoff,),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        for job_id in ids:
            conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, "
                "error = ?, message = ? WHERE id = ? AND status IN ('queued','running')",
                (_now(), "任务心跳中断（进程退出或机器休眠），已回收", "心跳中断", job_id),
            )
        return ids

    def touch(self, job_id: str, owner: str, *, progress: float, message: str,
              cursor: Any = None, rows: int | None = None,
              done: int | None = None, total: int | None = None) -> str:
        """Refresh the lease and the visible state; returns ``ok|cancelled|lost``.

        One statement does both the heartbeat and the progress write so a
        polling console sees movement, and the return value is how the monitor
        learns that somebody cancelled the job or that the lease was reclaimed.
        """
        stamp = _now()
        cursor_json = _dumps(cursor)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE jobs SET heartbeat_at = ?, progress = ?, message = ?, "
                "cursor = COALESCE(?, cursor), rows = COALESCE(?, rows), "
                "done = COALESCE(?, done), total = COALESCE(?, total) "
                "WHERE id = ? AND owner = ? AND status = 'running'",
                (stamp, float(progress), message, cursor_json, rows, done, total,
                 job_id, owner),
            )
            conn.commit()
            if cur.rowcount == 0:
                return "lost"
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return "cancelled" if row and int(row["cancel_requested"] or 0) else "ok"

    def set_cursor(self, job_id: str, cursor: Any) -> None:
        """Persist a checkpoint immediately — losing it is losing work."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE jobs SET cursor = ?, heartbeat_at = ? WHERE id = ?",
                (_dumps(cursor), _now(), job_id),
            )
            conn.commit()

    def request_cancel(self, job_id: str) -> str:
        """Ask a job to stop; returns ``missing|finished|queued|cancelled``."""
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return "missing"
            status = str(row["status"])
            if status in {"done", "failed", "cancelled"}:
                return "finished"
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1, message = ? WHERE id = ?",
                ("已请求取消，等待当前单元结束", job_id),
            )
            conn.commit()
            return "cancelled"

    def cancel_requested(self, job_id: str) -> bool:
        rows = self._rows(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
        )
        return bool(rows and int(rows[0]["cancel_requested"] or 0))

    def prune(self, keep: int) -> int:
        """Drop finished rows beyond the newest *keep*; live ones always stay."""
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "DELETE FROM jobs WHERE status NOT IN ('queued','running') AND id NOT IN ("
                "  SELECT id FROM jobs WHERE status NOT IN ('queued','running') "
                "  ORDER BY queued_at DESC, rowid DESC LIMIT ?)",
                (int(keep),),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    # -- migration --------------------------------------------------------
    def import_json(self, path: Path) -> int:
        """Import the old ``jobs.json`` history when the table is empty."""
        if self._rows("SELECT id FROM jobs LIMIT 1"):
            return 0
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - missing or corrupt history is just empty
            return 0
        if not isinstance(raw, list):
            return 0
        imported = 0
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            job = dict(item)
            job.setdefault("queued_at", _now())
            job["job_key"] = job.get("job_key") or ""
            # A history entry that still says "running" is not running now.
            if job.get("status") in {"queued", "running"}:
                job["status"] = "failed"
                job["error"] = job.get("error") or "进程重启，任务中断"
                job["finished_at"] = job.get("finished_at") or _now()
            try:
                self.save(job)
                imported += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not block the rest
                logger.warning("could not import job record %r: %s", item.get("id"), exc)
        return imported


# ---------------------------------------------------------------------------
# Row <-> dict
# ---------------------------------------------------------------------------


#: Columns declared ``NOT NULL`` in sqlite.  A caller that omits one (an old
#: ``jobs.json`` record, a hand-built dict in a test) must not turn into an
#: IntegrityError halfway through a backfill's bookkeeping.
_REQUIRED_TEXT = ("dataset", "job_key", "status", "message", "queued_at")


def _encode(job: dict[str, Any], column: str) -> Any:
    value = job.get(column)
    if column == "params":
        return _dumps(value) or "{}"
    if column in {"report", "cursor"}:
        return _dumps(value)
    if column == "cancel_requested":
        return 1 if value else 0
    if column in _REQUIRED_TEXT:
        return "" if value is None else value
    if column == "progress":
        return 0.0 if value is None else float(value)
    if column == "rows":
        return 0 if value is None else int(value)
    return value


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["params"] = _load_json(item.get("params")) or {}
    item["report"] = _load_json(item.get("report"))
    item["cursor"] = _load_json(item.get("cursor"))
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    return item


def _load_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _shift(seconds: float) -> str:
    return (dt.datetime.now() + dt.timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )
