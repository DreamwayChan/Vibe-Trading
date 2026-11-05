/**
 * Ingest status surfaces.
 *
 * The data center can start a backfill, and a backfill outlives the click, so
 * the page needs two things text alone cannot give: a visible progress bar
 * while a job runs (otherwise the button looks broken for minutes) and an
 * outcome line afterwards (otherwise "did it work?" is answered by re-reading a
 * five-year chart and guessing).
 */

import { useTranslation } from "react-i18next";
import { AlertTriangle, Ban, CheckCircle2, Loader2, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import type { IngestJob } from "@/lib/dataClient";

const STATUS_STYLE: Record<string, string> = {
  queued: "text-muted-foreground",
  running: "text-primary",
  done: "text-emerald-600 dark:text-emerald-400",
  failed: "text-destructive",
  cancelled: "text-amber-600 dark:text-amber-400",
};

// Literal map, not a template key: the i18n bundle is typed, so a computed key
// would not typecheck (`dataCenter.ingestQueued` etc. must exist as literals).
const STATUS_KEY = {
  queued: "dataCenter.ingestQueued",
  running: "dataCenter.ingestRunning",
  done: "dataCenter.ingestDone",
  failed: "dataCenter.ingestFailed",
  cancelled: "dataCenter.ingestCancelled",
} as const;

export function IngestProgress({ value }: { value: number }) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)));
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
      role="progressbar"
      aria-valuenow={percent}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-primary transition-[width] duration-300"
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

/** One job as a compact line + bar; used in the drawer and the page banner. */
export function IngestJobLine({
  job,
  compact = false,
  onCancel,
}: {
  job: IngestJob;
  compact?: boolean;
  onCancel?: (job: IngestJob) => void;
}) {
  const { t } = useTranslation();
  const report = job.report;
  const running = job.status === "queued" || job.status === "running";
  const cursor = job.cursor;
  const processed = cursor?.done ?? job.done;
  const totalSymbols = cursor?.total ?? job.total;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        {running ? (
          <Loader2 className="h-3 w-3 animate-spin text-primary" aria-hidden />
        ) : job.status === "failed" ? (
          <AlertTriangle className="h-3 w-3 text-destructive" aria-hidden />
        ) : job.status === "cancelled" ? (
          <Ban className="h-3 w-3 text-amber-600 dark:text-amber-400" aria-hidden />
        ) : (
          <CheckCircle2 className="h-3 w-3 text-emerald-600 dark:text-emerald-400" aria-hidden />
        )}
        <span className={cn("font-medium", STATUS_STYLE[job.status])}>
          {t(STATUS_KEY[job.status] ?? STATUS_KEY.queued)}
        </span>
        {!compact && <span className="font-mono text-muted-foreground">{job.dataset}</span>}
        <span className="text-muted-foreground">
          {job.message || t("dataCenter.ingestQueued")}
        </span>
        {totalSymbols ? (
          <span className="tabular-nums text-muted-foreground">
            {t("dataCenter.ingestCursor", {
              done: processed ?? 0,
              total: totalSymbols,
            })}
          </span>
        ) : null}
        {job.seconds != null && (
          <span className="ms-auto tabular-nums text-muted-foreground">
            {job.seconds.toFixed(1)}s
          </span>
        )}
        {running && onCancel && (
          <button
            type="button"
            onClick={() => onCancel(job)}
            className="rounded border px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:border-destructive/40 hover:text-destructive"
            title={t("dataCenter.ingestCancelHint")}
          >
            {t("dataCenter.ingestCancel")}
          </button>
        )}
      </div>

      {running && <IngestProgress value={job.progress} />}

      {report && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span>
            {t("dataCenter.ingestResult", {
              rows: report.rows,
              sessions: report.sessions,
              symbols: report.symbols,
            })}
          </span>
          {report.source && (
            <span>{t("dataCenter.ingestSource", { source: report.source })}</span>
          )}
          {report.start && (
            <span className="tabular-nums">
              {report.start} → {report.end ?? "—"}
            </span>
          )}
        </div>
      )}

      {job.error && (
        <p className="text-[11px] text-destructive">
          {t("dataCenter.ingestJobFailed", { error: job.error })}
        </p>
      )}

      {!compact && report?.notes?.length ? (
        <ul className="space-y-0.5 text-[11px] text-muted-foreground">
          {report.notes.slice(0, 4).map((note) => (
            <li key={note}>· {note}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Page-level banner: the running job (if any) plus the last finished one. */
export function IngestStatusBar({
  activeJobs,
  lastJob,
  onOpenDataset,
  onCancel,
}: {
  activeJobs: IngestJob[];
  lastJob: IngestJob | null;
  onOpenDataset: (name: string) => void;
  onCancel?: (job: IngestJob) => void;
}) {
  const { t } = useTranslation();
  const job = activeJobs[0] ?? lastJob;
  if (!job) return null;
  const running = job.status === "queued" || job.status === "running";

  return (
    <div
      className={cn(
        "rounded-md border px-3 py-2",
        running ? "border-primary/30 bg-primary/5" : "bg-card",
      )}
    >
      <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Zap className="h-3 w-3" aria-hidden />
        {running
          ? t("dataCenter.ingestRunningHint")
          : t("dataCenter.ingestLastHint")}
        <button
          type="button"
          onClick={() => onOpenDataset(job.dataset)}
          className="ms-auto font-mono underline-offset-2 hover:text-foreground hover:underline"
        >
          {job.dataset}
        </button>
      </div>
      <IngestJobLine job={job} onCancel={onCancel} />
    </div>
  );
}
