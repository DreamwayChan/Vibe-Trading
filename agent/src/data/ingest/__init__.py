"""Ingest orchestration: one job runner for every declared dataset.

Why a runner instead of "call the fetcher directly"
---------------------------------------------------
Each dataset's ingester needs the same four things around it: a bound on how
much history one run may pull, a single writer per dataset (two concurrent
backfills of ``bars_daily`` would each merge against a stale copy of the year
file and silently drop rows), a record of what happened, and a catalog touch so
the console's freshness follows the write.

Doing that per ingester is how the *previous* generation of this project ended
up with four half-overlapping fetch loops.  So the ingesters stay pure-ish
(:mod:`src.data.ingest.bars` knows how to fetch bars and nothing else) and this
module owns the policy:

* **Single flight** — a second job for a dataset that is already running is
  rejected with :class:`IngestBusy`, not queued.  ``lake.write_dataset`` merges
  read-modify-write per partition; two overlapping runs could interleave and
  lose the other's rows.  The claim is taken in sqlite, so it holds across
  processes — the console and a CLI run can no longer collide.
* **Bounded** — every ingester takes ``max_*`` limits, and the runner passes a
  deadline so a UI-triggered job cannot run for an hour in the background.
* **Recorded** — :class:`IngestJob` keeps status, progress, row counts, notes
  and a ``cursor``, persisted in ``<lake>/_ingest/_jobs.sqlite``.  The cursor is
  what makes a killed backfill resumable instead of restartable.
* **Cancellable** — :func:`cancel` raises a flag the ingester polls at unit
  boundaries and the monitor thread mirrors from the ledger, so a cancel from
  another process lands too.
* **Announced** — after a successful run the manifest is rebuilt (or touched
  when the dataset is too large to scan cheaply), so "最新日期" in the console
  moves without a manual rescan.

The registry stays the source of truth: a dataset is ingestible exactly when its
``ingest`` field names a ``module:function``; the runner resolves that string
instead of keeping a dispatch table that can drift from the registry.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import hashlib
import importlib
import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from src.data.ingest.ledger import HEARTBEAT_S, JobBusy, Ledger, owner_id

logger = logging.getLogger(__name__)

__all__ = [
    "IngestError",
    "IngestBusy",
    "IngestReport",
    "IngestJob",
    "progress",
    "note",
    "checkpoint",
    "cancelled",
    "cancel",
    "run",
    "submit",
    "job",
    "jobs",
    "active",
    "dataset_options",
]

# A UI-triggered job must finish while the tab is still open; the CLI can pass a
# larger value.  Ingesters check this and return early with a note instead of
# being killed mid-write.
DEFAULT_DEADLINE_S = 600.0
_JOB_HISTORY = 40
_MAX_SCAN_FILES = 2000


class IngestError(RuntimeError):
    """A run could not start or finish; the message is user-facing."""


class IngestBusy(IngestError):
    """Another job for the same dataset is already running."""


@dataclass
class IngestReport:
    """What one ingester achieved.

    Ingesters return this instead of a bare int so the console can say *what*
    landed (which sessions, how many symbols, from which source) rather than
    only how many rows moved.
    """

    dataset: str
    rows: int = 0
    sessions: int = 0
    symbols: int = 0
    source: str = ""
    start: str | None = None
    end: str | None = None
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "rows": self.rows,
            "sessions": self.sessions,
            "symbols": self.symbols,
            "source": self.source,
            "start": self.start,
            "end": self.end,
            "skipped": self.skipped,
            "notes": list(self.notes),
        }


@dataclass
class IngestJob:
    """One run's life cycle, as the API and the CLI report it.

    ``cursor`` is the resume point an ingester publishes through
    :func:`checkpoint`: a small JSON object such as
    ``{"session": "2026-11-02", "symbol": "603288.SH", "done": 412, "total": 5576}``.
    It is written as it advances, so the next run starts where this one stopped
    instead of re-fetching what it already had.
    """

    id: str
    dataset: str
    params: dict[str, Any]
    status: str = "queued"  # queued | running | done | failed | cancelled
    message: str = ""
    progress: float = 0.0
    queued_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    seconds: float | None = None
    report: dict[str, Any] | None = None
    error: str | None = None
    job_key: str = ""
    cursor: dict[str, Any] | None = None
    rows: int = 0
    done: int | None = None
    total: int | None = None
    owner: str = ""
    heartbeat_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "params": self.params,
            "status": self.status,
            "message": self.message,
            "progress": round(self.progress, 4),
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seconds": self.seconds,
            "report": self.report,
            "error": self.error,
            "job_key": self.job_key,
            "cursor": self.cursor,
            "rows": self.rows,
            "done": self.done,
            "total": self.total,
            "owner": self.owner,
            "heartbeat_at": self.heartbeat_at,
        }

    @property
    def running(self) -> bool:
        return self.status in {"queued", "running"}


class _RunContext:
    """Mutable handle an ingester uses to report progress to its job.

    ``ledger`` is captured once, when the job starts.  Resolving it per write
    would mean a background heartbeat could land in a *different* database if the
    lake root moved under it (which is exactly what a test fixture does), leaving
    one row claiming a run that another database owns.
    """

    def __init__(self, job: IngestJob, cancel: threading.Event, ledger: Ledger) -> None:
        self.job = job
        self.cancel = cancel
        self.ledger = ledger

    def update(self, fraction: float, message: str = "") -> None:
        self.job.progress = max(0.0, min(float(fraction), 0.999))
        if message:
            self.job.message = message

    def note(self, message: str) -> None:
        self.job.message = message
        logger.info("[%s] %s", self.job.dataset, message)


_current: contextvars.ContextVar[_RunContext | None] = contextvars.ContextVar(
    "ingest_context", default=None
)


def _ctx() -> _RunContext | None:
    return _current.get()


def progress(fraction: float, message: str = "") -> None:
    """Report progress from inside an ingester (no-op outside a job)."""
    ctx = _ctx()
    if ctx is not None:
        ctx.update(fraction, message)


def note(message: str) -> None:
    """Attach a human-readable step to the running job (no-op outside a job)."""
    ctx = _ctx()
    if ctx is not None:
        ctx.note(message)


def cancelled() -> bool:
    """Whether this run has been asked to stop (``False`` outside a job)."""
    ctx = _ctx()
    return bool(ctx is not None and ctx.cancel.is_set())


def checkpoint(cursor: dict[str, Any], *, done: int | None = None,
               total: int | None = None) -> None:
    """Publish the resume point and persist it immediately.

    Called *after* the unit of work is durable — a checkpoint written before the
    write is a promise the next run will not keep.  Persisting on every call is
    deliberate: the whole value of a cursor is that a ``kill -9`` one line later
    still finds it.
    """
    ctx = _ctx()
    if ctx is None:
        return
    ctx.job.cursor = dict(cursor)
    if done is not None:
        ctx.job.done = int(done)
    if total is not None:
        ctx.job.total = int(total)
    ctx.job.progress = max(ctx.job.progress, min(float(done or 0) / max(int(total or 0), 1), 0.999))
    try:
        ctx.ledger.set_cursor(ctx.job.id, ctx.job.cursor)
    except Exception as exc:  # noqa: BLE001 - a lost checkpoint is a re-fetch
        logger.debug("checkpoint write failed: %s", exc)


# ---------------------------------------------------------------------------
# Job store — sqlite ledger
# ---------------------------------------------------------------------------
#
# ``_live`` holds only the jobs this process is running; their progress fields
# are mutated by the ingester and read straight from memory so a poll does not
# hit disk.  Everything durable — status, cursor, report — goes to the ledger,
# which is also what another process reads to decide whether a dataset is busy.

_lock = threading.RLock()
_live: dict[str, IngestJob] = {}
_cancel_events: dict[str, threading.Event] = {}
_ledger_ref: tuple[str, Ledger] | None = None


def _ledger() -> Ledger:
    """The ledger for the current lake root, re-opened when the root moves."""
    global _ledger_ref
    from src.data.lake import lake_root

    root = lake_root()
    stamp = str(root)
    with _lock:
        if _ledger_ref is not None and _ledger_ref[0] == stamp:
            return _ledger_ref[1]
        ledger = Ledger(root / "_ingest" / "_jobs.sqlite")
        imported = ledger.import_json(root / "_ingest" / "jobs.json")
        if imported:
            logger.info("migrated %d job records from jobs.json into _jobs.sqlite", imported)
        # A row left behind by a process that died is not running now.  The
        # heartbeat check keeps a genuinely live job from being reclaimed.
        for job_id in ledger.expire_stale():
            logger.info("reclaimed stale ingest job %s", job_id)
        _ledger_ref = (stamp, ledger)
        return ledger


def _reset_for_tests() -> None:
    """Drop in-process state; the next call re-opens the ledger at the new root."""
    global _ledger_ref
    with _lock:
        if _ledger_ref is not None:
            _ledger_ref[1].close()
        _ledger_ref = None
        _live.clear()
        _cancel_events.clear()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _job_key(name: str, params: dict[str, Any]) -> str:
    """Stable identity of a target: same dataset + same effective params."""
    clean = {k: v for k, v in params.items() if v is not None and k != "cursor"}
    material = f"{name}|{json.dumps(clean, sort_keys=True, default=str)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _save(job_item: IngestJob, ledger: Ledger | None = None) -> None:
    """Best-effort write: the ledger is diagnostics plus a resume point."""
    try:
        (ledger or _ledger()).save(job_item.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.debug("ingest ledger write failed: %s", exc)


def job(job_id: str) -> dict[str, Any] | None:
    """Return one job by id (``None`` when unknown).

    A live job is answered from memory because its progress moves between
    heartbeats; anything else comes from the ledger, including jobs from a
    previous process.
    """
    live = _live.get(job_id)
    if live is not None and live.running:
        return live.to_dict()
    row = _ledger().get(job_id)
    if row is not None:
        return row
    return live.to_dict() if live is not None else None


def jobs(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent jobs, newest first."""
    limit = max(int(limit), 1)
    known = {row["id"]: row for row in _ledger().recent(limit)}
    for live in list(_live.values()):
        known[live.id] = live.to_dict()
    ordered = sorted(known.values(), key=lambda row: row.get("queued_at") or "", reverse=True)
    return ordered[:limit]


