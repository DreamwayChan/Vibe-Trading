"""Unit tests for the multi-token Tushare client pool."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backtest.loaders import tushare_pool as pool_mod
from backtest.loaders.tushare_pool import (
    TushareClientPool,
    bind_tushare_client,
    is_hourly_rate_limit,
    is_rate_limit_error,
    is_transport_error,
    parse_token_list,
    post_tushare_api,
    reset_tushare_pool,
    resolve_tushare_api_url,
    resolve_tushare_tokens,
)


@pytest.fixture(autouse=True)
def _reset_pool():
    reset_tushare_pool()
    yield
    reset_tushare_pool()


@pytest.fixture(autouse=True)
def _isolated_quota_ledger(tmp_path, monkeypatch):
    """Keep daily-quota bookkeeping out of the real lake during tests."""
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))


def test_parse_token_list_splits_comma_semicolon_newline() -> None:
    assert parse_token_list("a,b;c\nd") == ["a", "b", "c", "d"]
    assert parse_token_list("  x  ,  y  ") == ["x", "y"]
    assert parse_token_list("") == []


def test_resolve_tushare_tokens_merges_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "primary")
    monkeypatch.setenv("TUSHARE_TOKENS", "primary,extra1\nextra2")
    from src.config.accessor import reset_env_config

    reset_env_config()
    assert resolve_tushare_tokens() == ["primary", "extra1", "extra2"]


def test_resolve_skips_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "your-tushare-token")
    monkeypatch.setenv("TUSHARE_TOKENS", "")
    from src.config.accessor import reset_env_config

    reset_env_config()
    assert resolve_tushare_tokens() == []


def test_resolve_tushare_api_url_defaults_to_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TUSHARE_API_URL", raising=False)
    from src.config.accessor import reset_env_config

    reset_env_config()
    assert resolve_tushare_api_url() == "https://api.tushare.pro"


def test_bind_tushare_client_overrides_sdk_http_url() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self._DataApi__http_url = "http://api.tushare.pro"
            self._DataApi__timeout = 10

    api = FakeApi()
    bind_tushare_client(api, api_url="https://api.tushare.pro", timeout=30)
    assert api._DataApi__http_url == "https://api.tushare.pro"
    assert api._DataApi__timeout == 30


def test_is_rate_limit_error_detects_chinese_message() -> None:
    exc = RuntimeError(
        "抱歉，您访问接口(daily)频率超限(50次/分钟)，具体频次详情：https://tushare.pro/document/1?doc_id=108。"
    )
    assert is_rate_limit_error(exc)
    assert not is_rate_limit_error(RuntimeError("network timeout"))


def test_is_hourly_rate_limit_detects_index_daily_quota() -> None:
    hourly = RuntimeError(
        "抱歉，您访问接口(index_daily)频率超限(1次/小时)，具体频次详情：https://tushare.pro/document/1?doc_id=108。"
    )
    per_minute = RuntimeError("抱歉，您访问接口(daily)频率超限(50次/分钟)")
    assert is_hourly_rate_limit(hourly) is True
    assert is_hourly_rate_limit(per_minute) is False
    assert is_hourly_rate_limit(RuntimeError("network timeout")) is False


def test_is_transport_error_detects_reset_and_empty_json() -> None:
    assert is_transport_error(
        ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")
    )
    assert is_transport_error(RuntimeError("Expecting value: line 1 column 1 (char 0)"))
    assert is_transport_error(ValueError("tushare empty HTTP body from https://api.tushare.pro"))
    assert is_transport_error(
        RuntimeError(
            "('Connection aborted.', ConnectionResetError(10054, "
            "'远程主机强迫关闭了一个现有的连接。', None, 10054, None))"
        )
    )
    assert not is_transport_error(RuntimeError("抱歉，您访问接口(daily)频率超限"))
    assert not is_transport_error(ValueError("unknown ts_code"))


def test_post_tushare_api_raises_on_nonzero_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        text = '{"code":40101,"data":null,"msg":"不是合格的json格式，EOF"}'

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        pool_mod,
        "tushare_http_session",
        lambda: SimpleNamespace(post=lambda *a, **k: _Resp()),
    )
    with pytest.raises(RuntimeError, match="40101"):
        post_tushare_api("tok", "daily", ts_code="000001.SZ")


def test_post_tushare_api_builds_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        text = (
            '{"code":0,"msg":"","data":{"fields":["ts_code","close"],'
            '"items":[["000001.SZ",10.5]]}}'
        )

    captured: dict[str, object] = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(
        pool_mod,
        "tushare_http_session",
        lambda: SimpleNamespace(post=fake_post),
    )
    monkeypatch.setenv("TUSHARE_API_URL", "https://api.tushare.pro")
    from src.config.accessor import reset_env_config

    reset_env_config()
    frame = post_tushare_api(
        "tok", "daily", ts_code="000001.SZ", start_date="20250102", fields="ts_code,close"
    )
    assert captured["url"] == "https://api.tushare.pro"
    payload = captured["payload"]
    assert payload["api_name"] == "daily"
    assert payload["params"] == {"ts_code": "000001.SZ", "start_date": "20250102"}
    assert payload["fields"] == "ts_code,close"
    assert list(frame["ts_code"]) == ["000001.SZ"]


def test_pool_round_robins_ready_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(token: str, api_name: str, **_kwargs):
        del api_name
        return f"ok:{token}"

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    pool = TushareClientPool(tokens=["tok-a", "tok-b"], rpm=50, cooldown_seconds=0.01)
    assert pool.call("daily", ts_code="000001.SZ") == "ok:tok-a"
    assert pool.call("daily", ts_code="000002.SZ") == "ok:tok-b"
    assert pool.call("daily", ts_code="000003.SZ") == "ok:tok-a"


def test_pool_retries_on_rate_limit_with_other_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"bad": 0, "good": 0}

    def fake_post(token: str, api_name: str, **_kwargs):
        del api_name
        calls[token] += 1
        if token == "bad":
            raise RuntimeError("抱歉，您访问接口(daily)频率超限(50次/分钟)")
        return "good"

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    pool = TushareClientPool(
        tokens=["bad", "good"],
        rpm=50,
        cooldown_seconds=0.05,
    )
    assert pool.call("daily", ts_code="000001.SZ") == "good"
    assert calls["bad"] >= 1
    assert calls["good"] == 1


def test_pool_hourly_quota_fails_fast_and_keeps_daily_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """index_daily 1/hour must not sleep 65s×N or freeze the daily() quota."""
    sleeps: list[float] = []
    monkeypatch.setattr(pool_mod.time, "sleep", lambda s: sleeps.append(float(s)))
    calls: list[tuple[str, str]] = []

    def fake_post(token: str, api_name: str, **_kwargs):
        calls.append((token, api_name))
        if api_name == "index_daily":
            raise RuntimeError(
                "抱歉，您访问接口(index_daily)频率超限(1次/小时)，具体频次详情："
            )
        return "ok-daily"

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    pool = TushareClientPool(
        tokens=["tok-a", "tok-b", "tok-c"],
        rpm=50,
        cooldown_seconds=65.0,
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="1次/小时"):
        pool.call("index_daily", ts_code="000300.SH")
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert calls == [
        ("tok-a", "index_daily"),
        ("tok-b", "index_daily"),
        ("tok-c", "index_daily"),
    ]
    # daily() on the same tokens must still run — hourly block is per-endpoint.
    assert pool.call("daily", ts_code="000001.SZ") == "ok-daily"
    assert ("tok-a", "daily") in calls or ("tok-b", "daily") in calls or ("tok-c", "daily") in calls
    assert all(s < 2.0 for s in sleeps)


def test_pool_blocks_when_rpm_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_mod, "post_tushare_api", lambda *a, **k: "ok")
    pool = TushareClientPool(tokens=["only"], rpm=2, window_seconds=0.2, cooldown_seconds=0.01)
    assert pool.call("daily") == "ok"
    assert pool.call("daily") == "ok"
    started = time.monotonic()
    assert pool.call("daily") == "ok"
    elapsed = time.monotonic() - started
    # Third call must wait for the sliding window to free a slot.
    assert elapsed >= 0.15


def test_pool_retries_transport_error_and_rebuilds_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped keep-alive session must be discarded and retried."""
    monkeypatch.setattr(pool_mod.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}
    resets = {"n": 0}

    def fake_post(token: str, api_name: str, **_kwargs):
        del token, api_name
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")
        return "ok"

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    monkeypatch.setattr(
        pool_mod, "reset_tushare_http_session", lambda: resets.__setitem__("n", resets["n"] + 1)
    )
    pool = TushareClientPool(tokens=["only"], rpm=50, cooldown_seconds=0.01)
    assert pool.call("daily", ts_code="000001.SZ") == "ok"
    assert calls["n"] == 2
    assert resets["n"] >= 1


