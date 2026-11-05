import { useTranslation } from "react-i18next";
import { Loader2, Play, RefreshCw, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { IngestJobLine } from "@/components/data/IngestStatusBar";
import { IngestForm, type IngestBody } from "@/components/data/IngestForm";
import { StatusPill } from "@/components/data/StatusPill";
import {
  formatBytes,
  formatCount,
  type DatasetCard,
  type DatasetDetail,
  type IngestDatasetOption,
  type IngestJob,
} from "@/lib/dataClient";

function shortStamp(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replace("T", " ").slice(0, 16);
}

function isIngestRunning(job: IngestJob | null | undefined): boolean {
  return job?.status === "queued" || job?.status === "running";
}

export function DatasetTable({
  cards,
  selected,
  onSelect,
}: {
  cards: DatasetCard[];
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <table className="w-full text-xs">
        <thead className="bg-muted/40 text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-start font-medium">{t("dataCenter.colName")}</th>
            <th className="px-3 py-2 text-start font-medium">{t("dataCenter.colCategory")}</th>
            <th className="px-3 py-2 text-end font-medium">{t("dataCenter.colRows")}</th>
            <th className="px-3 py-2 text-end font-medium">{t("dataCenter.colSymbols")}</th>
            <th className="px-3 py-2 text-start font-medium">{t("dataCenter.colRange")}</th>
            <th className="px-3 py-2 text-start font-medium">{t("dataCenter.colUpdated")}</th>
            <th className="px-3 py-2 text-start font-medium">{t("dataCenter.colStatus")}</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {cards.map((card) => {
            const health = card.health;
            return (
              <tr
                key={card.name}
                onClick={() => onSelect(card.name)}
                className={cn(
                  "cursor-pointer transition-colors hover:bg-muted/50",
                  selected === card.name && "bg-primary/5",
                )}
              >
                <td className="px-3 py-2">
                  <div className="font-medium text-foreground">{card.label}</div>
                  <div className="font-mono text-[11px] text-muted-foreground">{card.name}</div>
                </td>
                <td className="px-3 py-2 text-muted-foreground">{card.category_label}</td>
                <td className="px-3 py-2 text-end tabular-nums">{formatCount(health.rows)}</td>
                <td className="px-3 py-2 text-end tabular-nums">{formatCount(health.symbols)}</td>
                <td className="px-3 py-2 whitespace-nowrap tabular-nums text-muted-foreground">
                  {health.date_min ?? "—"} → {health.date_max ?? "—"}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">
                  {shortStamp(health.last_write)}
                </td>
                <td className="px-3 py-2">
                  <StatusPill status={health.status} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function DatasetDetailCard({
  detail,
  loading,
  rebuilding,
  onClose,
  onRebuild,
  ingest,
}: {
  detail: DatasetDetail | null;
  loading: boolean;
  rebuilding: boolean;
  onClose: () => void;
  onRebuild: () => void;
  ingest?: {
    option: IngestDatasetOption | undefined;
    job: IngestJob | null;
    starting: boolean;
    onStart: () => void;
    onStartWith?: (body: IngestBody) => void;
    onCancel?: (job: IngestJob) => void;
  };
}) {
  const { t } = useTranslation();
  if (loading && !detail) {
    return (
      <div className="flex items-center gap-2 rounded-lg border bg-card p-6 text-xs text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        {t("dataCenter.loading")}
      </div>
    );
  }
  if (!detail) return null;

  const { spec, health, partitions, quality, schema_drift: drift } = detail;
  const missing = drift.missing ?? [];
  const checks = quality.checks ?? [];

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{spec.label}</span>
          <span className="font-mono text-[11px] text-muted-foreground">{spec.name}</span>
          <StatusPill status={health.status} />
        </div>
        <div className="flex items-center gap-1">
          {ingest?.option?.runnable && (
            <button
              type="button"
              onClick={ingest.onStart}
              disabled={ingest.starting || isIngestRunning(ingest.job)}
              className="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/5 px-2 py-1 text-[11px] text-primary hover:bg-primary/10 disabled:opacity-50"
              title={t("dataCenter.ingestHint")}
            >
              {ingest.starting || isIngestRunning(ingest.job) ? (
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              ) : (
                <Play className="h-3 w-3" aria-hidden />
              )}
              {t("dataCenter.ingest")}
            </button>
          )}
          <button
            type="button"
            onClick={onRebuild}
            disabled={rebuilding}
            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] hover:bg-muted disabled:opacity-50"
          >
            {rebuilding ? (
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
            ) : (
              <RefreshCw className="h-3 w-3" aria-hidden />
            )}
            {t("dataCenter.rebuildOne")}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label={t("dataCenter.close")}
          >
            <X className="h-3.5 w-3.5" aria-hidden />
          </button>
        </div>
      </div>

      <div className="flex flex-col gap-4 p-4 text-xs">
        <p className="text-muted-foreground">{spec.description}</p>

        {ingest && (ingest.option || ingest.job) && (
          <div className="rounded-md border bg-muted/20 p-3">
            {ingest.job ? (
              <IngestJobLine job={ingest.job} onCancel={ingest.onCancel} />
            ) : (
              <p className="text-[11px] text-muted-foreground">
                {ingest.option?.runnable
                  ? t("dataCenter.ingestIdle", { params: ingest.option.params.join(", ") })
                  : t("dataCenter.ingestReadOnly")}
              </p>
            )}
            {ingest.job && ingest.option?.runnable && (
              <p className="mt-1.5 text-[11px] text-muted-foreground">
                {t("dataCenter.ingestAvailable", { params: ingest.option.params.join(", ") })}
              </p>
            )}
            {ingest.option?.runnable && ingest.onStartWith && (
              <IngestForm
                option={ingest.option}
                starting={ingest.starting}
                disabled={isIngestRunning(ingest.job)}
                onSubmit={ingest.onStartWith}
              />
            )}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <div className="text-[11px] text-muted-foreground">{t("dataCenter.colRows")}</div>
            <div className="tabular-nums">{formatCount(health.rows)}</div>
          </div>
          <div>
            <div className="text-[11px] text-muted-foreground">{t("dataCenter.colSize")}</div>
            <div className="tabular-nums">{formatBytes(health.bytes)}</div>
          </div>
          <div>
            <div className="text-[11px] text-muted-foreground">{t("dataCenter.retention")}</div>
            <div>
              {spec.retention_days
                ? t("dataCenter.retentionDays", { days: spec.retention_days })
                : t("dataCenter.retentionKeep")}
            </div>
          </div>
          <div>
            <div className="text-[11px] text-muted-foreground">{t("dataCenter.sla")}</div>
            <div>{t("dataCenter.slaMinutes", { minutes: spec.sla_minutes })}</div>
          </div>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            <h4 className="mb-1.5 text-[11px] font-medium text-muted-foreground">
              {t("dataCenter.schema")}
            </h4>
            <div className="flex flex-wrap gap-1">
              {spec.columns.map((column) => {
                const isMissing = missing.includes(column.name);
                return (
                  <span
                    key={column.name}
                    title={column.type}
                    className={cn(
                      "rounded border px-1.5 py-0.5 font-mono text-[11px]",
                      isMissing
                        ? "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400"
                        : "text-muted-foreground",
                    )}
                  >
                    {column.name}
                  </span>
                );
              })}
            </div>
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              {t("dataCenter.keys", { keys: spec.keys.join(" + ") })}
              {spec.partitions.length > 0 &&
                ` · ${t("dataCenter.partitionsBy", { keys: spec.partitions.join(" / ") })}`}
            </p>
          </div>

          <div>
            <h4 className="mb-1.5 text-[11px] font-medium text-muted-foreground">
              {t("dataCenter.quality")}
            </h4>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <span>{t("dataCenter.dupKeys")}</span>
                <span className={cn("tabular-nums", quality.dup_keys ? "text-rose-600 dark:text-rose-400" : "")}>
                  {quality.dup_keys == null ? "—" : formatCount(quality.dup_keys)}
                </span>
              </div>
              {checks.map((check) => (
                <div key={check.id} className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[11px] text-muted-foreground">{check.id}</span>
                  <span
                    className={cn(
                      "tabular-nums",
                      check.violations ? "text-amber-600 dark:text-amber-400" : "text-muted-foreground",
                    )}
                  >
                    {check.violations == null ? "—" : formatCount(check.violations)}
                  </span>
                </div>
              ))}
              {checks.length === 0 && (
                <p className="text-[11px] text-muted-foreground">{t("dataCenter.noChecks")}</p>
              )}
            </div>
          </div>
        </div>

        <div>
          <h4 className="mb-1.5 text-[11px] font-medium text-muted-foreground">
            {t("dataCenter.partitions")}
          </h4>
          {partitions.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">{t("dataCenter.noData")}</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {partitions.map((partition) => (
                <span
                  key={partition.name}
                  className="rounded border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                >
                  {partition.name}
                  <span className="ms-1.5 tabular-nums text-foreground/70">
                    {partition.rows == null ? "—" : formatCount(partition.rows)}
                  </span>
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
          <span>
            {t("dataCenter.sources")}: {spec.sources.join(" → ")}
          </span>
          <span>
            {t("dataCenter.consumers")}: {spec.consumers.join("、")}
          </span>
        </div>
      </div>
    </div>
  );
}
