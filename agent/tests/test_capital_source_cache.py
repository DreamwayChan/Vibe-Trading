"""Cache behaviour of the capital-flow share adapters.

Regression guard for the "未披露" bug of 2026-11-03: both bourses answer
``200`` with **zero rows** while a day's file is still unpublished. That empty
answer used to be cached under the "settled day = cache forever" rule, so once
the session became a past day the page kept serving "未披露" even after the
exchange had published. These tests pin the rule: an empty snapshot is a
publish gap and must be retried; a non-empty one is final.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta

import pytest

from src.capital import sources


def _settled_day(days_ago: int = 3) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _compact(day: str) -> str:
    return day.replace("-", "")


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Point the adapter cache at a temp dir instead of ``~/.vibe-trading``."""
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path)
    return tmp_path


class _Resp:
    def __init__(self, rows):
        self._rows = rows
        self.status_code = 200
        self.text = ""

    def json(self):
        return {"result": self._rows}


def _rows(*codes):
    return [{"SEC_CODE": code, "TOT_VOL": "1000"} for code in codes]


def _write_snapshot(cache_dir, day: str, data: dict, *, attempts: int = 1, age_s: float = 0.0) -> None:
    path = cache_dir / "shares" / f"sse_{_compact(day)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"saved_at": "2026-11-05T00:00:00+00:00", "data": data, "attempts": attempts}),
        encoding="utf-8",
    )
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))


def test_settled_empty_snapshot_is_refetched(cache_dir, monkeypatch):
    """The 2026-11-03 case: an empty settled day must not be permanent."""
    day = _settled_day(2)
    _write_snapshot(cache_dir, day, {}, attempts=1, age_s=6 * 3600)

    calls: list[str] = []

    def fake_get(url, params=None, headers=None):
        calls.append(params["STAT_DATE"])
        return _Resp(_rows("510300", "510310"))

    monkeypatch.setattr(sources, "_get", fake_get)

    out = sources.sse_etf_shares(day)

    assert calls == [day], "an empty settled day must be asked again"
    assert out == {"510300": 1000 * sources._SSE_SHARE_UNIT, "510310": 1000 * sources._SSE_SHARE_UNIT}


def test_settled_full_snapshot_is_final(cache_dir, monkeypatch):
    """The normal path: a published day is never re-requested."""

    def fake_get(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("settled non-empty snapshot should be served from cache")

    monkeypatch.setattr(sources, "_get", fake_get)
    day = _settled_day(10)
    _write_snapshot(cache_dir, day, {"510300": 1.0e9}, age_s=48 * 3600)

    assert sources.sse_etf_shares(day) == {"510300": 1.0e9}


def test_fresh_empty_snapshot_is_reused(cache_dir, monkeypatch):
    """Within the empty TTL, no second request (page reloads stay cheap)."""

    def fake_get(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("fresh empty snapshot should be reused")

    monkeypatch.setattr(sources, "_get", fake_get)
    day = _settled_day(1)
    _write_snapshot(cache_dir, day, {}, attempts=2, age_s=60)

    assert sources.sse_etf_shares(day) == {}


def test_empty_snapshot_stops_after_max_attempts(cache_dir, monkeypatch):
    """A genuine gap (holiday, pre-listing day) is not re-asked forever."""

    def fake_get(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("gave up after _MAX_EMPTY_ATTEMPTS retries")

    monkeypatch.setattr(sources, "_get", fake_get)
    day = _settled_day(30)
    _write_snapshot(cache_dir, day, {}, attempts=sources._MAX_EMPTY_ATTEMPTS, age_s=48 * 3600)

    assert sources.sse_etf_shares(day) == {}


def test_attempt_counter_increments_on_refetch(cache_dir, monkeypatch):
    """Each retry is recorded so the give-up rule is reachable."""
    day = _settled_day(2)
    _write_snapshot(cache_dir, day, {}, attempts=3, age_s=6 * 3600)
    monkeypatch.setattr(sources, "_get", lambda *a, **k: _Resp([]))

    sources.sse_etf_shares(day)

    payload = json.loads((cache_dir / "shares" / f"sse_{_compact(day)}.json").read_text(encoding="utf-8"))
    assert payload["attempts"] == 4
    assert payload["data"] == {}


def test_szse_empty_day_is_retried(cache_dir, monkeypatch):
    """Same rule for the SZSE workbook: a lagged day heals on the next build."""
    import sys

    import pandas as pd

    day = _settled_day(2)
    path = cache_dir / "shares" / f"szse_{_compact(day)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": {}, "attempts": 1}), encoding="utf-8")
    stamp = time.time() - 6 * 3600
    os.utime(path, (stamp, stamp))

    frame = pd.DataFrame({"日期": [day], "基金代码": ["159919"], "基金份额": [5.0e8]})

    class _Ak:
        @staticmethod
        def fund_scale_daily_szse(**_kwargs):
            return frame

    monkeypatch.setitem(sys.modules, "akshare", _Ak)
    monkeypatch.setattr(sources, "_trading_day_span", lambda start, end: [day])

    out = sources.szse_etf_shares(day, day)

    assert out[day] == {"159919": 5.0e8}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["data"] == {"159919": 5.0e8}
    assert payload["attempts"] == 2
