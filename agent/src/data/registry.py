"""Dataset registry — the single declarative source of truth for the data lake.

Every dataset the data hub knows about is declared here **exactly once**: its
natural key, partitions, columns, quality predicates, source priority,
consumers and freshness SLA.  The catalog, the HTTP layer and the frontend are
all derived from these declarations, so adding a dataset is one entry plus one
ingester — never a new route, a new table branch, or a new page.

Why a registry instead of per-feature schemas: the same bar was previously
fetched (and cached) once per screen, with each screen deciding its own
adjustment, settlement cut-off and column set.  Declaring the shape in one place
is what makes "same number everywhere" checkable rather than aspirational.

Quality predicates
------------------
``checks`` are SQL boolean expressions that must be **false for every row**
(e.g. ``"low > least(open, close)"`` counts rows whose OHLC ordering is
impossible).  The catalog runs each one and reports the violation count, so a
new integrity rule is a one-line declaration rather than new scan code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "Category",
    "DatasetSpec",
    "REGISTRY",
    "CATEGORY_LABELS",
    "spec",
    "all_specs",
    "specs_by_category",
    "dataset_names",
]

Category = Literal["bars", "market", "sector", "capital", "reference", "derived"]

CATEGORY_LABELS: dict[str, str] = {
    "bars": "行情",
    "market": "市场指标",
    "sector": "板块",
    "capital": "资金",
    "reference": "参考数据",
    "derived": "派生",
}

# Retention defaults.  Five years of intraday is the agreed ceiling: it keeps a
# full 5-minute history for the default ~1800-name pool on a local disk while
# bounding the annual rewrite cost (see docs/design/data-center.md §6.6).
INTRADAY_RETENTION_DAYS = 1825
DAILY_RETENTION_DAYS = 3650


@dataclass(frozen=True)
class DatasetSpec:
    """One dataset's contract.

    Attributes:
        name: Lake directory name (also the API path segment).
        label: Human label, Chinese (matches the rest of the UI).
        category: Grouping used by the freshness matrix.
        keys: Natural key for upserts; reads are deduped on it too.
        partitions: Hive partition columns written under the dataset dir.
        columns: Ordered ``(name, logical_type)`` pairs.
        frequency: ``1d`` / ``5m`` / ``tick`` / ``event`` / ``yearly``.
        markets: Market codes this dataset covers.
        sources: Preferred sources, most trusted first.
        consumers: Feature pages / tools that read it (shown in the UI).
        settle_rule: When the data for a session can be treated as final.
        retention_days: Rows older than this may be pruned; ``None`` keeps all.
        sla_minutes: Minutes after the close by which a session must land.
        ingest: ``module:function`` of the ingester, ``None`` for read-only.
        checks: ``(id, sql_predicate)`` pairs that must never match a row.
        description: One-line explanation surfaced in the datasets table.
    """

    name: str
    label: str
    category: Category
    keys: tuple[str, ...]
    partitions: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]
    frequency: str
    markets: tuple[str, ...]
    sources: tuple[str, ...]
    consumers: tuple[str, ...]
    settle_rule: str
    retention_days: int | None
    sla_minutes: int
    ingest: str | None
    checks: tuple[tuple[str, str], ...] = ()
    description: str = ""

    @property
    def column_names(self) -> tuple[str, ...]:
        """Column names in declaration order."""
        return tuple(name for name, _ in self.columns)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for the HTTP layer."""
        return {
            "name": self.name,
            "label": self.label,
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, self.category),
            "keys": list(self.keys),
            "partitions": list(self.partitions),
            "columns": [{"name": n, "type": t} for n, t in self.columns],
            "frequency": self.frequency,
            "markets": list(self.markets),
            "sources": list(self.sources),
            "consumers": list(self.consumers),
            "settle_rule": self.settle_rule,
            "retention_days": self.retention_days,
            "sla_minutes": self.sla_minutes,
            "ingest": self.ingest,
            "checks": [{"id": cid, "predicate": sql} for cid, sql in self.checks],
            "description": self.description,
        }


_OHLCV_QUALITY: tuple[tuple[str, str], ...] = (
    ("ohlc_order", 'low > least("open", "close") OR high < greatest("open", "close")'),
    ("non_positive_price", 'least("open", "high", "low", "close") <= 0'),
    ("null_close", '"close" IS NULL'),
    ("negative_volume", '"volume" < 0'),
)

