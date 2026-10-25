import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { Crosshair, Eye, ExternalLink, GitCompare, Loader2, Pencil, Play, RotateCcw, Save, Trash2, Wrench, X } from "lucide-react";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { ForecastPanel } from "@/components/monitor/ForecastPanel";
import { DecisionCard } from "@/components/process/DecisionCard";
import { HorizonStrip } from "@/components/process/HorizonStrip";
import { TermHint } from "@/components/process/TermHint";
import {
  api,
  type MonitorBoard,
  type MonitorDayOps,
  type MonitorHistorySummary,
  type MonitorHolding,
  type MonitorSuggestion,
  type ProcessDecision,
  type RunData,
  type RunListItem,
} from "@/lib/api";
import { formatMetricVal, formatRunListOption } from "@/lib/formatters";
import { sortByTrendAction, trendActionClass, trendActionHintI18nKey, trendActionI18nKey } from "@/lib/trendAction";
import { cn } from "@/lib/utils";

/** Compact metrics line for the maintain-source list (title is shown separately). */
function formatRunMetricsBits(run: RunListItem): string {
  const bits: string[] = [];
  if (typeof run.total_return === "number" && Number.isFinite(run.total_return)) {
    bits.push(`ret ${formatMetricVal("total_return", run.total_return)}`);
  }
  if (typeof run.excess_return === "number" && Number.isFinite(run.excess_return)) {
    bits.push(`ex ${formatMetricVal("excess_return", run.excess_return)}`);
  }
  if (typeof run.sharpe === "number" && Number.isFinite(run.sharpe)) {
    bits.push(`sharpe ${formatMetricVal("sharpe", run.sharpe)}`);
  }
  if (typeof run.max_drawdown === "number" && Number.isFinite(run.max_drawdown)) {
    bits.push(`dd ${formatMetricVal("max_drawdown", run.max_drawdown)}`);
  }
  return bits.join(" · ");
}

const DEFAULT_SCHEDULE = "30 7 * * *";

function actionClass(action: string): string {
  const a = action.toLowerCase();
  if (a === "buy") return "text-emerald-600 dark:text-emerald-400 font-semibold";
  if (a === "sell") return "text-rose-600 dark:text-rose-400 font-semibold";
  return "text-muted-foreground";
}

function holdingActionLabel(action: string | undefined, t: TFunction): string {
  const a = (action || "hold").toLowerCase();
  if (a === "enter") return t("monitor.holdingEnter");
  if (a === "exit") return t("monitor.holdingExit");
  if (a === "add") return t("monitor.holdingAdd");
  if (a === "trim") return t("monitor.holdingTrim");
  return t("monitor.holdingHold");
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

function emptyOps(ops?: MonitorDayOps | null): boolean {
  if (!ops) return true;
  return !ops.entered?.length && !ops.exited?.length && !ops.held?.length;
}

function positionLabel(position: string | undefined, t: TFunction): string {
  const p = (position || "unknown").toLowerCase();
  if (p === "long") return t("monitor.positionLong");
  if (p === "flat") return t("monitor.positionFlat");
  if (p === "short") return t("monitor.positionShort");
  return t("monitor.positionUnknown");
}

function positionClass(position: string | undefined): string {
  const p = (position || "unknown").toLowerCase();
  if (p === "long") return "text-emerald-600 dark:text-emerald-400";
  if (p === "short") return "text-rose-600 dark:text-rose-400";
  return "text-muted-foreground";
}

function formatPrice(price: number | null | undefined): string {
  if (price == null || Number.isNaN(price)) return "—";
  // Keep enough digits for FX/crypto while staying readable for equities.
  if (Math.abs(price) >= 1000) return price.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(price) >= 1) return price.toLocaleString(undefined, { maximumFractionDigits: 3 });
  return price.toLocaleString(undefined, { maximumFractionDigits: 6 });
}

