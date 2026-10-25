"""Eastmoney industry/concept lists and constituents, cached per trade date.

Board taxonomy is free push2 JSON (no Tushare points). Constituents are written
per BK so a half-finished first scan can resume without re-hitting every board.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from backtest.loaders.eastmoney_client import get_json
from src.market_pick.store import market_pick_dir

logger = logging.getLogger(__name__)

BoardType = Literal["industry", "concept"]

# Realtime ``push2`` is often RST'd (WAF / CDN). Research scripts already
# prefer ``push2delay`` for the same clist path — try that first.
_CLIST_URLS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
_UT = "bd1d9ddb04089700cf9c27f6f7426281"
# Sticky host for this process so a working delay node is not re-probed
# (and a dead push2 node is not re-hit) on every board.
_working_clist_url: str | None = None
_LIST_FIELDS = "f12,f14"
_MEMBER_FIELDS = "f12,f13,f14"

# Same event/hype boards the research screener drops — not a dual-track tape.
_EXCLUDE_NAME_SUBSTR = (
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
)

_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
}


@dataclass
class BoardUniverse:
    """One Eastmoney board and the A-share codes currently listed on it."""

    bk: str
    name: str
    board_type: BoardType
    symbols: list[str] = field(default_factory=list)


def load_board_universe(
    as_of: date,
    *,
    refresh_lists: bool = False,
) -> list[BoardUniverse]:
    """Industry + concept boards with constituents, using the daily member cache."""
    boards = _load_board_list(as_of, refresh=refresh_lists)
    out: list[BoardUniverse] = []
    for meta in boards:
        symbols = _load_constituents(as_of, meta["bk"], refresh=refresh_lists)
        out.append(
            BoardUniverse(
                bk=meta["bk"],
                name=meta["name"],
                board_type=meta["board_type"],  # type: ignore[arg-type]
                symbols=symbols,
            )
        )
    return out


def unique_symbols(boards: list[BoardUniverse]) -> list[str]:
    """Dedup member codes so OHLCV is fetched once, not once per board."""
    seen: set[str] = set()
    out: list[str] = []
    for board in boards:
        for code in board.symbols:
            if code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out


def _load_board_list(as_of: date, *, refresh: bool) -> list[dict[str, str]]:
    path = _list_path(as_of)
    if path.is_file() and not refresh:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                return [row for row in raw if isinstance(row, dict) and row.get("bk")]
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("market-pick board list cache unreadable: %s", exc)
    rows: list[dict[str, str]] = []
    for board_type in ("industry", "concept"):
        rows.extend(_fetch_board_list(board_type))  # type: ignore[arg-type]
    _atomic_write(path, rows)
    return rows


def _load_constituents(
    as_of: date,
    bk: str,
    *,
    refresh: bool,
) -> list[str]:
    path = _member_path(as_of, bk)
    if path.is_file() and not refresh:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [str(code).upper() for code in raw if str(code).strip()]
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("market-pick member cache miss for %s: %s", bk, exc)
    try:
        symbols = _fetch_constituents(bk)
    except Exception as exc:  # noqa: BLE001 — one dead board must not abort the day
        logger.warning("market-pick constituents %s failed: %s", bk, exc)
        return []
    _atomic_write(path, symbols)
    return symbols


def _clist_get(params: dict[str, Any]) -> Any:
    """GET clist, preferring the last host that answered in this process."""
    global _working_clist_url
    urls = list(_CLIST_URLS)
    if _working_clist_url in urls:
        urls = [_working_clist_url] + [url for url in urls if url != _working_clist_url]
    last_error: Exception | None = None
    for url in urls:
        try:
            payload = get_json(url, params=params)
        except Exception as exc:  # noqa: BLE001 — try the next Eastmoney host
            last_error = exc
            logger.warning("market-pick clist %s failed: %s", url, exc)
            continue
        _working_clist_url = url
        return payload
    if last_error is not None:
        raise last_error
    raise RuntimeError("market-pick clist: no Eastmoney host configured")


def _fetch_board_list(board_type: BoardType) -> list[dict[str, str]]:
    fs = _FS[board_type]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for pn in range(1, 8):
        payload = _clist_get(
            {
                "pn": pn,
                "pz": 100,
                "po": 1,
                "np": 1,
                "ut": _UT,
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": fs,
                "fields": _LIST_FIELDS,
            },
        )
        diff = _diff_rows(payload)
        if not diff:
            break
        for item in diff:
            if not isinstance(item, dict):
                continue
            bk = str(item.get("f12") or "").strip().upper()
            name = str(item.get("f14") or "").strip()
            if not bk or not name or bk in seen:
                continue
            if any(token in name for token in _EXCLUDE_NAME_SUBSTR):
                continue
            seen.add(bk)
            rows.append({"bk": bk, "name": name, "board_type": board_type})
        total = 0
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
        if total and len(seen) >= total:
            break
    return rows


def _fetch_constituents(bk: str) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for pn in range(1, 6):
        payload = _clist_get(
            {
                "pn": pn,
                "pz": 100,
                "po": 1,
                "np": 1,
                "ut": _UT,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": f"b:{bk}",
                "fields": _MEMBER_FIELDS,
            },
        )
        diff = _diff_rows(payload)
        if not diff:
            break
        for item in diff:
            if not isinstance(item, dict):
                continue
            code = _to_ts_code(item)
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
    return codes


def _to_ts_code(row: dict[str, Any]) -> str | None:
    raw = str(row.get("f12") or "").strip().zfill(6)
    if not raw.isdigit():
        return None
    # BJ + B-shares: dual-track tape is A-share only; Tencent 501s on 900/200.
    if raw.startswith(("4", "8", "92", "900", "200")):
        return None
    market = int(row.get("f13") or 0)
    if market == 1:
        return f"{raw}.SH"
    return f"{raw}.SZ"


def _diff_rows(payload: Any) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    diff = data.get("diff")
    if isinstance(diff, dict):
        return list(diff.values())
    if isinstance(diff, list):
        return diff
    return []


def _list_path(as_of: date) -> Path:
    return market_pick_dir() / "boards" / f"{as_of.isoformat()}.json"


def _member_path(as_of: date, bk: str) -> Path:
    return market_pick_dir() / "members" / as_of.isoformat() / f"{bk.upper()}.json"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=".mp_", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
