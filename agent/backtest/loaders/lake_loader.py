"""Lake loader: bars from the local lake, no network.

The ingest jobs (``scripts/build_tradability.py``, ``scripts/build_intraday.py``)
write into the lake; this loader is the read side, so a backtest can run on
accumulated history instead of re-fetching.  That matters most for intraday:
the free feeds only serve a recent window, so the lake is the only place a year
of 5-minute bars can exist.

Registered as source ``"lake"`` and placed just before ``local`` in the A-share
chain.  ``is_available()`` is False until something has actually been ingested,
so the chain skips it on a fresh install rather than returning an empty universe.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders._intervals import normalize_interval, resample_ohlcv
from backtest.loaders.base import validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

#: Finest-to-coarsest ordering, used to find a stored interval that can be
#: aggregated up to the one being requested.
_LADDER = ("1m", "5m", "15m", "30m", "1H", "4H", "1D")


@register
class DataLoader:
    """OHLCV loader backed by ``<lake>/bars_daily`` and ``<lake>/bars_intraday``."""

    name = "lake"
    markets = {"a_share"}
    requires_auth = False
    SUPPORTED_INTERVALS = frozenset({"1D", "1m", "5m", "15m", "30m", "1H", "4H"})

    def is_available(self) -> bool:
        """Available once at least one bar dataset exists on disk."""
        try:
            from src.data import bars as bar_store
            from src.data.lake import dataset_exists

            return dataset_exists(bar_store.DATASET_DAILY) or dataset_exists(
                bar_store.DATASET_INTRADAY
            )
        except Exception as exc:  # noqa: BLE001 — lake is optional at import time
            logger.debug("lake loader unavailable: %s", exc)
            return False

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Read stored bars for *codes* in the requested window.

        Args:
            codes: Symbols, exactly as ingested (e.g. ``600519.SH``).
            start_date: Inclusive lower bound.
            end_date: Inclusive upper bound.
            interval: Bar size; the lake keeps each interval in its own column.
            fields: Ignored.

        Returns:
            Mapping symbol -> OHLCV frame. When nothing is stored at the
            requested granularity, a finer stored interval is aggregated up; if
            neither exists the mapping is empty and the caller's fallback chain
            goes to the network.
        """
        validate_date_range(start_date, end_date)
        del fields

        from src.data import bars as bar_store

        token = normalize_interval(interval)
        try:
            long_frame = bar_store.load_bars(
                token, symbols=list(codes), start=start_date, end=end_date
            )
        except Exception as exc:  # noqa: BLE001 — a corrupt lake must not break the run
            logger.warning("lake read failed for %s: %s", token, exc)
            return {}

        if long_frame.empty:
            return self._aggregate_finer(
                bar_store, token, codes, start_date, end_date
            )

        return self._to_frames(long_frame, codes, token)

    def _aggregate_finer(
        self,
        bar_store,
        token: str,
        codes: List[str],
        start_date: str,
        end_date: str,
    ) -> Dict[str, pd.DataFrame]:
        """Serve *token* by aggregating a finer interval already on disk.

        Storing one minute granularity and deriving the coarser ones locally is
        free and exact (same source, same session boundaries), so a lake that
        holds 5-minute bars can answer a 1-hour request without going back to
        the network.
        """
        if token not in _LADDER:
            return {}
        ceiling = _LADDER.index(token)
        for finer in reversed(_LADDER[:ceiling]):
            try:
                frame = bar_store.load_bars(
                    finer, symbols=list(codes), start=start_date, end=end_date
                )
            except Exception as exc:  # noqa: BLE001 — try the next granularity
                logger.debug("lake read for %s failed: %s", finer, exc)
                continue
            if frame.empty:
                continue
            aggregated = {}
            for symbol, part in bar_store.bars_by_symbol(frame).items():
                aggregated[symbol] = resample_ohlcv(part, token, symbol=symbol)
            logger.info("lake served %s by aggregating stored %s bars", token, finer)
            return {sym: df for sym, df in aggregated.items() if df is not None and not df.empty}
        return {}

    @staticmethod
    def _to_frames(
        long_frame: pd.DataFrame, codes: List[str], token: str
    ) -> Dict[str, pd.DataFrame]:
        """Split a long frame into per-symbol frames and report what is missing."""
        from src.data import bars as bar_store

        frames = bar_store.bars_by_symbol(long_frame)
        missing = [code for code in codes if code not in frames]
        if missing:
            logger.info(
                "lake covered %d/%d symbols for %s; missing %s",
                len(frames), len(codes), token, missing[:5],
            )
        return frames
