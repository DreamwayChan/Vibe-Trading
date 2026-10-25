"""Structured run-report extras: per-symbol PnL and strategy-fit assessment.

Built on read from persisted artifacts so historical runs (which never wrote
``by_symbol`` into metrics.csv) still get a breakdown without re-backtesting.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce CSV / JSON cell values to float."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce CSV / JSON cell values to int."""
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _is_exit_row(row: Dict[str, Any]) -> bool:
    """Return True for trade rows that realize a round-trip (exit fills).

    ``trades.csv`` stores entry+exit pairs. Entries have ``holding_days=0`` and
    ``pnl=0``; exits carry realised ``pnl`` / ``return_pct`` / ``holding_days``.
    Prefer holding_days so breakeven exits (pnl==0) are still counted.
    """
    holding_days = _as_float(row.get("holding_days"), 0.0)
    if holding_days > 0:
        return True
    pnl = _as_float(row.get("pnl"), 0.0)
    if pnl != 0.0:
        return True
    side = str(row.get("side") or "").strip().lower()
    return side in {"sell", "cover", "close", "exit"}


def _exit_rows(trade_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter raw trade CSV rows down to realised exits."""
    return [row for row in trade_rows if _is_exit_row(row)]


def build_symbol_breakdown(
    trade_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Aggregate per-symbol PnL from ``trades.csv`` exit rows.

    Args:
        trade_rows: Raw trade dicts (entry + exit rows allowed).

    Returns:
        Ranked list (total_pnl desc) with count, win_rate, total/avg pnl,
        avg return %, avg holding days, and contribution % of absolute PnL.
    """
    exits = _exit_rows(trade_rows)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in exits:
        code = str(row.get("code") or row.get("symbol") or "").strip()
        if not code:
            continue
        groups.setdefault(code, []).append(row)

    abs_total = sum(abs(_as_float(row.get("pnl"))) for row in exits) or 0.0
    rows: List[Dict[str, Any]] = []
    for code, sym_rows in groups.items():
        pnls = [_as_float(r.get("pnl")) for r in sym_rows]
        returns = [_as_float(r.get("return_pct")) for r in sym_rows]
        holdings = [_as_float(r.get("holding_days")) for r in sym_rows]
        wins = [p for p in pnls if p > 0]
        total_pnl = round(sum(pnls), 2)
        count = len(sym_rows)
        rows.append(
            {
                "code": code,
                "count": count,
                "win_rate": round(len(wins) / count, 4) if count else 0.0,
                "total_pnl": total_pnl,
                "avg_pnl": round(sum(pnls) / count, 2) if count else 0.0,
                "avg_return_pct": round(sum(returns) / count, 4) if count else 0.0,
                "avg_holding_days": round(sum(holdings) / count, 2) if count else 0.0,
                "contribution_pct": (
                    round(abs(total_pnl) / abs_total, 4) if abs_total else 0.0
                ),
            }
        )

    rows.sort(key=lambda item: item["total_pnl"], reverse=True)
    return rows


def build_exit_reason_breakdown(
    trade_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Aggregate exit-reason stats from realised trade rows.

    Args:
        trade_rows: Raw trade dicts.

    Returns:
        List of ``{reason, count, total_pnl, avg_pnl, win_rate}`` sorted by
        absolute total_pnl descending.
    """
    exits = _exit_rows(trade_rows)
    groups: Dict[str, List[float]] = {}
    for row in exits:
        reason = str(row.get("reason") or "unknown").strip() or "unknown"
        groups.setdefault(reason, []).append(_as_float(row.get("pnl")))

    rows: List[Dict[str, Any]] = []
    for reason, pnls in groups.items():
        wins = [p for p in pnls if p > 0]
        count = len(pnls)
        total_pnl = round(sum(pnls), 2)
        rows.append(
            {
                "reason": reason,
                "count": count,
                "total_pnl": total_pnl,
                "avg_pnl": round(sum(pnls) / count, 2) if count else 0.0,
                "win_rate": round(len(wins) / count, 4) if count else 0.0,
            }
        )
    rows.sort(key=lambda item: abs(item["total_pnl"]), reverse=True)
    return rows


def _holding_buckets(exits: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bucket exits by holding horizon (short / medium / long)."""
    buckets = {
        "short": {"label": "short_<3d", "count": 0, "total_pnl": 0.0},
        "medium": {"label": "medium_3_20d", "count": 0, "total_pnl": 0.0},
        "long": {"label": "long_>20d", "count": 0, "total_pnl": 0.0},
    }
    for row in exits:
        days = _as_float(row.get("holding_days"))
        pnl = _as_float(row.get("pnl"))
        if days < 3:
            key = "short"
        elif days <= 20:
            key = "medium"
        else:
            key = "long"
        buckets[key]["count"] += 1
        buckets[key]["total_pnl"] = round(buckets[key]["total_pnl"] + pnl, 2)
    return [
        {
            "bucket": meta["label"],
            "count": meta["count"],
            "total_pnl": meta["total_pnl"],
        }
        for meta in buckets.values()
    ]


def _top_trades(
    exits: Sequence[Dict[str, Any]],
    *,
    limit: int = 5,
    winners: bool,
) -> List[Dict[str, Any]]:
    """Return top winner or loser exit rows by pnl."""
    ranked = sorted(
        exits,
        key=lambda row: _as_float(row.get("pnl")),
        reverse=winners,
    )
    selected = ranked[:limit]
    if winners:
        selected = [row for row in selected if _as_float(row.get("pnl")) > 0]
    else:
        selected = [row for row in selected if _as_float(row.get("pnl")) < 0]
    return [
        {
            "code": str(row.get("code") or ""),
            "side": str(row.get("side") or ""),
            "timestamp": str(row.get("timestamp") or row.get("time") or ""),
            "pnl": round(_as_float(row.get("pnl")), 2),
            "return_pct": round(_as_float(row.get("return_pct")), 4),
            "holding_days": round(_as_float(row.get("holding_days")), 2),
            "reason": str(row.get("reason") or ""),
        }
        for row in selected
    ]


def _planned_codes(
    *,
    config: Optional[Dict[str, Any]],
    run_card: Optional[Dict[str, Any]],
    strategy_spec: Optional[Dict[str, Any]],
    run_context: Optional[Dict[str, Any]],
) -> List[str]:
    """Best-effort universe from config / run_card / design / context."""
    candidates: Iterable[Any] = ()
    if isinstance(config, dict) and config.get("codes"):
        candidates = config.get("codes") or []
    elif isinstance(run_card, dict):
        backtest = run_card.get("backtest") or {}
        if isinstance(backtest, dict) and backtest.get("codes"):
            candidates = backtest.get("codes") or []
    if not candidates and isinstance(strategy_spec, dict):
        for key in ("codes", "universe", "symbols"):
            value = strategy_spec.get(key)
            if isinstance(value, list) and value:
                candidates = value
                break
    if not candidates and isinstance(run_context, dict):
        value = run_context.get("codes")
        if isinstance(value, list):
            candidates = value
    return [str(code).strip() for code in candidates if str(code).strip()]


def _check(
    check_id: str,
    label: str,
    status: str,
    detail: str,
    score: int,
) -> Dict[str, Any]:
    """Build one assessment checklist row."""
    return {
        "id": check_id,
        "label": label,
        "status": status,  # pass | warn | fail | skip
        "detail": detail,
        "score": max(0, min(100, int(score))),
    }


def _grade_for_score(score: int) -> str:
    """Map 0–100 score to a coarse grade label."""
    if score >= 80:
        return "excellent"
    if score >= 60:
        return "good"
    if score >= 40:
        return "fair"
    return "poor"


def build_strategy_assessment(
    *,
    trade_rows: Sequence[Dict[str, Any]],
    metrics: Optional[Dict[str, Any]] = None,
    validation: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    run_card: Optional[Dict[str, Any]] = None,
    strategy_spec: Optional[Dict[str, Any]] = None,
    run_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score how well the run results fit a usable strategy profile.

    This is a structured, artifact-based checklist — not an LLM opinion.
    Dimensions reuse existing metrics/validation/trades rather than inventing
    new return formulas.

    Args:
        trade_rows: Raw trades.csv rows.
        metrics: Portfolio metrics (csv row or BacktestMetrics dict).
        validation: Optional validation.json payload.
        config: Optional config.json.
        run_card: Optional run_card.json.
        strategy_spec: Optional design_spec.json.
        run_context: Optional normalized request context.

    Returns:
        Assessment payload with score, grade, checks, and attribution highlights.
    """
    metrics = dict(metrics or {})
    exits = _exit_rows(trade_rows)
    checks: List[Dict[str, Any]] = []

    # --- Performance (Sharpe / MaxDD / total return bands) ---
    sharpe = _as_float(metrics.get("sharpe"), float("nan"))
    max_dd = abs(_as_float(metrics.get("max_drawdown"), float("nan")))
    total_return = _as_float(metrics.get("total_return"), float("nan"))
    if sharpe == sharpe:  # not NaN
        dd_txt = f"{max_dd:.1%}" if max_dd == max_dd else "n/a"
        ret_txt = f"{total_return:.1%}" if total_return == total_return else "n/a"
        if sharpe >= 1.0 and (max_dd != max_dd or max_dd < 0.20):
            checks.append(
                _check(
                    "performance",
                    "Result quality",
                    "pass",
                    f"Sharpe {sharpe:.2f}, MaxDD {dd_txt}, total return {ret_txt}",
                    85 if sharpe >= 1.5 else 70,
                )
            )
        elif sharpe > 0.5 and (max_dd != max_dd or max_dd < 0.40):
            checks.append(
                _check(
                    "performance",
                    "Result quality",
                    "warn",
                    f"Sub-optimal: Sharpe {sharpe:.2f}, MaxDD {dd_txt}",
                    45,
                )
            )
        else:
            checks.append(
                _check(
                    "performance",
                    "Result quality",
                    "fail",
                    f"At-risk: Sharpe {sharpe:.2f}, MaxDD {dd_txt}, total return {ret_txt}",
                    15,
                )
            )
    else:
        checks.append(
            _check("performance", "Result quality", "skip", "No portfolio metrics available", 0)
        )

    # --- Statistical robustness from validation.json when present ---
    if isinstance(validation, dict) and validation:
        mc = validation.get("monte_carlo") if isinstance(validation.get("monte_carlo"), dict) else None
        wf = validation.get("walk_forward") if isinstance(validation.get("walk_forward"), dict) else None
        boot = validation.get("bootstrap") if isinstance(validation.get("bootstrap"), dict) else None
        parts: List[str] = []
        score = 55
        status = "warn"
        if mc and mc.get("error") is None:
            p_sharpe = _as_float(mc.get("p_value_sharpe"), 1.0)
            parts.append(f"MC p(Sharpe)={p_sharpe:.3f}")
            if p_sharpe <= 0.05:
                score += 20
                status = "pass"
            elif p_sharpe <= 0.20:
                score += 5
            else:
                score -= 15
                status = "fail"
        if wf and wf.get("error") is None:
            consistency = _as_float(wf.get("consistency_rate"), 0.0)
            parts.append(f"WF consistency={consistency:.0%}")
            if consistency >= 0.7:
                score += 15
                status = "pass" if status != "fail" else status
            elif consistency < 0.5:
                score -= 10
                status = "fail"
        if boot and boot.get("error") is None:
            prob_pos = _as_float(boot.get("prob_positive"), 0.0)
            parts.append(f"bootstrap P(+Sharpe)={prob_pos:.0%}")
            if prob_pos >= 0.7:
                score += 10
        checks.append(
            _check(
                "robustness",
                "Statistical robustness",
                status if parts else "skip",
                "; ".join(parts) if parts else "Validation present but empty",
                score if parts else 0,
            )
        )
    else:
        checks.append(
            _check(
                "robustness",
                "Statistical robustness",
                "skip",
                "No validation.json — run Monte Carlo / walk-forward for stronger evidence",
                40,
            )
        )

    # --- PnL concentration ---
    if exits:
        ranked = sorted(exits, key=lambda row: _as_float(row.get("pnl")), reverse=True)
        total_pnl = sum(_as_float(row.get("pnl")) for row in exits)
        top5 = ranked[:5]
        top5_pnl = sum(_as_float(row.get("pnl")) for row in top5)
        # Share of positive PnL coming from the five biggest winners
        positive_pnl = sum(p for p in (_as_float(r.get("pnl")) for r in exits) if p > 0)
        top5_share = (top5_pnl / positive_pnl) if positive_pnl > 0 else 0.0
        without_top5 = total_pnl - top5_pnl
        if top5_share >= 0.8 and positive_pnl > 0:
            checks.append(
                _check(
                    "concentration",
                    "PnL concentration",
                    "fail",
                    f"Top-5 winners contribute {top5_share:.0%} of profitable PnL; "
                    f"PnL without them = {without_top5:,.2f}",
                    20,
                )
            )
        elif top5_share >= 0.5 and positive_pnl > 0:
            checks.append(
                _check(
                    "concentration",
                    "PnL concentration",
                    "warn",
                    f"Top-5 winners contribute {top5_share:.0%} of profitable PnL; "
                    f"PnL without them = {without_top5:,.2f}",
                    50,
                )
            )
        else:
            checks.append(
                _check(
                    "concentration",
                    "PnL concentration",
                    "pass",
                    f"Top-5 winners contribute {top5_share:.0%} of profitable PnL; "
                    f"PnL without them = {without_top5:,.2f}",
                    80,
                )
            )
    else:
        checks.append(
            _check("concentration", "PnL concentration", "skip", "No realised exits", 0)
        )

    # --- Universe coverage: planned codes vs traded codes ---
    planned = _planned_codes(
        config=config,
        run_card=run_card,
        strategy_spec=strategy_spec,
        run_context=run_context,
    )
    traded = sorted({str(row.get("code") or "").strip() for row in exits if row.get("code")})
    if planned and traded:
        planned_set = set(planned)
        traded_set = set(traded)
        covered = planned_set & traded_set
        coverage = len(covered) / len(planned_set) if planned_set else 0.0
        outside = sorted(traded_set - planned_set)
        if coverage >= 0.5 and not outside:
            status, score = "pass", 80
        elif coverage >= 0.2:
            status, score = "warn", 55
        else:
            status, score = "fail", 25
        detail = (
            f"Traded {len(traded_set)}/{len(planned_set)} planned symbols "
            f"({coverage:.0%} coverage)"
        )
        if outside:
            detail += f"; {len(outside)} outside universe (e.g. {', '.join(outside[:5])})"
        checks.append(_check("universe", "Universe coverage", status, detail, score))
    elif traded:
        checks.append(
            _check(
                "universe",
                "Universe coverage",
                "pass",
                f"{len(traded)} symbols traded (no planned universe recorded)",
                60,
            )
        )
    else:
        checks.append(
            _check("universe", "Universe coverage", "skip", "No trades to compare", 0)
        )

    # --- Holding horizon vs config portfolio.rebalance / avg_holding_days ---
    avg_hold = _as_float(metrics.get("avg_holding_days"), float("nan"))
    if avg_hold != avg_hold and exits:
        avg_hold = sum(_as_float(r.get("holding_days")) for r in exits) / len(exits)
    rebalance = None
    if isinstance(config, dict):
        portfolio = config.get("portfolio")
        if isinstance(portfolio, dict):
            rebalance = str(portfolio.get("rebalance") or "").lower()
    if avg_hold == avg_hold:
        expected = None
        if rebalance in {"daily", "1d"}:
            expected = (1.0, 3.0)
        elif rebalance in {"weekly", "1w"}:
            expected = (3.0, 12.0)
        elif rebalance in {"monthly", "1m"}:
            expected = (15.0, 45.0)
        if expected:
            low, high = expected
            if low <= avg_hold <= high:
                checks.append(
                    _check(
                        "holding",
                        "Holding horizon",
                        "pass",
                        f"Avg holding {avg_hold:.1f}d fits {rebalance} rebalance",
                        80,
                    )
                )
            else:
                checks.append(
                    _check(
                        "holding",
                        "Holding horizon",
                        "warn",
                        f"Avg holding {avg_hold:.1f}d vs expected ~{low:.0f}–{high:.0f}d "
                        f"for {rebalance} rebalance",
                        45,
                    )
                )
        else:
            checks.append(
                _check(
                    "holding",
                    "Holding horizon",
                    "pass",
                    f"Avg holding {avg_hold:.1f} trading days",
                    65,
                )
            )
    else:
        checks.append(
            _check("holding", "Holding horizon", "skip", "No holding-period data", 0)
        )

    # --- Activity / sample size ---
    trade_count = _as_int(metrics.get("trade_count"), len(exits))
    if trade_count >= 30:
        checks.append(
            _check(
                "activity",
                "Sample size",
                "pass",
                f"{trade_count} realised round-trips — enough for basic inference",
                80,
            )
        )
    elif trade_count >= 10:
        checks.append(
            _check(
                "activity",
                "Sample size",
                "warn",
                f"Only {trade_count} round-trips — treat metrics as fragile",
                50,
            )
        )
    elif trade_count > 0:
        checks.append(
            _check(
                "activity",
                "Sample size",
                "fail",
                f"Only {trade_count} round-trips — results are anecdotal",
                20,
            )
        )
    else:
        checks.append(
            _check("activity", "Sample size", "fail", "No realised trades", 0)
        )

    scored = [c for c in checks if c["status"] != "skip"]
    overall = int(round(sum(c["score"] for c in scored) / len(scored))) if scored else 0
    grade = _grade_for_score(overall)

    fail_n = sum(1 for c in checks if c["status"] == "fail")
    warn_n = sum(1 for c in checks if c["status"] == "warn")
    if fail_n and overall < 40:
        summary = (
            f"{fail_n} check(s) failed — strategy fit looks weak on current evidence "
            f"(score {overall})"
        )
    elif fail_n:
        summary = (
            f"Overall {grade} ({overall}/100) but {fail_n} check(s) failed — "
            "review the weak dimensions before trusting the edge"
        )
    elif warn_n:
        summary = (
            f"Overall {grade} ({overall}/100); {warn_n} check(s) need attention — "
            "usable but not fully aligned"
        )
    elif scored:
        summary = (
            f"Overall {grade} ({overall}/100) — results are broadly consistent "
            "with a tradable profile"
        )
    else:
        summary = "Insufficient artifacts to assess strategy fit"

    return {
        "score": overall,
        "grade": grade,
        "summary": summary,
        "checks": checks,
        "highlights": {
            "top_winners": _top_trades(exits, winners=True),
            "top_losers": _top_trades(exits, winners=False),
            "holding_buckets": _holding_buckets(exits),
            "symbol_count": len({str(r.get("code") or "") for r in exits if r.get("code")}),
            "exit_count": len(exits),
        },
    }


def build_run_report_extras(
    *,
    trade_rows: Sequence[Dict[str, Any]],
    metrics: Optional[Dict[str, Any]] = None,
    validation: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    run_card: Optional[Dict[str, Any]] = None,
    strategy_spec: Optional[Dict[str, Any]] = None,
    run_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bundle attribution + assessment for ``GET /runs/{id}``.

    Returns:
        Dict with ``by_symbol``, ``by_exit_reason``, and ``strategy_assessment``.
    """
    return {
        "by_symbol": build_symbol_breakdown(trade_rows),
        "by_exit_reason": build_exit_reason_breakdown(trade_rows),
        "strategy_assessment": build_strategy_assessment(
            trade_rows=trade_rows,
            metrics=metrics,
            validation=validation,
            config=config,
            run_card=run_card,
            strategy_spec=strategy_spec,
            run_context=run_context,
        ),
    }
