"""New F0 confirm search for 半导体 BK1036 and 光刻机 BK0884.

Same process family as the tradable CRO / copper boards: qfq daily bars,
mom sort = gtja191_164, alive GTJA191 confirm legs, weekly 6-name equal
weight, MA20 half-size timing, A-share costs. Reverse-114 stays banned.

Rules fixed before any ranking is read:

* Each board freezes one F0 recipe: the confirm pair (or single, or the
  171+054 baseline, or pure 164) with the highest cost-aware excess on the
  cached engine. No second search if that label later looks worse.
* That label is re-run once on the production SignalEngine (suspension-price
  guard included) with Monte Carlo, bootstrap, and walk-forward.
* Both production reruns are judged once. A rerun qualifies only when the
  validation gate passes and total return is strictly above the equal-weight
  benchmark. If one qualifies, that one is filed. If both qualify, the higher
  excess is filed. If neither qualifies, the higher excess is filed with the
  hypothesis rejected. Confirm legs are not searched again.

Run from agent/:

    python scripts/research_semi_litho.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_AGENT_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from backtest.metrics import calc_bars_per_year  # noqa: E402
from backtest.runner import _AutoLoader, _create_market_engine  # noqa: E402
from backtest.validation import evaluate_run_validation  # noqa: E402
from scripts.research_sector_confirm_search import (  # noqa: E402
    BASELINE_CONFIRM,
    BANNED_CONFIRM,
    FALLBACK_IR,
    MOM_ID,
    _CachedSignalEngine,
    _build_panel,
    _combo_label,
    _precompute_scores,
    _write_signal_engine,
)
from src.factors.survival_store import survival_path  # noqa: E402
from src.data.hub import adjusted_bars  # noqa: E402

START_DATE = "2024-01-01"
END_DATE = "2026-11-23"
MIN_BARS = 120
MAX_UNIVERSE = 80
INITIAL_CASH = 1_000_000.0
PAIR_POOL = 12
RESEARCH_ROOT = _AGENT_ROOT / "runs" / "0research_semi_litho"


@dataclass(frozen=True)
class Board:
    key: str
    bk: str
    name: str
    board_type: str
    risk_note: str

    @property
    def panel(self) -> str:
        return f"{self.name} {self.bk}"

    @property
    def out_root(self) -> Path:
        return RESEARCH_ROOT / self.key


BOARDS = (
    Board(
        key="semi",
        bk="BK1036",
        name="半导体",
        board_type="industry",
        risk_note="资本开支与国产替代节奏、出口管制、板块拥挤；确认腿在同一窗口里重搜，有选择偏差",
    ),
    Board(
        key="litho",
        bk="BK0884",
        name="光刻机",
        board_type="concept",
        risk_note="设备订单兑现、海外管制与主题交易；确认腿在同一窗口里重搜，有选择偏差",
    ),
)


def _is_bse(code: str) -> bool:
    digits = code.split(".")[0]
    return code.upper().endswith(".BJ") or digits.startswith(("43", "83", "87", "82", "92"))


def fetch_constituents(board: Board) -> list[dict]:
    """Eastmoney constituents, largest market cap first. Cached members on failure."""
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    )
    rows: list[dict] = []
    try:
        total = None
        for page in range(1, 8):
            response = session.get(
                "https://push2delay.eastmoney.com/api/qt/clist/get",
                params={
                    "pn": page,
                    "pz": 100,
                    "po": 1,
                    "np": 1,
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f20",
                    "fs": f"b:{board.bk}",
                    "fields": "f12,f13,f14,f20",
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            total = data.get("total", total)
            diff = data.get("diff") or []
            if not diff:
                break
            for item in diff:
                code = str(item.get("f12") or "").zfill(6)
                market = int(item.get("f13") or 0)
                if market == 1:
                    ts_code = f"{code}.SH"
                elif _is_bse(code) or market == 0 and code.startswith(("43", "83", "87", "82", "92")):
                    ts_code = f"{code}.BJ"
                else:
                    ts_code = f"{code}.SZ"
                rows.append(
                    {
                        "ts_code": ts_code,
                        "name": str(item.get("f14") or ""),
                        "mv": item.get("f20") or 0,
                    }
                )
            if total and len(rows) >= int(total):
                break
            time.sleep(0.15)
    except Exception as exc:  # noqa: BLE001
        print(f"[universe] {board.bk} live list failed ({exc}); using cached members")
        rows = []

    if not rows:
        cache = (
            Path.home()
            / ".vibe-trading"
            / "market_pick"
            / "members"
            / ("2026-11-03" if board.key == "semi" else "2026-11-04")
            / f"{board.bk}.json"
        )
        codes = json.loads(cache.read_text(encoding="utf-8"))
        rows = [{"ts_code": code, "name": "", "mv": 10_000 - i} for i, code in enumerate(codes)]
        print(f"[universe] {board.bk} cache n={len(rows)} (order is the cache order, not a live cap sort)")

    kept = []
    for row in rows:
        code = str(row["ts_code"]).upper()
        if not code or _is_bse(code):
            continue
        kept.append({**row, "ts_code": code})
    kept.sort(key=lambda item: float(item.get("mv") or 0), reverse=True)
    print(f"[universe] {board.bk} {board.name} listed={len(kept)}")
    return kept


def load_ohlcv(board: Board, listed: list[dict]) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Qfq bars from the lake. Keep names that cover the window, cap at 80."""
    codes = [row["ts_code"] for row in listed]
    names = {row["ts_code"]: row["name"] for row in listed}
    frame = adjusted_bars("1d", symbols=codes, start=START_DATE, end=END_DATE, mode="qfq")
    if frame.empty:
        raise SystemExit(f"{board.bk}: lake returned no bars")
    frame["date"] = pd.to_datetime(frame["date"])
    start_cut = pd.Timestamp(START_DATE) + pd.Timedelta(days=10)
    end_cut = pd.Timestamp(END_DATE) - pd.Timedelta(days=7)
    data_map: dict[str, pd.DataFrame] = {}
    for code in codes:
        if len(data_map) >= MAX_UNIVERSE:
            break
        part = frame.loc[frame["symbol"] == code].set_index("date").sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in part.columns]
        part = part[keep].apply(pd.to_numeric, errors="coerce").dropna(subset=["open", "high", "low", "close"])
        if len(part) < MIN_BARS:
            continue
        if part.index.min() > start_cut or part.index.max() < end_cut:
            continue
        data_map[code] = part
    print(
        f"[bars] {board.bk} eligible={len(data_map)} "
        f"window={START_DATE}..{END_DATE} cap={MAX_UNIVERSE}"
    )
    if len(data_map) < 12:
        raise SystemExit(f"{board.bk}: only {len(data_map)} names cover the window")
    return data_map, {code: names.get(code, "") for code in data_map}


