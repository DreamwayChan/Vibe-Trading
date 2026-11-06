"""Decision journal: hard fields required before a signal is tradable.

A monitor signal is tradable only when it carries a hypothesis link or an
inline thesis, an invalidation condition, a planned position, a planned risk,
and a timestamp. Missing invalidation downgrades the signal (``tradable``
stays false). ``strict=True`` rejects the write instead of storing the
downgrade.

The validation gate is separate: see :func:`backtest.validation.evaluate_validation_gate`.
This module only enforces the journal fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX advisory lock (Linux/macOS).
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows byte-range lock.
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

from src.config.accessor import get_env_config
from src.hypotheses.registry import Hypothesis, HypothesisRegistry


_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.02
_THREAD_LOCK = threading.RLock()


def default_journal_path() -> Path:
    """Return the decision-journal JSON path.

    ``VIBE_TRADING_DECISION_JOURNAL_PATH`` overrides the default
    ``~/.vibe-trading/decision_journal.json``.
    """
    override = get_env_config().paths.vibe_trading_decision_journal_path.strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe-trading" / "decision_journal.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _side_or_blank(value: Any) -> str:
    text = _text(value).lower()
    return text if text in ("below", "above") else ""


def _positive_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
    return str(value).strip()


def parse_timestamp(value: str) -> str | None:
    """Return a UTC ``Z`` timestamp, ``""`` when blank, or None when invalid."""
    text = value.strip()
    if not text:
        return ""
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(parsed)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assess_journal_fields(
    *,
    hypothesis_id: str,
    thesis: str,
    invalidation: str,
    planned_position: str,
    planned_risk: str,
    timestamp: str,
    hypothesis_found: bool,
) -> list[str]:
    """Return stable reason codes. Empty means the journal fields are tradable.

    Invalidation is required on the signal itself or, when a hypothesis link
    resolves, on that hypothesis's ``invalidation_notes``. A blank invalidation
    is always ``missing_invalidation``.
    """
    reasons: list[str] = []
    if hypothesis_id and not hypothesis_found:
        reasons.append("hypothesis_not_found")
    # A resolved hypothesis link counts only when it actually carries a thesis.
    if not thesis:
        reasons.append("missing_thesis")
    if not invalidation:
        reasons.append("missing_invalidation")
    if not planned_position:
        reasons.append("missing_position")
    if not planned_risk:
        reasons.append("missing_risk")
    if timestamp == "INVALID":
        reasons.append("invalid_timestamp")
    elif not timestamp:
        reasons.append("missing_timestamp")
    return reasons


@dataclass
class DecisionSignal:
    """One journal row. ``tradable`` is false when any hard field is missing."""

    signal_id: str
    timestamp: str
    run_id: str
    hypothesis_id: str = ""
    thesis: str = ""
    invalidation: str = ""
    planned_position: str = ""
    planned_risk: str = ""
    symbol: str = ""
    tradable: bool = False
    block_reasons: list[str] = field(default_factory=list)
    # Optional machine-checkable form of ``invalidation`` (per-symbol only).
    invalidation_side: str = ""
    invalidation_price: float | None = None

    def breached_by(self, price: float | None) -> bool:
        """True when ``price`` has crossed the structured invalidation level."""
        if price is None or self.invalidation_price is None or price != price:
            return False
        if self.invalidation_side == "below":
            return price < self.invalidation_price
        if self.invalidation_side == "above":
            return price > self.invalidation_price
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionSignal":
        reasons = data.get("block_reasons") or []
        if not isinstance(reasons, list):
            reasons = []
        return cls(
            signal_id=str(data.get("signal_id") or ""),
            timestamp=str(data.get("timestamp") or ""),
            run_id=str(data.get("run_id") or ""),
            hypothesis_id=str(data.get("hypothesis_id") or ""),
            thesis=str(data.get("thesis") or ""),
            invalidation=str(data.get("invalidation") or ""),
            planned_position=str(data.get("planned_position") or ""),
            planned_risk=str(data.get("planned_risk") or ""),
            symbol=str(data.get("symbol") or "").strip().upper(),
            tradable=bool(data.get("tradable", False)),
            block_reasons=[str(item) for item in reasons if str(item).strip()],
            invalidation_side=_side_or_blank(data.get("invalidation_side")),
            invalidation_price=_positive_or_none(data.get("invalidation_price")),
        )


class DecisionJournal:
    """Append-only JSON journal of monitor decision signals."""

    def __init__(
        self,
        path: Path | None = None,
        registry: HypothesisRegistry | None = None,
    ) -> None:
        self.path = path or default_journal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry if registry is not None else HypothesisRegistry()

    def record(
        self,
        *,
        run_id: str,
        hypothesis_id: str = "",
        thesis: str = "",
        invalidation: str = "",
        planned_position: str = "",
        planned_risk: str = "",
        timestamp: str = "",
        symbol: str = "",
        strict: bool = False,
        invalidation_side: str = "",
        invalidation_price: float | None = None,
    ) -> DecisionSignal:
        """Append a signal. Missing invalidation downgrades unless ``strict``.

        ``strict=True`` raises :class:`ValueError` and does not write when the
        signal cannot be tradable.

        ``invalidation_side`` (``below``/``above``) with ``invalidation_price``
        is an optional machine-checkable form of the invalidation text; the
        monitor flags the signal ``invalidated`` once the price crosses it.

        Raises:
            ValueError: ``run_id`` is blank, the invalidation rule is
                incomplete or lacks a symbol, or ``strict`` and the signal is
                not tradable.
        """
        run_key = _text(run_id)
        if not run_key:
            raise ValueError("run_id is required")
        side = _side_or_blank(invalidation_side)
        level = _positive_or_none(invalidation_price)
        if _text(invalidation_side) and not side:
            raise ValueError("invalidation_side must be 'below' or 'above'")
        if invalidation_price is not None and level is None:
            raise ValueError("invalidation_price must be a positive number")
        if bool(side) != (level is not None):
            raise ValueError("invalidation_side and invalidation_price must be set together")
        if side and not _text(symbol):
            raise ValueError("a structured invalidation level needs a symbol")

        linked_id = _text(hypothesis_id)
        thesis_text = _text(thesis)
        invalidation_text = _text(invalidation)
        position_text = _text(planned_position)
        risk_text = _text(planned_risk)
        symbol_text = _text(symbol).upper()

        hypothesis_found = False
        if linked_id:
            hypothesis = self._find(linked_id)
            hypothesis_found = hypothesis is not None
            if hypothesis is not None:
                if not thesis_text:
                    thesis_text = hypothesis.thesis.strip()
                if not invalidation_text:
                    invalidation_text = hypothesis.invalidation_notes.strip()

        parsed = parse_timestamp(_text(timestamp))
        if parsed is None:
            timestamp_text = "INVALID"
        elif parsed == "":
            timestamp_text = _utc_now()
        else:
            timestamp_text = parsed

        reasons = assess_journal_fields(
            hypothesis_id=linked_id,
            thesis=thesis_text,
            invalidation=invalidation_text,
            planned_position=position_text,
            planned_risk=risk_text,
            timestamp=timestamp_text,
            hypothesis_found=hypothesis_found,
        )
        if strict and reasons:
            raise ValueError("not tradable: " + ", ".join(reasons))

        stored_timestamp = _utc_now() if timestamp_text == "INVALID" else timestamp_text
        signal = DecisionSignal(
            signal_id=_new_signal_id(run_key, symbol_text, stored_timestamp),
            timestamp=stored_timestamp if timestamp_text != "INVALID" else timestamp_text,
            run_id=run_key,
            hypothesis_id=linked_id,
            thesis=thesis_text,
            invalidation=invalidation_text,
            planned_position=position_text,
            planned_risk=risk_text,
            symbol=symbol_text,
            tradable=not reasons,
            block_reasons=reasons,
            invalidation_side=side,
            invalidation_price=level,
        )
        if timestamp_text == "INVALID":
            # Keep the caller's raw value visible; tradable stays false.
            signal.timestamp = _text(timestamp) or "INVALID"
        with self._locked():
            records = self.list()
            records.append(signal)
            self._save(records)
        return signal

    def list(self) -> list[DecisionSignal]:
        """Load every journal row. Missing file is an empty journal."""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid decision journal JSON: {self.path}") from exc
        if not isinstance(raw, list):
            raise ValueError("decision journal storage must contain a JSON list")
        return [DecisionSignal.from_dict(item) for item in raw if isinstance(item, dict)]

    def latest_for(self, run_id: str) -> tuple[DecisionSignal | None, dict[str, DecisionSignal]]:
        """Latest strategy-level signal and the latest signal per symbol.

        A blank ``symbol`` covers the whole strategy. A per-symbol row overrides
        that blanket for one name and does not fall back when it is blocked.
        """
        key = _text(run_id)
        strategy: DecisionSignal | None = None
        by_symbol: dict[str, DecisionSignal] = {}
        for signal in self.list():
            if signal.run_id != key:
                continue
            if not signal.symbol:
                if strategy is None or signal.timestamp >= strategy.timestamp:
                    strategy = signal
            else:
                previous = by_symbol.get(signal.symbol)
                if previous is None or signal.timestamp >= previous.timestamp:
                    by_symbol[signal.symbol] = signal
        return strategy, by_symbol

    def history(
        self,
        run_id: str,
        *,
        symbol: str = "",
        limit: int = 50,
    ) -> list[DecisionSignal]:
        """Newest-first signals for one run, optionally narrowed to one symbol."""
        key = _text(run_id)
        sym = _text(symbol).upper()
        # Reverse first so equal timestamps keep newest-written-first order.
        rows = [
            signal
            for signal in reversed(self.list())
            if signal.run_id == key and (not sym or signal.symbol == sym)
        ]
        rows.sort(key=lambda signal: signal.timestamp, reverse=True)
        return rows[: max(0, limit)]

    def _find(self, hypothesis_id: str) -> Hypothesis | None:
        for hypothesis in self.registry.list():
            if hypothesis.hypothesis_id == hypothesis_id:
                return hypothesis
        return None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-modify-write across threads and processes.

        Raises:
            TimeoutError: the lock could not be acquired in time.
        """
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with _THREAD_LOCK:
            with open(lock_path, "a+b") as handle:
                deadline = time.monotonic() + _LOCK_TIMEOUT_S
                while True:
                    try:
                        _try_lock(handle)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"decision journal is locked: {lock_path}"
                            ) from None
                        time.sleep(_LOCK_POLL_S)
                try:
                    yield
                finally:
                    _unlock(handle)

    def _save(self, records: list[DecisionSignal]) -> None:
        payload = [signal.to_dict() for signal in records]
        # Unique temp name so concurrent writers never share a half-written file.
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            tmp_path.replace(self.path)
        finally:
            tmp_path.unlink(missing_ok=True)


def _try_lock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    raise OSError("no supported advisory lock backend")


def _unlock(handle: Any) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _new_signal_id(run_id: str, symbol: str, timestamp: str) -> str:
    seed = f"{run_id}|{symbol}|{timestamp}|{uuid.uuid4()}"
    return "sig_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
