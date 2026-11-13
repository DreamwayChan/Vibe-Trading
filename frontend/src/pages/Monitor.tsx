import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { BookOpen, ChevronDown, ChevronUp, Crosshair, Eye, ExternalLink, GitCompare, Loader2, Pencil, Play, RotateCcw, Save, Trash2, Wrench, X } from "lucide-react";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { ForecastPanel } from "@/components/monitor/ForecastPanel";
import { LoopStatus } from "@/components/monitor/LoopStatus";
import { RegimeChip, RegimePanelModal } from "@/components/monitor/RegimeDeclaration";
import { DecisionCard } from "@/components/process/DecisionCard";
import { HorizonStrip } from "@/components/process/HorizonStrip";
import { TermHint } from "@/components/process/TermHint";
import {
  api,
  type MonitorBoard,
  type MonitorDayOps,
  type MonitorHistorySummary,
  type MonitorHolding,
  type MonitorHypothesis,
  type MonitorJournalEntry,
  type MonitorSuggestion,
  type ProcessDecision,
  type RunData,
  type RunListItem,
  type StrategyStyle,
} from "@/lib/api";
import type { CostCompareReport, PaperBookSnapshot } from "@/lib/loopStatus";
import { gateStatusFromGate, oosReviewFromGate, type GateStatus } from "@/lib/oosPolicy";
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

/** `as const` so the values stay literal i18n keys — t() only accepts known keys here. */
const REASON_I18N = {
  missing_invalidation: "monitor.reasonMissingInvalidation",
  missing_thesis: "monitor.reasonMissingThesis",
  missing_position: "monitor.reasonMissingPosition",
  missing_risk: "monitor.reasonMissingRisk",
  missing_journal: "monitor.reasonMissingJournal",
  missing_validation: "monitor.reasonMissingValidation",
  validation_unreadable: "monitor.reasonValidationUnreadable",
  config_unreadable: "monitor.reasonConfigUnreadable",
  costs_not_applied: "monitor.reasonCostsNotApplied",
  missing_monte_carlo: "monitor.reasonMissingMonteCarlo",
  missing_bootstrap: "monitor.reasonMissingBootstrap",
  missing_walk_forward: "monitor.reasonMissingWalkForward",
  monte_carlo_failed: "monitor.reasonMonteCarloFailed",
  bootstrap_failed: "monitor.reasonBootstrapFailed",
  walk_forward_failed: "monitor.reasonWalkForwardFailed",
  monte_carlo_p_value: "monitor.reasonMonteCarloPValue",
  bootstrap_ci_lower: "monitor.reasonBootstrapCi",
  walk_forward_consistency: "monitor.reasonWalkForwardConsistency",
  missing_out_of_sample: "monitor.reasonMissingOutOfSample",
  out_of_sample_failed: "monitor.reasonOutOfSampleFailed",
  out_of_sample_degraded: "monitor.reasonOutOfSampleDegraded",
  hypothesis_not_found: "monitor.reasonHypothesisNotFound",
  invalid_timestamp: "monitor.reasonInvalidTimestamp",
  missing_timestamp: "monitor.reasonMissingTimestamp",
  journal_unreadable: "monitor.reasonJournalUnreadable",
  stale_journal: "monitor.reasonStaleJournal",
  admission_error: "monitor.reasonAdmissionError",
  missing_observation: "monitor.reasonMissingObservation",
  observation_not_objective: "monitor.reasonObservationNotObjective",
  missing_hypothesis: "monitor.reasonMissingHypothesis",
  hypothesis_not_causal: "monitor.reasonHypothesisNotCausal",
  missing_logic: "monitor.reasonMissingLogic",
  missing_validation_plan: "monitor.reasonMissingValidationPlan",
  invalidated: "monitor.reasonInvalidated",
  paper_book_decay: "monitor.reasonPaperBookDecay",
  uncausal_scan: "monitor.reasonUncausalScan",
  archived: "monitor.reasonArchived",
  hypothesis_rejected: "monitor.reasonHypothesisRejected",
  not_actionable: "monitor.reasonNotActionable",
  ashare_st: "monitor.reasonAshareSt",
  ashare_limit_up: "monitor.reasonAshareLimitUp",
  ashare_one_word: "monitor.reasonAshareOneWord",
  ashare_extreme_board: "monitor.reasonAshareExtremeBoard",
} as const;

type ReasonI18nKey = (typeof REASON_I18N)[keyof typeof REASON_I18N];

const RISK_I18N = {
  st: "monitor.riskSt",
  limit_up: "monitor.riskLimitUp",
  one_word: "monitor.riskOneWord",
  consecutive_board: "monitor.riskConsecutive",
  extreme_board: "monitor.riskExtreme",
} as const;

type RiskI18nKey = (typeof RISK_I18N)[keyof typeof RISK_I18N];

/** Backend sends free-form codes; unknown ones fall through as plain text. */
function riskLabel(flag: string, t: TFunction): string {
  const key = (RISK_I18N as Record<string, RiskI18nKey | undefined>)[flag];
  return key ? t(key) : flag;
}

function reasonLabel(code: string, t: TFunction): string {
  const key = (REASON_I18N as Record<string, ReasonI18nKey | undefined>)[code];
  return key ? t(key) : code;
}

function isNotFound(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status?: number }).status === 404
  );
}

function barsLabel(source: string | null | undefined, t: TFunction): string {
  if (!source) return "";
  if (source === "lake") return t("monitor.barsLake");
  if (source === "lake+network") return t("monitor.barsLakePlus");
  if (source === "network") return t("monitor.barsNetwork");
  return source;
}

type PoolView = {
  tradable?: boolean;
  tradable_reasons?: string[];
  bars_source?: string | null;
  validation_gate?: Record<string, unknown> | null;
  hypothesis?: MonitorHypothesis | null;
};

