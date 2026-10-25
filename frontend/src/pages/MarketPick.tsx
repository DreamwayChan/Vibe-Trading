import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { ChevronDown, ChevronUp, ChevronsUpDown, Compass, Loader2, RefreshCw } from "lucide-react";
import {
  ApiError,
  api,
  type MarketPickBoardScore,
  type MarketPickHistorySummary,
  type MarketPickResult,
} from "@/lib/api";
import { HorizonStrip } from "@/components/process/HorizonStrip";
import { cn } from "@/lib/utils";

type BoardSortKey = "rank" | "rs" | "knife" | "ret20";
type SortDir = "asc" | "desc";

function finiteOrNull(value: number | null | undefined): number | null {
  if (value == null || Number.isNaN(value) || !Number.isFinite(value)) return null;
  return value;
}

function boardSortValue(row: MarketPickBoardScore, key: BoardSortKey): number | null {
  if (key === "rank") return finiteOrNull(row.rank);
  if (key === "rs") return finiteOrNull(row.rs_share);
  if (key === "knife") return finiteOrNull(row.knife_share);
  return finiteOrNull(row.ret_20d);
}

function compareBoards(
  left: MarketPickBoardScore,
  right: MarketPickBoardScore,
  key: BoardSortKey,
  dir: SortDir,
): number {
  const leftVal = boardSortValue(left, key);
  const rightVal = boardSortValue(right, key);
  const leftHas = leftVal != null;
  const rightHas = rightVal != null;
  // Keep blanks at the bottom in both directions so a missing metric never
  // jumps to the top just because the user asked for high-to-low.
  if (leftHas && rightHas) {
    const delta = leftVal - rightVal;
    if (delta !== 0) return dir === "asc" ? delta : -delta;
  } else if (leftHas) {
    return -1;
  } else if (rightHas) {
    return 1;
  }
  return left.name.localeCompare(right.name);
}