REGISTRY: dict[str, DatasetSpec] = {
    "bars_daily": DatasetSpec(
        name="bars_daily",
        label="日线行情",
        category="bars",
        keys=("symbol", "date"),
        partitions=("year",),
        columns=(
            ("symbol", "string"),
            ("date", "date"),
            ("interval", "string"),
            ("open", "float"),
            ("high", "float"),
            ("low", "float"),
            ("close", "float"),
            ("volume", "float"),
            ("amount", "float"),
            ("pre_close", "float"),
            ("pct_chg", "float"),
            ("adj_factor", "float"),
            ("source", "string"),
            ("vintage", "timestamp"),
        ),
        frequency="1d",
        markets=("cn",),
        sources=("tushare", "tencent", "eastmoney"),
        consumers=("回测", "监控看板", "市场优选", "优选板块", "相关性", "Agent 工具"),
        settle_rule="close+5m",
        retention_days=DAILY_RETENTION_DAYS,
        sla_minutes=40,
        ingest="src.data.ingest.bars:daily",
        checks=_OHLCV_QUALITY,
        description="不复权 OHLCV + 成交额 + 复权因子，全站唯一日线来源；volume 单位为手，amount 单位为元。",
    ),
    "bars_intraday": DatasetSpec(
        name="bars_intraday",
        label="分钟线",
        category="bars",
        keys=("symbol", "interval", "date"),
        partitions=("date",),
        columns=(
            ("symbol", "string"),
            ("date", "timestamp"),
            ("interval", "string"),
            ("open", "float"),
            ("high", "float"),
            ("low", "float"),
            ("close", "float"),
            ("volume", "float"),
            ("amount", "float"),
            ("source", "string"),
            ("vintage", "timestamp"),
        ),
        frequency="5m",
        markets=("cn",),
        sources=("tencent", "akshare", "baostock"),
        consumers=("数据中心 K 线", "分钟回测", "监控盘中"),
        settle_rule="close+5m",
        retention_days=INTRADAY_RETENTION_DAYS,
        sla_minutes=60,
        ingest="src.data.ingest.bars:intraday",
        checks=_OHLCV_QUALITY,
        description="1m–1H 原始分钟线；4H 及更长周期由本地按交易时段聚合，不重复入库。",
    ),
    "tick_agg": DatasetSpec(
        name="tick_agg",
        label="逐笔聚合",
        category="bars",
        keys=("symbol", "date"),
        partitions=("date",),
        columns=(
            ("symbol", "string"),
            ("date", "date"),
            ("trades", "int"),
            ("volume", "float"),
            ("amount", "float"),
            ("vwap", "float"),
            ("buy_volume", "float"),
            ("sell_volume", "float"),
            ("neutral_volume", "float"),
            ("buy_amount", "float"),
            ("sell_amount", "float"),
            ("active_buy_ratio", "float"),
            ("net_active_amount", "float"),
            ("big_amount", "float"),
            ("big_amount_ratio", "float"),
            ("big_net_amount", "float"),
            ("big_trade_count", "int"),
            ("avg_trade_amount", "float"),
            ("post_close_volume", "float"),
            ("post_close_amount", "float"),
            ("prints_complete", "bool"),
            ("open_auction_volume", "float"),
            ("open_auction_amount", "float"),
            ("close_auction_volume", "float"),
            ("close_auction_amount", "float"),
            ("first_time", "string"),
            ("last_time", "string"),
            ("source", "string"),
        ),
        frequency="tick",
        markets=("cn",),
        sources=("tencent",),
        consumers=("资金验证", "微观结构", "数据中心 K 线副图"),
        settle_rule="close+30m",
        retention_days=INTRADAY_RETENTION_DAYS,
        sla_minutes=180,
        ingest="src.data.ingest.ticks:daily",
        checks=(
            ("negative_volume", '"volume" < 0'),
            ("active_ratio_range", 'active_buy_ratio < 0 OR active_buy_ratio > 1'),
        ),
        description="逐笔成交按日聚合：主动买卖比、大单净额、竞价拆分，一标的日一行。",
    ),
    "tradability": DatasetSpec(
        name="tradability",
        label="可交易性面板",
        category="reference",
        keys=("symbol", "date"),
        partitions=("year",),
        columns=(
            ("symbol", "string"),
            ("date", "string"),
            ("is_suspended", "bool"),
            ("is_st", "bool"),
            ("board", "string"),
            ("pre_close", "float"),
            ("limit_up", "float"),
            ("limit_down", "float"),
            ("close", "float"),
            ("volume", "float"),
            ("amount", "float"),
            ("band_mismatch", "bool"),
        ),
        frequency="1d",
        markets=("cn",),
        sources=("bars_daily", "tushare", "akshare"),
        consumers=("回测引擎", "数据质量"),
        settle_rule="close+30m",
        retention_days=INTRADAY_RETENTION_DAYS,
        sla_minutes=60,
        ingest="src.data.ingest.tradability:daily",
        checks=(
            ("limit_order", '"limit_down" > "limit_up"'),
            ("band_mismatch", '"band_mismatch"'),
        ),
        description="停牌/ST/板块/涨跌停价；引擎判断能否成交的唯一依据，由 bars_daily 派生。",
    ),
    "calendar": DatasetSpec(
        name="calendar",
        label="交易日历",
        category="reference",
        keys=("trade_date",),
        partitions=(),
        columns=(("trade_date", "date"), ("source", "string")),
        frequency="yearly",
        markets=("cn",),
        sources=("akshare", "offline"),
        consumers=("全部",),
        settle_rule="yearly",
        retention_days=None,
        sla_minutes=43200,
        ingest="src.data.ingest.calendar:refresh",
        checks=(),
        description="交易所口径交易日；所有 as_of、区间契约、回测年化的基准。",
    ),
}


def spec(name: str) -> DatasetSpec:
    """Return the declaration for *name*.

    Raises:
        KeyError: When the dataset was never declared — callers should treat an
            unknown name as a programming error, not a missing file.
    """
    try:
        return REGISTRY[name]
    except KeyError as exc:  # pragma: no cover - defensive
        known = ", ".join(sorted(REGISTRY))
        raise KeyError(f"unknown dataset {name!r}; declared: {known}") from exc


def all_specs() -> tuple[DatasetSpec, ...]:
    """Every declared spec, in declaration order."""
    return tuple(REGISTRY.values())


def specs_by_category() -> dict[str, list[DatasetSpec]]:
    """Group specs by category, preserving declaration order inside each."""
    grouped: dict[str, list[DatasetSpec]] = {key: [] for key in CATEGORY_LABELS}
    for item in REGISTRY.values():
        grouped.setdefault(item.category, []).append(item)
    return grouped


def dataset_names() -> tuple[str, ...]:
    """Declared dataset names."""
    return tuple(REGISTRY.keys())