/** Registry status on the board; unknown codes fall through as plain text. */
const HYPOTHESIS_STATUS_I18N = {
  rejected: "monitor.hypothesisRejected",
  validated: "monitor.hypothesisValidated",
  monitoring: "monitor.hypothesisMonitoring",
  testing: "monitor.hypothesisTesting",
  exploring: "monitor.hypothesisExploring",
} as const;

type HypothesisStatusKey =
  (typeof HYPOTHESIS_STATUS_I18N)[keyof typeof HYPOTHESIS_STATUS_I18N];

function hypothesisStatusLabel(status: string, t: TFunction): string {
  const key = (HYPOTHESIS_STATUS_I18N as Record<string, HypothesisStatusKey | undefined>)[status];
  return key ? t(key) : status;
}

type GateCheckView = {
  passed?: boolean;
  observed?: number | null;
  threshold?: number | null;
};

/**
 * Turn the validation gate payload into a sentence a reader can act on.
 *
 * The board used to show only reason codes ("monte_carlo_p_value"), which say
 * which check failed but not by how much. These notes carry the observed value
 * and the threshold so the gap is visible; codes covered here are dropped from
 * the code list to avoid saying the same thing twice.
 */
function gateFailureNotes(
  gate: Record<string, unknown> | null | undefined,
  t: TFunction,
): { codes: Set<string>; lines: string[] } {
  const codes = new Set<string>();
  const lines: string[] = [];
  const checks = (gate?.checks ?? null) as Record<string, GateCheckView> | null;
  if (!checks) return { codes, lines };

  const fmt = (value: number) => (Math.abs(value) >= 1 ? value.toFixed(2) : value.toFixed(3));
  const push = (code: string, text: string) => {
    codes.add(code);
    lines.push(text);
  };

  const bounds = (check: GateCheckView | undefined) =>
    check && check.passed === false && typeof check.observed === "number" && typeof check.threshold === "number"
      ? { observed: fmt(check.observed), threshold: fmt(check.threshold) }
      : null;

  const mc = bounds(checks.monte_carlo);
  if (mc) push("monte_carlo_p_value", t("monitor.gateDetailMonteCarlo", mc));

  const bs = bounds(checks.bootstrap);
  if (bs) push("bootstrap_ci_lower", t("monitor.gateDetailBootstrap", bs));

  const wf = bounds(checks.walk_forward);
  if (wf) push("walk_forward_consistency", t("monitor.gateDetailWalkForward", wf));

  if (checks.costs && checks.costs.passed === false) {
    // Only meaningful when an artifact existed: a missing validation.json used
    // to stamp costs as failed, which read as "this backtest ignored fees".
    const hasArtifact = Boolean(checks.monte_carlo || checks.bootstrap || checks.walk_forward);
    if (hasArtifact) push("costs_not_applied", t("monitor.gateDetailCosts"));
  }

  const oos = bounds(checks.out_of_sample);
  if (oos) push("out_of_sample_degraded", t("monitor.gateDetailOos", oos));

  return { codes, lines };
}

const GATE_STATUS_KEY = {
  passed: "monitor.gatePassed",
  failed: "monitor.gateFailed",
  off: "monitor.gateOff",
  unknown: "monitor.gateUnknown",
} as const satisfies Record<GateStatus, "monitor.gatePassed" | "monitor.gateFailed" | "monitor.gateOff" | "monitor.gateUnknown">;

type JournalSampleKind = "stock" | "strategy" | "anomaly" | "behavioral";

const JOURNAL_SAMPLE_LABEL = {
  stock: "monitor.journalSampleStock",
  strategy: "monitor.journalSampleStrategy",
  anomaly: "monitor.journalSampleAnomaly",
  behavioral: "monitor.journalSampleBehavioral",
} as const;

type JournalSample = {
  hypothesis: string;
  symbol: string;
  thesis: string;
  observation: string;
  causal: string;
  logic: string;
  validation: string;
  invalidation: string;
  position: string;
  risk: string;
};

/** One label/value line in the journal sample gallery. */
function JournalSampleRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="flex-1">{value}</dd>
    </div>
  );
}

