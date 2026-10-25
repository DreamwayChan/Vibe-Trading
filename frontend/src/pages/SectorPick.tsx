import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { ExternalLink, Loader2, Play, Save, Target } from "lucide-react";
import {
  api,
  type MonitorDayOps,
  type MonitorHolding,
  type SectorPickBoard,
  type SectorPickCatalogItem,
  type SectorPickHistorySeries,
  type SectorPickHistorySummary,
  type SectorPickResult,
  type SectorPickScore,
} from "@/lib/api";
import { formatMetricVal } from "@/lib/formatters";
import {
  sortByTrendAction,
  trendActionClass,
  trendActionHintI18nKey,
  trendActionI18nKey,
} from "@/lib/trendAction";
import { cn } from "@/lib/utils";
import { SectorPickHistoryChart } from "@/components/charts/SectorPickHistoryChart";
import { DecisionCard } from "@/components/process/DecisionCard";
import { HorizonStrip } from "@/components/process/HorizonStrip";
import { TermHint } from "@/components/process/TermHint";

function holdingActionLabel(action: string | undefined, t: TFunction): string {
  const a = (action || "hold").toLowerCase();
  if (a === "enter") return t("sectorPick.holdingEnter");
  if (a === "exit") return t("sectorPick.holdingExit");
  if (a === "add") return t("sectorPick.holdingAdd");
  if (a === "trim") return t("sectorPick.holdingTrim");
  return t("sectorPick.holdingHold");
}

function holdingActionClass(action: string | undefined): string {
  const a = (action || "hold").toLowerCase();
  if (a === "enter" || a === "add") return "text-emerald-600 dark:text-emerald-400 font-semibold";
  if (a === "exit" || a === "trim") return "text-rose-600 dark:text-rose-400 font-semibold";
  return "text-muted-foreground";
}

function formatWeight(weight: number | null | undefined): string {
  if (weight == null || Number.isNaN(weight)) return "—";
  return `${(weight * 100).toFixed(2)}%`;
}

function formatPct(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function formatPrice(price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return "—";
  if (Math.abs(price) >= 1000) return price.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(price) >= 1) return price.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return price.toLocaleString(undefined, { maximumFractionDigits: 6 });
}

function emptyOps(ops?: MonitorDayOps | null): boolean {
  if (!ops) return true;
  return !ops.entered?.length && !ops.exited?.length && !ops.held?.length;
}

function stanceLabel(stance: string | undefined, t: TFunction): string {
  if (stance === "full") return t("sectorPick.stanceFull");
  if (stance === "half_watch") return t("sectorPick.stanceHalf");
  return t("sectorPick.stanceNone");
}

