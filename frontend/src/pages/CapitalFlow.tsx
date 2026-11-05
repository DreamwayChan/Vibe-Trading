import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { AlertTriangle, Info, Loader2, RefreshCw } from "lucide-react";
import { EtfOiChart } from "@/components/capital/EtfOiChart";
import { ApiError, api, type CapitalEtfOiReport, type CapitalFlowRegime, type CapitalMcapMode } from "@/lib/api";
import { cn } from "@/lib/utils";

const WINDOWS = [60, 120, 250] as const;

function isNum(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** 亿元, with an explicit sign for flows. */
function fmtYi(value: number | null | undefined, digits = 1, signed = false): string {
  if (!isNum(value)) return "—";
  const scaled = value / 1e8;
  const body = Math.abs(scaled).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  if (signed) return `${scaled >= 0 ? "+" : "−"}${body}`;
  return `${scaled < 0 ? "−" : ""}${body}`;
}

function fmtPct(value: number | null | undefined, digits = 3): string {
  if (!isNum(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

function fmtBp(value: number | null | undefined, digits = 1): string {
  if (!isNum(value)) return "—";
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}bp`;
}

function fmtShares(value: number | null | undefined): string {
  if (!isNum(value)) return "—";
  return (value / 1e8).toFixed(2);
}

function regimeTone(regime: CapitalFlowRegime): string {
  // Chinese convention: money coming in is red, going out is green.
  if (regime === "surge") return "text-rose-600 dark:text-rose-400";
  if (regime === "inflow") return "text-rose-500 dark:text-rose-400";
  if (regime === "outflow") return "text-emerald-500 dark:text-emerald-400";
  if (regime === "drain") return "text-emerald-600 dark:text-emerald-400";
  return "text-muted-foreground";
}

function regimeLabel(regime: CapitalFlowRegime, t: TFunction): string {
  switch (regime) {
    case "surge":
      return t("capitalFlow.regimeSurge");
    case "inflow":
      return t("capitalFlow.regimeInflow");
    case "outflow":
      return t("capitalFlow.regimeOutflow");
    case "drain":
      return t("capitalFlow.regimeDrain");
    default:
      return t("capitalFlow.regimeNeutral");
  }
}

function flowTone(value: number | null | undefined): string {
  if (!isNum(value) || value === 0) return "text-muted-foreground";
  return value > 0 ? "text-rose-600 dark:text-rose-400" : "text-emerald-600 dark:text-emerald-400";
}

function Kpi({
  label,
  value,
  unit,
  hint,
  tone,
  testId,
}: {
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  tone?: string;
  testId?: string;
}) {
  return (
    <div className="rounded-lg border bg-card p-3.5" data-testid={testId}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 flex items-baseline gap-1 font-mono text-xl font-semibold", tone)}>
        <span>{value}</span>
        {unit ? <span className="text-xs font-normal text-muted-foreground">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div> : null}
    </div>
  );
}

export function CapitalFlow() {
  const { t } = useTranslation();
  const [windowDays, setWindowDays] = useState<number>(120);
  const [mcapMode, setMcapMode] = useState<CapitalMcapMode>("circ_mv");
  const [showIndex, setShowIndex] = useState(true);
  const [report, setReport] = useState<CapitalEtfOiReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showMethod, setShowMethod] = useState(false);

  const load = useCallback(
    async (force: boolean) => {
      if (force) setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        const data = await api.getEtfOi({ window: windowDays, mcap_mode: mcapMode, refresh: force });
        setReport(data);
      } catch (err) {
        const message = err instanceof ApiError ? err.message : String(err);
        setError(message);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [mcapMode, windowDays],
  );

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.getEtfOi({ window: windowDays, mcap_mode: mcapMode });
        if (alive) setReport(data);
      } catch (err) {
        if (alive) setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [mcapMode, windowDays]);

  const stats = report?.stats;
  const lastPoint = report?.points.length ? report.points[report.points.length - 1] : undefined;
  const prevPoint = report && report.points.length > 1 ? report.points[report.points.length - 2] : undefined;
  const indexChangePct =
    isNum(lastPoint?.index_close) && isNum(prevPoint?.index_close) && prevPoint?.index_close
      ? (lastPoint!.index_close! / prevPoint!.index_close! - 1) * 100
      : null;
  const methodology = report?.methodology;
  const caveats = methodology?.caveats || [];
  const formulas = methodology?.formulas || {};

  const streakText = useMemo(() => {
    if (!stats?.inflow_streak) return t("capitalFlow.streakNone");
    const days = Math.abs(stats.inflow_streak);
    return stats.inflow_streak > 0
      ? t("capitalFlow.streakIn", { days })
      : t("capitalFlow.streakOut", { days });
  }, [stats?.inflow_streak, t]);

  const scoreWidth = isNum(stats?.score) ? Math.max(2, Math.min(100, stats?.score ?? 0)) : 0;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5 p-6">
      {/* ---------------------------------------------------------------- header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{t("capitalFlow.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted-foreground">
            {t("capitalFlow.subtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex overflow-hidden rounded-md border text-xs">
            {WINDOWS.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setWindowDays(option)}
                className={cn(
                  "px-2.5 py-1.5 transition-colors",
                  windowDays === option ? "bg-primary text-primary-foreground" : "hover:bg-muted",
                )}
              >
                {t("capitalFlow.windowDays", { days: option })}
              </button>
            ))}
          </div>
          <select
            value={mcapMode}
            onChange={(event) => setMcapMode(event.target.value as CapitalMcapMode)}
            className="rounded-md border bg-background px-2 py-1.5 text-xs"
            title={t("capitalFlow.mcapModeHint")}
            aria-label={t("capitalFlow.mcapModeLabel")}
          >
            <option value="circ_mv">{t("capitalFlow.mcapCirc")}</option>
            <option value="weight_implied">{t("capitalFlow.mcapImplied")}</option>
          </select>
          <label className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1.5 text-xs">
            <input
              type="checkbox"
              checked={showIndex}
              onChange={(event) => setShowIndex(event.target.checked)}
              className="h-3 w-3"
            />
            {t("capitalFlow.chartIndex")}
          </label>
          <button
            type="button"
            onClick={() => load(true)}
            disabled={loading || refreshing}
            className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
          >
            {refreshing ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {refreshing ? t("capitalFlow.refreshing") : t("capitalFlow.refresh")}
          </button>
        </div>
      </div>

      {report ? (
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
          <span>
            {t("capitalFlow.asOf")} <span className="font-mono">{report.as_of || "—"}</span>
          </span>
          <span>
            {t("capitalFlow.fundCount", { count: report.universe_size })} ·{" "}
            {t("capitalFlow.weightsDate", { date: report.mcap.weights_date || "—" })}
          </span>
          {report.cached ? <span>{t("capitalFlow.cachedHint")}</span> : null}
          {report.provisional ? (
            <span className="inline-flex items-center gap-1 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-amber-700 dark:text-amber-400">
              <AlertTriangle className="h-3 w-3" />
              {t("capitalFlow.pendingHint")}
            </span>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {t("capitalFlow.loadFailed")}
          <span className="ml-1 font-mono text-xs opacity-80">{error}</span>
        </div>
      ) : null}

      {loading && !report ? (
        <div className="flex items-center gap-2 rounded-lg border p-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("capitalFlow.loadingFirst")}
        </div>
      ) : null}

      {report ? (
        <>
          {/* ------------------------------------------------------------- KPIs */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <Kpi
              testId="kpi-oi"
              label={t("capitalFlow.kpiOi")}
              value={fmtPct(stats?.oi_pct)}
              hint={[
                isNum(stats?.oi_change_bp)
                  ? `${t("capitalFlow.vsPrevDay")} ${fmtBp(stats?.oi_change_bp)}`
                  : null,
                isNum(stats?.oi_percentile)
                  ? `${t("capitalFlow.oiPercentile")} ${stats?.oi_percentile!.toFixed(0)}%`
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
              tone={flowTone(stats?.oi_change_bp)}
            />
            <Kpi
              testId="kpi-aum"
              label={t("capitalFlow.kpiAum")}
              value={fmtYi(stats?.aum, 0)}
              unit={t("capitalFlow.unitYi")}
              hint={
                isNum(stats?.aum_change)
                  ? `${t("capitalFlow.aumChange")} ${fmtYi(stats?.aum_change, 1, true)}`
                  : undefined
              }
              tone={flowTone(stats?.aum_change)}
            />
            <Kpi
              testId="kpi-flow"
              label={t("capitalFlow.kpiFlow")}
              value={isNum(stats?.net_flow) ? fmtYi(stats?.net_flow, 1, true) : t("capitalFlow.pendingBadge")}
              unit={isNum(stats?.net_flow) ? t("capitalFlow.unitYi") : undefined}
              hint={
                isNum(lastPoint?.nav_pnl)
                  ? `${t("capitalFlow.navPnl")} ${fmtYi(lastPoint?.nav_pnl, 1, true)}`
                  : undefined
              }
              tone={flowTone(stats?.net_flow)}
            />
            <Kpi
              testId="kpi-flow20"
              label={t("capitalFlow.kpiFlow20")}
              value={fmtYi(stats?.flow_20d, 1, true)}
              unit={t("capitalFlow.unitYi")}
              hint={
                isNum(stats?.flow_20d_bp)
                  ? `${t("capitalFlow.flow20Bp")} ${fmtBp(stats?.flow_20d_bp)}`
                  : undefined
              }
              tone={flowTone(stats?.flow_20d)}
            />
            <Kpi
              testId="kpi-index"
              label={t("capitalFlow.kpiIndex")}
              value={isNum(lastPoint?.index_close) ? lastPoint!.index_close!.toFixed(2) : "—"}
              hint={
                isNum(indexChangePct)
                  ? `${t("capitalFlow.indexChange")} ${indexChangePct! >= 0 ? "+" : "−"}${Math.abs(
                      indexChangePct!,
                    ).toFixed(2)}%`
                  : undefined
              }
              tone={flowTone(indexChangePct)}
            />
          </div>

          {/* --------------------------------------------------------- chart + state */}
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_260px]">
            <div className="rounded-lg border p-3">
              <div className="mb-1 flex items-baseline justify-between">
                <h2 className="text-sm font-semibold">{t("capitalFlow.chartTitle")}</h2>
                <span className="text-[11px] text-muted-foreground">{t("capitalFlow.chartHint")}</span>
              </div>
              <EtfOiChart points={report.points} showIndex={showIndex} height={420} />
            </div>

            <div className="flex flex-col gap-3 rounded-lg border p-4" data-testid="state-card">
              <div>
                <div className="text-xs text-muted-foreground">{t("capitalFlow.stateTitle")}</div>
                <div className={cn("mt-1 text-2xl font-semibold", regimeTone(stats?.regime ?? "neutral"))}>
                  {regimeLabel(stats?.regime ?? "neutral", t)}
                </div>
              </div>

              <div>
                <div className="flex items-baseline justify-between text-[11px] text-muted-foreground">
                  <span>{t("capitalFlow.stateScore")}</span>
                  <span className="font-mono text-sm text-foreground">
                    {isNum(stats?.score) ? stats?.score!.toFixed(0) : "—"}
                  </span>
                </div>
                <div className="mt-1 h-2 overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn(
                      "h-full rounded-full transition-all",
                      (stats?.score ?? 50) >= 58
                        ? "bg-rose-500"
                        : (stats?.score ?? 50) <= 42
                          ? "bg-emerald-500"
                          : "bg-slate-400",
                    )}
                    style={{ width: `${scoreWidth}%` }}
                  />
                </div>
                <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
                  <span>{t("capitalFlow.scaleOut")}</span>
                  <span>{t("capitalFlow.scaleIn")}</span>
                </div>
              </div>

              <dl className="space-y-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <dt className="text-muted-foreground">{t("capitalFlow.streakLabel")}</dt>
                  <dd className="font-mono">{streakText}</dd>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <dt className="text-muted-foreground">{t("capitalFlow.zLabel")}</dt>
                  <dd className="font-mono">
                    {isNum(stats?.flow_z) ? stats?.flow_z!.toFixed(2) : "—"}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <dt className="text-muted-foreground">{t("capitalFlow.flow5Label")}</dt>
                  <dd className={cn("font-mono", flowTone(stats?.flow_5d))}>
                    {fmtYi(stats?.flow_5d, 1, true)}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <dt className="text-muted-foreground">{t("capitalFlow.flowWindowLabel")}</dt>
                  <dd className={cn("font-mono", flowTone(stats?.flow_window))}>
                    {fmtYi(stats?.flow_window, 0, true)}
                  </dd>
                </div>
              </dl>

              <p className="mt-auto rounded-md bg-muted/60 px-2 py-2 text-[10px] leading-relaxed text-muted-foreground">
                {t("capitalFlow.stateNote")}
              </p>
            </div>
          </div>

          {/* ------------------------------------------------------------- funds */}
          <div className="rounded-lg border">
            <div className="flex items-baseline justify-between border-b px-3 py-2">
              <h2 className="text-sm font-semibold">{t("capitalFlow.tableTitle")}</h2>
              <span className="text-[11px] text-muted-foreground">{t("capitalFlow.tableHint")}</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-muted/40 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">{t("capitalFlow.colFund")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("capitalFlow.colShares")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("capitalFlow.colSharesChange")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("capitalFlow.colNav")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("capitalFlow.colAum")}</th>
                    <th className="px-3 py-2 text-left font-medium">{t("capitalFlow.colWeight")}</th>
                    <th className="px-3 py-2 text-right font-medium">{t("capitalFlow.colFlow")}</th>
                  </tr>
                </thead>
                <tbody>
                  {report.funds.map((fund) => (
                    <tr key={fund.code} className="border-t">
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-muted-foreground">{fund.code}</span>
                          <span>{fund.name}</span>
                          {fund.stale ? (
                            <span className="rounded border border-amber-500/40 bg-amber-500/10 px-1 text-[10px] text-amber-700 dark:text-amber-400">
                              {t("capitalFlow.pendingBadge")}
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td className="px-3 py-2 text-right font-mono">{fmtShares(fund.shares)}</td>
                      <td
                        className={cn(
                          "px-3 py-2 text-right font-mono",
                          flowTone(fund.shares_change),
                        )}
                      >
                        {isNum(fund.shares_change)
                          ? `${fund.shares_change >= 0 ? "+" : "−"}${(
                              Math.abs(fund.shares_change) / 1e8
                            ).toFixed(3)}`
                          : "—"}
                      </td>
                      <td className="px-3 py-2 text-right font-mono">
                        {isNum(fund.nav) ? fund.nav!.toFixed(4) : "—"}
                      </td>
                      <td className="px-3 py-2 text-right font-mono">{fmtYi(fund.aum, 1)}</td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-1.5">
                          <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-sky-500/70"
                              style={{ width: `${Math.min(100, (fund.group_weight ?? 0) * 100)}%` }}
                            />
                          </div>
                          <span className="font-mono text-muted-foreground">
                            {isNum(fund.group_weight)
                              ? `${(fund.group_weight! * 100).toFixed(1)}%`
                              : "—"}
                          </span>
                        </div>
                      </td>
                      <td className={cn("px-3 py-2 text-right font-mono", flowTone(fund.net_flow))}>
                        {isNum(fund.net_flow) ? fmtYi(fund.net_flow, 1, true) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="border-t px-3 py-1.5 text-[10px] text-muted-foreground">
              {t("capitalFlow.tableUnit")}
            </div>
          </div>

          {/* --------------------------------------------------------- methodology */}
          <div className="rounded-lg border">
            <button
              type="button"
              onClick={() => setShowMethod((open) => !open)}
              className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-semibold hover:bg-muted/40"
            >
              <span className="inline-flex items-center gap-1.5">
                <Info className="h-3.5 w-3.5 text-muted-foreground" />
                {t("capitalFlow.methodTitle")}
              </span>
              <span className="text-xs font-normal text-muted-foreground">
                {showMethod ? t("capitalFlow.methodHide") : t("capitalFlow.methodShow")}
              </span>
            </button>
            {showMethod ? (
              <div className="space-y-3 border-t px-3 py-3 text-xs leading-relaxed">
                <div>
                  <div className="mb-1 font-medium">{t("capitalFlow.methodFormulas")}</div>
                  <ul className="space-y-1 text-muted-foreground">
                    <li>
                      <span className="text-foreground">ETF OI</span> = {formulas.oi || "—"}
                    </li>
                    <li>
                      <span className="text-foreground">{t("capitalFlow.kpiAum")}</span> = {formulas.aum || "—"}
                    </li>
                    <li>
                      <span className="text-foreground">{t("capitalFlow.netFlowShort")}</span> ={" "}
                      {formulas.net_flow || "—"}
                    </li>
                    <li>
                      <span className="text-foreground">{t("capitalFlow.navPnlShort")}</span> ={" "}
                      {formulas.nav_pnl || "—"}
                    </li>
                    <li>
                      <span className="text-foreground">{t("capitalFlow.denominatorLabel")}</span> ={" "}
                      {formulas.mcap || "—"}
                    </li>
                  </ul>
                </div>

                <div>
                  <div className="mb-1 font-medium">{t("capitalFlow.methodDenominator")}</div>
                  <p className="text-muted-foreground">
                    {t("capitalFlow.denominatorDetail", {
                      mode: report.mcap.mode === "circ_mv" ? t("capitalFlow.mcapCirc") : t("capitalFlow.mcapImplied"),
                      total: fmtYi(report.mcap.total, 2),
                      index: isNum(report.mcap.index_close) ? report.mcap.index_close!.toFixed(2) : "—",
                    })}
                  </p>
                </div>

                <div>
                  <div className="mb-1 font-medium">{t("capitalFlow.methodCaveats")}</div>
                  <ul className="list-disc space-y-1 pl-4 text-muted-foreground">
                    {caveats.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>

                {report.warnings.length ? (
                  <div>
                    <div className="mb-1 font-medium">{t("capitalFlow.methodWarnings")}</div>
                    <ul className="list-disc space-y-1 pl-4 text-amber-700 dark:text-amber-400">
                      {report.warnings.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                <div>
                  <div className="mb-1 font-medium">{t("capitalFlow.methodSources")}</div>
                  <ul className="flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
                    {(methodology?.sources || []).map((source) => (
                      <li key={source.name}>
                        {source.url ? (
                          <a
                            className="underline decoration-dotted hover:text-foreground"
                            href={source.url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {source.name}
                          </a>
                        ) : (
                          source.name
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
