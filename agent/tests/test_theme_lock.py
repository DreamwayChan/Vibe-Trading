"""Locked theme vs rank-1 map: hold while healthy, switch on failure or hysteresis."""

from __future__ import annotations

from src.sector_pick.models import SectorScore
from src.sector_pick.theme_lock import (
    CHALLENGER_CONFIRM_DAYS,
    ThemeLock,
    advance_lock_identity,
    should_steal,
    tick_challenger,
)


def _row(key: str, *, score: float, rank: int, name: str | None = None) -> SectorScore:
    return SectorScore(
        key=key,
        name=name or key,
        panel="x",
        run_id=f"run_{key}",
        variant="f0_prime",
        enabled=True,
        timing_mult=1.0,
        ret_20d=0.1,
        live_score=score,
        rank=rank,
    )


def test_theme_lock_roundtrip_ignores_junk() -> None:
    lock = ThemeLock.from_dict(
        {
            "locked_key": "copper",
            "challenger_key": "robot",
            "challenger_days": "2",
            "challenger_as_of": "2026-08-18",
        }
    )
    assert lock.locked_key == "copper"
    assert lock.challenger_days == 2
    restored = ThemeLock.from_dict(lock.to_dict())
    assert restored == lock
    assert ThemeLock.from_dict(None).locked_key is None
    assert ThemeLock.from_dict({"challenger_days": "nope"}).challenger_days == 0


def test_tick_challenger_starts_then_freezes_same_day() -> None:
    locked = _row("copper", score=1.0, rank=2, name="金属铜")
    leader = _row("robot", score=1.4, rank=1, name="人形机器人")
    first = tick_challenger(ThemeLock(locked_key="copper"), map_row=leader, locked_row=locked, as_of="2026-08-18")
    assert first.challenger_key == "robot"
    assert first.challenger_days == 1
    again = tick_challenger(first, map_row=leader, locked_row=locked, as_of="2026-08-18")
    assert again.challenger_days == 1
    next_day = tick_challenger(again, map_row=leader, locked_row=locked, as_of="2026-08-19")
    assert next_day.challenger_days == 2
    assert should_steal(next_day, map_row=leader, locked_row=locked) is True


def test_tick_challenger_clears_when_map_is_the_lock_or_margin_is_thin() -> None:
    locked = _row("copper", score=1.0, rank=1)
    assert tick_challenger(
        ThemeLock(locked_key="copper", challenger_key="robot", challenger_days=1),
        map_row=locked,
        locked_row=locked,
        as_of="2026-08-18",
    ).challenger_key is None
    thin = _row("robot", score=1.1, rank=1)
    cleared = tick_challenger(
        ThemeLock(locked_key="copper", challenger_key="robot", challenger_days=1),
        map_row=thin,
        locked_row=locked,
        as_of="2026-08-18",
    )
    assert cleared.challenger_key is None
    assert should_steal(cleared, map_row=thin, locked_row=locked) is False
    assert CHALLENGER_CONFIRM_DAYS == 2


def test_advance_lock_identity_ticks_then_steals_without_book_probe() -> None:
    locked = _row("speceq", score=0.85, rank=6, name="其他专用设备")
    leader = _row("vrar", score=1.73, rank=1, name="虚拟现实")
    day1 = advance_lock_identity(
        ThemeLock(locked_key="speceq"),
        map_row=leader,
        locked_row=locked,
        as_of="2026-09-07",
    )
    assert day1.locked_key == "speceq"
    assert day1.challenger_key == "vrar"
    assert day1.challenger_days == 1
    day2 = advance_lock_identity(
        day1, map_row=leader, locked_row=locked, as_of="2026-09-08"
    )
    assert day2.locked_key == "vrar"
    assert day2.challenger_key is None
    same_day = advance_lock_identity(
        day1, map_row=leader, locked_row=locked, as_of="2026-09-07"
    )
    assert same_day.challenger_days == 1
    assert same_day.locked_key == "speceq"