function formatPct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(2)}%`;
}

function formatNum(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function sortSuggestions(items: MonitorSuggestion[]): MonitorSuggestion[] {
  const order: Record<string, number> = { buy: 0, sell: 1, hold: 2 };
  return [...items].sort(
    (a, b) =>
      (order[a.action.toLowerCase()] ?? 9) - (order[b.action.toLowerCase()] ?? 9) ||
      a.symbol.localeCompare(b.symbol),
  );
}

function codesFromRun(run: RunData | null): string[] {
  const ctxCodes = run?.run_context?.codes;
  if (Array.isArray(ctxCodes) && ctxCodes.length) {
    return ctxCodes.map((c) => String(c)).filter(Boolean);
  }
  const backtest = run?.run_card?.backtest;
  if (backtest && typeof backtest === "object") {
    const codes = (backtest as { codes?: unknown }).codes;
    if (Array.isArray(codes) && codes.length) {
      return codes.map((c) => String(c)).filter(Boolean);
    }
  }
  return [];
}

function periodFromRun(run: RunData | null): string {
  const backtest = run?.run_card?.backtest;
  if (backtest && typeof backtest === "object") {
    const bt = backtest as { start_date?: unknown; end_date?: unknown };
    const start = bt.start_date != null ? String(bt.start_date) : "";
    const end = bt.end_date != null ? String(bt.end_date) : "";
    if (start || end) return [start || "?", end || "?"].join(" → ");
  }
  const ctx = run?.run_context;
  if (ctx && typeof ctx === "object") {
    const start = ctx.start_date != null ? String(ctx.start_date) : "";
    const end = ctx.end_date != null ? String(ctx.end_date) : "";
    if (start || end) return [start || "?", end || "?"].join(" → ");
  }
  return "";
}

/** Modal: prompt + metrics summary + signal_engine.py for the selected monitor strategy run. */
function StrategyDetailModal({
  runId,
  onClose,
}: {
  runId: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState<RunData | null>(null);
  const [codeFiles, setCodeFiles] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [detail, code] = await Promise.all([
          api.getRun(runId, { chart_payload: "summary" }),
          api.getRunCode(runId).catch(() => ({}) as Record<string, string>),
        ]);
        if (cancelled) return;
        setRun(detail);
        setCodeFiles(code || {});
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : t("monitor.strategyDetailFailed"));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, t]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const engineCode = codeFiles["signal_engine.py"] || Object.values(codeFiles)[0] || "";
  const engineName = codeFiles["signal_engine.py"]
    ? "signal_engine.py"
    : Object.keys(codeFiles)[0] || "signal_engine.py";
  const codes = codesFromRun(run);
  const period = periodFromRun(run);
  const metrics = run?.metrics;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="monitor-strategy-detail-title"
        className="relative w-full max-w-3xl max-h-[85vh] rounded-xl border bg-background shadow-2xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-b shrink-0">
          <div className="min-w-0">
            <h2 id="monitor-strategy-detail-title" className="text-sm font-semibold">
              {t("monitor.strategyDetailTitle")}
            </h2>
            <p className="text-xs text-muted-foreground font-mono truncate mt-0.5">{runId}</p>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <Link
              to={`/runs/${encodeURIComponent(runId)}`}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium border hover:bg-muted"
            >
              <ExternalLink className="h-3.5 w-3.5" />
              {t("monitor.strategyOpenRun")}
            </Link>
            <button
              type="button"
              onClick={onClose}
              aria-label={t("monitor.strategyClose")}
              className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-auto p-4 space-y-4">
          {loading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("monitor.strategyDetailLoading")}
            </div>
          )}

          {!loading && error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {!loading && !error && (
            <>
              <section className="space-y-1.5">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {t("monitor.strategyPrompt")}
                </h3>
                {run?.prompt ? (
                  <pre className="text-sm whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 max-h-48 overflow-auto">
                    {run.prompt}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground">{t("monitor.strategyNoPrompt")}</p>
                )}
              </section>

              {(period || codes.length > 0 || metrics) && (
                <section className="grid gap-3 sm:grid-cols-2">
                  {period && (
                    <div className="rounded-md border p-3">
                      <div className="text-xs text-muted-foreground">{t("monitor.strategyPeriod")}</div>
                      <div className="text-sm font-mono mt-1">{period}</div>
                    </div>
                  )}
                  {metrics && (
                    <div className="rounded-md border p-3">
                      <div className="text-xs text-muted-foreground">{t("monitor.strategyMetrics")}</div>
                      <div className="text-sm mt-1 flex flex-wrap gap-x-3 gap-y-1">
                        <span>ret {formatPct(metrics.total_return)}</span>
                        <span>sharpe {formatNum(metrics.sharpe)}</span>
                        <span>mdd {formatPct(metrics.max_drawdown)}</span>
                      </div>
                    </div>
                  )}
                  {codes.length > 0 && (
                    <div className="rounded-md border p-3 sm:col-span-2">
                      <div className="text-xs text-muted-foreground">{t("monitor.strategyUniverse")}</div>
                      <div className="text-xs font-mono mt-1 leading-relaxed break-words">
                        {codes.join(", ")}
                      </div>
                    </div>
                  )}
                </section>
              )}

              <section className="space-y-1.5">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {t("monitor.strategyCode")}
                  <span className="ml-2 font-mono font-normal normal-case">{engineName}</span>
                </h3>
                {engineCode ? (
                  <pre className="text-[11px] leading-relaxed font-mono whitespace-pre overflow-auto rounded-md border bg-muted/20 p-3 max-h-[40vh]">
                    {engineCode}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground">{t("monitor.strategyNoCode")}</p>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function Monitor() {
  const { t } = useTranslation();
  const [board, setBoard] = useState<MonitorBoard | null>(null);
  const [symbolsText, setSymbolsText] = useState("");
  const [runId, setRunId] = useState("");
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [schedule, setSchedule] = useState(DEFAULT_SCHEDULE);
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);
  // Opt-in: re-bench the bound run so RunDetail K-lines / markers update.
  // Persisted locally so the choice survives reloads; default off = legacy scan.
  const [refreshReport, setRefreshReport] = useState(() => {
    try {
      return window.localStorage.getItem("vibe.monitor.refreshReport") === "1";
    } catch {
      return false;
    }
  });
  const [error, setError] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [maintainOpen, setMaintainOpen] = useState(false);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [history, setHistory] = useState<MonitorHistorySummary[]>([]);
  const [historyAsOf, setHistoryAsOf] = useState<string | null>(null);
  const [viewHoldings, setViewHoldings] = useState<MonitorHolding[]>([]);
  const [viewOps, setViewOps] = useState<MonitorDayOps>({ entered: [], exited: [], held: [] });
  const [viewDecision, setViewDecision] = useState<ProcessDecision | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [forecastOpen, setForecastOpen] = useState(false);

  const applyBoard = (next: MonitorBoard) => {
    setBoard(next);
    setSymbolsText((next.symbols || []).join(", "));
    setRunId(next.strategy?.run_id || "");
    const sched = (next.schedule || "").trim();
    setScheduleEnabled(Boolean(sched));
    setSchedule(sched || DEFAULT_SCHEDULE);
    // Live board always shows the latest snapshot until the user picks history.
    setHistoryAsOf(null);
    setViewHoldings(next.holdings || []);
    setViewOps(next.ops || { entered: [], exited: [], held: [] });
    setViewDecision(next.decision || null);
  };

  const refreshHistory = async (rid: string) => {
    if (!rid.trim()) {
      setHistory([]);
      return;
    }
    try {
      const rows = await api.listMonitorHistory(30, rid.trim());
      setHistory(Array.isArray(rows) ? rows : []);
    } catch {
      setHistory([]);
    }
  };

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [monitor, runList] = await Promise.all([
          api.getMonitor(),
          // Only strategy-ready runs: chat/Q&A sessions create run dirs without
          // signal_engine.py and must not appear in this picker.
          api.listRuns(50, { strategyReady: true }).catch(() => [] as RunListItem[]),
        ]);
        if (cancelled) return;
        applyBoard(monitor);
        setRuns(Array.isArray(runList) ? runList : []);
        // Previously bound chat-only runs stay in state so the user can see why
        // scan fails; the orphan option below labels them as unusable.
        await refreshHistory(monitor.strategy?.run_id || "");
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : t("monitor.loadFailed"));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [t]);

  const suggestions = useMemo(
    () => sortSuggestions(board?.suggestions || []),
    [board?.suggestions],
  );
  // Same dual-track order as 优选板块: hold → buy_dip → … → reduce.
  const sortedHoldings = useMemo(
    () => sortByTrendAction(viewHoldings),
    [viewHoldings],
  );

  const parseSymbols = (): string[] =>
    symbolsText
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);

  const saveConfig = async () => {
    setSaving(true);
    setError(null);
    try {
      // Save only the currently selected strategy board (symbols stay per-source).
      const next = await api.updateMonitor({
        symbols: parseSymbols(),
        strategy: { type: "run_dir", run_id: runId.trim() || null },
        schedule: scheduleEnabled ? schedule.trim() || DEFAULT_SCHEDULE : "",
      });
      applyBoard(next);
      await refreshHistory(next.strategy?.run_id || runId);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  /** Switch strategy source: load that board's symbols + last scan (do not reuse the previous board). */
  const switchStrategy = async (nextRunId: string) => {
    const previousRunId = runId;
    setRunId(nextRunId);
    setSaving(true);
    setError(null);
    try {
      // Persist the board we are leaving so edits are not lost when switching back.
      if (previousRunId.trim()) {
        await api.updateMonitor({
          symbols: parseSymbols(),
          strategy: { type: "run_dir", run_id: previousRunId.trim() },
          schedule: scheduleEnabled ? schedule.trim() || DEFAULT_SCHEDULE : "",
        });
      }
      // Selecting a source loads (or creates) that strategy's own stored board.
      const next = await api.updateMonitor({
        strategy: { type: "run_dir", run_id: nextRunId.trim() || null },
      });
      applyBoard(next);
      await refreshHistory(next.strategy?.run_id || nextRunId);
    } catch (e) {
      // Roll local select back if the server rejected the switch.
      setRunId(previousRunId);
      setError(e instanceof Error ? e.message : t("monitor.saveFailed"));
    } finally {
      setSaving(false);
    }
  };

  const runScan = async () => {
    setScanning(true);
    setError(null);
    try {
      // Persist current edits first so scan uses what the user sees.
      const saved = await api.updateMonitor({
        symbols: parseSymbols(),
        strategy: { type: "run_dir", run_id: runId.trim() || null },
        schedule: scheduleEnabled ? schedule.trim() || DEFAULT_SCHEDULE : "",
      });
      applyBoard(saved);
      const scanned = await api.scanMonitor({ refresh_report: refreshReport });
      applyBoard(scanned);
      await refreshHistory(scanned.strategy?.run_id || runId);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.scanFailed"));
    } finally {
      setScanning(false);
    }
  };

  /** Wipe history for the active strategy, then rescan today as day one. */
  const confirmResetHistory = async () => {
    if (resetting || !runId.trim()) return;
    setResetting(true);
    setError(null);
    try {
      // Persist edits first so the reset/rescan uses the symbols on screen.
      await api.updateMonitor({
        symbols: parseSymbols(),
        strategy: { type: "run_dir", run_id: runId.trim() || null },
        schedule: scheduleEnabled ? schedule.trim() || DEFAULT_SCHEDULE : "",
      });
      const reset = await api.resetMonitor();
      applyBoard(reset);
      await refreshHistory(reset.strategy?.run_id || runId);
      setResetConfirmOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.resetFailed"));
    } finally {
      setResetting(false);
    }
  };

  const selectHistoryDay = async (asOf: string | null) => {
    if (!asOf) {
      setHistoryAsOf(null);
      setViewHoldings(board?.holdings || []);
      setViewOps(board?.ops || { entered: [], exited: [], held: [] });
      setViewDecision(board?.decision || null);
      return;
    }
    setHistoryLoading(true);
    setError(null);
    try {
      const detail = await api.getMonitorHistoryDay(asOf, runId.trim() || undefined);
      setHistoryAsOf(asOf);
      setViewHoldings(detail.holdings || []);
      setViewOps(detail.ops || { entered: [], exited: [], held: [] });
      setViewDecision(detail.decision || null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.historyLoadFailed"));
    } finally {
      setHistoryLoading(false);
    }
  };

  const discussHref = (row: MonitorSuggestion) => {
    const prompt = encodeURIComponent(
      t("monitor.discussPrompt", {
        symbol: row.symbol,
        action: row.action.toUpperCase(),
        position: positionLabel(row.position, t),
        reason: row.reason,
      }),
    );
    return `/agent?prefill=${prompt}`;
  };

  /** Permanently drop a backtest run from disk + the strategy-source list. */
  const confirmDeleteRun = async () => {
    const targetId = (pendingDeleteId || "").trim();
    if (!targetId || deleting) return;
    setDeleting(true);
    setError(null);
    try {
      await api.deleteRun(targetId);
      setRuns((prev) => prev.filter((r) => r.run_id !== targetId));
      setPendingDeleteId(null);
      // If the deleted run was the active source, clear the board pointer.
      if (runId.trim() === targetId) {
        setDetailOpen(false);
        const next = await api.updateMonitor({
          strategy: { type: "run_dir", run_id: null },
        });
        applyBoard(next);
        await refreshHistory("");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.deleteRunFailed"));
      setPendingDeleteId(null);
    } finally {
      setDeleting(false);
    }
  };

  /** Update the display title shown in the strategy-source picker. */
  const renameRunTitle = async (targetId: string, nextTitle: string) => {
    const cleaned = nextTitle.trim();
    if (!cleaned) return;
    setError(null);
    try {
      const result = await api.renameRun(targetId, cleaned);
      const savedTitle = (result.title || cleaned).trim();
      setRuns((prev) =>
        prev.map((r) => (r.run_id === targetId ? { ...r, title: savedTitle } : r)),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.renameRunFailed"));
      throw e;
    }
  };

  if (loading) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-muted-foreground gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("monitor.loading")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6 p-6 max-w-5xl mx-auto">
      <div className="flex items-start gap-3">
        <Crosshair className="h-6 w-6 text-primary mt-0.5" />
        <div>
          <h1 className="text-2xl font-bold">{t("monitor.title")}</h1>
          <p className="text-sm text-muted-foreground mt-1">{t("monitor.subtitle")}</p>
        </div>
      </div>

      <div className="flex flex-col gap-4 border rounded-lg p-4">
        <div className="flex flex-col gap-1.5">
          <label className="text-sm font-medium">{t("monitor.symbols")}</label>
          <input
            type="text"
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
            placeholder="600519.SH, 000858.SZ"
            className="w-full px-3 py-2 rounded-md border bg-background text-sm"
          />
          <p className="text-xs text-muted-foreground">{t("monitor.symbolsHint")}</p>
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-2">
            <label className="text-sm font-medium">{t("monitor.strategyRun")}</label>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setMaintainOpen(true)}
                disabled={saving || scanning || deleting}
                className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground hover:underline disabled:opacity-40 disabled:no-underline disabled:cursor-not-allowed"
              >
                <Wrench className="h-3.5 w-3.5" />
                {t("monitor.maintainRuns")}
              </button>
              <button
                type="button"
                onClick={() => setDetailOpen(true)}
                disabled={!runId.trim()}
                title={!runId.trim() ? t("monitor.strategyDetailEmpty") : undefined}
                className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline disabled:opacity-40 disabled:no-underline disabled:cursor-not-allowed"
              >
                <Eye className="h-3.5 w-3.5" />
                {t("monitor.viewStrategy")}
              </button>
            </div>
          </div>
          <select
            value={runId}
            onChange={(e) => {
              void switchStrategy(e.target.value);
            }}
            disabled={saving || scanning || deleting}
            className="w-full px-3 py-2 rounded-md border bg-background text-sm disabled:opacity-50"
          >
            <option value="">{t("monitor.pickRun")}</option>
            {runId.trim() && !runs.some((r) => r.run_id === runId) ? (
              <option value={runId}>
                {t("monitor.strategyOrphanRun", { runId })}
              </option>
            ) : null}
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {formatRunListOption(r)}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">{t("monitor.strategyHint")}</p>
        </div>

        <div className="flex flex-col gap-2">
          <label className="inline-flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={scheduleEnabled}
              onChange={(e) => setScheduleEnabled(e.target.checked)}
            />
            {t("monitor.enableSchedule")}
          </label>
          {scheduleEnabled && (
            <input
              type="text"
              value={schedule}
              onChange={(e) => setSchedule(e.target.value)}
              className="w-full px-3 py-2 rounded-md border bg-background text-sm font-mono"
              placeholder={DEFAULT_SCHEDULE}
            />
          )}
          <p className="text-xs text-muted-foreground">{t("monitor.scheduleHint")}</p>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={saveConfig}
            disabled={saving || scanning || resetting}
            className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {t("monitor.save")}
          </button>
          <button
            type="button"
            onClick={runScan}
            disabled={saving || scanning || resetting || !runId.trim()}
            className="inline-flex items-center gap-2 rounded-md bg-primary text-primary-foreground px-3 py-2 text-sm hover:opacity-90 disabled:opacity-50"
          >
            {scanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            {scanning ? t("monitor.scanning") : t("monitor.scanNow")}
          </button>
          <label
            className="inline-flex items-center gap-2 text-sm text-muted-foreground select-none"
            title={t("monitor.refreshReportHint")}
          >
            <input
              type="checkbox"
              checked={refreshReport}
              disabled={saving || scanning || resetting}
              onChange={(e) => {
                const next = e.target.checked;
                setRefreshReport(next);
                try {
                  window.localStorage.setItem(
                    "vibe.monitor.refreshReport",
                    next ? "1" : "0",
                  );
                } catch {
                  // Ignore quota / private-mode write failures.
                }
              }}
            />
            {t("monitor.refreshReport")}
          </label>
          <button
            type="button"
            onClick={() => setResetConfirmOpen(true)}
            disabled={saving || scanning || resetting || !runId.trim()}
            className="inline-flex items-center gap-2 rounded-md border border-destructive/40 px-3 py-2 text-sm text-destructive hover:bg-destructive/5 disabled:opacity-50"
          >
            {resetting ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
            {resetting ? t("monitor.resetting") : t("monitor.resetHistory")}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {viewDecision && (
        <div className="rounded-lg border p-4 flex flex-col gap-3">
          <HorizonStrip
            className="mt-0"
            regime={
              typeof viewDecision.overlays_raw?.market_regime === "string"
                ? viewDecision.overlays_raw.market_regime
                : null
            }
            horizons={
              viewDecision.overlays_raw?.horizons as Record<string, unknown> | undefined
            }
          />
          <DecisionCard decision={viewDecision} />
        </div>
      )}

      <div className="flex flex-col gap-2">
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <h2 className="text-lg font-semibold">{t("monitor.holdings")}</h2>
            <p className="text-xs text-muted-foreground mt-0.5">{t("monitor.holdingsHint")}</p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            {board?.last_scan_at && (
              <span className="text-xs text-muted-foreground">
                {t("monitor.lastScan", { time: board.last_scan_at })}
                {historyAsOf ? ` · ${historyAsOf}` : ""}
              </span>
            )}
            <button
              type="button"
              onClick={() => setForecastOpen(true)}
              disabled={saving || scanning || resetting || !runId.trim()}
              className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
            >
              <GitCompare className="h-4 w-4" />
              <TermHint hint={t("process.hintAnalog")}>{t("monitor.forecast")}</TermHint>
            </button>
          </div>
        </div>

        {history.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <div className="text-xs font-medium text-muted-foreground">{t("monitor.history")}</div>
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
                {t("monitor.historyLatest")}
              </button>
              {history.map((row) => (
                <button
                  key={row.as_of}
                  type="button"
                  onClick={() => selectHistoryDay(row.as_of)}
                  disabled={historyLoading}
                  title={`${row.holding_count} holdings · +${row.entered_count}/-${row.exited_count}`}
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
            <p className="text-xs text-muted-foreground">{t("monitor.historyHint")}</p>
          </div>
        )}

        {!emptyOps(viewOps) && (
          <div className="grid gap-2 sm:grid-cols-3 border rounded-lg p-3">
            <div>
              <div className="text-xs font-medium text-emerald-600 dark:text-emerald-400">
                {t("monitor.opsEntered")} ({viewOps.entered.length})
              </div>
              <div className="text-xs font-mono mt-1 break-words">
                {viewOps.entered.length ? viewOps.entered.join(", ") : "—"}
              </div>
            </div>
            <div>
              <div className="text-xs font-medium text-rose-600 dark:text-rose-400">
                {t("monitor.opsExited")} ({viewOps.exited.length})
              </div>
              <div className="text-xs font-mono mt-1 break-words">
                {viewOps.exited.length ? viewOps.exited.join(", ") : "—"}
              </div>
            </div>
            <div>
              <div className="text-xs font-medium text-muted-foreground">
                {t("monitor.opsHeld")} ({viewOps.held.length})
              </div>
              <div className="text-xs font-mono mt-1 break-words">
                {viewOps.held.length ? viewOps.held.join(", ") : "—"}
              </div>
            </div>
          </div>
        )}
        {emptyOps(viewOps) && viewHoldings.length > 0 && (
          <p className="text-xs text-muted-foreground">{t("monitor.opsEmpty")}</p>
        )}

        {sortedHoldings.length === 0 ? (
          <p className="text-sm text-muted-foreground border rounded-lg p-4">
            {t("monitor.emptyHoldings")}
          </p>
        ) : (
          <div className="border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-muted-foreground text-left">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("monitor.colSymbol")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colWeight")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colHoldingAction")}</th>
                  <th className="px-3 py-2 font-medium">
                    <TermHint hint={t("process.hintStructure")}>{t("monitor.colTrendAction")}</TermHint>
                  </th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colPrice")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colAsOf")}</th>
                </tr>
              </thead>
              <tbody>
                {sortedHoldings.map((row) => {
                  const trendKey = trendActionI18nKey(row.trend?.action);
                  const trendHintKey = trendActionHintI18nKey(row.trend?.action);
                  return (
                  <tr key={`${row.symbol}-${row.action}-${row.data_as_of || ""}`} className="border-t">
                    <td className="px-3 py-2 font-mono">{row.symbol}</td>
                    <td className="px-3 py-2 font-mono tabular-nums">{formatWeight(row.weight)}</td>
                    <td className={cn("px-3 py-2", holdingActionClass(row.action))}>
                      {holdingActionLabel(row.action, t)}
                      {row.prev_weight != null && row.action !== "enter" && (
                        <span className="ml-1 text-[11px] text-muted-foreground font-normal">
                          ({formatWeight(row.prev_weight)} → {formatWeight(row.weight)})
                        </span>
                      )}
                    </td>
                    <td
                      className={cn("px-3 py-2", trendActionClass(row.trend?.action), trendHintKey && "cursor-help")}
                      title={trendHintKey ? t(trendHintKey) : undefined}
                    >
                      {trendKey ? t(trendKey) : "—"}
                    </td>
                    <td className="px-3 py-2 font-mono tabular-nums">{formatPrice(row.price)}</td>
                    <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span>{row.data_as_of || "—"}</span>
                        {row.provisional && (
                          <span
                            className="inline-flex rounded px-1 py-0.5 text-[10px] font-medium bg-amber-500/15 text-amber-700 dark:text-amber-400"
                            title={t("monitor.provisionalHint")}
                          >
                            {t("monitor.provisional")}
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold">{t("monitor.suggestions")}</h2>
        </div>

        {suggestions.length === 0 ? (
          <p className="text-sm text-muted-foreground border rounded-lg p-4">
            {t("monitor.emptySuggestions")}
          </p>
        ) : (
          <div className="border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-muted-foreground text-left">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("monitor.colSymbol")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colPrice")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colPosition")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colAction")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colReason")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.colAsOf")}</th>
                  <th className="px-3 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {suggestions.map((row) => (
                  <tr key={`${row.symbol}-${row.action}-${row.as_of}`} className="border-t">
                    <td className="px-3 py-2 font-mono">{row.symbol}</td>
                    <td className="px-3 py-2 font-mono tabular-nums">{formatPrice(row.price)}</td>
                    <td className={cn("px-3 py-2", positionClass(row.position))}>
                      {positionLabel(row.position, t)}
                    </td>
                    <td className={cn("px-3 py-2 uppercase", actionClass(row.action))}>
                      {row.action}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{row.reason}</td>
                    <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span>{row.as_of}</span>
                        {row.provisional && (
                          <span
                            className="inline-flex rounded px-1 py-0.5 text-[10px] font-medium bg-amber-500/15 text-amber-700 dark:text-amber-400"
                            title={t("monitor.provisionalHint")}
                          >
                            {t("monitor.provisional")}
                          </span>
                        )}
                      </div>
                      {row.data_as_of && row.data_as_of !== row.as_of && (
                        <div className="text-[11px] opacity-80">
                          {t("monitor.dataAsOf", { date: row.data_as_of })}
                        </div>
                      )}
                      {row.provisional && row.data_as_of && row.data_as_of === row.as_of && (
                        <div className="text-[11px] opacity-80">{t("monitor.provisionalHint")}</div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Link
                        to={discussHref(row)}
                        className="text-xs text-primary hover:underline"
                      >
                        {t("monitor.discuss")}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {detailOpen && runId.trim() && (
        <StrategyDetailModal runId={runId.trim()} onClose={() => setDetailOpen(false)} />
      )}

      {forecastOpen && (
        <ForecastPanel asOf={historyAsOf} onClose={() => setForecastOpen(false)} />
      )}

      {maintainOpen && (
        <RunSourceMaintainModal
          runs={runs}
          activeRunId={runId}
          deleting={deleting}
          onClose={() => setMaintainOpen(false)}
          onRequestDelete={(id) => setPendingDeleteId(id)}
          onRename={renameRunTitle}
        />
      )}

      <ConfirmDialog
        open={Boolean(pendingDeleteId)}
        title={t("monitor.deleteRunTitle")}
        description={t("monitor.deleteRunDesc")}
        confirmLabel={deleting ? t("monitor.deletingRun") : t("monitor.deleteRunConfirm")}
        cancelLabel={t("monitor.strategyClose")}
        tone="destructive"
        onConfirm={() => {
          void confirmDeleteRun();
        }}
        onCancel={() => {
          if (!deleting) setPendingDeleteId(null);
        }}
      >
        {pendingDeleteId ? (
          <p className="rounded-md border bg-muted/40 px-2.5 py-2 font-mono text-[11px] break-all text-foreground">
            {pendingDeleteId}
          </p>
        ) : null}
      </ConfirmDialog>

      <ConfirmDialog
        open={resetConfirmOpen}
        title={t("monitor.resetHistoryTitle")}
        description={t("monitor.resetHistoryDesc")}
        confirmLabel={resetting ? t("monitor.resetting") : t("monitor.resetHistoryConfirm")}
        cancelLabel={t("monitor.strategyClose")}
        tone="destructive"
        onConfirm={() => {
          void confirmResetHistory();
        }}
        onCancel={() => {
          if (!resetting) setResetConfirmOpen(false);
        }}
      />
    </div>
  );
}

/** Modal listing strategy-source runs so operators can rename or prune the picker list. */
function RunSourceMaintainModal({
  runs,
  activeRunId,
  deleting,
  onClose,
  onRequestDelete,
  onRename,
}: {
  runs: RunListItem[];
  activeRunId: string;
  deleting: boolean;
  onClose: () => void;
  onRequestDelete: (runId: string) => void;
  onRename: (runId: string, title: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renaming, setRenaming] = useState(false);

  const beginRename = (run: RunListItem) => {
    // Seed with the current display title (not the raw run_id).
    setRenameTarget(run.run_id);
    setRenameValue((run.title || run.prompt || "").trim());
  };

  const commitRename = async () => {
    const targetId = (renameTarget || "").trim();
    if (!targetId || renaming) return;
    const cleaned = renameValue.trim();
    if (!cleaned) {
      setRenameTarget(null);
      return;
    }
    const current = runs.find((r) => r.run_id === targetId);
    const previous = (current?.title || current?.prompt || "").trim();
    // No-op when the operator did not change the label.
    if (cleaned === previous) {
      setRenameTarget(null);
      return;
    }
    setRenaming(true);
    try {
      await onRename(targetId, cleaned);
      setRenameTarget(null);
    } catch {
      // Parent surfaces the error; keep the input open for retry.
    } finally {
      setRenaming(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-maintain-title"
        className="flex max-h-[80vh] w-full max-w-3xl flex-col rounded-2xl border bg-background shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
          <div>
            <h2 id="run-maintain-title" className="text-sm font-semibold">
              {t("monitor.maintainRunsTitle")}
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">{t("monitor.maintainRunsHint")}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label={t("monitor.strategyClose")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {runs.length === 0 ? (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              {t("monitor.maintainRunsEmpty")}
            </p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {runs.map((r) => {
                const isActive = activeRunId.trim() === r.run_id;
                const isRenaming = renameTarget === r.run_id;
                // Full title (not the compact picker ellipsis); metrics / run_id sit below.
                const displayTitle = (r.title || r.prompt || r.run_id).replace(/\n/g, " ").trim();
                const metricsBits = formatRunMetricsBits(r);
                return (
                  <li
                    key={r.run_id}
                    className={cn(
                      "flex items-start gap-2 rounded-md px-2 py-2",
                      isActive ? "bg-primary/10" : "hover:bg-muted/60",
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      {isRenaming ? (
                        <input
                          autoFocus
                          type="text"
                          value={renameValue}
                          disabled={renaming || deleting}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              void commitRename();
                            }
                            if (e.key === "Escape") {
                              e.preventDefault();
                              setRenameTarget(null);
                            }
                          }}
                          onBlur={() => {
                            void commitRename();
                          }}
                          className="w-full rounded-md border bg-background px-2 py-1 text-sm"
                          aria-label={t("monitor.renameRun")}
                        />
                      ) : (
                        <div className="break-words text-sm font-medium text-foreground">
                          {displayTitle}
                        </div>
                      )}
                      {metricsBits ? (
                        <div className="mt-0.5 break-words text-[11px] text-muted-foreground">
                          {metricsBits}
                        </div>
                      ) : null}
                      <div className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">
                        {r.run_id}
                        {isActive ? (
                          <span className="ms-2 text-primary">{t("monitor.maintainRunsActive")}</span>
                        ) : null}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => beginRename(r)}
                      disabled={deleting || renaming || isRenaming}
                      title={t("monitor.renameRun")}
                      className="shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-40"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => onRequestDelete(r.run_id)}
                      disabled={deleting || renaming}
                      title={t("monitor.deleteRun")}
                      className="shrink-0 rounded-md p-1.5 text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-40"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
