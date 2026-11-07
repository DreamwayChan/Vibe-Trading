"""Data foundation for A-share research.

Four pieces, deliberately split so each can be used without the others:

* :mod:`src.data.lake` - partitioned parquet store with key upserts.
* :mod:`src.data.calendar` - the exchange trading calendar.
* :mod:`src.data.tradability` - per-(symbol, date) halt / price-band panel.
* :mod:`src.data.bars` - daily and intraday OHLCV persisted into the lake,
  which is what lets window-limited minute feeds accumulate into history.

Import the submodule you need rather than relying on package-level re-exports;
the calendar and panel paths touch the filesystem and (for the calendar seed)
the network on first use.
"""