def active() -> list[dict[str, Any]]:
    """Jobs currently queued or running, in the order they started."""
    rows = {row["id"]: row for row in _ledger().live()}
    for live in list(_live.values()):
        if live.running:
            rows[live.id] = live.to_dict()
        else:
            rows.pop(live.id, None)
    return sorted(rows.values(), key=lambda row: row.get("queued_at") or "")


def cancel(job_id: str) -> dict[str, Any]:
    """Ask one job to stop at its next unit boundary.

    The flag lives in the ledger so a cancel from the console reaches a CLI run,
    and is mirrored into the process-local event so the run stops within one
    unit instead of waiting for the next heartbeat.

    Raises:
        IngestError: The job id is unknown.
    """
    outcome = _ledger().request_cancel(job_id)
    if outcome == "missing":
        raise IngestError(f"未知任务：{job_id}")
    event = _cancel_events.get(job_id)
    if event is not None:
        event.set()
    return job(job_id) or {}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _callable_for(name: str) -> Callable[..., Any]:
    """Resolve ``spec.ingest`` (``"module:function"``) to a callable.

    Raises:
        IngestError: The dataset is unknown, declares no ingester, or the
            declared target cannot be imported — all three are configuration
            bugs the operator must see, not silent no-ops.
    """
    from src.data.registry import REGISTRY

    if name not in REGISTRY:
        raise IngestError(f"未知数据集：{name}")
    target = REGISTRY[name].ingest
    if not target:
        raise IngestError(f"{REGISTRY[name].label} 未声明入库函数（只读数据集）")
    module_name, _, attr = target.partition(":")
    if not module_name or not attr:
        raise IngestError(f"{name} 的 ingest 声明无效：{target!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise IngestError(f"无法加载 {module_name}：{exc}") from exc
    func = getattr(module, attr, None)
    if not callable(func):
        raise IngestError(f"{module_name} 没有可调用的 {attr}")
    return func


def _accepted_params(func: Callable[..., Any]) -> set[str]:
    """Parameter names *func* accepts (including ``**kwargs`` → empty set = all)."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return set()
    names: set[str] = set()
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return set()  # accepts anything
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.add(param.name)
    return names


def _filter_params(func: Callable[..., Any], params: dict[str, Any]) -> dict[str, Any]:
    accepted = _accepted_params(func)
    if not accepted:
        return dict(params)
    return {k: v for k, v in params.items() if k in accepted and v is not None}


def dataset_options() -> list[dict[str, Any]]:
    """Which datasets can be ingested right now, and what the UI may send."""
    from src.data.registry import REGISTRY

    out: list[dict[str, Any]] = []
    for name, dataset_spec in REGISTRY.items():
        runnable = bool(dataset_spec.ingest)
        reason = "" if runnable else "只读数据集"
        accepted: set[str] = set()
        if runnable:
            try:
                accepted = _accepted_params(_callable_for(name))
            except IngestError as exc:
                runnable, reason = False, str(exc)
        out.append(
            {
                "name": name,
                "label": dataset_spec.label,
                "category": dataset_spec.category,
                "ingest": dataset_spec.ingest,
                "runnable": runnable,
                "reason": reason,
                "params": sorted(accepted),
                "sla_minutes": dataset_spec.sla_minutes,
                "sources": list(dataset_spec.sources),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

# Progress is persisted by the monitor every HEARTBEAT_S seconds; the in-memory
# object is what the API answers from meanwhile, so an ingester may call
# ``progress()`` as often as it likes.
class _Monitor(threading.Thread):
    """Keeps the lease alive and mirrors a cross-process cancel into the run."""

    def __init__(
        self, job_item: IngestJob, cancel_event: threading.Event, ledger: Ledger
    ) -> None:
        super().__init__(name=f"ingest-monitor-{job_item.id}", daemon=True)
        self.job = job_item
        self.cancel = cancel_event
        self.ledger = ledger
        self.lost = False
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - timing driven
        while not self._stop.wait(HEARTBEAT_S):
            try:
                outcome = self.ledger.touch(
                    self.job.id,
                    self.job.owner,
                    progress=self.job.progress,
                    message=self.job.message,
                    cursor=self.job.cursor,
                    rows=self.job.rows,
                    done=self.job.done,
                    total=self.job.total,
                )
            except Exception as exc:  # noqa: BLE001 - one missed beat is fine
                logger.debug("ledger heartbeat failed: %s", exc)
                continue
            if outcome == "cancelled":
                logger.info("job %s cancelled by request", self.job.id)
                self.cancel.set()
            elif outcome == "lost":
                # Another process reclaimed the row: stop writing before two
                # processes are both merging partitions.
                logger.warning("job %s lost its lease; stopping", self.job.id)
                self.lost = True
                self.cancel.set()
                break

    def stop(self) -> None:
        self._stop.set()


def _finish(
    job_item: IngestJob,
    *,
    report: IngestReport | None,
    error: str | None,
    cancel_event: threading.Event | None = None,
    ledger: Ledger | None = None,
) -> None:
    """Close a job, refresh the catalog and release the claim."""
    requested = bool(cancel_event is not None and cancel_event.is_set())
    job_item.finished_at = _now()
    if error:
        job_item.status = "failed"
    elif requested:
        job_item.status = "cancelled"
        job_item.message = "已取消"
    else:
        job_item.status = "done"
    job_item.error = error
    if report is not None:
        job_item.report = report.to_dict()
        job_item.rows = report.rows
        job_item.progress = 1.0
        if not requested:
            job_item.message = (
                f"写入 {report.rows} 行 / {report.sessions} 个交易日 / {report.symbols} 个标的"
            )
    elif error:
        job_item.message = error
    if job_item.started_at:
        try:
            started = dt.datetime.fromisoformat(job_item.started_at)
            job_item.seconds = round((dt.datetime.now() - started).total_seconds(), 2)
        except ValueError:  # pragma: no cover - defensive
            job_item.seconds = None

    if report is not None and report.rows:
        _refresh_catalog(report.dataset, report.source, job_item.id)

    _save(job_item, ledger)
    with _lock:
        _live.pop(job_item.id, None)
        _cancel_events.pop(job_item.id, None)


def _refresh_catalog(name: str, source: str, job_id: str) -> None:
    """Make the manifest agree with what was just written.

    A full rescan is the accurate answer and is cheap for a year-partitioned
    dataset, but an intraday dataset with thousands of session files is not —
    there the accumulated ``touch`` is the honest, bounded choice.
    """
    from src.data import catalog

    try:
        if catalog.file_count(name) <= _MAX_SCAN_FILES:
            catalog.rebuild(name)
        else:
            catalog.touch(name, source=source, job=job_id)
    except Exception as exc:  # noqa: BLE001 - catalog is bookkeeping
        logger.warning("catalog refresh after ingest of %s failed: %s", name, exc)


def _resume_point(job_item: IngestJob, ledger: Ledger) -> dict[str, Any] | None:
    """The cursor an unfinished run of the same target left behind."""
    if not job_item.job_key:
        return None
    found = ledger.resume_cursor(job_item.job_key)
    if found is None:
        return None
    previous, cursor = found
    job_item.cursor = dict(cursor)
    job_item.message = f"从任务 {previous} 的断点继续"
    logger.info("resuming %s from job %s at %s", job_item.dataset, previous, cursor)
    return cursor


def _execute(
    job_item: IngestJob,
    func: Callable[..., Any],
    params: dict[str, Any],
    deadline_s: float,
    cancel_event: threading.Event,
) -> None:
    ledger = _ledger()
    job_item.status = "running"
    job_item.started_at = _now()
    job_item.progress = 0.02
    _save(job_item, ledger)

    ctx = _RunContext(job_item, cancel_event, ledger)
    token = _current.set(ctx)
    monitor = _Monitor(job_item, cancel_event, ledger)
    monitor.start()
    started = time.monotonic()
    try:
        kwargs = _filter_params(func, params)
        accepted = _accepted_params(func)
        if not accepted or "deadline_s" in accepted:
            kwargs.setdefault("deadline_s", deadline_s)
        if "cursor" in accepted and "cursor" not in params:
            resume = _resume_point(job_item, ledger)
            if resume:
                kwargs["cursor"] = resume
        report = func(**kwargs)
        if isinstance(report, dict):
            report = IngestReport(**report)
        if not isinstance(report, IngestReport):
            raise IngestError(f"{func.__name__} 没有返回 IngestReport")
        if cancel_event.is_set():
            report.notes.append(
                f"已取消，已完成 {job_item.done or 0}/{job_item.total or '?'}；再次运行会从断点继续"
            )
        elif time.monotonic() - started >= deadline_s:
            report.notes.append(f"已达单次时限 {deadline_s:.0f}s，剩余区间请再次运行")
        _finish(
            job_item, report=report, error=None, cancel_event=cancel_event, ledger=ledger
        )
    except Exception as exc:  # noqa: BLE001 - the job record is the report
        logger.exception("ingest job %s (%s) failed", job_item.id, job_item.dataset)
        _finish(
            job_item, report=None, error=str(exc) or exc.__class__.__name__, ledger=ledger
        )
    except BaseException:
        # Ctrl-C: record it, then let the interrupt behave like an interrupt.
        logger.warning("ingest job %s interrupted", job_item.id)
        _finish(
            job_item,
            report=None,
            error="进程中断（Ctrl-C 或信号）",
            cancel_event=cancel_event,
            ledger=ledger,
        )
        raise
    finally:
        monitor.stop()
        _current.reset(token)


def _new_job(name: str, params: dict[str, Any]) -> IngestJob:
    """Claim the dataset in the ledger and register the job locally.

    Raises:
        IngestBusy: Another live job — in this process or another — owns it.
    """
    job_item = IngestJob(
        id=uuid.uuid4().hex[:12],
        dataset=name,
        params={k: v for k, v in params.items() if v is not None},
        queued_at=_now(),
        status="queued",
        message="排队中",
        job_key=_job_key(name, params),
        owner=owner_id(),
    )
    ledger = _ledger()
    try:
        ledger.claim(job_item.to_dict())
    except JobBusy as exc:
        raise IngestBusy(
            f"{name} 正在入库（任务 {exc.job_id}{'，' + exc.owner if exc.owner else ''}），请等待其完成"
        ) from exc
    _save(job_item, ledger)
    with _lock:
        _live[job_item.id] = job_item
        _cancel_events[job_item.id] = threading.Event()
    return job_item


def run(name: str, *, deadline_s: float = DEFAULT_DEADLINE_S, **params: Any) -> dict[str, Any]:
    """Run one ingest synchronously and return the finished job.

    Raises:
        IngestBusy: A job for this dataset is already running.
        IngestError: The dataset is not ingestible.
    """
    func = _callable_for(name)
    job_item = _new_job(name, params)
    _execute(job_item, func, params, deadline_s, _cancel_events[job_item.id])
    return job(job_item.id) or job_item.to_dict()


def submit(
    name: str, *, deadline_s: float = DEFAULT_DEADLINE_S, **params: Any
) -> dict[str, Any]:
    """Start one ingest on a worker thread; returns the queued job.

    The HTTP layer must not block for minutes, and a backfill is exactly that,
    so the route returns the job id and the page polls it.
    """
    func = _callable_for(name)
    job_item = _new_job(name, params)
    cancel_event = _cancel_events[job_item.id]
    thread = threading.Thread(
        target=_execute,
        args=(job_item, func, params, deadline_s, cancel_event),
        name=f"ingest-{name}",
        daemon=True,
    )
    thread.start()
    return job_item.to_dict()