function SortableHeader({
  label,
  column,
  activeKey,
  dir,
  sortByLabel,
  glossary,
  onSort,
}: {
  label: string;
  column: BoardSortKey;
  activeKey: BoardSortKey;
  dir: SortDir;
  sortByLabel: string;
  /** Term gloss shown on hover; kept ahead of the sort hint. */
  glossary?: string;
  onSort: (key: BoardSortKey) => void;
}) {
  const isActive = activeKey === column;
  const title = glossary ? `${glossary}\n\n${sortByLabel}` : sortByLabel;
  return (
    <th
      className="px-3 py-2 font-medium"
      aria-sort={isActive ? (dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        onClick={() => onSort(column)}
        title={title}
        aria-label={sortByLabel}
        className="inline-flex items-center gap-1 whitespace-nowrap cursor-pointer hover:text-foreground"
      >
        {label}
        {isActive ? (
          dir === "asc" ? (
            <ChevronUp className="h-3 w-3" aria-hidden />
          ) : (
            <ChevronDown className="h-3 w-3" aria-hidden />
          )
        ) : (
          <ChevronsUpDown className="h-3 w-3 opacity-40" aria-hidden />
        )}
      </button>
    </th>
  );
}

function formatPct(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function todayISO(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function stanceLabel(stance: string | undefined, t: TFunction): string {
  if (stance === "full") return t("marketPick.stanceFull");
  if (stance === "half_watch") return t("marketPick.stanceHalf");
  return t("marketPick.stanceNone");
}

export function MarketPick() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [result, setResult] = useState<MarketPickResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [tracking, setTracking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<MarketPickHistorySummary[]>([]);
  const [historyAsOf, setHistoryAsOf] = useState<string | null>(null);
  const [historyDetail, setHistoryDetail] = useState<MarketPickResult | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  // Rank asc matches the scan order; other columns start high-to-low on first click.
  const [sort, setSort] = useState<{ key: BoardSortKey; dir: SortDir }>({
    key: "rank",
    dir: "asc",
  });

  const applyLiveResult = (next: MarketPickResult | null) => {
    setResult(next);
    // A new live scan always returns to "latest"; history pills stay for replay.
    setHistoryAsOf(null);
    setHistoryDetail(null);
  };

  const refreshHistory = async () => {
    try {
      const rows = await api.listMarketPickHistory(30);
      setHistory(Array.isArray(rows) ? rows : []);
    } catch {
      setHistory([]);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const board = await api.getMarketPick();
        if (cancelled) return;
        applyLiveResult(board.result ?? null);
        await refreshHistory();
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : t("marketPick.loadFailed"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  const liveResult = result;
  const viewResult = historyAsOf && historyDetail ? historyDetail : liveResult;

  const ranked = useMemo(() => {
    const rows = [...(viewResult?.boards || [])];
    rows.sort((left, right) => compareBoards(left, right, sort.key, sort.dir));
    return rows;
  }, [viewResult, sort]);

  const toggleSort = (nextKey: BoardSortKey) => {
    setSort((prev) => {
      if (prev.key === nextKey) {
        return { key: nextKey, dir: prev.dir === "asc" ? "desc" : "asc" };
      }
      // Rank 1 is the top pick, so a fresh click restores low-to-high rank.
      // Strength / knife / 20-day are "higher is stronger", so they open desc.
      return { key: nextKey, dir: nextKey === "rank" ? "asc" : "desc" };
    });
  };

  const recommended = useMemo(() => {
    const bk = viewResult?.recommended_bk;
    if (!bk) return null;
    return ranked.find((row) => row.bk === bk) ?? null;
  }, [viewResult, ranked]);

  const runScan = async () => {
    setScanning(true);
    setError(null);
    // Same-day overwrite uses the live snapshot date, not a replayed history day.
    const force = Boolean(liveResult && liveResult.as_of === todayISO());
    try {
      const board = await api.scanMarketPick({ force });
      applyLiveResult(board.result ?? null);
      await refreshHistory();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(t("marketPick.alreadyScanned"));
        try {
          const board = await api.getMarketPick();
          applyLiveResult(board.result ?? null);
          await refreshHistory();
        } catch {
          /* keep the 409 copy */
        }
      } else {
        setError(e instanceof Error ? e.message : t("marketPick.scanFailed"));
      }
    } finally {
      setScanning(false);
    }
  };

  const selectHistoryDay = async (asOf: string | null) => {
    if (!asOf) {
      setHistoryAsOf(null);
      setHistoryDetail(null);
      return;
    }
    setHistoryLoading(true);
    setError(null);
    try {
      const detail = await api.getMarketPickHistoryDay(asOf);
      setHistoryAsOf(asOf);
      setHistoryDetail(detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("marketPick.historyLoadFailed"));
    } finally {
      setHistoryLoading(false);
    }
  };

  const trackBoard = async (row: MarketPickBoardScore) => {
    const key = row.catalog_key;
    if (!key) return;
    setTracking(key);
    setError(null);
    try {
      const board = await api.getSectorPick();
      const keys = new Set(board.enabled_keys || []);
      keys.add(key);
      await api.updateSectorPick({ enabled_keys: [...keys] });
      navigate(`/sector-pick?key=${encodeURIComponent(key)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("marketPick.trackFailed"));
    } finally {
      setTracking(null);
    }
  };

  if (loading) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("marketPick.loading")}
      </div>
    );
  }

  const regimeKey = viewResult?.market_regime ? viewResult.market_regime : null;

  return (
    <div className="flex flex-col gap-6 p-6 max-w-5xl mx-auto">
      <div className="flex items-start gap-3">
        <Compass className="h-6 w-6 text-primary mt-0.5" />
        <div>
          <h1 className="text-2xl font-bold">{t("marketPick.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("marketPick.subtitle")}</p>
          <HorizonStrip regime={regimeKey} horizons={viewResult?.horizons} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={runScan}
          disabled={scanning}
          className="inline-flex items-center gap-2 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm hover:opacity-90 disabled:opacity-50"
        >
          {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          {scanning ? t("marketPick.scanning") : t("marketPick.refreshToday")}
        </button>
        {liveResult?.scanned_at && (
          <span className="text-xs text-muted-foreground">
            {t("marketPick.lastScan", { time: liveResult.scanned_at })}
            {historyAsOf ? ` · ${historyAsOf}` : liveResult.as_of ? ` · ${liveResult.as_of}` : ""}
          </span>
        )}
        {viewResult ? (
          <span className="text-xs text-muted-foreground">
            {t("marketPick.cacheStats", {
              unique: viewResult.unique_symbols ?? 0,
              ok: viewResult.ohlcv_ok ?? 0,
              missing: Math.max(0, (viewResult.unique_symbols ?? 0) - (viewResult.ohlcv_ok ?? 0)),
            })}
          </span>
        ) : null}
      </div>

      {history.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <div className="text-xs font-medium text-muted-foreground">{t("marketPick.history")}</div>
          <div className="flex flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => selectHistoryDay(null)}
              disabled={historyLoading}
              className={cn(
                "rounded-md border px-2 py-1 text-xs",
                !historyAsOf ? "border-primary bg-primary/10 text-primary" : "hover:bg-muted",
              )}
            >
              {t("marketPick.historyLatest")}
            </button>
            {history.map((row) => (
              <button
                key={row.as_of}
                type="button"
                onClick={() => selectHistoryDay(row.as_of)}
                disabled={historyLoading}
                title={`${row.recommended_name || row.recommended_bk || "—"} · ${row.stance || "none"}`}
                className={cn(
                  "rounded-md border px-2 py-1 text-xs font-mono",
                  historyAsOf === row.as_of
                    ? "border-primary bg-primary/10 text-primary"
                    : "hover:bg-muted",
                )}
              >
                {row.as_of}
              </button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">{t("marketPick.historyHint")}</p>
        </div>
      )}

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="rounded-lg border p-4">
        {recommended ? (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 className="text-lg font-semibold">{recommended.name}</h2>
              <span className="text-xs rounded-md border px-2 py-0.5">
                {stanceLabel(viewResult?.stance, t)}
              </span>
              <span className="text-xs text-muted-foreground font-mono">{recommended.bk}</span>
            </div>
            <p className="text-sm text-muted-foreground">{viewResult?.reason}</p>
          </div>
        ) : viewResult ? (
          <div className="flex flex-col gap-2">
            <h2 className="text-lg font-semibold">{t("marketPick.noPickTitle")}</h2>
            <p className="text-sm text-muted-foreground">{viewResult.reason || t("marketPick.noPickHint")}</p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{t("marketPick.emptyHero")}</p>
        )}
      </div>

      {ranked.length === 0 ? null : (
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <SortableHeader
                  label={t("marketPick.colRank")}
                  column="rank"
                  activeKey={sort.key}
                  dir={sort.dir}
                  sortByLabel={t("marketPick.sortBy", { column: t("marketPick.colRank") })}
                  onSort={toggleSort}
                />
                <th className="px-3 py-2 font-medium">{t("marketPick.colBoard")}</th>
                <SortableHeader
                  label={t("marketPick.colRs")}
                  column="rs"
                  activeKey={sort.key}
                  dir={sort.dir}
                  sortByLabel={t("marketPick.sortBy", { column: t("marketPick.colRs") })}
                  glossary={t("monitor.hintRsShare")}
                  onSort={toggleSort}
                />
                <SortableHeader
                  label={t("marketPick.colKnife")}
                  column="knife"
                  activeKey={sort.key}
                  dir={sort.dir}
                  sortByLabel={t("marketPick.sortBy", { column: t("marketPick.colKnife") })}
                  glossary={t("monitor.hintKnifeShare")}
                  onSort={toggleSort}
                />
                <th className="px-3 py-2 font-medium">{t("marketPick.colClassified")}</th>
                <SortableHeader
                  label={t("marketPick.colRet20")}
                  column="ret20"
                  activeKey={sort.key}
                  dir={sort.dir}
                  sortByLabel={t("marketPick.sortBy", { column: t("marketPick.colRet20") })}
                  onSort={toggleSort}
                />
                <th className="px-3 py-2 font-medium">{t("marketPick.colRecipe")}</th>
                <th className="px-3 py-2 font-medium">{t("marketPick.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {ranked.map((row) => {
                const isHero = row.bk === viewResult?.recommended_bk;
                return (
                  <tr key={row.bk} className={cn("border-t", isHero && "bg-primary/5")}>
                    <td className="px-3 py-2 font-mono">{row.rank ?? "—"}</td>
                    <td className="px-3 py-2">
                      <div className="font-medium">{row.name}</div>
                      <div className="text-xs text-muted-foreground font-mono">
                        {row.bk}
                        {row.board_type === "concept" ? ` · ${t("marketPick.concept")}` : ` · ${t("marketPick.industry")}`}
                      </div>
                      {row.skip_reason ? (
                        <div className="text-xs text-muted-foreground mt-0.5">{row.skip_reason}</div>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 cursor-help" title={t("monitor.hintRsShare")}>
                      {formatPct(row.rs_share)}
                    </td>
                    <td className="px-3 py-2 cursor-help" title={t("monitor.hintKnifeShare")}>
                      {formatPct(row.knife_share)}
                    </td>
                    <td className="px-3 py-2 font-mono">
                      {row.classified ?? 0}/{row.universe_size ?? 0}
                    </td>
                    <td className="px-3 py-2">{formatPct(row.ret_20d, 1)}</td>
                    <td className="px-3 py-2">
                      {row.catalog_name ? (
                        <span>{row.catalog_name}</span>
                      ) : (
                        <span className="text-muted-foreground">{t("marketPick.noRecipe")}</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {row.catalog_key ? (
                        <button
                          type="button"
                          onClick={() => trackBoard(row)}
                          disabled={tracking === row.catalog_key || scanning}
                          className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50"
                        >
                          {tracking === row.catalog_key ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : null}
                          {t("marketPick.track")}
                        </button>
                      ) : (
                        <span className="text-xs text-muted-foreground">{t("marketPick.tapeOnly")}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        {t("marketPick.disclaimer")}{" "}
        <Link to="/sector-pick" className="underline underline-offset-2">
          {t("layout.sectorPick")}
        </Link>
      </p>
    </div>
  );
}
