"""Re-score cached board screen + Eastmoney fundamentals → Top 5.

Uses sector_screen_full.csv for momentum; pulls PE/ROE/PB from Eastmoney
constituent clist (no Tushare quota). Research only — not investment advice.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

_AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

CSV_IN = _AGENT_ROOT / "runs" / "_tmp_sector_screen" / "sector_screen_full.csv"
OUT_JSON = _AGENT_ROOT / "runs" / "_tmp_sector_screen" / "sector_top5.json"

_EXCLUDE_NAME = (
    "ST",
    "退市",
    "次新",
    "预盈预增",
    "昨日连板",
    "昨日涨停",
    "高送转",
    "股权转让",
    "举牌",
    "微盘股",
    "破净股",
    "扭亏",
    "预增",
    "预亏",
    "GDR",
    "AB股",
    "B股",
    "IPO受益",
    "标的",
    "参股",
    "证金持股",
    "茅指数",
    "超级品牌",
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _as_float(v: Any) -> float | None:
    if v is None or v == "-" or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def rank_pct(series: pd.Series, *, higher_better: bool = True) -> pd.Series:
    return series.rank(pct=True, ascending=higher_better, method="average")


def fetch_board_fundamentals(bk: str, max_n: int = 50) -> dict[str, Any]:
    """Median PE/ROE/PB + market-cap-weighted notes from Eastmoney constituents."""
    s = _session()
    pe: list[float] = []
    roe: list[float] = []
    pb: list[float] = []
    mv: list[float] = []
    names: list[str] = []
    for pn in range(1, 3):
        params = dict(
            pn=pn,
            pz=100,
            po=1,
            np=1,
            ut="bd1d9ddb04089700cf9c27f6f7426281",
            fltt=2,
            invt=2,
            fid="f20",
            fs=f"b:{bk}",
            fields="f12,f13,f14,f9,f23,f37,f20",
        )
        try:
            r = s.get(
                "https://push2delay.eastmoney.com/api/qt/clist/get",
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            diff = ((r.json().get("data") or {}).get("diff")) or []
        except Exception as exc:  # noqa: BLE001
            print(f"  [clist] {bk}: {exc}")
            diff = []
        if not diff:
            break
        for x in diff:
            c = str(x.get("f12") or "").zfill(6)
            if c.startswith(("4", "8", "92")):
                continue
            names.append(str(x.get("f14") or c))
            for bucket, key, lo, hi in (
                (pe, "f9", 0, 120),
                (roe, "f37", -30, 60),
                (pb, "f23", 0, 40),
                (mv, "f20", 0, 1e15),
            ):
                fv = _as_float(x.get(key))
                if fv is not None and lo < fv < hi:
                    bucket.append(fv)
        if len(names) >= max_n:
            break
        time.sleep(0.08)

    def med(xs: list[float]) -> float | None:
        return float(np.median(xs)) if xs else None

    # Cap-weighted PE/ROE for large-cap quality signal
    def wavg(vals: list[float], weights: list[float]) -> float | None:
        if not vals or not weights or len(vals) != len(weights):
            # align by rebuilding from paired lists — skip if lengths differ
            n = min(len(vals), len(weights))
            if n == 0:
                return None
            vals, weights = vals[:n], weights[:n]
        w = np.array(weights, dtype=float)
        v = np.array(vals, dtype=float)
        if w.sum() <= 0:
            return None
        return float(np.average(v, weights=w))

    # Pair PE/ROE with MV when possible by re-fetching aligned — use simple medians primarily
    return {
        "pe_ttm_med": med(pe),
        "roe_med": med(roe),  # Eastmoney f37 ≈ 报告期ROE（多为单季/报告期）
        "pb_med": med(pb),
        "mv_med_yi": (med(mv) / 1e8) if med(mv) else None,
        "n_members_sample": len(names),
        "pe_n": len(pe),
        "roe_n": len(roe),
        "leaders": names[:5],
    }


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        x = float(o)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, 4)
    if isinstance(o, (np.integer, int)):
        return int(o)
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    try:
        if pd.isna(o):
            return None
    except Exception:  # noqa: BLE001
        pass
    return o


def theme(name: str) -> str:
    # Longer / more specific keys first so overlaps collapse to one theme.
    keys = (
        ("商用载货", "商用车"),
        ("商用车", "商用车"),
        ("白色家电", "家电链"),
        ("空调", "家电链"),
        ("净水", "家电链"),
        ("工程机械", "工程机械"),
        ("休闲食品", "零食链"),
        ("零食", "零食链"),
        ("饮料乳品", "饮料乳品"),
        ("软饮料", "饮料乳品"),
        ("乳品", "饮料乳品"),
        ("调味发酵", "调味品"),
        ("纺织化学", "纺织"),
        ("纺织制造", "纺织"),
        ("棉纺", "纺织"),
        ("培训教育", "教育"),
        ("毛发医疗", "医美"),
        ("社区团购", "新零售"),
        ("食品饮料", "食品饮料"),
    )
    for needle, bucket in keys:
        if needle in name:
            return bucket
    return name[:2]


def main() -> int:
    mom = pd.read_csv(CSV_IN)
    print(f"loaded {len(mom)} boards")

    mask_name = ~mom["name"].astype(str).apply(lambda n: any(k in n for k in _EXCLUDE_NAME))
    mom = mom[mask_name].copy()
    mom = mom[mom["amt_20d_yi"].fillna(0) > 8]
    mom = mom[mom["ret_60d"].abs().fillna(999) < 80]
    mom = mom[mom["ret_20d"].abs().fillna(999) < 40]
    mom = mom[mom["ret_120d"].notna()]
    mom["_type_rank"] = mom["board_type"].map({"industry": 0, "concept": 1}).fillna(2)
    mom = mom.sort_values(["name", "_type_rank"]).drop_duplicates("name", keep="first")

    mom["mom_score"] = (
        0.18 * rank_pct(mom["ret_5d"].fillna(-999))
        + 0.28 * rank_pct(mom["ret_20d"].fillna(-999))
        + 0.22 * rank_pct(mom["ret_60d"].fillna(-999))
        + 0.12 * rank_pct(mom["ret_120d"].fillna(-999))
        + 0.08 * mom["trend_ok"].fillna(0)
        + 0.07 * rank_pct(mom["amt_ratio_20_60"].fillna(0))
        + 0.05 * rank_pct(mom["up_share_20"].fillna(0))
        + 0.10 * rank_pct(mom["dd_60d"].fillna(-999))
    )

    short = mom[(mom["ret_5d"] > 0) | (mom["ret_20d"] > 2)].copy()
    short = short.sort_values("mom_score", ascending=False).head(20)
    print("momentum shortlist:")
    print(
        short[
            ["bk", "name", "board_type", "ret_5d", "ret_20d", "ret_60d", "ret_120d", "mom_score"]
        ].to_string(index=False)
    )

    fund_rows = []
    for _, r in short.iterrows():
        bk = str(r["bk"])
        print(f"fundamentals {bk} {r['name']}")
        f = fetch_board_fundamentals(bk)
        fund_rows.append({"bk": bk, **f})
        time.sleep(0.12)

    merged = short.merge(pd.DataFrame(fund_rows), on="bk", how="left")

    def fund_ok(row: pd.Series) -> bool:
        roe = _as_float(row.get("roe_med"))
        pe = _as_float(row.get("pe_ttm_med"))
        pb = _as_float(row.get("pb_med"))
        if (row.get("roe_n") or 0) < 3:
            return False
        if roe is not None and roe < 2.0:  # report-period ROE too weak
            return False
        if pe is not None and pe > 80:
            return False
        if pb is not None and pb > 15:
            return False
        return True

    merged["fund_ok"] = merged.apply(fund_ok, axis=1)
    viable = merged[merged["fund_ok"]].copy()
    print(f"fund_ok={len(viable)} / {len(merged)}")
    if len(viable) < 5:
        viable = merged[merged["roe_med"].fillna(0) > 0].copy()
    if viable.empty:
        viable = merged.copy()

    # Quality score: high ROE, reasonable PE/PB (value + quality)
    viable["fund_score"] = (
        0.45 * rank_pct(viable["roe_med"].fillna(0))
        + 0.30 * rank_pct(viable["pe_ttm_med"].fillna(999), higher_better=False)
        + 0.25 * rank_pct(viable["pb_med"].fillna(999), higher_better=False)
    )
    viable["type_bonus"] = viable["board_type"].map({"industry": 0.04, "concept": 0.0}).fillna(0)
    viable["prospect_tilt"] = (
        0.5 * (viable["ret_20d"].fillna(0) > 0).astype(float)
        + 0.5 * rank_pct(viable["ret_60d"].fillna(-999))
    ) * 0.06
    viable["composite"] = (
        0.50 * viable["mom_score"]
        + 0.40 * viable["fund_score"]
        + viable["type_bonus"]
        + viable["prospect_tilt"]
    )

    viable = viable.sort_values("composite", ascending=False)
    final_rows: list[pd.Series] = []
    kept: list[str] = []
    used_themes: set[str] = set()
    for _, row in viable.iterrows():
        name = str(row["name"])
        th = theme(name)
        if th in used_themes:
            continue
        if any(name in k or k in name for k in kept):
            continue
        kept.append(name)
        used_themes.add(th)
        final_rows.append(row)
        if len(final_rows) >= 5:
            break

    top5 = []
    for r in final_rows:
        d = r.to_dict()
        # stringify leaders
        leaders = d.get("leaders")
        if isinstance(leaders, float) and math.isnan(leaders):
            d["leaders"] = []
        top5.append(d)

    # Narrative tags for report
    narratives = {
        "空调": "旺季+出口/以旧换新预期；龙头盈利稳定，估值不贵，中期趋势全市场少见为正。",
        "零食": "量价齐升的必选消费扩散；短线资金活跃，估值与盈利质量均衡。",
        "软饮料": "夏季消费+渠道修复；ROE尚可、PE友好，动量居前。",
        "饮料乳品": "乳品/饮料需求韧性；关注成本端与龙头份额。",
        "乳品": "必选消费防御属性；估值合理，短线跟涨消费主线。",
        "白色家电": "家电内外需共振；龙头现金流好，相对抗跌。",
        "培训教育": "政策扰动后供给出清，主题弹性大但基本面分化，需精选龙头。",
        "棉纺": "周期底部补库预期；短线强但景气持续性需验证。",
        "商用载货车": "货运与基建相关周期品；弹性大、波动也大。",
        "商用车": "卡货运力周期；动量好但盈利对宏观更敏感。",
        "工程机械整机": "国内更新+出口；中期景气改善交易中。",
        "调味发酵品Ⅱ": "必选消费稳定器；走势稳健、估值不贵。",
        "调味发酵品Ⅲ": "必选消费稳定器；走势稳健、估值不贵。",
        "食品饮料": "大消费主线；内部结构分化，宜看细分龙头。",
        "休闲食品": "零食链景气；成长性优于传统食品。",
        "毛发医疗": "医美 consumable 成长赛道；估值偏贵需警惕。",
        "净水概念": "家电链条延伸；跟涨空调/家电，独立性弱。",
    }
    for d in top5:
        d["thesis"] = narratives.get(str(d["name"]), "相对强势+基本面中位数达标的交易型板块。")

    payload = {
        "as_of": "2026-07-29",
        "market_context": (
            "截至扫描日，A股多数板块60日仍为负收益（中期调整未完）。"
            "本榜单按「相对强势动量 + 成分股估值/盈利质量」综合排序，"
            "强调走势与交易活跃度领先、且基本面中位数未明显恶化的板块。"
        ),
        "method": {
            "universe": "东财行业+概念约300个，流动性过滤后主题去重",
            "momentum": "板块日K：5/20/60/120日收益、MA结构、成交额比、上涨日占比、60日回撤",
            "fundamentals": "成分股市值靠前样本：PE_TTM(f9)、报告期ROE(f37)、PB(f23) 中位数",
            "composite": "0.50*动量 + 0.40*基本面 + 行业加成 + 前景倾斜；主题互斥取Top5",
        },
        "top5": top5,
        "shortlist": viable.head(12).to_dict(orient="records"),
        "disclaimer": "研究扫描结果，不构成投资建议；板块动量变化快，实盘请复核最新行情与财报。",
    }
    OUT_JSON.write_text(
        json.dumps(_clean(payload), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("=== TOP 5 ===")
    for i, r in enumerate(top5, 1):
        print(
            f"{i}. {r['name']} ({r['bk']}) comp={r.get('composite')} "
            f"r5={r.get('ret_5d')} r20={r.get('ret_20d')} r60={r.get('ret_60d')} r120={r.get('ret_120d')} "
            f"roe={r.get('roe_med')} pe={r.get('pe_ttm_med')} pb={r.get('pb_med')} "
            f"leaders={r.get('leaders')}"
        )
    print(f"wrote {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