def test_pool_does_not_retry_non_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_post(token: str, api_name: str, **_kwargs):
        del token, api_name
        calls["n"] += 1
        raise ValueError("unknown ts_code")

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    pool = TushareClientPool(tokens=["only"], rpm=50, cooldown_seconds=0.01)
    with pytest.raises(ValueError, match="unknown ts_code"):
        pool.call("daily", ts_code="000001.SZ")
    assert calls["n"] == 1


def test_get_tushare_pool_singleton_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "singleton-token")
    monkeypatch.delenv("TUSHARE_TOKENS", raising=False)
    from src.config.accessor import reset_env_config

    reset_env_config()
    reset_tushare_pool()
    first = pool_mod.get_tushare_pool()
    second = pool_mod.get_tushare_pool()
    assert first is second
    assert first.token_count == 1
    reset_tushare_pool()
    third = pool_mod.get_tushare_pool()
    assert third is not first


# ---------------------------------------------------------------------------
# Per-day quotas and sub-RPM endpoint pacing
#
# ``stk_mins`` on a 120-point account answers 40203 with three different
# messages depending on which window broke: "2次/天" (a day, resets at Beijing
# midnight), "1次/小时" and "1次/分钟". Treating all three as "RPM breach, cool
# 65s and try the next token" spends every account's whole daily allowance in
# one request — which is exactly what happened before these tests existed.
# ---------------------------------------------------------------------------