def _screen_one(board: Board, confirm_ids: list[str], scores, irs, data_map) -> dict:
    lab = _combo_label(confirm_ids)
    run_dir = board.out_root / f"screen_{lab}"
    metrics_path = run_dir / "artifacts" / "metrics.csv"
    if metrics_path.exists():
        metrics = pd.read_csv(metrics_path).iloc[0].to_dict()
        row = _row(lab, confirm_ids, metrics, run_dir)
        print(f"  [cache] {board.key} {lab} excess={row['excess_return']:.4f}")
        return row

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    cfg = _config(board, confirm_ids, sorted(data_map), validation=False)
    (run_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    sig = _CachedSignalEngine(confirm_ids, scores, irs, veto_max=False)
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year("1D", "tushare")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)
    metrics = pd.read_csv(metrics_path).iloc[0].to_dict()
    row = _row(lab, confirm_ids, metrics, run_dir)
    print(f"  [run] {board.key} {lab} excess={row['excess_return']:.4f} sharpe={row['sharpe']:.3f}")
    return row


def _row(lab: str, confirm_ids: list[str], metrics: dict, run_dir: Path) -> dict:
    return {
        "label": lab,
        "confirm_ids": list(confirm_ids),
        "total_return": float(metrics["total_return"]),
        "excess_return": float(metrics["excess_return"]),
        "sharpe": float(metrics["sharpe"]),
        "max_drawdown": float(metrics["max_drawdown"]),
        "benchmark_return": float(metrics.get("benchmark_return") or 0.0),
        "run_dir": str(run_dir),
    }


