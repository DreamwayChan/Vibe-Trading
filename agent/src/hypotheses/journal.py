"""Decision journal: hard fields required before a signal is tradable.

A monitor signal is tradable only when it carries the five hypothesis fields
(observation, a causal hypothesis, logic, a validation plan, and invalidation),
plus a thesis or hypothesis link, a planned position, a planned risk, and a
timestamp. A missing field downgrades the signal (``tradable`` stays false).
``strict=True`` rejects the write instead of storing the downgrade.

A linked hypothesis supplies any of those texts it already has. A causal
thesis can fill a blank causal hypothesis, and a causal hypothesis can fill a
blank thesis.

Optional machine invalidation (price side and level, or a deviation / decay
threshold) is stored beside the text. The monitor and the paper book trip it.

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
from src.hypotheses.fields import assess_five_fields, looks_causal, parse_machine_invalidation
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


def _clean_outcome(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        pnl = float(value.get("pnl_pct"))
    except (TypeError, ValueError):
        return None
    if pnl != pnl or pnl in (float("inf"), float("-inf")):
        return None
    return {
        "pnl_pct": pnl,
        "note": str(value.get("note") or "").strip(),
        "recorded_at": str(value.get("recorded_at") or ""),
    }


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
    observation: str = "",
    hypothesis: str = "",
    logic: str = "",
    validation_plan: str = "",
) -> list[str]:
    """Return stable reason codes. Empty means the journal fields are tradable.

    The five hypothesis fields are required on the signal or, when a hypothesis
    link resolves, on that hypothesis. A blank invalidation is always
    ``missing_invalidation``.
    """
    reasons: list[str] = []
    if hypothesis_id and not hypothesis_found:
        reasons.append("hypothesis_not_found")
    reasons.extend(
        assess_five_fields(
            observation=observation,
            hypothesis=hypothesis,
            logic=logic,
            validation_plan=validation_plan,
            invalidation=invalidation,
        )
    )
    # A resolved hypothesis link counts only when it actually carries a thesis.
    if not thesis:
        reasons.append("missing_thesis")
    if not planned_position:
        reasons.append("missing_position")
    if not planned_risk:
        reasons.append("missing_risk")
    if timestamp == "INVALID":
        reasons.append("invalid_timestamp")
    elif not timestamp:
        reasons.append("missing_timestamp")
    return reasons


def journal_max_age_days() -> int:
    """Configured journal expiry in days; 0 means entries never expire."""
    return int(get_env_config().paths.vibe_trading_decision_journal_max_age_days)


def is_stale(signal: "DecisionSignal", *, max_age_days: int | None = None, now: datetime | None = None) -> bool:
    """True when ``signal`` is older than the expiry window.

    An unparseable timestamp is never reported stale here; it already carries
    ``invalid_timestamp``.
    """
    limit = journal_max_age_days() if max_age_days is None else max_age_days
    if limit <= 0:
        return False
    parsed = parse_timestamp(signal.timestamp)
    if not parsed:
        return False
    moment = datetime.fromisoformat(parsed[:-1] + "+00:00")
    reference = now or datetime.now(timezone.utc)
    return (reference - moment).total_seconds() > limit * 86400


def signal_age_days(signal: "DecisionSignal", *, now: datetime | None = None) -> int | None:
    """Whole days since ``signal.timestamp``; None when it cannot be parsed.

    Used by read paths that must show the same expiry the admission gate
    applies. ``is_stale`` answers yes/no; this answers "how old", so a history
    row can say why it no longer counts instead of silently looking valid.
    """
    parsed = parse_timestamp(signal.timestamp)
    if not parsed:
        return None
    moment = datetime.fromisoformat(parsed[:-1] + "+00:00")
    reference = now or datetime.now(timezone.utc)
    return max(0, int((reference - moment).total_seconds() // 86400))


@dataclass
class DecisionSignal:
    """One journal row. ``tradable`` is false when any hard field is missing."""

    signal_id: str
    timestamp: str
    run_id: str
    hypothesis_id: str = ""
    thesis: str = ""
    observation: str = ""
    hypothesis: str = ""
    logic: str = ""
    validation_plan: str = ""
    invalidation: str = ""
    planned_position: str = ""
    planned_risk: str = ""
    symbol: str = ""
    tradable: bool = False
    block_reasons: list[str] = field(default_factory=list)
    # Optional machine-checkable form of ``invalidation``.
    invalidation_kind: str = ""
    invalidation_side: str = ""
    invalidation_level: float | None = None
    invalidation_price: float | None = None
    # Realised result recorded after the trade: {"pnl_pct", "note", "recorded_at"}.
    outcome: dict[str, Any] | None = None

    def breached_by(self, price: float | None) -> bool:
        """True when ``price`` has crossed a structured price invalidation."""
        if self.invalidation_kind != "price":
            return False
        level = self.invalidation_level
        if level is None:
            level = self.invalidation_price
        if price is None or level is None or price != price:
            return False
        if self.invalidation_side == "below":
            return price < level
        if self.invalidation_side == "above":
            return price > level
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionSignal":
        reasons = data.get("block_reasons") or []
        if not isinstance(reasons, list):
            reasons = []
        kind, side, level = parse_machine_invalidation(
            kind=str(data.get("invalidation_kind") or ""),
            side=str(data.get("invalidation_side") or ""),
            price=data.get("invalidation_price"),
            level=data.get("invalidation_level"),
        )
        return cls(
            signal_id=str(data.get("signal_id") or ""),
            timestamp=str(data.get("timestamp") or ""),
            run_id=str(data.get("run_id") or ""),
            hypothesis_id=str(data.get("hypothesis_id") or ""),
            thesis=str(data.get("thesis") or ""),
            observation=str(data.get("observation") or ""),
            hypothesis=str(data.get("hypothesis") or ""),
            logic=str(data.get("logic") or ""),
            validation_plan=str(data.get("validation_plan") or ""),
            invalidation=str(data.get("invalidation") or ""),
            planned_position=str(data.get("planned_position") or ""),
            planned_risk=str(data.get("planned_risk") or ""),
            symbol=str(data.get("symbol") or "").strip().upper(),
            tradable=bool(data.get("tradable", False)),
            block_reasons=[str(item) for item in reasons if str(item).strip()],
            invalidation_kind=kind,
            invalidation_side=side,
            invalidation_level=level,
            invalidation_price=level if kind == "price" else None,
            outcome=_clean_outcome(data.get("outcome")),
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
        observation: str = "",
        hypothesis: str = "",
        logic: str = "",
        validation_plan: str = "",
        invalidation_kind: str = "",
        invalidation_side: str = "",
        invalidation_price: float | None = None,
        invalidation_level: float | None = None,
    ) -> DecisionSignal:
        """Append a signal. A missing five-field downgrades unless ``strict``.

        ``strict=True`` raises :class:`ValueError` and does not write when the
        signal cannot be tradable.

        ``invalidation_side`` (``below``/``above``) with ``invalidation_price``
        is a machine-checkable price rule. ``invalidation_kind`` of
        ``deviation`` or ``decay`` with ``invalidation_level`` stores the
        threshold the paper book trips. A price rule needs a symbol.

        Raises:
            ValueError: ``run_id`` is blank, the invalidation rule is
                incomplete or lacks a symbol, or ``strict`` and the signal is
                not tradable.
        """
        run_key = _text(run_id)
        if not run_key:
            raise ValueError("run_id is required")

        linked_id = _text(hypothesis_id)
        thesis_text = _text(thesis)
        observation_text = _text(observation)
        hypothesis_text = _text(hypothesis)
        logic_text = _text(logic)
        plan_text = _text(validation_plan)
        invalidation_text = _text(invalidation)
        position_text = _text(planned_position)
        risk_text = _text(planned_risk)
        symbol_text = _text(symbol).upper()

        caller_set_rule = any(
            (
                _text(invalidation_kind),
                _text(invalidation_side),
                invalidation_price is not None,
                invalidation_level is not None,
            )
        )
        rule_kind = invalidation_kind
        rule_side = invalidation_side
        rule_price = invalidation_price
        rule_level = invalidation_level

        hypothesis_found = False
        linked: Hypothesis | None = None
        if linked_id:
            linked = self._find(linked_id)
            hypothesis_found = linked is not None
            if linked is not None:
                if not thesis_text:
                    thesis_text = linked.thesis.strip()
                if not observation_text:
                    observation_text = linked.observation.strip()
                if not hypothesis_text:
                    hypothesis_text = linked.hypothesis.strip()
                if not logic_text:
                    logic_text = linked.logic.strip()
                if not plan_text:
                    plan_text = linked.validation_plan.strip()
                if not invalidation_text:
                    invalidation_text = (linked.invalidation or linked.invalidation_notes).strip()
                if not caller_set_rule and linked.invalidation_kind:
                    if linked.invalidation_kind != "price" or symbol_text:
                        rule_kind = linked.invalidation_kind
                        rule_side = linked.invalidation_side
                        rule_level = linked.invalidation_level
                        caller_set_rule = True

        if not hypothesis_text and thesis_text and looks_causal(thesis_text):
            hypothesis_text = thesis_text
        if not thesis_text and hypothesis_text:
            thesis_text = hypothesis_text

        kind, side, level = parse_machine_invalidation(
            kind=rule_kind,
            side=rule_side,
            price=rule_price,
            level=rule_level,
        )
        if kind == "price" and not symbol_text:
            raise ValueError("a structured invalidation level needs a symbol")

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
            observation=observation_text,
            hypothesis=hypothesis_text,
            logic=logic_text,
            validation_plan=plan_text,
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
            observation=observation_text,
            hypothesis=hypothesis_text,
            logic=logic_text,
            validation_plan=plan_text,
            invalidation=invalidation_text,
            planned_position=position_text,
            planned_risk=risk_text,
            symbol=symbol_text,
            tradable=not reasons,
            block_reasons=reasons,
            invalidation_kind=kind,
            invalidation_side=side,
            invalidation_level=level,
            invalidation_price=level if kind == "price" else None,
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

    def record_outcome(self, signal_id: str, *, pnl_pct: float, note: str = "") -> DecisionSignal:
        """Attach the realised result (percent) to an existing entry.

        Raises:
            KeyError: no entry has ``signal_id``.
            ValueError: ``pnl_pct`` is not a finite number.
        """
        outcome = _clean_outcome({"pnl_pct": pnl_pct, "note": note})
        if outcome is None:
            raise ValueError("pnl_pct must be a finite number")
        outcome["recorded_at"] = _utc_now()
        key = _text(signal_id)
        with self._locked():
            records = self.list()
            for record in records:
                if record.signal_id == key:
                    record.outcome = outcome
                    self._save(records)
                    return record
        raise KeyError(key)

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


def outcome_summary(signals: list[DecisionSignal]) -> dict[str, Any]:
    """Count reviewed entries and win rate / mean PnL over them."""
    pnls = [float(signal.outcome["pnl_pct"]) for signal in signals if signal.outcome]
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "reviewed": len(pnls),
        "wins": wins,
        "win_rate": wins / len(pnls) if pnls else None,
        "mean_pnl_pct": sum(pnls) / len(pnls) if pnls else None,
    }


def machine_levels(run_id: str, *, journal: DecisionJournal | None = None) -> dict[str, float | None]:
    """Tightest deviation and decay thresholds on the latest journal rows.

    Returns ``{"deviation": float | None, "decay": float | None}``. A missing
    journal or an unreadable file leaves both unset so callers keep their
    global defaults.
    """
    book = journal if journal is not None else DecisionJournal()
    found: dict[str, float | None] = {"deviation": None, "decay": None}
    try:
        strategy, by_symbol = book.latest_for(run_id)
    except ValueError:
        return found
    rows = [strategy] if strategy is not None else []
    rows.extend(by_symbol.values())
    for row in rows:
        if row.invalidation_kind not in ("deviation", "decay"):
            continue
        level = row.invalidation_level
        if level is None:
            continue
        current = found[row.invalidation_kind]
        if current is None or level < current:
            found[row.invalidation_kind] = level
    return found


def price_breaches(
    run_id: str,
    prices: dict[str, float],
    *,
    journal: DecisionJournal | None = None,
) -> list[str]:
    """Symbols whose latest journal row is crossed by ``prices``."""
    if not run_id:
        return []
    book = journal if journal is not None else DecisionJournal()
    try:
        _strategy, by_symbol = book.latest_for(run_id)
    except ValueError:
        return []
    hit: list[str] = []
    for symbol, entry in by_symbol.items():
        price = prices.get(symbol)
        if entry.breached_by(price) and symbol not in hit:
            hit.append(symbol)
    return hit