function SuggestionTable({
  rows,
  lane,
  t,
  discussHref,
}: {
  rows: MonitorSuggestion[];
  lane: "research" | "tradable";
  t: TFunction;
  discussHref: (row: MonitorSuggestion) => string;
}) {
  const research = lane === "research";
  return (
    <section
      data-testid={research ? "research-board" : "tradable-signals"}
      className={cn(
        "rounded-lg p-3 flex flex-col gap-2",
        research
          ? "border border-dashed border-amber-500/60 bg-amber-500/5"
          : "border border-emerald-500/40 bg-emerald-500/5",
      )}
    >
      <div>
        <h3 className="text-sm font-semibold inline-flex items-center gap-2">
          {research ? t("monitor.researchBoardTitle") : t("monitor.tradableLaneTitle")}
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-[10px] font-medium",
              research
                ? "border border-dashed border-amber-500/70 text-amber-800 dark:text-amber-300"
                : "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
            )}
          >
            {research ? t("monitor.researchOnly") : t("monitor.laneTradable")}
          </span>
        </h3>
        <p className="text-xs text-muted-foreground mt-0.5">
          {research ? t("monitor.researchBoardHint") : t("monitor.tradableLaneHint")}
        </p>
      </div>
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {research ? t("monitor.suggestionsResearchEmpty") : t("monitor.suggestionsTradableEmpty")}
        </p>
      ) : (
        <div className="border rounded-lg overflow-hidden bg-background">
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
              {rows.map((row) => (
                <tr
                  key={`${row.symbol}-${row.action}-${row.as_of}`}
                  data-lane={lane}
                  className={cn(
                    "border-t",
                    research ? "bg-amber-500/[0.06]" : "bg-emerald-500/[0.07]",
                  )}
                >
                  <td className="px-3 py-2 font-mono">
                    <span>{row.symbol}</span>
                    {(row.risk_flags || []).map((flag) => (
                      <span
                        key={flag}
                        className="ml-1 inline-flex rounded px-1 py-0.5 text-[10px] font-medium bg-rose-500/15 text-rose-700 dark:text-rose-300"
                      >
                        {riskLabel(flag, t)}
                      </span>
                    ))}
                  </td>
                  <td className="px-3 py-2 font-mono tabular-nums">{formatPrice(row.price)}</td>
                  <td className={cn("px-3 py-2", positionClass(row.position))}>
                    {positionLabel(row.position, t)}
                  </td>
                  <td className={cn("px-3 py-2 uppercase", actionClass(row.action))}>
                    {row.action}
                    {typeof row.tradable === "boolean" &&
                      (row.action === "buy" || row.action === "sell") && (
                        <span
                          className={cn(
                            "ml-2 inline-flex rounded px-1 py-0.5 text-[10px] font-medium normal-case",
                            row.tradable
                              ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                              : "bg-amber-500/15 text-amber-700 dark:text-amber-400",
                          )}
                          title={(row.block_reasons || [])
                            .map((code) => reasonLabel(code, t))
                            .join(" · ")}
                        >
                          {row.tradable ? t("monitor.signalTradable") : t("monitor.signalBlocked")}
                        </span>
                      )}
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
                    <Link to={discussHref(row)} className="text-xs text-primary hover:underline">
                      {t("monitor.discuss")}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
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
  /**
   * Runs carrying an ``ARCHIVED`` marker: kept on disk and in the report
   * library, but not bindable as a strategy source. They are kept out of the
   * picker (an unselectable row only adds noise) and shown in 维护来源, where
   * they can still be renamed or deleted.
   */
  const [archivedRuns, setArchivedRuns] = useState<RunListItem[]>([]);
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
  const [poolOverride, setPoolOverride] = useState<PoolView | null>(null);
  const [journalHypothesis, setJournalHypothesis] = useState("");
  const [journalThesis, setJournalThesis] = useState("");
  const [journalObservation, setJournalObservation] = useState("");
  const [journalCausal, setJournalCausal] = useState("");
  const [journalLogic, setJournalLogic] = useState("");
  const [journalValidation, setJournalValidation] = useState("");
  const [journalInvalidation, setJournalInvalidation] = useState("");
  const [journalSide, setJournalSide] = useState("");
  const [journalPrice, setJournalPrice] = useState("");
  const [journalPosition, setJournalPosition] = useState("");
  const [journalRisk, setJournalRisk] = useState("");
  const [journalSymbol, setJournalSymbol] = useState("");
  const [journalSummary, setJournalSummary] = useState<string | null>(null);
  const [journalSaving, setJournalSaving] = useState(false);
  const [journalError, setJournalError] = useState<string | null>(null);
  const [journalRows, setJournalRows] = useState<MonitorJournalEntry[]>([]);
  const [journalFilter, setJournalFilter] = useState("");
  // The five-field form is only needed when a decision is being written, so it
  // stays folded: the pool status and the history are the parts read daily.
  const [journalFormOpen, setJournalFormOpen] = useState(false);
  const [journalSampleOpen, setJournalSampleOpen] = useState(false);
  const [journalSampleKind, setJournalSampleKind] = useState<JournalSampleKind>("stock");
  const [costCompare, setCostCompare] = useState<CostCompareReport | null>(null);
  const [costError, setCostError] = useState<string | null>(null);
  const [costLoading, setCostLoading] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [paperBook, setPaperBook] = useState<PaperBookSnapshot | null>(null);
  const [paperError, setPaperError] = useState<string | null>(null);
  // 持仓股一栏上方的设计市况徽章 + 其证据弹窗（与维护来源列表里的是同一套组件）。
  const [regimeRunId, setRegimeRunId] = useState<string | null>(null);
  const [regimeStyleCache, setRegimeStyleCache] = useState<Record<string, StrategyStyle>>({});

  // Filled examples so the form never looks like a blank wall of fields. The
  // values are illustrative only; the note under them says what to replace.
  const journalSamples: Record<JournalSampleKind, JournalSample> = useMemo(
    () => ({
      stock: {
        hypothesis: "",
        symbol: "600519",
        thesis: t("monitor.journalSampleStockThesis"),
        observation: t("monitor.journalSampleStockObservation"),
        causal: t("monitor.journalSampleStockHypothesis"),
        logic: t("monitor.journalSampleStockLogic"),
        validation: t("monitor.journalSampleStockValidation"),
        invalidation: t("monitor.journalSampleStockInvalidation"),
        position: t("monitor.journalSampleStockPosition"),
        risk: t("monitor.journalSampleStockRisk"),
      },
      strategy: {
        hypothesis: "",
        symbol: "",
        thesis: t("monitor.journalSampleStrategyThesis"),
        observation: t("monitor.journalSampleStrategyObservation"),
        causal: t("monitor.journalSampleStrategyHypothesis"),
        logic: t("monitor.journalSampleStrategyLogic"),
        validation: t("monitor.journalSampleStrategyValidation"),
        invalidation: t("monitor.journalSampleStrategyInvalidation"),
        position: t("monitor.journalSampleStrategyPosition"),
        risk: t("monitor.journalSampleStrategyRisk"),
      },
      anomaly: {
        hypothesis: "",
        symbol: "",
        thesis: t("monitor.journalSampleAnomalyThesis"),
        observation: t("monitor.journalSampleAnomalyObservation"),
        causal: t("monitor.journalSampleAnomalyHypothesis"),
        logic: t("monitor.journalSampleAnomalyLogic"),
        validation: t("monitor.journalSampleAnomalyValidation"),
        invalidation: t("monitor.journalSampleAnomalyInvalidation"),
        position: t("monitor.journalSampleAnomalyPosition"),
        risk: t("monitor.journalSampleAnomalyRisk"),
      },
      behavioral: {
        hypothesis: "",
        symbol: "",
        thesis: t("monitor.journalSampleBehavioralThesis"),
        observation: t("monitor.journalSampleBehavioralObservation"),
        causal: t("monitor.journalSampleBehavioralHypothesis"),
        logic: t("monitor.journalSampleBehavioralLogic"),
        validation: t("monitor.journalSampleBehavioralValidation"),
        invalidation: t("monitor.journalSampleBehavioralInvalidation"),
        position: t("monitor.journalSampleBehavioralPosition"),
        risk: t("monitor.journalSampleBehavioralRisk"),
      },
    }),
    [t],
  );
  const activeJournalSample = journalSamples[journalSampleKind];

  const applyJournalSample = () => {
    setJournalHypothesis(activeJournalSample.hypothesis);
    setJournalSymbol(activeJournalSample.symbol);
    setJournalThesis(activeJournalSample.thesis);
    setJournalObservation(activeJournalSample.observation);
    setJournalCausal(activeJournalSample.causal);
    setJournalLogic(activeJournalSample.logic);
    setJournalValidation(activeJournalSample.validation);
    setJournalInvalidation(activeJournalSample.invalidation);
    setJournalPosition(activeJournalSample.position);
    setJournalRisk(activeJournalSample.risk);
    setJournalError(null);
  };

  const applyBoard = (next: MonitorBoard) => {
    setBoard(next);
    setSymbolsText((next.symbols || []).join(", "));
    setRunId(next.strategy?.run_id || "");
    const sched = (next.schedule || "").trim();
    setScheduleEnabled(Boolean(sched));
    setSchedule(sched || DEFAULT_SCHEDULE);
    // Live board always shows the latest snapshot until the user picks history.
    setHistoryAsOf(null);
    setPoolOverride(null);
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
          // Fetch every run, then split locally: the picker must hide chat/Q&A
          // dirs (no signal_engine.py) while still showing archived strategy
          // runs as disabled options, so it does not look like reports vanished.
          api.listRuns(100).catch(() => [] as RunListItem[]),
        ]);
        if (cancelled) return;
        applyBoard(monitor);
        const allRuns = Array.isArray(runList) ? runList : [];
        const usableRuns = allRuns.filter((run) => run.has_signal_engine && !run.archived);
        setRuns(usableRuns.slice(0, 50));
        setArchivedRuns(allRuns.filter((run) => run.has_signal_engine && run.archived));
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

  /**
   * Label for the bound run when the picker does not carry it (archived, chat
   * run from an older board, or outside the top-50 slice). Without it the
   * select would render blank while the board is actually bound; it is one
   * option for the current value, never a second choice.
   */
  const boundRunOption = useMemo(() => {
    const id = runId.trim();
    if (!id || runs.some((r) => r.run_id === id)) return null;
    return archivedRuns.some((r) => r.run_id === id)
      ? `${id} · ${t("monitor.strategyOptionArchived")}`
      : t("monitor.strategyOrphanRun", { runId: id });
  }, [runId, runs, archivedRuns, t]);

  const suggestions = useMemo(
    () => sortSuggestions(board?.suggestions || []),
    [board?.suggestions],
  );
  const tradableSuggestions = useMemo(
    () => suggestions.filter((row) => row.tradable === true),
    [suggestions],
  );
  const researchSuggestions = useMemo(
    () => suggestions.filter((row) => row.tradable !== true),
    [suggestions],
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
      setPoolOverride(null);
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
      setPoolOverride(
        typeof detail.tradable === "boolean"
          ? {
              tradable: detail.tradable,
              tradable_reasons: detail.tradable_reasons,
              bars_source: detail.bars_source,
              validation_gate: detail.validation_gate,
              hypothesis: detail.hypothesis,
            }
          : null,
      );
      setViewHoldings(detail.holdings || []);
      setViewOps(detail.ops || { entered: [], exited: [], held: [] });
      setViewDecision(detail.decision || null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.historyLoadFailed"));
    } finally {
      setHistoryLoading(false);
    }
  };

  const pool: PoolView | null =
    poolOverride ??
    (board && typeof board.tradable === "boolean"
      ? {
          tradable: board.tradable,
          tradable_reasons: board.tradable_reasons,
          bars_source: board.bars_source,
          validation_gate: board.validation_gate,
          hypothesis: board.hypothesis,
        }
      : null);
  const gateNotes = pool && !pool.tradable ? gateFailureNotes(pool.validation_gate, t) : null;
  const remainingPoolReasons = (pool?.tradable_reasons || []).filter(
    (code) => !gateNotes?.codes.has(code),
  );
  const strategyBound = Boolean(runId.trim() || board?.strategy?.shadow_id);

  const strategyKey = `${board?.strategy?.type ?? ""}:${board?.strategy?.run_id ?? ""}:${board?.strategy?.shadow_id ?? ""}`;

  const refreshJournal = async () => {
    if (!strategyBound) {
      setJournalRows([]);
      return;
    }
    try {
      const rows = await api.listMonitorJournal(50, journalFilter.trim().toUpperCase());
      setJournalRows(Array.isArray(rows) ? rows : []);
      const summary = await api.journalSummary();
      if (summary && summary.reviewed > 0) {
        const rate = summary.win_rate == null ? "—" : `${Math.round(summary.win_rate * 100)}%`;
        const mean = summary.mean_pnl_pct == null ? "—" : `${summary.mean_pnl_pct.toFixed(2)}%`;
        setJournalSummary(
          t("monitor.journalOutcomeSummary", {
            reviewed: summary.reviewed,
            winRate: rate,
            mean,
          }),
        );
      } else {
        setJournalSummary(null);
      }
    } catch {
      setJournalRows([]);
      setJournalSummary(null);
    }
  };

  useEffect(() => {
    void refreshJournal();
  }, [strategyKey, strategyBound, journalFilter]);

  useEffect(() => {
    const id = runId.trim();
    if (!id) {
      setCostCompare(null);
      setCostError(null);
      setPaperBook(null);
      setPaperError(null);
      setCostLoading(false);
      return;
    }
    let cancelled = false;
    setCostLoading(true);
    setCostError(null);
    setPaperError(null);
    const read = async <T,>(
      load: () => Promise<T>,
      fallback: string,
    ): Promise<{ value: T | null; error: string | null }> => {
      try {
        return { value: await load(), error: null };
      } catch (error) {
        if (isNotFound(error)) return { value: null, error: null };
        return {
          value: null,
          error: error instanceof Error ? error.message : fallback,
        };
      }
    };
    (async () => {
      const [costResult, paperResult] = await Promise.all([
        read(() => api.getCostCompare(id), t("monitor.costCompareFailed")),
        read(() => api.getPaperBook(id), t("monitor.paperBookFailed")),
      ]);
      if (cancelled) return;
      setCostCompare(costResult.value);
      setCostError(costResult.error);
      setPaperBook(paperResult.value);
      setPaperError(paperResult.error);
      setCostLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, t]);

  // 持仓股一栏上方的设计市况：跟当前绑定的回测走。名单里的紧凑申报先顶上，
  // 再拉一次完整证据；接口不可用（旧后端）就退回名单值，不显示空白徽章。
  useEffect(() => {
    const id = runId.trim();
    if (!id) return;
    // 旧后端没有该接口时静默跳过，徽章退回名单里的紧凑申报。
    if (typeof api.getRunRegime !== "function") return;
    let cancelled = false;
    api
      .getRunRegime(id)
      .then((res) => {
        if (cancelled || !res.strategy_style) return;
        setRegimeStyleCache((prev) => ({ ...prev, [id]: res.strategy_style }));
      })
      .catch(() => {
        // 404/网络问题都只是少了证据细节，徽章仍可用名单数据渲染。
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  const activeRegimeStyle = useMemo(() => {
    const id = runId.trim();
    if (!id) return null;
    return regimeStyleCache[id] ?? runs.find((r) => r.run_id === id)?.strategy_style ?? null;
  }, [runId, regimeStyleCache, runs]);

  const runCostCompare = async () => {
    const id = runId.trim();
    if (!id || comparing) return;
    setComparing(true);
    setCostError(null);
    try {
      setCostCompare(await api.postCostCompare(id));
    } catch (error) {
      setCostError(error instanceof Error ? error.message : t("monitor.costCompareFailed"));
    } finally {
      setComparing(false);
    }
  };

  const saveDecision = async () => {
    if (journalSaving || !strategyBound) return;
    setJournalSaving(true);
    setJournalError(null);
    try {
      const price = Number(journalPrice);
      const next = await api.recordMonitorSignal({
        hypothesis_id: journalHypothesis.trim() || undefined,
        thesis: journalThesis.trim() || undefined,
        observation: journalObservation.trim() || undefined,
        hypothesis: journalCausal.trim() || undefined,
        logic: journalLogic.trim() || undefined,
        validation_plan: journalValidation.trim() || undefined,
        invalidation: journalInvalidation.trim() || undefined,
        invalidation_kind: journalSide && Number.isFinite(price) && price > 0 ? "price" : undefined,
        invalidation_side: journalSide || undefined,
        invalidation_price: journalSide && Number.isFinite(price) && price > 0 ? price : undefined,
        planned_position: journalPosition.trim() || undefined,
        planned_risk: journalRisk.trim() || undefined,
        symbol: journalSymbol.trim() || undefined,
      });
      applyBoard(next);
      await refreshJournal();
    } catch (e) {
      setJournalError(e instanceof Error ? e.message : t("monitor.journalFailed"));
    } finally {
      setJournalSaving(false);
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
      setArchivedRuns((prev) => prev.filter((r) => r.run_id !== targetId));
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
      setArchivedRuns((prev) =>
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
            {boundRunOption ? <option value={runId}>{boundRunOption}</option> : null}
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

      <LoopStatus
        runId={runId}
        cost={costCompare}
        costError={costError}
        costLoading={costLoading}
        comparing={comparing}
        onCompare={() => {
          void runCostCompare();
        }}
        paper={paperBook}
        paperError={paperError}
        tradable={typeof pool?.tradable === "boolean" ? pool.tradable : null}
      />

      <div className="rounded-lg border p-4 flex flex-col gap-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">{t("monitor.journalTitle")}</h2>
            <p className="text-xs text-muted-foreground mt-0.5">{t("monitor.journalHint")}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {journalFormOpen && (
              <button
                type="button"
                onClick={() => setJournalSampleOpen((open) => !open)}
                aria-expanded={journalSampleOpen}
                aria-controls="journal-sample"
                className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <BookOpen className="h-3.5 w-3.5" />
                {journalSampleOpen ? t("monitor.journalSampleHide") : t("monitor.journalSampleToggle")}
              </button>
            )}
            <button
              type="button"
              onClick={() => setJournalFormOpen((open) => !open)}
              aria-expanded={journalFormOpen}
              aria-controls="journal-form"
              className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              {journalFormOpen ? (
                <ChevronUp className="h-3.5 w-3.5" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5" />
              )}
              {journalFormOpen ? t("monitor.journalFormCollapse") : t("monitor.journalFormExpand")}
            </button>
          </div>
        </div>
        {pool && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted-foreground">{t("monitor.gateTitle")}</span>
            <span
              className={cn(
                "inline-flex rounded px-1.5 py-0.5 text-xs font-medium",
                pool.tradable
                  ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                  : "bg-amber-500/15 text-amber-700 dark:text-amber-400",
              )}
            >
              {pool.tradable ? t("monitor.gateIn") : t("monitor.gateOut")}
            </span>
            {pool.hypothesis && (
              <span
                data-testid="hypothesis-chip"
                title={pool.hypothesis.title || pool.hypothesis.id || ""}
                className={cn(
                  "inline-flex rounded px-1.5 py-0.5 text-xs font-medium",
                  pool.hypothesis.status === "rejected"
                    ? "bg-destructive/15 text-destructive"
                    : pool.hypothesis.status === "validated"
                      ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                      : "bg-muted text-muted-foreground",
                )}
              >
                {t("monitor.hypothesisChip", {
                  status: hypothesisStatusLabel(pool.hypothesis.status || "", t),
                })}
              </span>
            )}
            {pool.bars_source && (
              <span className="text-xs text-muted-foreground">
                {t("monitor.barsSource", { source: barsLabel(pool.bars_source, t) })}
              </span>
            )}
            {remainingPoolReasons.length > 0 && (
              <span className="text-xs text-muted-foreground">
                {remainingPoolReasons.map((code) => reasonLabel(code, t)).join(" · ")}
              </span>
            )}
            {gateNotes && gateNotes.lines.length > 0 && (
              <p className="w-full text-xs text-amber-700 dark:text-amber-400">
                {gateNotes.lines.join(" · ")}
              </p>
            )}
            <p data-testid="gate-pass-line" className="w-full text-xs text-muted-foreground">
              {t(GATE_STATUS_KEY[gateStatusFromGate(pool.validation_gate)])}
            </p>
            <p data-testid="oos-policy-line" className="w-full text-xs text-muted-foreground">
              {oosReviewFromGate(pool.validation_gate).reviewed
                ? t("monitor.oosTailReviewed")
                : t("monitor.oosNotReviewed")}
            </p>
          </div>
        )}
        {journalFormOpen && journalSampleOpen && (
          <div
            id="journal-sample"
            data-testid="journal-sample"
            className="flex flex-col gap-2 rounded-md border bg-muted/30 p-3"
          >
            <div className="flex flex-wrap items-center gap-1.5">
              {(Object.keys(JOURNAL_SAMPLE_LABEL) as JournalSampleKind[]).map((kind) => (
                <button
                  key={kind}
                  type="button"
                  onClick={() => setJournalSampleKind(kind)}
                  aria-pressed={journalSampleKind === kind}
                  className={cn(
                    "rounded-full border px-2.5 py-1 text-xs",
                    journalSampleKind === kind
                      ? "bg-background font-medium"
                      : "text-muted-foreground hover:bg-background",
                  )}
                >
                  {t(JOURNAL_SAMPLE_LABEL[kind])}
                </button>
              ))}
            </div>
            <dl className="flex flex-col gap-1 text-xs">
              <JournalSampleRow
                label={t("monitor.journalSymbol")}
                value={activeJournalSample.symbol || t("monitor.journalSampleSymbolBlank")}
              />
              <JournalSampleRow label={t("monitor.journalObservation")} value={activeJournalSample.observation} />
              <JournalSampleRow label={t("monitor.journalHypothesisCausal")} value={activeJournalSample.causal} />
              <JournalSampleRow label={t("monitor.journalLogic")} value={activeJournalSample.logic} />
              <JournalSampleRow label={t("monitor.journalValidationPlan")} value={activeJournalSample.validation} />
              <JournalSampleRow label={t("monitor.journalThesis")} value={activeJournalSample.thesis} />
              <JournalSampleRow label={t("monitor.journalInvalidation")} value={activeJournalSample.invalidation} />
              <JournalSampleRow label={t("monitor.journalPosition")} value={activeJournalSample.position} />
              <JournalSampleRow label={t("monitor.journalRisk")} value={activeJournalSample.risk} />
            </dl>
            <p className="text-xs leading-relaxed text-muted-foreground">{t("monitor.journalSampleNote")}</p>
            {(journalSampleKind === "anomaly" || journalSampleKind === "behavioral") && (
              <p data-testid="journal-sample-mechanism" className="text-xs leading-relaxed text-muted-foreground">
                {t("monitor.journalSampleMechanismNote")}
              </p>
            )}
            <button
              type="button"
              onClick={applyJournalSample}
              className="inline-flex w-fit items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-xs hover:bg-muted"
            >
              <Pencil className="h-3.5 w-3.5" />
              {t("monitor.journalSampleFill")}
            </button>
          </div>
        )}
        {journalFormOpen && (
          <>
            <div id="journal-form" className="grid gap-2 sm:grid-cols-2">
              <input
                type="text"
                value={journalHypothesis}
                onChange={(e) => setJournalHypothesis(e.target.value)}
                placeholder={t("monitor.journalHypothesis")}
                aria-label={t("monitor.journalHypothesis")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm"
              />
              <input
                type="text"
                value={journalSymbol}
                onChange={(e) => setJournalSymbol(e.target.value)}
                placeholder={t("monitor.journalSymbol")}
                aria-label={t("monitor.journalSymbol")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm font-mono"
              />
              <input
                type="text"
                value={journalObservation}
                onChange={(e) => setJournalObservation(e.target.value)}
                placeholder={t("monitor.journalObservation")}
                aria-label={t("monitor.journalObservation")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm sm:col-span-2"
              />
              <input
                type="text"
                value={journalCausal}
                onChange={(e) => setJournalCausal(e.target.value)}
                placeholder={t("monitor.journalHypothesisCausal")}
                aria-label={t("monitor.journalHypothesisCausal")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm sm:col-span-2"
              />
              <input
                type="text"
                value={journalLogic}
                onChange={(e) => setJournalLogic(e.target.value)}
                placeholder={t("monitor.journalLogic")}
                aria-label={t("monitor.journalLogic")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm sm:col-span-2"
              />
              <input
                type="text"
                value={journalValidation}
                onChange={(e) => setJournalValidation(e.target.value)}
                placeholder={t("monitor.journalValidationPlan")}
                aria-label={t("monitor.journalValidationPlan")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm sm:col-span-2"
              />
              <input
                type="text"
                value={journalThesis}
                onChange={(e) => setJournalThesis(e.target.value)}
                placeholder={t("monitor.journalThesis")}
                aria-label={t("monitor.journalThesis")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm sm:col-span-2"
              />
              <input
                type="text"
                value={journalInvalidation}
                onChange={(e) => setJournalInvalidation(e.target.value)}
                placeholder={t("monitor.journalInvalidation")}
                aria-label={t("monitor.journalInvalidation")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm sm:col-span-2"
              />
              <div
                role="group"
                aria-label={t("monitor.journalInvalidationSide")}
                className="flex flex-wrap items-center gap-3 px-1 text-sm"
              >
                {(["", "below", "above"] as const).map((side) => (
                  <label key={side || "none"} className="inline-flex items-center gap-1.5">
                    <input
                      type="radio"
                      name="journal-invalidation-side"
                      value={side}
                      checked={journalSide === side}
                      onChange={() => setJournalSide(side)}
                    />
                    {side === "below"
                      ? t("monitor.journalInvalidationBelow")
                      : side === "above"
                        ? t("monitor.journalInvalidationAbove")
                        : t("monitor.journalInvalidationSide")}
                  </label>
                ))}
              </div>
              <input
                type="text"
                inputMode="decimal"
                value={journalPrice}
                onChange={(e) => setJournalPrice(e.target.value)}
                placeholder={t("monitor.journalInvalidationPrice")}
                aria-label={t("monitor.journalInvalidationPrice")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm font-mono"
              />
              <input
                type="text"
                value={journalPosition}
                onChange={(e) => setJournalPosition(e.target.value)}
                placeholder={t("monitor.journalPosition")}
                aria-label={t("monitor.journalPosition")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm"
              />
              <input
                type="text"
                value={journalRisk}
                onChange={(e) => setJournalRisk(e.target.value)}
                placeholder={t("monitor.journalRisk")}
                aria-label={t("monitor.journalRisk")}
                className="w-full px-3 py-2 rounded-md border bg-background text-sm"
              />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={saveDecision}
                disabled={journalSaving || saving || scanning || resetting || !strategyBound}
                className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
              >
                {journalSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                {journalSaving ? t("monitor.journalSaving") : t("monitor.journalSubmit")}
              </button>
              {journalError && <span className="text-sm text-destructive">{journalError}</span>}
            </div>
          </>
        )}
        {strategyBound && (
          <div className="flex flex-col gap-2 border-t pt-3" data-testid="journal-history">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <h3 className="text-sm font-medium">{t("monitor.journalHistoryTitle")}</h3>
              <input
                type="text"
                value={journalFilter}
                onChange={(e) => setJournalFilter(e.target.value)}
                placeholder={t("monitor.journalHistoryFilter")}
                aria-label={t("monitor.journalHistoryFilter")}
                className="w-40 px-2 py-1 rounded-md border bg-background text-xs font-mono"
              />
            </div>
            {journalSummary && (
              <p className="text-xs text-muted-foreground" data-testid="journal-summary">
                {journalSummary}
              </p>
            )}
            {journalRows.length === 0 ? (
              <p className="text-xs text-muted-foreground">{t("monitor.journalHistoryEmpty")}</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {journalRows.map((row) => (
                  <li
                    key={row.signal_id}
                    data-lane={row.tradable ? "tradable" : "research"}
                    className={cn(
                      "rounded-md border px-3 py-2 text-xs flex flex-col gap-1",
                      row.tradable
                        ? "border-emerald-500/50 bg-emerald-500/10"
                        : "border-dashed border-amber-500/60 bg-muted/60",
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={cn(
                          "inline-flex rounded px-1.5 py-0.5 font-medium",
                          row.tradable
                            ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                            : "border border-dashed border-amber-500/70 text-amber-800 dark:text-amber-300",
                        )}
                      >
                        {row.tradable ? t("monitor.laneTradable") : t("monitor.laneResearch")}
                      </span>
                      <span className="font-mono text-muted-foreground">{row.timestamp}</span>
                      <span className="font-mono">{row.symbol || t("monitor.journalWholeStrategy")}</span>
                      <span
                        title={t("monitor.journalFieldsHint")}
                        className={cn(
                          "inline-flex rounded px-1.5 py-0.5 font-medium",
                          row.tradable
                            ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400"
                            : "bg-amber-500/15 text-amber-700 dark:text-amber-400",
                        )}
                      >
                        {row.tradable ? t("monitor.journalFieldsComplete") : t("monitor.journalFieldsMissing")}
                      </span>
                      {row.stale && (
                        <span className="inline-flex rounded bg-muted px-1.5 py-0.5 font-medium text-muted-foreground">
                          {t("monitor.journalStale", { days: row.age_days ?? 0 })}
                        </span>
                      )}
                      {row.block_reasons.length > 0 && (
                        <span className="text-muted-foreground">
                          {row.block_reasons.map((code) => reasonLabel(code, t)).join(" · ")}
                        </span>
                      )}
                    </div>
                    {row.thesis && <div>{row.thesis}</div>}
                    {row.hypothesis && <div>{row.hypothesis}</div>}
                    {row.outcome ? (
                      <div className="text-muted-foreground">
                        {row.outcome.pnl_pct}%{row.outcome.note ? ` · ${row.outcome.note}` : ""}
                      </div>
                    ) : (
                      <form
                        className="flex flex-wrap items-center gap-2"
                        onSubmit={(event) => {
                          event.preventDefault();
                          const data = new FormData(event.currentTarget);
                          const pnl = Number(data.get("pnl"));
                          if (!Number.isFinite(pnl)) return;
                          void api
                            .recordJournalOutcome(row.signal_id, {
                              pnl_pct: pnl,
                              note: String(data.get("note") || ""),
                            })
                            .then(() => refreshJournal())
                            .catch((err: unknown) => {
                              setJournalError(err instanceof Error ? err.message : t("monitor.journalFailed"));
                            });
                        }}
                      >
                        <input
                          name="pnl"
                          inputMode="decimal"
                          aria-label={t("monitor.journalOutcomePnl")}
                          placeholder={t("monitor.journalOutcomePnl")}
                          className="w-28 px-2 py-1 rounded-md border bg-background font-mono"
                        />
                        <input
                          name="note"
                          aria-label={t("monitor.journalOutcomeNote")}
                          placeholder={t("monitor.journalOutcomeNote")}
                          className="w-40 px-2 py-1 rounded-md border bg-background"
                        />
                        <button type="submit" className="rounded-md border px-2 py-1 hover:bg-muted">
                          {t("monitor.journalOutcomeSave")}
                        </button>
                      </form>
                    )}
                    <div className="text-muted-foreground">
                      {[row.invalidation, row.planned_position, row.planned_risk].filter(Boolean).join(" · ")}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

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
        {/* 当前回测的设计市况：放在持仓股一栏上方，点开就是同一套证据弹窗。 */}
        {activeRegimeStyle ? (
          <div className="flex flex-col gap-1" data-testid="holdings-regime">
            <div className="text-xs font-medium text-muted-foreground">
              {t("monitor.holdingsRegimeTitle")}
            </div>
            <RegimeChip style={activeRegimeStyle} onClick={() => setRegimeRunId(runId.trim())} />
          </div>
        ) : null}
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

        {(board?.journal_alerts?.length ?? 0) > 0 && !historyAsOf && (
          <div
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300"
            data-testid="journal-holding-alert"
          >
            {t("monitor.journalHoldingAlert", { symbols: (board?.journal_alerts || []).join(", ") })}
          </div>
        )}

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
                  const alerted = (board?.journal_alerts || []).includes(row.symbol);
                  const holdingLane =
                    pool?.tradable === true && !alerted
                      ? "tradable"
                      : pool?.tradable === false || alerted
                        ? "research"
                        : "unknown";
                  return (
                  <tr
                    key={`${row.symbol}-${row.action}-${row.data_as_of || ""}`}
                    data-lane={holdingLane}
                    className={cn(
                      "border-t",
                      holdingLane === "tradable" && "bg-emerald-500/[0.07]",
                      holdingLane === "research" && "bg-amber-500/[0.06]",
                    )}
                  >
                    <td className="px-3 py-2 font-mono">
                      <span>{row.symbol}</span>
                      {holdingLane === "research" && (
                        <span className="ml-1.5 inline-flex rounded border border-dashed border-amber-500/70 px-1 py-0.5 text-[10px] font-medium text-amber-800 dark:text-amber-300">
                          {t("monitor.researchOnly")}
                        </span>
                      )}
                      {holdingLane === "tradable" && (
                        <span className="ml-1.5 inline-flex rounded bg-emerald-500/15 px-1 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-400">
                          {t("monitor.laneTradable")}
                        </span>
                      )}
                    </td>
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

      <div className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">{t("monitor.suggestions")}</h2>
        {suggestions.length === 0 && (
          <p className="text-sm text-muted-foreground border rounded-lg p-4">
            {t("monitor.emptySuggestions")}
          </p>
        )}
        <SuggestionTable
          rows={tradableSuggestions}
          lane="tradable"
          t={t}
          discussHref={discussHref}
        />
        <SuggestionTable
          rows={researchSuggestions}
          lane="research"
          t={t}
          discussHref={discussHref}
        />
      </div>

      {detailOpen && runId.trim() && (
        <StrategyDetailModal runId={runId.trim()} onClose={() => setDetailOpen(false)} />
      )}

      {forecastOpen && (
        <ForecastPanel asOf={historyAsOf} onClose={() => setForecastOpen(false)} />
      )}

      {maintainOpen && (
        <RunSourceMaintainModal
          runs={[...runs, ...archivedRuns]}
          activeRunId={runId}
          deleting={deleting}
          regimeStyles={regimeStyleCache}
          onOpenRegime={(id) => setRegimeRunId(id)}
          onClose={() => setMaintainOpen(false)}
          onRequestDelete={(id) => setPendingDeleteId(id)}
          onRename={renameRunTitle}
        />
      )}

      {regimeRunId ? (
        <RegimePanelModal
          runId={regimeRunId}
          initialStyle={
            regimeStyleCache[regimeRunId] ??
            runs.find((r) => r.run_id === regimeRunId)?.strategy_style ??
            null
          }
          onClose={() => setRegimeRunId(null)}
          onSaved={(style) =>
            setRegimeStyleCache((prev) => ({ ...prev, [regimeRunId]: style }))
          }
        />
      ) : null}

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
  regimeStyles,
  onOpenRegime,
  onClose,
  onRequestDelete,
  onRename,
}: {
  runs: RunListItem[];
  activeRunId: string;
  deleting: boolean;
  /** 已保存的市况申报缓存（页级共享，保证持仓股栏与这里显示同一个值）。 */
  regimeStyles: Record<string, StrategyStyle>;
  onOpenRegime: (runId: string) => void;
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
                      {r.strategy_style?.style_label ? (
                        <div className="mt-0.5 break-words text-[11px] text-muted-foreground">
                          {`风格：${r.strategy_style.style_label}`}
                          {r.strategy_style.timing_of_declaration === "retrospective"
                            ? " · 事后补记"
                            : ""}
                        </div>
                      ) : null}
                      <RegimeChip
                        style={regimeStyles[r.run_id] ?? r.strategy_style}
                        onClick={() => onOpenRegime(r.run_id)}
                      />
                      <div className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">
                        {r.run_id}
                        {r.archived ? (
                          <span className="ms-2 rounded bg-muted px-1 py-0.5 font-sans text-[10px]">
                            {t("monitor.strategyOptionArchived")}
                          </span>
                        ) : null}
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
