import { useTranslation } from "react-i18next";
import { AlertTriangle, Database, FileWarning, Info, Layers, Rows3, Wallet } from "lucide-react";
import { cn } from "@/lib/utils";
import { StatusPill } from "@/components/data/StatusPill";
import { formatBytes, formatCount, type DataOverview, type Freshness } from "@/lib/dataClient";

/** Category order and labels. Literal keys so i18n stays type-checked. */
const CATEGORIES = [
  { key: "bars", labelKey: "dataCenter.categoryBars" },
  { key: "market", labelKey: "dataCenter.categoryMarket" },
  { key: "sector", labelKey: "dataCenter.categorySector" },
  { key: "capital", labelKey: "dataCenter.categoryCapital" },
  { key: "reference", labelKey: "dataCenter.categoryReference" },
  { key: "derived", labelKey: "dataCenter.categoryDerived" },
] as const;

function Kpi({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Database;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border bg-card px-4 py-3">
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Icon className="h-3.5 w-3.5" aria-hidden />
        {label}
      </span>
      <span className="text-xl font-medium tabular-nums">{value}</span>
      {hint && <span className="text-[11px] text-muted-foreground/70">{hint}</span>}
    </div>
  );
}

function FreshnessRow({ item, onSelect }: { item: Freshness; onSelect: (name: string) => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={() => onSelect(item.name)}
      className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-start text-xs transition-colors hover:bg-muted/60"
    >
      <span className="min-w-0 flex-1 truncate">{item.label}</span>
      <span className="shrink-0 tabular-nums text-muted-foreground">
        {item.date_max ?? t("dataCenter.noData")}
      </span>
      <span className="w-16 shrink-0 text-end tabular-nums text-muted-foreground">
        {formatCount(item.rows)}
      </span>
      <StatusPill status={item.status} />
    </button>
  );
}

export function OverviewPanel({
  overview,
  onSelectDataset,
}: {
  overview: DataOverview | null;
  onSelectDataset: (name: string) => void;
}) {
  const { t } = useTranslation();
  if (!overview) return null;

  const byCategory = new Map<string, Freshness[]>();
  for (const item of overview.freshness) {
    const bucket = byCategory.get(item.category) ?? [];
    bucket.push(item);
    byCategory.set(item.category, bucket);
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi
          icon={Database}
          label={t("dataCenter.kpiDatasets")}
          value={`${overview.counts.with_data}/${overview.counts.declared}`}
          hint={t("dataCenter.kpiDatasetsHint", { empty: overview.counts.empty })}
        />
        <Kpi
          icon={Rows3}
          label={t("dataCenter.kpiRows")}
          value={formatCount(overview.counts.total_rows)}
          hint={t("dataCenter.kpiRowsHint")}
        />
        <Kpi
          icon={Layers}
          label={t("dataCenter.kpiLake")}
          value={formatBytes(overview.lake.bytes)}
          hint={t("dataCenter.kpiLakeHint", { files: overview.lake.files })}
        />
        <Kpi
          icon={FileWarning}
          label={t("dataCenter.kpiAlerts")}
          value={String(overview.alerts.length)}
          hint={t("dataCenter.kpiAlertsHint")}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="flex flex-col gap-3 lg:col-span-2">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">{t("dataCenter.freshness")}</h2>
            <span className="text-[11px] text-muted-foreground">
              {t("dataCenter.freshnessHint", { date: overview.as_of.date })}
            </span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            {CATEGORIES.filter((category) => byCategory.has(category.key)).map((category) => (
              <div key={category.key} className="rounded-lg border bg-card p-2">
                <div className="flex items-center justify-between px-2 pb-1">
                  <span className="text-xs font-medium">{t(category.labelKey)}</span>
                  <span className="text-[11px] text-muted-foreground">
                    {(byCategory.get(category.key) ?? []).length}
                  </span>
                </div>
                {(byCategory.get(category.key) ?? []).map((item) => (
                  <FreshnessRow key={item.name} item={item} onSelect={onSelectDataset} />
                ))}
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <div className="rounded-lg border bg-card p-3">
            <h3 className="mb-2 flex items-center gap-1.5 text-xs font-medium">
              <Wallet className="h-3.5 w-3.5" aria-hidden />
              {t("dataCenter.quota")}
            </h3>
            {overview.quota.tokens === 0 || overview.quota.endpoints.length === 0 ? (
              <p className="text-[11px] text-muted-foreground">
                {overview.quota.note || t("dataCenter.quotaEmpty")}
              </p>
            ) : (
              <div className="space-y-1.5">
                <p className="text-[11px] text-muted-foreground">
                  {t("dataCenter.quotaTokens", { tokens: overview.quota.tokens })}
                </p>
                {overview.quota.endpoints.map((endpoint) => {
                  const total = Math.max(1, endpoint.per_token * endpoint.tokens);
                  const used = Math.max(0, total - endpoint.remaining);
                  const ratio = Math.min(1, used / total);
                  return (
                    <div key={endpoint.api} className="space-y-1">
                      <div className="flex items-center justify-between text-[11px]">
                        <span className="font-mono">{endpoint.api}</span>
                        <span className="tabular-nums text-muted-foreground">
                          {endpoint.remaining}/{total}
                        </span>
                      </div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                        <div
                          className={cn("h-full rounded-full", ratio > 0.8 ? "bg-rose-500" : "bg-primary")}
                          style={{ width: `${Math.round(ratio * 100)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="rounded-lg border bg-card p-3 text-[11px] text-muted-foreground">
            <div className="flex items-center justify-between">
              <span>{t("dataCenter.calendar")}</span>
              <span className="text-foreground">
                {overview.calendar.available
                  ? t("dataCenter.calendarReady", { source: overview.calendar.source })
                  : t("dataCenter.calendarMissing")}
              </span>
            </div>
            <div className="mt-1.5 flex items-center justify-between gap-2">
              <span className="shrink-0">{t("dataCenter.lakeRoot")}</span>
              <span className="truncate font-mono text-foreground" title={overview.lake.root}>
                {overview.lake.root}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3">
        <h3 className="mb-1 flex items-center gap-1.5 text-xs font-medium">
          <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
          {t("dataCenter.alerts")}
        </h3>
        {overview.alerts.length === 0 ? (
          <p className="py-2 text-[11px] text-muted-foreground">{t("dataCenter.noAlerts")}</p>
        ) : (
          <ul className="divide-y">
            {overview.alerts.slice(0, 12).map((alert, index) => (
              <li key={`${alert.dataset}-${alert.code}-${index}`}>
                <button
                  type="button"
                  onClick={() => onSelectDataset(alert.dataset)}
                  className="flex w-full items-start gap-2 px-1 py-1.5 text-start text-xs hover:bg-muted/60"
                >
                  {alert.severity === "warning" ? (
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" aria-hidden />
                  ) : (
                    <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
                  )}
                  <span className="min-w-0 flex-1">{alert.message}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