def _config(board: Board, confirm_ids: list[str], codes: list[str], *, validation: bool) -> dict:
    cfg = {
        "codes": codes,
        "source": "tushare",
        "start_date": START_DATE,
        "end_date": END_DATE,
        "interval": "1D",
        "initial_cash": INITIAL_CASH,
        "commission": 0.0003,
        "stamp_duty": 0.001,
        "slippage": 0.0005,
        "alphas": [MOM_ID, *confirm_ids],
        "parameters": {
            "strategy": f"confirm_search_{_combo_label(confirm_ids)}_F0",
            "panel": board.panel,
            "confirm_ids": list(confirm_ids),
            "veto_max": False,
            "universe": (
                f"东财{('行业' if board.board_type == 'industry' else '概念')}"
                f"「{board.name}」{board.bk}（排除北交所；市值优先最多{MAX_UNIVERSE}只；前复权）"
            ),
            "price_basis": "qfq",
            "role": "全新确认腿搜索，规则事先写死，不事后换腿",
        },
        "_run_card_effective_sources": ["local_artifacts"],
    }
    if validation:
        cfg["validation"] = {
            "monte_carlo": {"n_simulations": 1000},
            "bootstrap": {"n_bootstrap": 1000, "confidence": 0.95},
            "walk_forward": {"n_windows": 5},
        }
    return cfg


def load_factor_irs() -> tuple[dict[str, float], list[str]]:
    """Alive confirm legs, plus the fixed mom even if the CSI300 bench killed it.

    The reference boards sort on gtja191_164. The 2026-11-03 survival file
    marks that factor dead on CSI300, so it is not allowed to enter the
    confirm pool. Its stored IR is kept only so the engine weight table loads.
    """
    payload = json.loads(survival_path().read_text(encoding="utf-8"))
    alphas = payload.get("alphas") or payload
    stored: dict[str, float] = {}
    alive: list[str] = []
    for aid, row in alphas.items():
        if not str(aid).startswith("gtja191_") or not isinstance(row, dict):
            continue
        try:
            stored[str(aid)] = float(row.get("ir") or 0.0)
        except (TypeError, ValueError):
            stored[str(aid)] = 0.0
        if row.get("category") == "alive" and aid not in BANNED_CONFIRM and aid != MOM_ID:
            alive.append(str(aid))
    alive.sort()
    irs = {aid: stored.get(aid, FALLBACK_IR.get(aid, 0.10)) for aid in alive}
    irs[MOM_ID] = stored.get(MOM_ID, FALLBACK_IR[MOM_ID])
    for aid in BASELINE_CONFIRM:
        irs.setdefault(aid, stored.get(aid, FALLBACK_IR.get(aid, 0.10)))
    return irs, alive


