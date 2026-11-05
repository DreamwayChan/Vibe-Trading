"""Tracked CSI 300 index-fund universe.

The list is curated rather than auto-discovered on purpose: ETF names are
abbreviated differently by each bourse, so name matching silently pulls in
look-alikes (红利300 / 300价值 / 300增强 / 中证A100 …). A fixed, auditable
whitelist keeps the ETF-OI series comparable across days.

Every code below was verified against the fund's full name on 天天基金
(``fS_name``) and against exchange-published shares. Extend the list by editing
``CSI300_FUNDS`` or by passing ``codes=`` to ``GET /capital/etf-oi``.
"""

from __future__ import annotations

from src.capital.models import EtfFundDef

# CSI 300 index used as the benchmark / market-cap anchor.
INDEX_CODE = "000300"
INDEX_SINA_SYMBOL = "sh000300"
INDEX_LABEL = "沪深300"

# The headline CSI 300 ETFs, ordered roughly by size (largest first).
CSI300_FUNDS: tuple[EtfFundDef, ...] = (
    EtfFundDef("510300", "沪深300ETF华泰柏瑞", "SH"),
    EtfFundDef("510310", "沪深300ETF易方达", "SH"),
    EtfFundDef("510330", "沪深300ETF华夏", "SH"),
    EtfFundDef("159919", "沪深300ETF嘉实", "SZ"),
    EtfFundDef("510350", "沪深300ETF工银", "SH"),
    EtfFundDef("510360", "沪深300ETF广发", "SH"),
    EtfFundDef("510390", "沪深300ETF平安", "SH"),
    EtfFundDef("515130", "沪深300ETF博时", "SH"),
    EtfFundDef("515330", "沪深300ETF天弘", "SH"),
    EtfFundDef("515380", "沪深300ETF泰康", "SH"),
    EtfFundDef("515660", "沪深300ETF国联安", "SH"),
    EtfFundDef("510320", "沪深300ETF中金", "SH"),
    EtfFundDef("510370", "沪深300ETF兴业", "SH"),
    EtfFundDef("159925", "沪深300ETF南方", "SZ"),
)

# Codes that are definitely 沪深300 trackers; used to reject bad overrides.
KNOWN_CODES = {fund.code: fund for fund in CSI300_FUNDS}

def exchange_of(code: str) -> str:
    """Infer the bourse from a 6-digit code (kept in sync with ``_em_secid``).

    Funds on the SSE run 5xxxxx, SZSE funds 15xxxx/16xxxx/18xxxx; shares follow
    the same prefix convention (6xxxxx/9xxxxx vs 0xxxxx/3xxxxx).
    """
    return "SH" if code.startswith(("5", "6", "9")) else "SZ"


def universe(override: str | None = None) -> list[EtfFundDef]:
    """Return the tracked fund list.

    Args:
        override: Comma/space separated codes from the API. Unknown codes are
            accepted (they might be a newly listed CSI 300 fund) but keep the
            curated names when the code is known.
    """
    if not override or not str(override).strip():
        return list(CSI300_FUNDS)

    out: list[EtfFundDef] = []
    seen: set[str] = set()
    for raw in str(override).replace(",", " ").split():
        code = raw.strip()
        if not code.isdigit() or len(code) != 6 or code in seen:
            continue
        seen.add(code)
        known = KNOWN_CODES.get(code)
        out.append(known if known is not None else EtfFundDef(code, code, exchange_of(code)))  # type: ignore[arg-type]
    return out or list(CSI300_FUNDS)


def universe_meta() -> dict[str, object]:
    """Static description for the API / methodology panel."""
    return {
        "index_code": INDEX_CODE,
        "index_label": INDEX_LABEL,
        "funds": [f.to_dict() for f in CSI300_FUNDS],
        "count": len(CSI300_FUNDS),
        "note": (
            "固定白名单：仅统计跟踪沪深300的场内ETF（不含联接基金、增强指数、"
            "300价值/成长/红利等衍生指数产品）。"
        ),
    }
