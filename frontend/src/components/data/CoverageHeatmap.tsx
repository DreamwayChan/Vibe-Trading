/**
 * Coverage heat map — symbol × session presence.
 *
 * Drawn as a CSS grid rather than a chart library on purpose: the thing that
 * matters here is the *hole*, and a discrete cell is unambiguous in a way a
 * continuous heat map is not.  Column count is bounded by the hub (it buckets
 * to months when the span exceeds ~180 sessions), so the DOM stays small.
 */

import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { CoverageResponse } from "@/lib/dataClient";

function tone(covered: number, sessions: number): string {
  if (sessions <= 0) return "bg-muted/30";
  if (covered <= 0) return "bg-rose-500/25";
  const ratio = covered / sessions;
  if (ratio >= 0.999) return "bg-emerald-500/80";
  if (ratio >= 0.8) return "bg-emerald-500/60";
  if (ratio >= 0.5) return "bg-amber-500/60";
  return "bg-amber-500/35";
}

export function CoverageHeatmap({ grid }: { grid: CoverageResponse | null }) {
  const { t } = useTranslation();
  if (!grid || grid.buckets.length === 0) {
    return <p className="text-[11px] text-muted-foreground">{t("dataCenter.noData")}</p>;
  }

  const lookup = new Map<string, number>();
  for (const cell of grid.cells) lookup.set(`${cell.symbol}\u0000${cell.bucket}`, cell.covered);
  const sessionsByBucket = new Map(grid.buckets.map((b) => [b.key, b.sessions]));

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-medium">{t("dataCenter.coverageTitle")}</h3>
        <span className="text-[11px] text-muted-foreground">
          {t(grid.granularity === "month" ? "dataCenter.coverageByMonth" : "dataCenter.coverageBySession", {
            symbols: grid.symbols.length,
            buckets: grid.buckets.length,
          })}
        </span>
      </div>

      <div className="overflow-x-auto rounded-lg border bg-card p-2">
        <table className="w-full border-separate border-spacing-[1px] text-[10px]">
          <thead>
            <tr>
              <th className="sticky start-0 bg-card px-1 text-start font-medium text-muted-foreground">
                {t("dataCenter.colSymbols")}
              </th>
              {grid.buckets.map((bucket) => (
                <th
                  key={bucket.key}
                  className="w-2 p-0 text-center font-normal text-muted-foreground"
                  title={bucket.key}
                >
                  <span className="block origin-bottom-left -rotate-90 whitespace-nowrap pb-1 text-[9px]">
                    {grid.granularity === "month" ? bucket.key.slice(2) : bucket.key.slice(5)}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.symbols.map((row) => (
              <tr key={row.symbol}>
                <td className="sticky start-0 bg-card px-1 font-mono whitespace-nowrap text-muted-foreground">
                  {row.symbol}
                </td>
                {grid.buckets.map((bucket) => {
                  const covered = lookup.get(`${row.symbol}\u0000${bucket.key}`) ?? 0;
                  const sessions = sessionsByBucket.get(bucket.key) ?? 0;
                  return (
                    <td key={bucket.key} className="p-0">
                      <div
                        className={cn("h-3 w-2 rounded-[1px]", tone(covered, sessions))}
                        title={`${row.symbol} · ${bucket.key} · ${covered}/${sessions}`}
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[10px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-3 rounded-[1px] bg-emerald-500/80" />
          {t("dataCenter.coverageFull")}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-3 rounded-[1px] bg-amber-500/60" />
          {t("dataCenter.coveragePartial")}
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-3 rounded-[1px] bg-rose-500/25" />
          {t("dataCenter.coverageMissing")}
        </span>
        {grid.truncated && <span>{t("dataCenter.coverageTruncated")}</span>}
        {grid.warnings.map((warning) => (
          <span key={warning}>{warning}</span>
        ))}
      </div>
    </div>
  );
}