def screen_board(
    board: Board,
    irs: dict[str, float],
    alive: list[str],
    data_map: dict[str, pd.DataFrame],
) -> dict:
    """Return the single frozen F0 label for this board."""
    import itertools

    board.out_root.mkdir(parents=True, exist_ok=True)
    panel = _build_panel(data_map)
    needed = sorted(set(alive) | {MOM_ID, *BASELINE_CONFIRM})
    print(f"[precompute] {board.name} factors={len(needed)} symbols={panel['close'].shape[1]}")
    t0 = time.time()
    scores = _precompute_scores(panel, needed)
    print(f"[precompute] {board.name} done {time.time() - t0:.1f}s scored={len(scores)}")
    if MOM_ID not in scores:
        raise SystemExit(f"{board.name}: mom factor failed")

    cands = [aid for aid in alive if aid in scores]
    combos: list[list[str]] = [list(BASELINE_CONFIRM), []]
    combos.extend([aid] for aid in cands)
    print(f"[stage1] {board.name} singles={len(cands)} + baseline + pure_164")
    rows = []
    t1 = time.time()
    for i, ids in enumerate(combos, 1):
        rows.append(_screen_one(board, ids, scores, irs, data_map))
        if i % 10 == 0 or i == len(combos):
            print(f"  stage1 {i}/{len(combos)} elapsed={time.time() - t1:.1f}s")

    singles = [row for row in rows if len(row["confirm_ids"]) == 1]
    singles.sort(key=lambda row: row["excess_return"], reverse=True)
    pool = [row["confirm_ids"][0] for row in singles[:PAIR_POOL]]
    print(f"[stage2] {board.name} pairs from {pool}")
    seen = {tuple(row["confirm_ids"]) for row in rows}
    for i, (a, b) in enumerate(itertools.combinations(pool, 2), 1):
        key = tuple(sorted((a, b)))
        if key in seen or key == tuple(sorted(BASELINE_CONFIRM)):
            continue
        seen.add(key)
        rows.append(_screen_one(board, [a, b], scores, irs, data_map))
        if i % 15 == 0:
            print(f"  stage2 {i} elapsed={time.time() - t1:.1f}s")

    rows.sort(key=lambda row: (row["excess_return"], row["sharpe"], row["total_return"]), reverse=True)
    winner = rows[0]
    summary = {
        "board": board.key,
        "panel": board.panel,
        "frozen": winner,
        "baseline": next(
            (row for row in rows if row["confirm_ids"] == list(BASELINE_CONFIRM)),
            None,
        ),
        "top5": rows[:5],
        "n_scored": len(rows),
    }
    (board.out_root / "frozen.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[frozen] {board.name} {winner['label']} "
        f"excess={winner['excess_return']:.4f} total={winner['total_return']:.4f}"
    )
    return summary


