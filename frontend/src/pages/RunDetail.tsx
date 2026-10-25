import i18n from '@/i18n';
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  CheckCircle2,
  ClipboardCheck,
  Code2,
  Database,
  Download,
  FileCheck2,
  Fingerprint,
  List,
  Loader2,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, type BacktestMetrics, type RunCard, type RunData, type StrategyAssessment, type SymbolPnLRow } from "@/lib/api";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { EquityChart } from "@/components/charts/EquityChart";
import { MetricsCard } from "@/components/chat/MetricsCard";
import { ValidationPanel } from "@/components/charts/ValidationPanel";
import { Skeleton, SkeletonMetrics, SkeletonChart } from "@/components/common/Skeleton";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";

const rehypePlugins = [rehypeHighlight];

type Tab = "chart" | "trades" | "assessment" | "runCard" | "code" | "validation";
type ChartPayload = Pick<RunData, "price_series" | "indicator_series" | "trade_markers">;
type ChartCache = Record<string, ChartPayload>;
type ChartLoadProgress = { done: number; total: number };

function downloadCsv(filename: string, csvContent: string) {
  const blob = new Blob(["\uFEFF" + csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function escapeCsvField(value: unknown): string {
  const str = String(value ?? "");
  if (str.includes(",") || str.includes('"') || str.includes("\n")) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function buildTradesCsv(trades: Array<Record<string, string>>): string {
  if (trades.length === 0) return "";
  const keys = [...new Set(trades.flatMap(Object.keys))];
  const header = keys.map(escapeCsvField).join(",");
  const rows = trades.map(tr => keys.map(k => escapeCsvField(tr[k])).join(","));
  return [header, ...rows].join("\n");
}

function buildMetricsCsv(metrics: BacktestMetrics): string {
  const header = "metric,value";
  const rows = Object.entries(metrics).map(([k, v]) => `${escapeCsvField(k)},${escapeCsvField(v)}`);
  return [header, ...rows].join("\n");
}

function cacheFromRun(run: RunData | null, requestedSymbol?: string): ChartCache {
  if (!run?.price_series) return {};
  const cache: ChartCache = {};
  const markerRows = run.trade_markers || [];
  for (const [symbol, bars] of Object.entries(run.price_series)) {
    cache[symbol] = {
      price_series: { [symbol]: bars },
      indicator_series: run.indicator_series?.[symbol] ? { [symbol]: run.indicator_series[symbol] } : {},
      trade_markers: markerRows.filter((marker) => !marker.code || marker.code === symbol),
    };
  }
  if (requestedSymbol && !cache[requestedSymbol]) {
    cache[requestedSymbol] = { price_series: {}, indicator_series: {}, trade_markers: [] };
  }
  return cache;
}

function yieldToBrowser(): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, 0);
  });
}

/** Symbols that actually traded — not the full backtest universe. */
function tradedChartSymbols(run: RunData | null): string[] {
  if (!run) return [];

  // Prefer attribution rows, ranked by contribution so bulk chart load
  // surfaces the most impactful names first (high → low).
  const bySymbolRows = [...(run.by_symbol || [])].sort(
    (left, right) => (right.contribution_pct ?? 0) - (left.contribution_pct ?? 0),
  );
  const fromBySymbol = bySymbolRows
    .map((row) => String(row.code || "").trim())
    .filter(Boolean);
  if (fromBySymbol.length > 0) {
    return [...new Set(fromBySymbol)];
  }

  // Fallback: unique codes from the trade log (summary payloads may truncate).
  const traded = new Set(
    (run.trade_log || [])
      .map((row) => String(row.code || "").trim())
      .filter(Boolean),
  );
  if (traded.size === 0) return [];

  // Keep dropdown order when the symbol is also in chart_symbols.
  const ordered = (run.chart_symbols || []).filter((symbol) => traded.has(symbol));
  if (ordered.length > 0) return ordered;
  return [...traded].sort();
}