def test_daily_quota_marker_is_not_classified_as_hourly() -> None:
    daily = RuntimeError("频率超限(2次/天)")
    hourly = RuntimeError("频率超限(1次/小时)")
    per_minute = RuntimeError("频率超限(1次/分钟)")

    assert pool_mod.is_daily_quota_error(daily) is True
    assert pool_mod.is_daily_quota_error(hourly) is False
    assert pool_mod.is_daily_quota_error(per_minute) is False

    assert pool_mod.is_hourly_rate_limit(hourly) is True
    assert pool_mod.is_per_minute_limit(per_minute) is True
    assert pool_mod.is_per_minute_limit(hourly) is False


def test_seconds_until_reset_is_within_a_day() -> None:
    remaining = pool_mod.seconds_until_quota_reset()
    assert 0 <= remaining <= 24 * 3600


def test_daily_quota_exhausts_once_and_stops_retrying(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))
    attempts: list[str] = []

    def fake_post(token: str, api_name: str, **_kwargs):
        attempts.append(token)
        raise RuntimeError(
            "tushare stk_mins error 40203: 抱歉，您访问接口(stk_mins)频率超限(2次/天)"
        )

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    pool = TushareClientPool(tokens=["tok-a", "tok-b", "tok-c"], rpm=50, cooldown_seconds=0.01)

    with pytest.raises(pool_mod.TushareQuotaExhausted):
        pool.call("stk_mins", ts_code="600519.SH")
    # One attempt per token: no retry storm after the day is gone.
    assert sorted(attempts) == ["tok-a", "tok-b", "tok-c"]

    # The ledger remembers, so a fresh pool fails without any HTTP call.
    second = TushareClientPool(tokens=["tok-a", "tok-b", "tok-c"], rpm=50)
    attempts.clear()
    with pytest.raises(pool_mod.TushareQuotaExhausted):
        second.call("stk_mins", ts_code="000001.SZ")
    assert attempts == []
    assert second.quota_report()["stk_mins"]["remaining"] == 0


def test_daily_exhaustion_does_not_block_daily_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("VIBE_TRADING_LAKE_ROOT", str(tmp_path))

    def fake_post(token: str, api_name: str, **_kwargs):
        if api_name == "stk_mins":
            raise RuntimeError("频率超限(2次/天)")
        return f"ok:{api_name}"

    monkeypatch.setattr(pool_mod, "post_tushare_api", fake_post)
    pool = TushareClientPool(tokens=["tok-a"], rpm=50, cooldown_seconds=0.01)
    with pytest.raises(pool_mod.TushareQuotaExhausted):
        pool.call("stk_mins", ts_code="600519.SH")
    assert pool.call("daily", ts_code="600519.SH") == "ok:daily"


def test_min_interval_paces_endpoint_across_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """stk_mins allows 1/minute per token, so the second call must use tok-b."""
    monkeypatch.setattr(pool_mod, "post_tushare_api", lambda token, api, **k: f"ok:{token}")
    pool = TushareClientPool(tokens=["tok-a", "tok-b"], rpm=50, cooldown_seconds=0.01)

    assert pool.call("stk_mins", ts_code="600519.SH") == "ok:tok-a"
    # tok-a just fired and must wait ~61s; the scheduler should reach for tok-b
    # instead of sleeping.
    assert pool.call("stk_mins", ts_code="000001.SZ") == "ok:tok-b"
    assert pool.earliest_wait("stk_mins") > 50


def test_min_interval_does_not_apply_to_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pool_mod, "post_tushare_api", lambda token, api, **k: f"ok:{token}")
    pool = TushareClientPool(tokens=["tok-a"], rpm=50, cooldown_seconds=0.01)
    assert pool.call("daily", ts_code="600519.SH") == "ok:tok-a"
    assert pool.call("daily", ts_code="600519.SH") == "ok:tok-a"
    assert pool.earliest_wait("daily") == 0.0
