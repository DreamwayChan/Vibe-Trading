import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Save, X } from "lucide-react";
import { ForecastCandlestickChart } from "@/components/charts/ForecastCandlestickChart";
import { DecisionCard } from "@/components/process/DecisionCard";
import { HorizonStrip } from "@/components/process/HorizonStrip";
import {
  api,
  type MonitorForecastItem,
  type MonitorForecastMarket,
  type MonitorForecastReport,
  type MonitorForecastSaveSummary,
} from "@/lib/api";
import {
  sortByTrendAction,
  trendActionClass,
  trendActionHintI18nKey,
  trendActionI18nKey,
} from "@/lib/trendAction";
import { cn } from "@/lib/utils";

function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(0)}%`;
}

function formatWeight(weight: number | null | undefined): string {
  if (weight == null || Number.isNaN(weight)) return "—";
  return `${(weight * 100).toFixed(2)}%`;
}

function formatPrice(price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return "—";
  if (Math.abs(price) >= 1000) return price.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(price) >= 1) return price.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return price.toLocaleString(undefined, { maximumFractionDigits: 6 });
}

/** Compact local timestamp for saved-result pills. */
export function formatForecastSavedAt(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

interface Props {
  asOf: string | null;
  onClose: () => void;
}

export function ForecastPanel({ asOf, onClose }: Props) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [liveReport, setLiveReport] = useState<MonitorForecastReport | null>(null);
  const [report, setReport] = useState<MonitorForecastReport | null>(null);
  const [saves, setSaves] = useState<MonitorForecastSaveSummary[]>([]);
  const [viewingId, setViewingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      setViewingId(null);
      try {
        const [next, saved] = await Promise.all([
          api.forecastMonitor({ as_of: asOf }),
          api.listMonitorForecastSaves(50).catch(() => [] as MonitorForecastSaveSummary[]),
        ]);
        if (cancelled) return;
        setLiveReport(next);
        setReport(next);
        setSaves(Array.isArray(saved) ? saved : []);
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : t("monitor.forecastFailed"));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [asOf, t]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const showLive = () => {
    setViewingId(null);
    setSaveError(null);
    setReport(liveReport);
  };

  const openSaved = async (snapshotId: string) => {
    setHistoryLoading(true);
    setSaveError(null);
    try {
      const saved = await api.getMonitorForecastSave(snapshotId);
      setViewingId(saved.id);
      setReport(saved);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : t("monitor.forecastHistoryLoadFailed"));
    } finally {
      setHistoryLoading(false);
    }
  };

  const saveCurrent = async () => {
    if (!liveReport || !liveReport.items?.length || viewingId) return;
    setSaving(true);
    setSaveError(null);
    try {
      const stored = await api.saveMonitorForecast(liveReport);
      setSaves((prev) => {
        const rest = prev.filter((row) => row.id !== stored.id);
        return [
          {
            id: stored.id,
            saved_at: stored.saved_at,
            run_id: stored.run_id,
            as_of: stored.as_of,
            bias: stored.bias,
            holding_count: stored.holding_count,
            evaluated_count: stored.evaluated_count,
          },
          ...rest,
        ];
      });
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : t("monitor.forecastSaveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const calledCount =
    typeof report?.called_count === "number"
      ? report.called_count
      : (report?.items || []).filter((item) => {
          const a = item.assessment;
          if (!a) return false;
          if (typeof a.called === "boolean") return a.called;
          return a.bias === "up" || a.bias === "down";
        }).length;
  const biasKey =
    report?.bias === "up"
      ? "monitor.forecastBiasUp"
      : report?.bias === "down"
        ? "monitor.forecastBiasDown"
        : calledCount === 0
          ? "monitor.forecastBiasAbstained"
          : "monitor.forecastBiasNeutral";
  const biasHintKey =
    report?.bias === "up"
      ? "monitor.hintBiasUp"
      : report?.bias === "down"
        ? "monitor.hintBiasDown"
        : calledCount === 0
          ? "monitor.hintBiasAbstained"
          : "monitor.hintBiasNeutral";
  const canSave = Boolean(liveReport?.items?.length) && !viewingId && !saving && !loading;
  const sortedItems = useMemo(
    () => sortByTrendAction(report?.items || []),
    [report?.items],
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="monitor-forecast-title"
        className="relative w-full max-w-5xl max-h-[90vh] rounded-xl border bg-background shadow-2xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 px-4 py-3 border-b shrink-0">
          <div className="min-w-0">
            <h2 id="monitor-forecast-title" className="text-sm font-semibold">
              {t("monitor.forecastTitle")}
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">{t("monitor.forecastHint")}</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              type="button"
              onClick={saveCurrent}
              disabled={!canSave}
              className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {saving ? t("monitor.forecastSaving") : t("monitor.forecastSave")}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-1 text-muted-foreground hover:bg-muted"
              aria-label={t("monitor.forecastClose")}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="overflow-y-auto px-4 py-3 flex flex-col gap-3">
          {(saves.length > 0 || !loading) && (
            <div className="flex flex-col gap-1.5">
              <div className="text-xs font-medium text-muted-foreground">{t("monitor.forecastHistory")}</div>
              <div className="flex flex-wrap gap-1.5">
                <button
                  type="button"
                  onClick={showLive}
                  disabled={loading || historyLoading}
                  className={cn(
                    "rounded-md border px-2 py-1 text-xs",
                    viewingId === null ? "border-primary bg-primary/10 text-primary" : "hover:bg-muted",
                  )}
                >
                  {t("monitor.forecastHistoryLive")}
                </button>
                {saves.map((row) => (
                  <button
                    key={row.id}
                    type="button"
                    onClick={() => openSaved(row.id)}
                    disabled={historyLoading}
                    title={`${row.evaluated_count} · ${row.bias}`}
                    className={cn(
                      "rounded-md border px-2 py-1 text-xs font-mono",
                      viewingId === row.id
                        ? "border-primary bg-primary/10 text-primary"
                        : "hover:bg-muted",
                    )}
                  >
                    {formatForecastSavedAt(row.saved_at)}
                  </button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">{t("monitor.forecastHistoryHint")}</p>
            </div>
          )}

          {loading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("monitor.forecasting")}
            </div>
          )}
          {error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
          {saveError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {saveError}
            </div>
          )}
          {!loading && !error && report?.decision && (
            <div className="rounded-md border p-3">
              <DecisionCard decision={report.decision} />
            </div>
          )}
          {!loading && !error && report && report.items.length === 0 && (
            <p className="text-sm text-muted-foreground py-6 text-center">{t("monitor.forecastEmpty")}</p>
          )}
          {!loading && !error && report && report.items.length > 0 && (
            <>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span className="text-foreground font-medium cursor-help" title={t(biasHintKey)}>
                  {t(biasKey)}
                </span>
                {report.market && !report.market.error ? (
                  <span
                    className="cursor-help"
                    title={
                      report.market.decisive
                        ? t("monitor.hintMarketDecisive")
                        : t("monitor.hintMarketIdle")
                    }
                  >
                    {report.market.decisive && report.market.bias === "up"
                      ? t("monitor.forecastMarketUp", { pct: formatPct(report.market.up_prob) })
                      : report.market.decisive && report.market.bias === "down"
                        ? t("monitor.forecastMarketDown", { pct: formatPct(report.market.down_prob) })
                        : t("monitor.forecastMarketIdle")}
                  </span>
                ) : null}
                {report.as_of && <span>{report.as_of}</span>}
                {viewingId && <span>{t("monitor.forecastViewingSaved")}</span>}
                {report.capped && (
                  <span>{t("monitor.forecastCapped", { count: report.evaluated_count })}</span>
                )}
              </div>
              <HorizonStrip
                className="mt-0"
                regime={report.trend?.market_regime}
                horizons={report.trend?.horizons}
              />
              {sortedItems.map((item) => (
                <ForecastCard
                  key={`${viewingId || "live"}-${item.symbol}`}
                  item={item}
                  market={report.market}
                />
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ForecastCard({
  item,
  market,
}: {
  item: MonitorForecastItem;
  market?: MonitorForecastMarket | null;
}) {
  const { t } = useTranslation();
  const a = item.assessment || {
    bias: "neutral",
    style_bucket: "low",
    ma_structure: "mixed",
    analog_count: 0,
    up_count: 0,
    down_count: 0,
  };
  const called =
    typeof a.called === "boolean" ? a.called : a.bias === "up" || a.bias === "down";
  const styleKey =
    a.style_bucket === "high"
      ? "monitor.forecastStyleHigh"
      : a.style_bucket === "medium"
        ? "monitor.forecastStyleMedium"
        : "monitor.forecastStyleLow";
  const maKey =
    a.ma_structure === "bull"
      ? "monitor.forecastMaBull"
      : a.ma_structure === "bear"
        ? "monitor.forecastMaBear"
        : "monitor.forecastMaMixed";
  const upWins = (item.up_prob || 0) >= (item.down_prob || 0);
  const trend = item.trend;
  const actionKey = trendActionI18nKey(trend?.action);
  const actionHintKey = trendActionHintI18nKey(trend?.action);
  const actionClass = trendActionClass(trend?.action);
  // Colored probs = formal call; muted = band only or market veto cleared the call.
  const probHint = called ? t("monitor.hintProbCalled") : t("monitor.hintProbMuted");

  return (
    <section className="rounded-lg border p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <h3 className="font-mono text-sm font-semibold">{item.symbol}</h3>
          <span className="text-xs text-muted-foreground">{formatWeight(item.weight)}</span>
          <span className="text-xs text-muted-foreground">{formatPrice(item.price)}</span>
          {actionKey && (
            <span
              className={cn("text-xs font-medium cursor-help", actionClass)}
              title={actionHintKey ? t(actionHintKey) : undefined}
            >
              {t(actionKey)}
            </span>
          )}
        </div>
        {!item.error && (
          <div className="flex gap-2 text-xs font-medium">
            <span
              className={cn(
                "cursor-help",
                called && upWins ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground",
              )}
              title={probHint}
            >
              {t("monitor.forecastUpProb", { pct: formatPct(item.up_prob) })}
            </span>
            <span
              className={cn(
                "cursor-help",
                called && !upWins ? "text-rose-600 dark:text-rose-400" : "text-muted-foreground",
              )}
              title={probHint}
            >
              {t("monitor.forecastDownProb", { pct: formatPct(item.down_prob) })}
            </span>
          </div>
        )}
      </div>
      {item.error ? (
        <p className="text-xs text-destructive">{t("monitor.forecastSymbolError", { error: item.error })}</p>
      ) : (
        <>
          {trend ? (
            <p className="text-xs text-muted-foreground">
              <span
                className="cursor-help"
                title={
                  trend.stock_regime === "bull"
                    ? t("monitor.hintStockBull")
                    : trend.stock_regime === "bear"
                      ? t("monitor.hintStockBear")
                      : t("monitor.hintStockChop")
                }
              >
                {t(
                  trend.stock_regime === "bull"
                    ? "monitor.forecastStockBull"
                    : trend.stock_regime === "bear"
                      ? "monitor.forecastStockBear"
                      : "monitor.forecastStockChop",
                )}
              </span>
              {" · "}
              <span
                className="cursor-help"
                title={
                  trend.swing === "oversold"
                    ? t("monitor.hintSwingOversold")
                    : trend.swing === "overbought"
                      ? t("monitor.hintSwingOverbought")
                      : t("monitor.hintSwingMid")
                }
              >
                {t(
                  trend.swing === "oversold"
                    ? "monitor.forecastSwingOversold"
                    : trend.swing === "overbought"
                      ? "monitor.forecastSwingOverbought"
                      : "monitor.forecastSwingMid",
                )}
              </span>
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground leading-relaxed">
            <span className="cursor-help" title={t("monitor.hintStyle")}>
              {t("monitor.forecastStyle", { bucket: t(styleKey) })}
            </span>
            {" · "}
            {t(maKey)}
            {" · "}
            <span className="cursor-help" title={t("monitor.hintAnalogs")}>
              {t("monitor.forecastAnalogs", {
                count: a.analog_count,
                up: a.up_count,
                down: a.down_count,
              })}
            </span>
            {typeof a.confidence === "number" ? (
              <>
                {" · "}
                <span className="cursor-help" title={t("monitor.hintConfidence")}>
                  {t("monitor.forecastConfidence", { pct: formatPct(a.confidence) })}
                </span>
              </>
            ) : null}
            {!called && a.market_veto === "market_disagree" ? (
              <>
                {" · "}
                <span className="cursor-help text-amber-700 dark:text-amber-400" title={t("monitor.hintVetoDisagree")}>
                  {t("monitor.forecastVetoDisagree")}
                </span>
              </>
            ) : !called && a.market_veto === "market_abstain" ? (
              <>
                {" · "}
                <span className="cursor-help" title={t("monitor.hintVetoAbstained")}>
                  {t("monitor.forecastVetoAbstained")}
                </span>
              </>
            ) : !called ? (
              <>
                {" · "}
                <span className="cursor-help" title={t("monitor.hintAbstained")}>
                  {t("monitor.forecastAbstained")}
                </span>
              </>
            ) : called && market?.decisive && market.bias === a.bias ? (
              <>
                {" · "}
                <span className="cursor-help" title={t("monitor.hintMarketConfirm")}>
                  {t("monitor.forecastMarketConfirm")}
                </span>
              </>
            ) : null}
          </p>
          <ForecastCandlestickChart item={item} height={300} />
        </>
      )}
    </section>
  );
}