export function RunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const [run, setRun] = useState<RunData | null>(null);
  const [code, setCode] = useState<Record<string, string>>({});
  const [tab, setTab] = useState<Tab>("chart");
  const [loading, setLoading] = useState(true);
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [chartPickerSymbol, setChartPickerSymbol] = useState("");
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>([]);
  const [chartCache, setChartCache] = useState<ChartCache>({});
  const [chartLoadingSymbols, setChartLoadingSymbols] = useState<Record<string, boolean>>({});
  const [bulkChartLoading, setBulkChartLoading] = useState(false);
  const [bulkChartProgress, setBulkChartProgress] = useState<ChartLoadProgress>({ done: 0, total: 0 });
  const chartCacheRef = useRef<ChartCache>({});
  const cancelBulkChartLoadRef = useRef(false);
  const runGenerationRef = useRef(0);

  const hasValidation = !!run?.validation;
  const hasRunCard = !!run?.run_card;
  const hasAssessment = !!run?.strategy_assessment;
  const TABS: { id: Tab; label: string; icon: typeof BarChart3; hidden?: boolean }[] = [
    { id: "chart", label: i18n.t("runDetail.chart"), icon: BarChart3 },
    { id: "trades", label: i18n.t("runDetail.trades"), icon: List },
    { id: "assessment", label: i18n.t("runDetail.assessment"), icon: ClipboardCheck, hidden: !hasAssessment },
    { id: "validation", label: i18n.t("runDetail.validation"), icon: ShieldCheck, hidden: !hasValidation },
    { id: "runCard", label: i18n.t("runDetail.runCard"), icon: FileCheck2, hidden: !hasRunCard },
    { id: "code", label: i18n.t("runDetail.code"), icon: Code2 },
  ];

  useEffect(() => {
    const generation = ++runGenerationRef.current;
    cancelBulkChartLoadRef.current = true;
    setRun(null);
    setCode({});
    setTab("chart");
    setLoading(true);
    setSelectedSymbol("");
    setChartPickerSymbol("");
    setSelectedSymbols([]);
    chartCacheRef.current = {};
    setChartCache({});
    setChartLoadingSymbols({});
    setBulkChartLoading(false);
    setBulkChartProgress({ done: 0, total: 0 });

    if (!runId) {
      setLoading(false);
      return;
    }

    const requestedRunId = runId;
    Promise.all([
      api.getRun(requestedRunId, { chart_payload: "summary" }).catch(() => null),
      api.getRunCode(requestedRunId).catch(() => ({})),
    ]).then(([r, c]) => {
      if (runGenerationRef.current !== generation) return;
      setRun(r);
      setCode(c || {});
      const firstSymbol = r?.chart_symbols?.[0] || Object.keys(r?.price_series || {})[0] || "";
      setSelectedSymbol(firstSymbol);
      setChartPickerSymbol(firstSymbol);
      setSelectedSymbols(firstSymbol ? [firstSymbol] : []);
      const initialCache = cacheFromRun(r, firstSymbol);
      chartCacheRef.current = initialCache;
      setChartCache(initialCache);
      if (firstSymbol && !initialCache[firstSymbol]?.price_series?.[firstSymbol]?.length) {
        void loadChartSymbol(firstSymbol, requestedRunId, generation);
      }
    }).finally(() => {
      if (runGenerationRef.current === generation) setLoading(false);
    });

    return () => {
      cancelBulkChartLoadRef.current = true;
      if (runGenerationRef.current === generation) runGenerationRef.current += 1;
    };
  }, [runId]);

  if (loading) {
    return (
      <div className="p-8 space-y-4">
        <Skeleton className="h-6 w-48" />
        <SkeletonMetrics />
        <SkeletonChart height={400} />
      </div>
    );
  }
  if (!run) return (
    <div className="p-8 space-y-2">
      <p className="text-red-500 font-medium">{i18n.t("runDetail.runNotFound")}</p>
      <p className="text-sm text-muted-foreground">
        {i18n.t("runDetail.runNotFoundDesc")}, or your browser may not have API access configured.
        Check that the API authentication key is set in Settings if accessing remotely.
      </p>
      <button
        onClick={() => navigate(-1)}
        className="text-sm text-primary hover:underline inline-flex items-center gap-1.5"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> {i18n.t("runDetail.goBack")}
      </button>
    </div>
  );

  const ok = run.status === "success";

  async function loadChartSymbol(
    symbol: string,
    requestedRunId = runId,
    generation = runGenerationRef.current,
  ) {
    if (!requestedRunId || !symbol || runGenerationRef.current !== generation) return;
    if (chartCacheRef.current[symbol]?.price_series?.[symbol]?.length) return;
    setChartLoadingSymbols((prev) => ({ ...prev, [symbol]: true }));
    try {
      const nextRun = await api.getRun(requestedRunId, { chart_symbol: symbol });
      if (runGenerationRef.current !== generation) return;
      const nextCache = cacheFromRun(nextRun, symbol);
      const mergedCache = { ...chartCacheRef.current, ...nextCache };
      chartCacheRef.current = mergedCache;
      setChartCache(mergedCache);
      setRun((prev) => prev ? {
        ...prev,
        chart_symbols: nextRun.chart_symbols?.length ? nextRun.chart_symbols : prev.chart_symbols,
        equity_curve: nextRun.equity_curve?.length ? nextRun.equity_curve : prev.equity_curve,
        trade_log: nextRun.trade_log?.length ? nextRun.trade_log : prev.trade_log,
        // Keep attribution/assessment from the first full payload — chart
        // reloads may omit or recompute them; don't wipe the tables.
        by_symbol: prev.by_symbol?.length ? prev.by_symbol : nextRun.by_symbol,
        by_exit_reason: prev.by_exit_reason?.length ? prev.by_exit_reason : nextRun.by_exit_reason,
        strategy_assessment: prev.strategy_assessment || nextRun.strategy_assessment,
      } : nextRun);
    } finally {
      if (runGenerationRef.current === generation) {
        setChartLoadingSymbols((prev) => {
          const next = { ...prev };
          delete next[symbol];
          return next;
        });
      }
    }
  }

  async function handleAddChartSymbol(symbol: string) {
    if (!symbol) return;
    setSelectedSymbol(symbol);
    setChartPickerSymbol(symbol);
    setSelectedSymbols((prev) => prev.includes(symbol) ? prev : [...prev, symbol]);
    await loadChartSymbol(symbol);
  }

  async function handleCurrentChartOnly(symbol: string) {
    if (!symbol) return;
    setSelectedSymbol(symbol);
    setChartPickerSymbol(symbol);
    setSelectedSymbols([symbol]);
    await loadChartSymbol(symbol);
  }

  function handleRemoveChartSymbol(symbol: string) {
    const nextSymbols = selectedSymbols.filter((item) => item !== symbol);
    setSelectedSymbols(nextSymbols);
    if (selectedSymbol === symbol) {
      const fallback = nextSymbols[0] || run?.chart_symbols?.[0] || "";
      setSelectedSymbol(fallback);
      setChartPickerSymbol(fallback);
    }
  }

  async function handleLoadAllChartSymbols() {
    // One-click load for every traded symbol, contribution-high first.
    const symbols = tradedChartSymbols(run);
    if (symbols.length === 0 || bulkChartLoading) return;
    const generation = runGenerationRef.current;
    const requestedRunId = runId;
    cancelBulkChartLoadRef.current = false;
    setBulkChartLoading(true);
    setBulkChartProgress({ done: 0, total: symbols.length });
    // Keep chip/chart order aligned with contribution ranking as items appear.
    const selectedInRankOrder = new Set<string>();
    try {
      for (let index = 0; index < symbols.length; index += 1) {
        if (cancelBulkChartLoadRef.current || runGenerationRef.current !== generation) break;
        const symbol = symbols[index];
        selectedInRankOrder.add(symbol);
        setSelectedSymbol(symbol);
        setChartPickerSymbol(symbol);
        setSelectedSymbols(symbols.filter((item) => selectedInRankOrder.has(item)));
        await loadChartSymbol(symbol, requestedRunId, generation);
        if (runGenerationRef.current !== generation) break;
        setBulkChartProgress({ done: index + 1, total: symbols.length });
        await yieldToBrowser();
      }
    } finally {
      if (runGenerationRef.current === generation) setBulkChartLoading(false);
    }
  }

  function handleCancelLoadAllCharts() {
    cancelBulkChartLoadRef.current = true;
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b p-4 space-y-3">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(-1)}
            className="p-1 rounded-md hover:bg-muted transition-colors text-muted-foreground hover:text-foreground"
            title={i18n.t("runDetail.goBack")}
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          {ok ? <CheckCircle2 className="h-5 w-5 text-success" /> : <XCircle className="h-5 w-5 text-danger" />}
          <h1 className="font-mono text-sm font-medium">{runId}</h1>
          {run.elapsed_seconds && <span className="text-xs text-muted-foreground">{run.elapsed_seconds.toFixed(1)}s</span>}
        </div>
        {run.prompt && <p className="text-sm text-muted-foreground">{run.prompt}</p>}
        {run.metrics && <MetricsCard metrics={run.metrics as Record<string, number>} />}

        <div className="flex items-center gap-1">
          {TABS.filter(t => !t.hidden).map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => setTab(id)}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm transition-colors",
                tab === id ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"
              )}
            >
              <Icon className="h-3.5 w-3.5" /> {label}
            </button>
          ))}

          <div className="ml-auto flex gap-1">
            {run.trade_log && run.trade_log.length > 0 && (
              <button
                onClick={() => downloadCsv(`trades_${runId}.csv`, buildTradesCsv(run.trade_log!))}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs text-muted-foreground hover:bg-muted transition-colors"
                title={i18n.t("runDetail.downloadTradesCsv")}
              >
                <Download className="h-3.5 w-3.5" /> {i18n.t("runDetail.downloadTradesCsv")}
              </button>
            )}
            {run.metrics && (
              <button
                onClick={() => downloadCsv(`metrics_${runId}.csv`, buildMetricsCsv(run.metrics!))}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs text-muted-foreground hover:bg-muted transition-colors"
                title={i18n.t("runDetail.downloadMetricsCsv")}
              >
                <Download className="h-3.5 w-3.5" /> {i18n.t("runDetail.downloadMetricsCsv")}
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        <ErrorBoundary>
          {tab === "chart" && (
            <ChartTab
              run={run}
              chartPickerSymbol={chartPickerSymbol}
              selectedSymbols={selectedSymbols}
              chartCache={chartCache}
              loadingSymbols={chartLoadingSymbols}
              bulkLoading={bulkChartLoading}
              bulkProgress={bulkChartProgress}
              onPickSymbol={setChartPickerSymbol}
              onAddSymbol={handleAddChartSymbol}
              onCurrentOnly={handleCurrentChartOnly}
              onRemoveSymbol={handleRemoveChartSymbol}
              onLoadAll={handleLoadAllChartSymbols}
              onCancelLoadAll={handleCancelLoadAllCharts}
            />
          )}
          {tab === "trades" && <TradesTab run={run} />}
          {tab === "assessment" && run.strategy_assessment && (
            <AssessmentTab assessment={run.strategy_assessment} bySymbol={run.by_symbol || []} />
          )}
          {tab === "validation" && run.validation && <ValidationPanel data={run.validation} />}
          {tab === "runCard" && run.run_card && <RunCardTab card={run.run_card} />}
          {tab === "code" && <CodeTab code={code} />}
        </ErrorBoundary>
      </div>
    </div>
  );
}