export function SectorPick() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const focusKey = (searchParams.get("key") || "").trim();
  const previewedFocus = useRef<string | null>(null);
  const [board, setBoard] = useState<SectorPickBoard | null>(null);
  const [enabled, setEnabled] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [previewing, setPreviewing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<SectorPickHistorySummary[]>([]);
  const [historySeries, setHistorySeries] = useState<SectorPickHistorySeries | null>(null);
  const [historyAsOf, setHistoryAsOf] = useState<string | null>(null);
  const [historyDetail, setHistoryDetail] = useState<SectorPickResult | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const applyBoard = (next: SectorPickBoard) => {
    setBoard(next);
    setEnabled(new Set(next.enabled_keys || []));
    setHistoryAsOf(null);
    setHistoryDetail(null);
  };

  const refreshHistory = async () => {
    try {
      const [rows, series] = await Promise.all([
        api.listSectorPickHistory(30),
        api.listSectorPickHistorySeries(30),
      ]);
      setHistory(Array.isArray(rows) ? rows : []);
      setHistorySeries(series && Array.isArray(series.days) ? series : { days: [] });
    } catch {
      setHistory([]);
      setHistorySeries(null);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const next = await api.getSectorPick();
        if (cancelled) return;
        applyBoard(next);
        await refreshHistory();
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : t("sectorPick.loadFailed"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  const catalog = board?.catalog ?? [];
  const liveResult = board?.result ?? null;
  const result = historyAsOf && historyDetail ? historyDetail : liveResult;
  const recommended = useMemo(() => {
    const key = result?.recommended_key;
    if (!key) return null;
    return (result?.sectors || []).find((row) => row.key === key) ?? null;
  }, [result]);
  const mapLeader = useMemo(() => {
    const key = result?.map_key;
    if (!key) return null;
    return (result?.sectors || []).find((row) => row.key === key) ?? null;
  }, [result]);
  const challenger = useMemo(() => {
    const key = result?.challenger_key;
    if (!key) return null;
    return (result?.sectors || []).find((row) => row.key === key) ?? null;
  }, [result]);
  const catalogByKey = useMemo(() => {
    return new Map(catalog.map((row) => [row.key, row]));
  }, [catalog]);
  const rankedCatalog = useMemo(() => {
    const scores = new Map((result?.sectors || []).map((row) => [row.key, row]));
    return [...catalog].sort((left, right) => {
      const leftRank = scores.get(left.key)?.rank;
      const rightRank = scores.get(right.key)?.rank;
      const leftHasRank = leftRank != null && Number.isFinite(leftRank);
      const rightHasRank = rightRank != null && Number.isFinite(rightRank);
      // Rank 1 is the live winner — keep unscored / missing runs after ranked rows.
      if (leftHasRank && rightHasRank) return Number(leftRank) - Number(rightRank);
      if (leftHasRank) return -1;
      if (rightHasRank) return 1;
      return 0;
    });
  }, [catalog, result]);

  const toggleKey = (key: string) => {
    setEnabled((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const saveEnabled = async () => {
    setSaving(true);
    setError(null);
    try {
      const keys = catalog.filter((row) => enabled.has(row.key)).map((row) => row.key);
      const next = await api.updateSectorPick({ enabled_keys: keys });
      applyBoard(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("sectorPick.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const runScan = async () => {
    setScanning(true);
    setError(null);
    try {
      const keys = catalog.filter((row) => enabled.has(row.key)).map((row) => row.key);
      const next = await api.scanSectorPick({ enabled_keys: keys });
      applyBoard(next);
      await refreshHistory();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("sectorPick.scanFailed"));
    } finally {
      setScanning(false);
    }
  };

  const previewHoldings = async (key: string) => {
    setPreviewing(key);
    setError(null);
    try {
      const next = await api.previewSectorPickHoldings(key);
      applyBoard(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("sectorPick.previewFailed"));
    } finally {
      setPreviewing(null);
    }
  };

  useEffect(() => {
    if (!focusKey || loading || !board) return;
    if (previewedFocus.current === focusKey) return;
    if (!board.catalog.some((row) => row.key === focusKey)) return;
    previewedFocus.current = focusKey;
    void previewHoldings(focusKey);
  }, [focusKey, loading, board]);

  const selectHistoryDay = async (asOf: string | null) => {
    if (!asOf) {
      setHistoryAsOf(null);
      setHistoryDetail(null);
      return;
    }
    setHistoryLoading(true);
    setError(null);
    try {
      const detail = await api.getSectorPickHistoryDay(asOf);
      setHistoryAsOf(asOf);
      setHistoryDetail(detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("sectorPick.historyLoadFailed"));
    } finally {
      setHistoryLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("sectorPick.loading")}
      </div>
    );
  }

  const holdings: MonitorHolding[] = sortByTrendAction(result?.holdings || []);
  const ops = result?.ops;
  const holdingsKey = result?.holdings_key || result?.recommended_key;
  const holdingsCatalog = holdingsKey ? catalogByKey.get(holdingsKey) : undefined;
  const holdingsName = holdingsCatalog?.name || holdingsKey || "";
  const holdingsRunTitle = holdingsCatalog?.run_title || "";
  const recommendedCatalog = recommended ? catalogByKey.get(recommended.key) : undefined;

  return (
    <div className="flex flex-col gap-6 p-6 max-w-5xl mx-auto">
      <div className="flex items-start gap-3">
        <Target className="h-6 w-6 text-primary mt-0.5" />
        <div>
          <h1 className="text-2xl font-bold">{t("sectorPick.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("sectorPick.subtitle")}</p>
          <HorizonStrip regime={result?.market_regime} horizons={result?.horizons} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={saveEnabled}
          disabled={saving || scanning}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
        >
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          {t("sectorPick.save")}
        </button>
        <button
          type="button"
          onClick={runScan}
          disabled={saving || scanning || enabled.size === 0}
          className="inline-flex items-center gap-2 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm hover:opacity-90 disabled:opacity-50"
        >
          {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
          {scanning ? t("sectorPick.scanning") : t("sectorPick.scanNow")}
        </button>
        {liveResult?.scanned_at && (
          <span className="text-xs text-muted-foreground">
            {t("sectorPick.lastScan", { time: liveResult.scanned_at })}
            {historyAsOf ? ` · ${historyAsOf}` : ""}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="rounded-lg border p-4">
        {result?.decision ? (
          <div className="flex flex-col gap-3">
            {recommended ? (
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="text-lg font-semibold">{recommended.name}</span>
                <LockBadges
                  t={t}
                  mapLeader={mapLeader}
                  recommendedKey={recommended.key}
                  challengerName={challenger?.name || result.challenger_key}
                  challengerDays={result.challenger_days}
                />
                {result.provisional && (
                  <span
                    className="text-xs rounded-md border border-amber-500/40 text-amber-700 dark:text-amber-400 px-2 py-0.5"
                    title={t("sectorPick.provisionalHint")}
                  >
                    {t("sectorPick.provisional")}
                  </span>
                )}
              </div>
            ) : null}
            <DecisionCard
              decision={result.decision}
              overlayReason={result.reason}
              overlayStanceLabel={stanceLabel(result.stance, t)}
            />
            {recommendedCatalog?.run_title && (
              <p className="text-xs text-muted-foreground" title={recommendedCatalog.run_id}>
                {recommendedCatalog.run_title}
              </p>
            )}
            {recommended?.run_id && (
              <div className="flex flex-wrap gap-2">
                <Link
                  to={`/monitor`}
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  {t("sectorPick.openMonitor")}
                </Link>
                <Link
                  to={`/runs/${encodeURIComponent(recommended.run_id)}`}
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  <ExternalLink className="h-3 w-3" />
                  {t("sectorPick.openRun")}
                </Link>
              </div>
            )}
          </div>
        ) : recommended ? (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 className="text-lg font-semibold">{recommended.name}</h2>
              <LockBadges
                t={t}
                mapLeader={mapLeader}
                recommendedKey={recommended.key}
                challengerName={challenger?.name || result?.challenger_key}
                challengerDays={result?.challenger_days}
              />
              <span className="text-xs rounded-md border px-2 py-0.5">
                {stanceLabel(result?.stance, t)}
              </span>
              {result?.provisional && (
                <span
                  className="text-xs rounded-md border border-amber-500/40 text-amber-700 dark:text-amber-400 px-2 py-0.5"
                  title={t("sectorPick.provisionalHint")}
                >
                  {t("sectorPick.provisional")}
                </span>
              )}
            </div>
            {recommendedCatalog?.run_title && (
              <p className="text-xs text-muted-foreground" title={recommendedCatalog.run_id}>
                {recommendedCatalog.run_title}
              </p>
            )}
            {recommendedCatalog?.run_missing && (
              <p className="text-xs text-destructive">
                {t("sectorPick.runMissing")}
                {recommended.run_id ? ` · ${recommended.run_id}` : ""}
              </p>
            )}
            <p className="text-sm text-muted-foreground">{result?.reason}</p>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>{t("sectorPick.timing")}: {recommended.timing_mult === 1 ? "1.0" : recommended.timing_mult === 0.5 ? "0.5" : "—"}</span>
              <span>{t("sectorPick.ret20d")}: {formatPct(recommended.ret_20d)}</span>
              <span>{t("sectorPick.confirm")}: {formatPct(recommended.confirm_pass_rate, 0)}</span>
              {recommended.excess_return != null && (
                <span>
                  {t("sectorPick.histExcess")}: {formatMetricVal("excess_return", recommended.excess_return)}
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-2 mt-1">
              {recommended.run_id && (
                <>
                  <Link
                    to={`/monitor`}
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    {t("sectorPick.openMonitor")}
                  </Link>
                  <Link
                    to={`/runs/${encodeURIComponent(recommended.run_id)}`}
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    <ExternalLink className="h-3 w-3" />
                    {t("sectorPick.openRun")}
                  </Link>
                </>
              )}
            </div>
          </div>
        ) : result ? (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 className="text-lg font-semibold">{t("sectorPick.noTradableTitle")}</h2>
              <span className="text-xs rounded-md border px-2 py-0.5">
                {stanceLabel(result.stance, t)}
              </span>
            </div>
            <p className="text-sm text-muted-foreground">{result.reason}</p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{t("sectorPick.emptyHero")}</p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <div>
          <h2 className="text-lg font-semibold">
            {t("sectorPick.holdings")}
            {holdingsName ? ` · ${holdingsName}` : ""}
          </h2>
          {holdingsRunTitle ? (
            <p className="text-xs text-muted-foreground mt-0.5" title={holdingsCatalog?.run_id}>
              {holdingsRunTitle}
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground mt-0.5">{t("sectorPick.holdingsHint")}</p>
        </div>
        {history.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <div className="text-xs font-medium text-muted-foreground">{t("sectorPick.history")}</div>
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
                {t("sectorPick.historyLatest")}
              </button>
              {history.map((row) => (
                <button
                  key={row.as_of}
                  type="button"
                  onClick={() => selectHistoryDay(row.as_of)}
                  disabled={historyLoading}
                  title={`${row.recommended_key || ""} · ${row.holding_count} holdings · +${row.entered_count}/-${row.exited_count}`}
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
            <p className="text-xs text-muted-foreground">{t("sectorPick.historyHint")}</p>
          </div>
        )}
        {ops && !emptyOps(ops) && (
          <p className="text-xs text-muted-foreground">
            {t("sectorPick.todayOps")}: {t("sectorPick.opsEntered")} {ops.entered?.length || 0}
            {" · "}
            {t("sectorPick.opsExited")} {ops.exited?.length || 0}
            {" · "}
            {t("sectorPick.opsHeld")} {ops.held?.length || 0}
          </p>
        )}
        {holdings.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("sectorPick.emptyHoldings")}</p>
        ) : (
          <div className="overflow-x-auto rounded-md border">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("sectorPick.colSymbol")}</th>
                  <th className="px-3 py-2 font-medium">{t("sectorPick.colWeight")}</th>
                  <th className="px-3 py-2 font-medium">{t("sectorPick.colHoldingAction")}</th>
                  <th
                    className="px-3 py-2 font-medium cursor-help"
                    title={t("monitor.colTrendActionHint")}
                  >
                    {t("sectorPick.colTrendAction")}
                  </th>
                  <th className="px-3 py-2 font-medium">{t("sectorPick.colPrice")}</th>
                </tr>
              </thead>
              <tbody>
                {holdings.map((row) => {
                  const actionKey = trendActionI18nKey(row.trend?.action);
                  const actionHintKey = trendActionHintI18nKey(row.trend?.action);
                  return (
                  <tr key={row.symbol} className="border-t">
                    <td className="px-3 py-2 font-mono">{row.symbol}</td>
                    <td className="px-3 py-2">{formatWeight(row.weight)}</td>
                    <td className={cn("px-3 py-2", holdingActionClass(row.action))}>
                      {holdingActionLabel(row.action, t)}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-2",
                        trendActionClass(row.trend?.action),
                        actionHintKey && "cursor-help",
                      )}
                      title={actionHintKey ? t(actionHintKey) : undefined}
                    >
                      {actionKey ? t(actionKey) : "—"}
                    </td>
                    <td className="px-3 py-2">{formatPrice(row.price)}</td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {historySeries && historySeries.days.length > 0 && (
        <SectorPickHistoryChart
          series={historySeries}
          catalog={catalog}
          selectedAsOf={historyAsOf}
          onSelectDay={selectHistoryDay}
        />
      )}

      <div className="flex flex-col gap-2">
        <div>
          <h2 className="text-lg font-semibold">{t("sectorPick.tableTitle")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("sectorPick.tableHint")}</p>
        </div>
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">{t("sectorPick.colInclude")}</th>
                <th className="px-3 py-2 font-medium">{t("sectorPick.colRank")}</th>
                <th className="px-3 py-2 font-medium">{t("sectorPick.colSector")}</th>
                <th className="px-3 py-2 font-medium">
                  <TermHint hint={t("process.hintF0")}>{t("sectorPick.colVariant")}</TermHint>
                </th>
                <th className="px-3 py-2 font-medium">
                  <TermHint hint={t("process.hintStructure")}>{t("sectorPick.timing")}</TermHint>
                </th>
                <th
                  className="px-3 py-2 font-medium cursor-help"
                  title={t("monitor.hintRsShare")}
                >
                  {t("sectorPick.colRs")}
                </th>
                <th
                  className="px-3 py-2 font-medium cursor-help"
                  title={t("monitor.hintKnifeShare")}
                >
                  {t("sectorPick.colKnife")}
                </th>
                <th className="px-3 py-2 font-medium">{t("sectorPick.ret20d")}</th>
                <th className="px-3 py-2 font-medium">{t("sectorPick.confirm")}</th>
                <th className="px-3 py-2 font-medium">{t("sectorPick.histExcess")}</th>
                <th className="px-3 py-2 font-medium">{t("sectorPick.colActions")}</th>
              </tr>
            </thead>
            <tbody>
              {rankedCatalog.map((row) => (
                <SectorRow
                  key={row.key}
                  catalog={row}
                  score={(result?.sectors || []).find((s) => s.key === row.key)}
                  checked={enabled.has(row.key)}
                  isRecommended={row.key === result?.recommended_key}
                  isMapLeader={row.key === result?.map_key}
                  isFocused={Boolean(focusKey) && row.key === focusKey}
                  isHoldings={row.key === holdingsKey}
                  scanning={scanning}
                  previewing={previewing}
                  hasResult={Boolean(liveResult) && !historyAsOf}
                  t={t}
                  onToggle={() => toggleKey(row.key)}
                  onPreview={() => previewHoldings(row.key)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">{t("sectorPick.disclaimer")}</p>
    </div>
  );
}

function LockBadges({
  t,
  mapLeader,
  recommendedKey,
  challengerName,
  challengerDays,
}: {
  t: TFunction;
  mapLeader: SectorPickScore | null;
  recommendedKey: string;
  challengerName?: string | null;
  challengerDays?: number | null;
}) {
  const mapDiffers = Boolean(mapLeader && mapLeader.key !== recommendedKey);
  const days = Number(challengerDays || 0);
  return (
    <>
      <span className="text-xs rounded-md border border-primary/40 px-2 py-0.5">
        <TermHint hint={t("sectorPick.hintLock")}>{t("sectorPick.badgeLock")}</TermHint>
      </span>
      {mapDiffers && mapLeader ? (
        <span className="text-xs rounded-md border px-2 py-0.5 text-muted-foreground">
          <TermHint hint={t("sectorPick.hintMap")}>
            {t("sectorPick.badgeMap")}
            {`: ${mapLeader.name}`}
          </TermHint>
        </span>
      ) : null}
      {days > 0 && challengerName ? (
        <span className="text-xs rounded-md border px-2 py-0.5 text-muted-foreground">
          <TermHint hint={t("sectorPick.hintChallenger")}>
            {t("sectorPick.challengerLead", { name: challengerName, days, need: 2 })}
          </TermHint>
        </span>
      ) : null}
    </>
  );
}

function SectorRow({
  catalog,
  score,
  checked,
  isRecommended,
  isMapLeader,
  isFocused,
  isHoldings,
  scanning,
  previewing,
  hasResult,
  t,
  onToggle,
  onPreview,
}: {
  catalog: SectorPickCatalogItem;
  score?: SectorPickScore;
  checked: boolean;
  isRecommended: boolean;
  isMapLeader?: boolean;
  isFocused?: boolean;
  isHoldings: boolean;
  scanning: boolean;
  previewing: string | null;
  hasResult: boolean;
  t: TFunction;
  onToggle: () => void;
  onPreview: () => void;
}) {
  const variantLabel =
    catalog.variant === "f1_max" ? t("sectorPick.variantF1") : t("sectorPick.variantF0");
  return (
    <tr className={cn("border-t", isRecommended && "bg-primary/5", isFocused && "ring-1 ring-inset ring-primary/40")}>
      <td className="px-3 py-2">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          aria-label={catalog.name}
          disabled={scanning}
        />
      </td>
      <td className="px-3 py-2 font-mono">{score?.rank ?? "—"}</td>
      <td className="px-3 py-2">
        <div className="font-medium">
          {catalog.name}
          {isRecommended ? (
            <span className="ml-1.5 text-[10px] rounded border border-primary/40 px-1 py-0.5 align-middle">
              {t("sectorPick.badgeLock")}
            </span>
          ) : null}
          {isMapLeader && !isRecommended ? (
            <span className="ml-1.5 text-[10px] rounded border px-1 py-0.5 align-middle text-muted-foreground">
              {t("sectorPick.badgeMap")}
            </span>
          ) : null}
        </div>
        {catalog.run_missing ? (
          <div className="text-xs text-destructive mt-0.5">
            {t("sectorPick.runMissing")}
            {catalog.run_id ? ` · ${catalog.run_id}` : ""}
          </div>
        ) : catalog.run_title ? (
          <div className="text-xs text-muted-foreground mt-0.5" title={catalog.run_id}>
            {catalog.run_title}
          </div>
        ) : catalog.run_id ? (
          <div className="text-xs font-mono text-muted-foreground mt-0.5">{catalog.run_id}</div>
        ) : null}
        <div className="text-xs text-muted-foreground">{catalog.panel}</div>
        {score?.error && <div className="text-xs text-destructive mt-0.5">{score.error}</div>}
        {score?.book_healthy === false && (
          <div
            className="text-xs text-amber-700 dark:text-amber-400 mt-0.5"
            title={score.skip_reason || t("sectorPick.bookResidualHint")}
          >
            {t("sectorPick.bookResidual")}
            {score.holding_count != null ? ` · ${score.holding_count}` : ""}
            {score.max_weight != null ? ` · ${formatPct(score.max_weight, 0)}` : ""}
          </div>
        )}
      </td>
      <td className="px-3 py-2 text-xs">
        <TermHint hint={t("process.hintF0")}>{variantLabel}</TermHint>
      </td>
      <td className="px-3 py-2 font-mono">
        {score?.timing_mult == null ? "—" : score.timing_mult.toFixed(1)}
      </td>
      <td className="px-3 py-2 font-mono cursor-help" title={t("monitor.hintRsShare")}>
        {formatPct(score?.rs_share, 0)}
      </td>
      <td className="px-3 py-2 font-mono cursor-help" title={t("monitor.hintKnifeShare")}>
        {formatPct(score?.knife_share, 0)}
      </td>
      <td className="px-3 py-2 font-mono">{formatPct(score?.ret_20d)}</td>
      <td className="px-3 py-2 font-mono">{formatPct(score?.confirm_pass_rate, 0)}</td>
      <td className="px-3 py-2 font-mono">
        {score?.excess_return == null ? "—" : formatMetricVal("excess_return", score.excess_return)}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap gap-2">
          {hasResult && (
            <button
              type="button"
              onClick={onPreview}
              disabled={scanning || previewing !== null}
              className="text-xs text-primary hover:underline disabled:opacity-50"
            >
              {previewing === catalog.key
                ? t("sectorPick.previewing")
                : isHoldings
                  ? t("sectorPick.showingHoldings")
                  : t("sectorPick.previewHoldings")}
            </button>
          )}
          {catalog.run_id && (
            <Link
              to={`/runs/${encodeURIComponent(catalog.run_id)}`}
              className="text-xs text-muted-foreground hover:underline"
            >
              {t("sectorPick.openRun")}
            </Link>
          )}
        </div>
      </td>
    </tr>
  );
}
