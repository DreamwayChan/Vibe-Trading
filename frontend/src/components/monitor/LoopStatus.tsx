import { Loader2, Scale } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  SOAK_SESSIONS,
  costGlance,
  decayFigure,
  formatAbsPct,
  formatMoney,
  formatPctOrDash,
  paperNav,
  unrealizedPnl,
  type CostCompareReport,
  type CostMetricRow,
  type PaperBookSnapshot,
} from "@/lib/loopStatus";

function metricCell(row: CostMetricRow | undefined, key: keyof CostMetricRow, kind: "pct" | "money"): string {
  const value = row?.[key];
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return kind === "money" ? formatMoney(value) : formatPctOrDash(value);
}

export function LoopStatus({
  runId,
  cost,
  costError,
  costLoading,
  comparing,
  onCompare,
  paper,
  paperError,
  tradable,
}: {
  runId: string;
  cost: CostCompareReport | null;
  costError: string | null;
  costLoading: boolean;
  comparing: boolean;
  onCompare: () => void;
  paper: PaperBookSnapshot | null;
  paperError: string | null;
  tradable: boolean | null;
}) {
  const { t } = useTranslation();
  const bound = Boolean(runId.trim());
  const metrics = cost?.metrics;
  const glance = costGlance(metrics?.delta?.total_return);
  const glanceText =
    glance?.kind === "ate"
      ? t("monitor.costAte", { pct: glance.pct })
      : glance?.kind === "ahead"
        ? t("monitor.costAhead", { pct: glance.pct })
        : glance?.kind === "flat"
          ? t("monitor.costFlat")
          : null;

  const window = paper?.window ?? null;
  const nav = paperNav(paper);
  const unrealized = unrealizedPnl(paper?.state);
  const decay = decayFigure(window?.decay);
  const tradableText =
    tradable === true
      ? t("monitor.paperTradableYes")
      : tradable === false
        ? t("monitor.paperTradableNo")
        : t("monitor.paperTradableUnknown");
  const soakText = window
    ? t("monitor.paperSoakGlance", {
        day: window.session_count ?? 0,
        total: SOAK_SESSIONS,
        deviation:
          typeof window.return_deviation === "number" && Number.isFinite(window.return_deviation)
            ? formatAbsPct(window.return_deviation)
            : "—",
        decay: decay == null ? t("monitor.paperSoakShort") : decay.toFixed(2),
        tradable: tradableText,
      })
    : null;

  const rows: Array<{ label: string; key: keyof CostMetricRow; kind: "pct" | "money" }> = [
    { label: t("monitor.costMetricReturn"), key: "total_return", kind: "pct" },
    { label: t("monitor.costMetricNav"), key: "nav", kind: "money" },
    { label: t("monitor.costMetricDrawdown"), key: "max_drawdown", kind: "pct" },
  ];

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <section
        data-testid="cost-compare-panel"
        className="rounded-lg border p-4 flex flex-col gap-3"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold inline-flex items-center gap-2">
              <Scale className="h-4 w-4" />
              {t("monitor.costCompareTitle")}
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">{t("monitor.costCompareHint")}</p>
          </div>
          <button
            type="button"
            onClick={onCompare}
            disabled={!bound || comparing || costLoading}
            className="inline-flex shrink-0 items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
          >
            {comparing ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {comparing ? t("monitor.costCompareRunning") : t("monitor.costCompareRun")}
          </button>
        </div>
        {costLoading && (
          <p className="text-xs text-muted-foreground inline-flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {t("monitor.loading")}
          </p>
        )}
        {costError && <p className="text-sm text-destructive">{costError}</p>}
        {glanceText && (
          <p data-testid="cost-compare-glance" className="text-sm font-medium">
            {glanceText}
          </p>
        )}
        {metrics ? (
          <div className="border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/50 text-muted-foreground text-left">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("monitor.costColMetric")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.costZero")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.costAware")}</th>
                  <th className="px-3 py-2 font-medium">{t("monitor.costDelta")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.key} className="border-t">
                    <td className="px-3 py-2 text-muted-foreground">{row.label}</td>
                    <td className="px-3 py-2 font-mono tabular-nums">
                      {metricCell(metrics.frictionless, row.key, row.kind)}
                    </td>
                    <td className="px-3 py-2 font-mono tabular-nums">
                      {metricCell(metrics.ashare_costs, row.key, row.kind)}
                    </td>
                    <td className="px-3 py-2 font-mono tabular-nums">
                      {metricCell(metrics.delta, row.key, row.kind)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !costLoading && <p className="text-sm text-muted-foreground">{t("monitor.costCompareEmpty")}</p>
        )}
      </section>

      <section
        data-testid="paper-book-panel"
        className="rounded-lg border p-4 flex flex-col gap-3"
      >
        <div>
          <h2 className="text-lg font-semibold">{t("monitor.paperBookTitle")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("monitor.paperBookHint")}</p>
        </div>
        {paperError && <p className="text-sm text-destructive">{paperError}</p>}
        {soakText ? (
          <p data-testid="paper-book-glance" className="text-sm font-medium">
            {soakText}
          </p>
        ) : (
          <p className="text-sm text-muted-foreground">{t("monitor.paperBookEmpty")}</p>
        )}
        {window?.decay?.decayed && (
          <p className="text-xs text-amber-700 dark:text-amber-400">{t("monitor.reasonPaperBookDecay")}</p>
        )}
        {(nav != null || unrealized != null || window) && (
          <dl className="grid grid-cols-2 gap-2 text-sm">
            <div className="rounded-md border px-3 py-2">
              <dt className="text-xs text-muted-foreground">{t("monitor.paperNav")}</dt>
              <dd className="font-mono tabular-nums mt-0.5">{formatMoney(nav)}</dd>
            </div>
            <div className="rounded-md border px-3 py-2" title={t("monitor.paperUnrealizedHint")}>
              <dt className="text-xs text-muted-foreground">{t("monitor.paperUnrealized")}</dt>
              <dd className="font-mono tabular-nums mt-0.5">{formatMoney(unrealized)}</dd>
            </div>
            <div className="rounded-md border px-3 py-2">
              <dt className="text-xs text-muted-foreground">{t("monitor.paperDeviation")}</dt>
              <dd className="font-mono tabular-nums mt-0.5">
                {typeof window?.return_deviation === "number" && Number.isFinite(window.return_deviation)
                  ? formatAbsPct(window.return_deviation)
                  : "—"}
              </dd>
            </div>
            <div className="rounded-md border px-3 py-2">
              <dt className="text-xs text-muted-foreground">{t("monitor.paperDecay")}</dt>
              <dd className="font-mono tabular-nums mt-0.5">
                {decay == null ? "—" : decay.toFixed(2)}
              </dd>
            </div>
          </dl>
        )}
      </section>
    </div>
  );
}