function RunCardTab({ card }: { card: RunCard }) {
  const backtest = card.backtest || {};
  const reproducibility = card.reproducibility || {};
  const metrics = card.metrics || {};
  const artifacts = card.artifacts || [];
  const warnings = card.warnings || [];
  const dataSources = card.data_sources || [];

  return (
    <div className="p-4 space-y-4">
      <div className="grid gap-3 md:grid-cols-4">
        <RunCardStat label={i18n.t("runDetail.schema")} value={card.schema_version || "unknown"} />
        <RunCardStat label={i18n.t("runDetail.generated")} value={formatRunCardValue(card.generated_at)} />
        <RunCardStat label={i18n.t("runDetail.dataSources")} value={dataSources.length ? dataSources.join(", ") : "None recorded"} />
        <RunCardStat label={i18n.t("runDetail.warnings")} value={String(warnings.length)} tone={warnings.length ? "warning" : "normal"} />
      </div>

      {warnings.length > 0 && (
        <section className="rounded-md border border-amber-500/25 bg-amber-500/5 p-3">
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-amber-700 dark:text-amber-300">
            <AlertTriangle className="h-4 w-4" />
            {i18n.t("runDetail.warnings")}
          </div>
          <ul className="space-y-1 text-xs text-muted-foreground">
            {warnings.map((warning, index) => <li key={index}>{warning}</li>)}
          </ul>
        </section>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <RunCardPanel title={i18n.t("runDetail.backtestSummary")} icon={Database}>
          <KeyValueTable data={backtest} empty={i18n.t("runDetail.noBacktestSummary")} />
        </RunCardPanel>
        <RunCardPanel title={i18n.t("runDetail.reproducibility")} icon={Fingerprint}>
          <KeyValueTable data={reproducibility} empty={i18n.t("runDetail.noReproducibilityHashes")} monospaceValues />
        </RunCardPanel>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <RunCardPanel title={i18n.t("runDetail.metrics")} icon={BarChart3}>
          <KeyValueTable data={metrics} empty={i18n.t("runDetail.noScalarMetrics")} />
        </RunCardPanel>
        <RunCardPanel title={i18n.t("runDetail.validationPayload")} icon={ShieldCheck}>
          {card.validation ? (
            <pre className="max-h-80 overflow-auto rounded-md bg-muted/40 p-3 text-xs leading-relaxed">
              {JSON.stringify(card.validation, null, 2)}
            </pre>
          ) : (
            <p className="text-sm text-muted-foreground">{i18n.t("runDetail.noValidationPayload")}</p>
          )}
        </RunCardPanel>
      </div>

      <RunCardPanel title={i18n.t("runDetail.artifactChecksums")} icon={FileCheck2}>
        {artifacts.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-2 pr-4">{i18n.t("runDetail.path")}</th>
                  <th className="py-2 pr-4">{i18n.t("runDetail.size")}</th>
                  <th className="py-2">{i18n.t("runDetail.sha256")}</th>
                </tr>
              </thead>
              <tbody>
                {artifacts.map((artifact) => (
                  <tr key={`${artifact.path}-${artifact.sha256}`} className="border-b last:border-0">
                    <td className="py-2 pr-4 font-mono text-xs">{artifact.path}</td>
                    <td className="py-2 pr-4 tabular-nums text-muted-foreground">{formatBytes(artifact.size_bytes)}</td>
                    <td className="py-2 font-mono text-xs text-muted-foreground">{shortHash(artifact.sha256)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{i18n.t("runDetail.noArtifactChecksums")}</p>
        )}
      </RunCardPanel>
    </div>
  );
}

function RunCardStat({ label, value, tone = "normal" }: { label: string; value: string; tone?: "normal" | "warning" }) {
  return (
    <div className="rounded-md border bg-card p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 truncate text-sm font-medium", tone === "warning" ? "text-amber-700 dark:text-amber-300" : "")}>{value}</div>
    </div>
  );
}

function RunCardPanel({ title, icon: Icon, children }: { title: string; icon: typeof FileCheck2; children: ReactNode }) {
  return (
    <section className="rounded-md border bg-card p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-medium">
        <Icon className="h-4 w-4 text-muted-foreground" />
        {title}
      </div>
      {children}
    </section>
  );
}

function KeyValueTable({ data, empty, monospaceValues = false }: { data: Record<string, unknown>; empty: string; monospaceValues?: boolean }) {
  const entries = Object.entries(data).filter(([, value]) => value !== undefined && value !== null && value !== "");
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">{empty}</p>;
  }
  return (
    <table className="w-full table-fixed text-sm">
      <tbody>
        {entries.map(([key, value]) => (
          <tr key={key} className="border-b last:border-0">
            <td className="w-36 py-2 pr-4 align-top text-muted-foreground">{key}</td>
            <td className={cn("py-2 align-top", monospaceValues ? "break-all font-mono text-xs" : "break-words text-right tabular-nums")}>{formatRunCardValue(value)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatRunCardValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4);
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return String(value ?? "");
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value)) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function shortHash(value: string): string {
  return value.length > 16 ? `${value.slice(0, 12)}...${value.slice(-6)}` : value;
}

function ChartTab({
  run,
  chartPickerSymbol,
  selectedSymbols,
  chartCache,
  loadingSymbols,
  bulkLoading,
  bulkProgress,
  onPickSymbol,
  onAddSymbol,
  onCurrentOnly,
  onRemoveSymbol,
  onLoadAll,
  onCancelLoadAll,
}: {
  run: RunData;
  chartPickerSymbol: string;
  selectedSymbols: string[];
  chartCache: ChartCache;
  loadingSymbols: Record<string, boolean>;
  bulkLoading: boolean;
  bulkProgress: ChartLoadProgress;
  onPickSymbol: (symbol: string) => void;
  onAddSymbol: (symbol: string) => void | Promise<void>;
  onCurrentOnly: (symbol: string) => void | Promise<void>;
  onRemoveSymbol: (symbol: string) => void;
  onLoadAll: () => void | Promise<void>;
  onCancelLoadAll: () => void;
}) {
  const chartSymbols = run.chart_symbols || Object.keys(run.price_series || {});
  const tradedSymbols = tradedChartSymbols(run);
  const entries = selectedSymbols
    .map((symbol) => [symbol, chartCache[symbol]?.price_series?.[symbol] || []] as const)
    .filter(([, bars]) => bars.length > 0);
  const hasEquity = run.equity_curve && run.equity_curve.length > 0;
  const progressPercent = bulkProgress.total > 0 ? Math.round((bulkProgress.done / bulkProgress.total) * 100) : 0;

  if (chartSymbols.length === 0 && entries.length === 0 && !hasEquity) {
    return (
      <div className="p-8 text-center text-muted-foreground space-y-2">
        <p className="text-sm">{i18n.t("runDetail.noChartData")}</p>
        <p className="text-xs">{i18n.t("runDetail.noChartDataDesc")}</p>
      </div>
    );
  }

  return (
    <div className="p-4 space-y-4">
      {chartSymbols.length > 0 && (
        <div className="rounded-md border bg-card p-3">
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="chart-symbol-select">
              {i18n.t("runDetail.symbol")}
            </label>
            <select
              id="chart-symbol-select"
              value={chartPickerSymbol}
              onChange={(event) => onPickSymbol(event.target.value)}
              className="h-8 rounded-md border bg-background px-2 text-sm"
            >
              {chartSymbols.map((symbol) => (
                <option key={symbol} value={symbol}>{symbol}</option>
              ))}
            </select>
            <button
              onClick={() => onCurrentOnly(chartPickerSymbol)}
              className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
              disabled={!chartPickerSymbol || !!loadingSymbols[chartPickerSymbol]}
            >
              {loadingSymbols[chartPickerSymbol] ? <Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" /> : null}
              {i18n.t("runDetail.showOnly")}
            </button>
            <button
              onClick={() => onAddSymbol(chartPickerSymbol)}
              className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
              disabled={!chartPickerSymbol || !!loadingSymbols[chartPickerSymbol]}
            >
              {i18n.t("runDetail.addSymbol")}
            </button>
            {/* Keep bulk actions on the right of the symbol toolbar. */}
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <button
                onClick={() => void onLoadAll()}
                className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
                disabled={bulkLoading || tradedSymbols.length === 0}
                title={
                  tradedSymbols.length === 0
                    ? i18n.t("runDetail.loadAllTradedEmpty")
                    : i18n.t("runDetail.loadAllTradedHint", { count: tradedSymbols.length })
                }
              >
                {bulkLoading ? <Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" /> : null}
                {i18n.t("runDetail.loadAll")}
              </button>
              {bulkLoading && (
                <button
                  onClick={onCancelLoadAll}
                  className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted"
                >
                  {i18n.t("runDetail.cancelLoad")}
                </button>
              )}
            </div>
          </div>
          {selectedSymbols.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {selectedSymbols.map((symbol) => (
                <button
                  key={symbol}
                  onClick={() => onRemoveSymbol(symbol)}
                  className="rounded-md bg-muted px-2 py-1 text-xs hover:bg-muted/80"
                >
                  {symbol} x
                </button>
              ))}
            </div>
          )}
          {bulkLoading && (
            <div className="mt-3 space-y-1">
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>{i18n.t("runDetail.loadingCharts")}</span>
                <span>{bulkProgress.done}/{bulkProgress.total}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div className="h-full bg-primary transition-all" style={{ width: `${progressPercent}%` }} />
              </div>
            </div>
          )}
        </div>
      )}
      {entries.length === 0 && (
        <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
          {Object.keys(loadingSymbols).length > 0 ? i18n.t("runDetail.loadingSelectedChart") : i18n.t("runDetail.pickSymbolToLoad")}
        </div>
      )}
      {entries.map(([sym, bars]) => (
        <div key={sym}>
          <h3 className="text-sm font-medium mb-1">{sym}</h3>
          <CandlestickChart data={bars} markers={chartCache[sym]?.trade_markers?.filter(m => m.code === sym)} indicators={chartCache[sym]?.indicator_series?.[sym]} height={500} />
        </div>
      ))}
      {hasEquity && (
        <div>
          <h3 className="text-sm font-medium mb-1">{i18n.t("runDetail.equityDrawdown")}</h3>
          <EquityChart data={run.equity_curve!} height={280} />
        </div>
      )}
    </div>
  );
}

function TradesTab({ run }: { run: RunData }) {
  const trades = run.trade_log || [];
  const bySymbol = run.by_symbol || [];
  const byReason = run.by_exit_reason || [];

  if (trades.length === 0 && bySymbol.length === 0) {
    return <div className="p-8 text-muted-foreground text-sm">{i18n.t("runDetail.noTrades")}</div>;
  }

  return (
    <div className="p-4 space-y-6">
      {bySymbol.length > 0 && (
        <section className="space-y-2">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium">{i18n.t("runDetail.symbolPnL")}</h3>
            <p className="text-xs text-muted-foreground">
              {i18n.t("runDetail.symbolPnLHint", { count: bySymbol.length })}
            </p>
          </div>
          <div className="overflow-auto rounded-md border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/30 text-left text-muted-foreground">
                  <th className="py-2 px-3">{i18n.t("runDetail.code2")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.tradeCount")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.winRate")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.totalPnl")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.avgPnl")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.avgReturn")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.avgHolding")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.contribution")}</th>
                </tr>
              </thead>
              <tbody>
                {bySymbol.map((row) => (
                  <tr key={row.code} className="border-b last:border-0 hover:bg-muted/20">
                    <td className="py-2 px-3 font-mono text-xs">{row.code}</td>
                    <td className="py-2 px-3 tabular-nums">{row.count}</td>
                    <td className="py-2 px-3 tabular-nums">{(row.win_rate * 100).toFixed(1)}%</td>
                    <td className={cn("py-2 px-3 tabular-nums font-medium", pnlTone(row.total_pnl))}>
                      {formatPnl(row.total_pnl)}
                    </td>
                    <td className={cn("py-2 px-3 tabular-nums", pnlTone(row.avg_pnl))}>
                      {formatPnl(row.avg_pnl)}
                    </td>
                    <td className={cn("py-2 px-3 tabular-nums", pnlTone(row.avg_return_pct))}>
                      {row.avg_return_pct.toFixed(2)}%
                    </td>
                    <td className="py-2 px-3 tabular-nums">{row.avg_holding_days.toFixed(1)}</td>
                    <td className="py-2 px-3 tabular-nums">{(row.contribution_pct * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {byReason.length > 0 && (
        <section className="space-y-2">
          <h3 className="text-sm font-medium">{i18n.t("runDetail.exitReasonBreakdown")}</h3>
          <div className="overflow-auto rounded-md border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/30 text-left text-muted-foreground">
                  <th className="py-2 px-3">{i18n.t("runDetail.reason")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.tradeCount")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.winRate")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.totalPnl")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.avgPnl")}</th>
                </tr>
              </thead>
              <tbody>
                {byReason.map((row) => (
                  <tr key={row.reason} className="border-b last:border-0 hover:bg-muted/20">
                    <td className="py-2 px-3">{row.reason}</td>
                    <td className="py-2 px-3 tabular-nums">{row.count}</td>
                    <td className="py-2 px-3 tabular-nums">{(row.win_rate * 100).toFixed(1)}%</td>
                    <td className={cn("py-2 px-3 tabular-nums font-medium", pnlTone(row.total_pnl))}>
                      {formatPnl(row.total_pnl)}
                    </td>
                    <td className={cn("py-2 px-3 tabular-nums", pnlTone(row.avg_pnl))}>
                      {formatPnl(row.avg_pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {trades.length > 0 && (
        <section className="space-y-2">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="text-sm font-medium">{i18n.t("runDetail.tradeLog")}</h3>
            <p className="text-xs text-muted-foreground">{i18n.t("runDetail.tradeLogHint")}</p>
          </div>
          <div className="overflow-auto rounded-md border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/30 text-left text-muted-foreground">
                  <th className="py-2 px-3">{i18n.t("runDetail.time")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.code2")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.side")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.price")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.qty")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.pnl")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.returnPct")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.holdingDays")}</th>
                  <th className="py-2 px-3">{i18n.t("runDetail.reason")}</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((tr, i) => {
                  const pnl = Number(tr.pnl);
                  const ret = Number(tr.return_pct);
                  const hold = Number(tr.holding_days);
                  const isExit = Number.isFinite(hold) ? hold > 0 : Number.isFinite(pnl) && pnl !== 0;
                  const side = String(tr.side || "").toUpperCase();
                  return (
                    <tr key={i} className="border-b last:border-0 hover:bg-muted/20">
                      <td className="py-2 px-3 font-mono text-xs">{tr.time || tr.timestamp}</td>
                      <td className="py-2 px-3">{tr.code}</td>
                      <td className={cn("py-2 px-3 font-medium", side === "BUY" ? "text-success" : "text-danger")}>
                        {side}
                      </td>
                      <td className="py-2 px-3 tabular-nums">{tr.price}</td>
                      <td className="py-2 px-3 tabular-nums">{tr.qty}</td>
                      <td className={cn("py-2 px-3 tabular-nums", isExit ? pnlTone(pnl) : "text-muted-foreground")}>
                        {isExit && Number.isFinite(pnl) ? formatPnl(pnl) : "—"}
                      </td>
                      <td className={cn("py-2 px-3 tabular-nums", isExit ? pnlTone(ret) : "text-muted-foreground")}>
                        {isExit && Number.isFinite(ret) ? `${ret.toFixed(2)}%` : "—"}
                      </td>
                      <td className="py-2 px-3 tabular-nums text-muted-foreground">
                        {isExit && Number.isFinite(hold) ? hold.toFixed(1) : "—"}
                      </td>
                      <td className="py-2 px-3 text-muted-foreground">{tr.reason}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

function AssessmentTab({
  assessment,
  bySymbol,
}: {
  assessment: StrategyAssessment;
  bySymbol: SymbolPnLRow[];
}) {
  const highlights = assessment.highlights || {};
  const topSymbols = bySymbol.slice(0, 8);

  return (
    <div className="p-4 space-y-6">
      <div className="grid gap-3 md:grid-cols-4">
        <RunCardStat
          label={i18n.t("runDetail.fitScore")}
          value={`${assessment.score}/100`}
          tone={assessment.score >= 60 ? "normal" : "warning"}
        />
        <RunCardStat
          label={i18n.t("runDetail.fitGrade")}
          value={i18n.t(`runDetail.grade.${assessment.grade}`, { defaultValue: assessment.grade })}
        />
        <RunCardStat
          label={i18n.t("runDetail.symbolsTraded")}
          value={String(highlights.symbol_count ?? topSymbols.length)}
        />
        <RunCardStat
          label={i18n.t("runDetail.realisedExits")}
          value={String(highlights.exit_count ?? 0)}
        />
      </div>

      <section className="rounded-md border bg-card p-4 space-y-2">
        <h3 className="text-sm font-medium">{i18n.t("runDetail.fitSummary")}</h3>
        <p className="text-sm text-muted-foreground">{assessment.summary}</p>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium">{i18n.t("runDetail.fitChecks")}</h3>
        <div className="space-y-2">
          {assessment.checks.map((check) => (
            <div
              key={check.id}
              className={cn(
                "rounded-md border p-3",
                check.status === "pass" && "border-emerald-500/30 bg-emerald-500/5",
                check.status === "warn" && "border-amber-500/30 bg-amber-500/5",
                check.status === "fail" && "border-destructive/30 bg-destructive/5",
                check.status === "skip" && "bg-muted/20",
              )}
            >
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className={cn("font-medium", checkStatusTone(check.status))}>
                  {i18n.t(`runDetail.checkStatus.${check.status}`, { defaultValue: check.status })}
                </span>
                <span className="font-medium">
                  {i18n.t(`runDetail.checkLabel.${check.id}`, { defaultValue: check.label })}
                </span>
                <span className="ml-auto text-xs tabular-nums text-muted-foreground">{check.score}/100</span>
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{check.detail}</p>
            </div>
          ))}
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <AttributionTradeList
          title={i18n.t("runDetail.topWinners")}
          rows={highlights.top_winners || []}
        />
        <AttributionTradeList
          title={i18n.t("runDetail.topLosers")}
          rows={highlights.top_losers || []}
        />
      </div>

      {(highlights.holding_buckets?.length || topSymbols.length > 0) && (
        <div className="grid gap-4 xl:grid-cols-2">
          {!!highlights.holding_buckets?.length && (
            <section className="space-y-2">
              <h3 className="text-sm font-medium">{i18n.t("runDetail.holdingBuckets")}</h3>
              <div className="overflow-auto rounded-md border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b bg-muted/30 text-left text-muted-foreground">
                      <th className="py-2 px-3">{i18n.t("runDetail.bucket")}</th>
                      <th className="py-2 px-3">{i18n.t("runDetail.tradeCount")}</th>
                      <th className="py-2 px-3">{i18n.t("runDetail.totalPnl")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {highlights.holding_buckets.map((row) => (
                      <tr key={row.bucket} className="border-b last:border-0">
                        <td className="py-2 px-3">
                          {i18n.t(`runDetail.bucketLabel.${row.bucket}`, { defaultValue: row.bucket })}
                        </td>
                        <td className="py-2 px-3 tabular-nums">{row.count}</td>
                        <td className={cn("py-2 px-3 tabular-nums", pnlTone(row.total_pnl))}>
                          {formatPnl(row.total_pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
          {topSymbols.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-sm font-medium">{i18n.t("runDetail.topSymbolsByPnl")}</h3>
              <div className="overflow-auto rounded-md border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b bg-muted/30 text-left text-muted-foreground">
                      <th className="py-2 px-3">{i18n.t("runDetail.code2")}</th>
                      <th className="py-2 px-3">{i18n.t("runDetail.totalPnl")}</th>
                      <th className="py-2 px-3">{i18n.t("runDetail.contribution")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {topSymbols.map((row) => (
                      <tr key={row.code} className="border-b last:border-0">
                        <td className="py-2 px-3 font-mono text-xs">{row.code}</td>
                        <td className={cn("py-2 px-3 tabular-nums", pnlTone(row.total_pnl))}>
                          {formatPnl(row.total_pnl)}
                        </td>
                        <td className="py-2 px-3 tabular-nums">{(row.contribution_pct * 100).toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function AttributionTradeList({
  title,
  rows,
}: {
  title: string;
  rows: Array<Record<string, string | number>>;
}) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-medium">{title}</h3>
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed p-4 text-xs text-muted-foreground">
          {i18n.t("runDetail.noAttributionRows")}
        </p>
      ) : (
        <div className="overflow-auto rounded-md border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/30 text-left text-muted-foreground">
                <th className="py-2 px-3">{i18n.t("runDetail.code2")}</th>
                <th className="py-2 px-3">{i18n.t("runDetail.pnl")}</th>
                <th className="py-2 px-3">{i18n.t("runDetail.returnPct")}</th>
                <th className="py-2 px-3">{i18n.t("runDetail.holdingDays")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => {
                const pnl = Number(row.pnl);
                return (
                  <tr key={`${row.code}-${index}`} className="border-b last:border-0">
                    <td className="py-2 px-3 font-mono text-xs">{String(row.code)}</td>
                    <td className={cn("py-2 px-3 tabular-nums", pnlTone(pnl))}>{formatPnl(pnl)}</td>
                    <td className="py-2 px-3 tabular-nums">{Number(row.return_pct).toFixed(2)}%</td>
                    <td className="py-2 px-3 tabular-nums">{Number(row.holding_days).toFixed(1)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatPnl(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const body = abs >= 1000 ? abs.toLocaleString(undefined, { maximumFractionDigits: 2 }) : abs.toFixed(2);
  if (value > 0) return `+${body}`;
  if (value < 0) return `-${body}`;
  return body;
}

function pnlTone(value: number): string {
  if (!Number.isFinite(value) || value === 0) return "";
  return value > 0 ? "text-success" : "text-danger";
}

function checkStatusTone(status: string): string {
  if (status === "pass") return "text-emerald-700 dark:text-emerald-300";
  if (status === "warn") return "text-amber-700 dark:text-amber-300";
  if (status === "fail") return "text-destructive";
  return "text-muted-foreground";
}

function CodeTab({ code }: { code: Record<string, string> }) {
  const files = Object.entries(code);
  const [active, setActive] = useState(files[0]?.[0] || "");
  if (files.length === 0) return <div className="p-8 text-muted-foreground text-sm">{i18n.t("runDetail.noCodeFiles")}</div>;
  return (
    <div className="flex flex-col h-full">
      <div className="flex gap-1 p-2 border-b">
        {files.map(([name]) => (
          <button key={name} onClick={() => setActive(name)} className={cn("px-3 py-1 rounded text-xs font-mono", active === name ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted")}>{name}</button>
        ))}
      </div>
      <div className="flex-1 overflow-auto p-3 text-[11px] leading-relaxed bg-muted/20 [&_pre]:m-0 [&_pre]:bg-transparent [&_code]:text-[11px]">
        <ReactMarkdown rehypePlugins={rehypePlugins}>
          {`\`\`\`python\n${code[active] || ""}\n\`\`\``}
        </ReactMarkdown>
      </div>
    </div>
  );
}
