"""Shared symbol-type detection utilities for loaders.

Exchange-listed ETF / LOF prefix codes (same pattern across loaders):
  SH: 50/51/52/56/58 (ETFs), SZ: 15/16 (ETFs + LOFs).
"""

from __future__ import annotations

_ETF_PREFIXES = frozenset({"15", "16", "50", "51", "52", "56", "58"})


def _is_etf_listed(code: str) -> bool:
    """Detect exchange-listed ETF / LOF symbols (e.g. 510050.SH, 159915.SZ)."""
    upper = code.upper()
    if not upper.endswith((".SH", ".SZ")):
        return False
    digits = upper.split(".")[0]
    if len(digits) != 6 or not digits.isdigit():
        return False
    return digits[:2] in _ETF_PREFIXES


def _is_b_share(code: str) -> bool:
    """Shanghai/Shenzhen B-shares (900xxx.SH / 200xxx.SZ). Tencent returns HTTP 501."""
    upper = code.upper()
    digits = upper.split(".")[0]
    if len(digits) != 6 or not digits.isdigit():
        return False
    if upper.endswith(".SH") and digits.startswith("900"):
        return True
    if upper.endswith(".SZ") and digits.startswith("200"):
        return True
    return False


def _is_cn_index(code: str) -> bool:
    """SSE/SZSE index codes (000xxx.SH, 399xxx.SZ), not 000xxx.SZ stocks."""
    upper = code.upper()
    digits = upper.split(".")[0]
    if len(digits) != 6 or not digits.isdigit():
        return False
    if upper.endswith(".SH") and digits.startswith("000"):
        return True
    if upper.endswith(".SZ") and digits.startswith("399"):
        return True
    return False