def _load_engine(path: Path):
    spec = importlib.util.spec_from_file_location(f"sig_{path.parent.parent.name}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SignalEngine()


def produce(board: Board, confirm_ids: list[str], irs: dict[str, float], data_map, names) -> Path:
    """One production-engine run with the full validation block."""
    lab = _combo_label(confirm_ids)
    run_dir = board.out_root / f"prod_{lab}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)
    _write_signal_engine(run_dir / "code" / "signal_engine.py", confirm_ids, irs, veto_max=False)
    cfg = _config(board, confirm_ids, sorted(data_map), validation=True)
    (run_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    for code, frame in data_map.items():
        out = frame.copy()
        out.index.name = "trade_date"
        out.to_csv(run_dir / "artifacts" / f"ohlcv_{code}.csv")
    (run_dir / "universe_meta.json").write_text(
        json.dumps({"names": names}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    sig = _load_engine(run_dir / "code" / "signal_engine.py")
    engine = _create_market_engine("tushare", cfg, cfg["codes"])
    bpy = calc_bars_per_year("1D", "tushare")
    print(f"[prod] {board.name} {lab}")
    engine.run_backtest(cfg, _AutoLoader(data_map), sig, run_dir, bars_per_year=bpy)
    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    print(
        f"        total={float(metrics['total_return']):.4f} "
        f"excess={float(metrics['excess_return']):.4f} sharpe={float(metrics['sharpe']):.3f}"
    )
    return run_dir


def factor_snapshot(data_map: dict[str, pd.DataFrame], factor_ids: list[str]) -> dict[str, dict]:
    """5-day rank IC on this panel. Descriptive, not a second search."""
    from src.factors.registry import Registry

    panel = _build_panel(data_map)
    close = panel["close"]
    fwd = close.shift(-5) / close - 1.0
    registry = Registry()
    out: dict[str, dict] = {}
    for aid in factor_ids:
        raw = registry.compute(aid, panel)
        if raw is None or raw.empty:
            continue
        score = raw.reindex(index=close.index, columns=close.columns)
        ics: list[float] = []
        for dt in score.index:
            left = score.loc[dt]
            right = fwd.loc[dt]
            both = left.notna() & right.notna()
            if int(both.sum()) < 8:
                continue
            ic = left[both].rank().corr(right[both].rank())
            if pd.notna(ic):
                ics.append(float(ic))
        if len(ics) < 20:
            continue
        arr = np.asarray(ics, dtype=float)
        std = float(arr.std(ddof=1))
        out[aid] = {
            "ic": float(arr.mean()),
            "ir": float(arr.mean() / std) if std > 0 else 0.0,
            "n": int(arr.size),
        }
    return out


def publish(winner_board: Board, run_dir: Path, other: dict, snap: dict[str, dict], names: dict[str, str]) -> str:
    """Copy the production run into the reports/monitor namespace and stamp the hypothesis."""
    from src.hypotheses.journal import DecisionJournal
    from src.hypotheses.registry import HypothesisRegistry
    from src.monitor.models import MonitorConfig, StrategyRef
    from src.monitor.scan import run_monitor_scan
    from src.monitor.store import board_id_for_strategy, save_board, set_active_board_id
    from src.sector_pick.regime_infer import infer_run, write_strategy_style

    metrics = pd.read_csv(run_dir / "artifacts" / "metrics.csv").iloc[0].to_dict()
    gate = evaluate_run_validation(run_dir)
    total = float(metrics["total_return"])
    bench = float(metrics.get("benchmark_return") or 0.0)
    excess = float(metrics["excess_return"])
    beats = total > bench
    qualified = bool(gate.passed) and beats
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    legs = "+".join(str(x).replace("gtja191_", "") for x in cfg["parameters"]["confirm_ids"]) or "pure164"
    run_id = f"{stamp}_05_{winner_board.key}_{legs}_qfq"
    dst = _AGENT_ROOT / "runs" / run_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(run_dir, dst)

    style = {
        "schema_version": 1,
        "declared_at": datetime.now(timezone.utc).isoformat(),
        "declared_by": "research_semi_litho",
        "timing_of_declaration": "prior_rule",
        "timing_note": "确认腿在看到验证门之前按超额冻结；验证门和是否跑赢等权是事先写死的失效条件。",
        "style_label": "横截面·周频·6等权·MA20择时",
        "mechanism": "sector_cross_section",
        "market_scope": "single_sector",
        "panel": winner_board.panel,
        "breadth": len(cfg["codes"]),
        "rebalance": "weekly",
        "holding_count": 6,
        "weighting": "equal",
        "sector_timing": "ma20_half",
        "risk_rules": {
            "peak_dd_stop": 0.15,
            "take_profit": 0.35,
            "time_stop_days": 20,
            "min_hold_days": 5,
            "max_turnover_per_rebalance": 0.2,
        },
        "factor_ids": cfg["alphas"],
        "designed_regime": "unspecified",
        "designed_regime_source": "none",
        "gate_policy": "global_default",
    }
    cfg = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    cfg["strategy_style"] = style
    cfg["parameters"]["hypothesis_pending"] = True
    (dst / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        write_strategy_style(dst, infer_run(dst), source="auto_inferred")
    except Exception as exc:  # noqa: BLE001
        print(f"[style] infer skipped: {exc}")

    ic_bits = []
    for aid, row in snap.items():
        ic_bits.append(f"{aid} 5日 RankIC {row['ic']:+.4f}（IR {row['ir']:+.2f}，n={row['n']}）")
    ic_text = "；".join(ic_bits) if ic_bits else "因子截面相关未算出"
    title = f"{winner_board.name} {winner_board.bk} 164+{legs}（确认腿重搜·复权）"
    observation = (
        f"{winner_board.panel} 配置池 {len(cfg['codes'])} 只，前复权日线 {START_DATE} 至 {END_DATE}。"
        f"生产引擎计成本总收益 {total:.2%}，等权基准 {bench:.2%}，超额 {excess:.2%}，"
        f"夏普 {float(metrics['sharpe']):.3f}，最大回撤 {float(metrics['max_drawdown']):.2%}。"
        f"同面板 5 日 RankIC：{ic_text}。"
        "gtja191_164 在 2026-11-03 沪深300存活表里的类别是 dead，这里仍固定用它排序；"
        "确认腿只从当天仍为 alive 的国泰君安因子里取。"
        f"对照板 {other['panel']} 冻结标签 {other['label']}，生产引擎超额 {other['excess_return']:.2%}。"
        "该配方是存活确认腿里按超额选出的一条，选择偏差还在。"
    )
    hypothesis = (
        f"因为日内涨跌幅度结构（gtja191_164）高分位对应抛压被吸收、价格没有被当天抬高，"
        f"因此在{winner_board.name}里按 164 排序、再用 {legs} 做确认后等权持有 6 只，"
        f"应在计成本之后高于板块等权。确认腿只削减样本，不保证提高质量。"
    )
    logic = (
        "链条是：164 高分位意味着日内上涨幅度小、卖压被持续吸收；确认腿按信息比率加权，"
        "低于当日 40% 分位的新开仓被挡掉；周频调仓、20% 换手上限和 5 日最短持有压低摩擦；"
        "板块等权收盘低于 MA20 时降到半仓。失效有两种：单边下跌里日内涨幅被压缩的个股持续落后，"
        "以及确认腿方向与有效特征相反时，确认只减少样本、把超额稀释掉。"
    )
    validation_plan = (
        f"数据：数据湖 bars_daily，前复权，{len(cfg['codes'])} 只，{START_DATE} 至 {END_DATE}，"
        "计成本（佣金万3、印花税千1、滑点万5）。信号一次冻结：164 的 5 日平滑横截面分位排序，"
        f"确认腿 {legs} 按 IR 加权后不低于当日 40% 分位，N=6 等权，保留带 Top9，"
        "单次换手上限 20%，最短持有 5 日，峰值回撤 15% 止损、止盈 35%、20 日时间止损且排名恶化 10 位，"
        "MA20 择时 1.0/0.5，每周第一个交易日调仓。"
        "判定：验证门四项都过（计成本、蒙特卡洛 p<=0.10、bootstrap 夏普下界>=0、走步一致性>=0.60），"
        f"且计成本总收益高于同窗口等权基准 {bench:.2%}。"
    )
    invalidation = (
        "以下任一成立即判假设不成立：一，验证门任一检查不通过"
        "（蒙特卡洛 p>0.10，或 bootstrap 夏普 95% 置信下界<0，或走步一致性<0.60，或未计成本）；"
        f"二，计成本总收益不高于同窗口等权基准 {bench:.2%}。"
        "触及任一条件即登记为 rejected，不调整参数、不更换确认腿、不缩短窗口重跑。"
    )
    status = "validated" if qualified else "rejected"
    hyp = HypothesisRegistry().create(
        title=title,
        thesis=hypothesis,
        status=status,
        universe=cfg["parameters"]["universe"],
        signal_definition=f"F0 mom=gtja191_164 confirm={legs} top_n=6 weekly MA20",
        data_sources=["lake_bars_daily_qfq"],
        observation=observation,
        hypothesis=hypothesis,
        logic=logic,
        validation_plan=validation_plan,
        invalidation=invalidation,
    )
    DecisionJournal().record(
        run_id=run_id,
        hypothesis_id=hyp.hypothesis_id,
        thesis=hypothesis,
        observation=observation,
        hypothesis=hypothesis,
        logic=logic,
        validation_plan=validation_plan,
        invalidation=invalidation,
        planned_position="6 只等权，板块等权低于 MA20 时半仓",
        planned_risk="峰值回撤 15% 止损、止盈 35%、20 日时间止损；单票纸面不超过净值 20%",
        strict=True,
    )
    cfg = json.loads((dst / "config.json").read_text(encoding="utf-8"))
    cfg["parameters"]["hypothesis_id"] = hyp.hypothesis_id
    cfg["parameters"].pop("hypothesis_pending", None)
    (dst / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (dst / "state.json").write_text(json.dumps({"status": "success"}, indent=2), encoding="utf-8")
    (dst / "req.json").write_text(
        json.dumps({"title": f"{winner_board.name}-{legs}", "prompt": title}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (dst / "REPORT.md").write_text(
        _report(winner_board, run_id, metrics, gate, qualified, legs, other, names, dst),
        encoding="utf-8",
    )

    config = MonitorConfig(strategy=StrategyRef(type="run_dir", run_id=run_id))
    try:
        config = run_monitor_scan(config, refresh_report=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[monitor] scan failed, board still saved: {exc}")
    board_id = board_id_for_strategy(config.strategy)
    save_board(board_id, config)
    set_active_board_id(board_id)
    print(
        f"[filed] {run_id} hypothesis={hyp.hypothesis_id} status={status} "
        f"gate={gate.passed} beats_benchmark={beats} tradable={config.tradable}"
    )
    return run_id


def _report(board, run_id, metrics, gate, qualified, legs, other, names, dst: Path) -> str:
    pos_path = dst / "artifacts" / "positions.csv"
    hold_md = "_持仓未写出_"
    if pos_path.exists():
        pos = pd.read_csv(pos_path, index_col=0, parse_dates=True)
        held_rows = pos.gt(0)
        if held_rows.any(axis=1).any():
            last = pos.index[held_rows.any(axis=1)][-1]
            held = pos.loc[last]
            held = held[held > 0].sort_values(ascending=False)
            lines = ["| # | 代码 | 名称 | 权重 |", "|---|------|------|------|"]
            for i, (code, weight) in enumerate(held.items(), 1):
                lines.append(f"| {i} | {code} | {names.get(code, '')} | {float(weight):.4f} |")
            hold_md = "\n".join(lines) + f"\n\n最近持仓日 {last.date()}。"
    flag = "可交易" if qualified else "未入池（假设 rejected）"
    return f"""# {board.name} {board.bk} F0 确认腿 — {legs}

研究/回测，不是实盘建议。样本外未审阅。{flag}。

| 项 | 值 |
|---|---|
| run | `{run_id}` |
| 总收益 | {float(metrics['total_return']):.2%} |
| 等权基准 | {float(metrics.get('benchmark_return') or 0):.2%} |
| 超额 | {float(metrics['excess_return']):.2%} |
| 夏普 | {float(metrics['sharpe']):.3f} |
| 最大回撤 | {float(metrics['max_drawdown']):.2%} |
| 验证门 | {"通过" if gate.passed else "未通过"} {gate.reasons} |
| 对照 | {other['panel']} `{other['label']}` 超额 {other['excess_return']:.2%} |

{hold_md}

{board.risk_note}。规则在排序之前写死：每块板只冻超额最高的一条 F0，生产引擎重跑一次，不换腿。
"""


def main() -> None:
    RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
    irs, alive = load_factor_irs()
    print(
        f"[survival] alive confirms={len(alive)} {alive}; "
        f"mom {MOM_ID} IR={irs[MOM_ID]:.4f} (CSI300 bench may mark it dead)"
    )

    produced: list[dict] = []
    for board in BOARDS:
        listed = fetch_constituents(board)
        data_map, names = load_ohlcv(board, listed)
        board.out_root.mkdir(parents=True, exist_ok=True)
        (board.out_root / "names.json").write_text(
            json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary = screen_board(board, irs, alive, data_map)
        frozen_ids = list(summary["frozen"]["confirm_ids"])
        prod = produce(board, frozen_ids, irs, data_map, names)
        metrics = pd.read_csv(prod / "artifacts" / "metrics.csv").iloc[0].to_dict()
        snap = factor_snapshot(data_map, [MOM_ID, *frozen_ids])
        produced.append(
            {
                "board": board,
                "run_dir": prod,
                "names": names,
                "snap": snap,
                "label": _combo_label(frozen_ids),
                "excess_return": float(metrics["excess_return"]),
                "total_return": float(metrics["total_return"]),
                "panel": board.panel,
            }
        )

    def _qualified(item: dict) -> bool:
        metrics = pd.read_csv(item["run_dir"] / "artifacts" / "metrics.csv").iloc[0]
        gate = evaluate_run_validation(item["run_dir"])
        beats = float(metrics["total_return"]) > float(metrics.get("benchmark_return") or 0.0)
        item["qualified"] = bool(gate.passed) and beats
        print(
            f"[judge] {item['panel']} {item['label']} excess={item['excess_return']:.4f} "
            f"gate={gate.passed} beats={beats} reasons={gate.reasons}"
        )
        return item["qualified"]

    for item in produced:
        _qualified(item)
    pool = [item for item in produced if item["qualified"]] or produced
    pool.sort(key=lambda item: item["excess_return"], reverse=True)
    best = pool[0]
    other = next(item for item in produced if item is not best)
    publish(best["board"], best["run_dir"], other, best["snap"], best["names"])
    print("[done]")


if __name__ == "__main__":
    main()
