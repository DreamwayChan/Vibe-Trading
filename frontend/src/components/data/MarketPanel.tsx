/**
 * Market tab — the K-line view of the data center.
 *
 * It reads `/data/bars` and nothing else: the chart cannot fetch a source of
 * its own, so what you see here is by construction what a backtest reading the
 * lake would see.  The envelope (as_of / derived_from / sources / warnings) is
 * rendered above the chart rather than hidden in a tooltip, because "which
 * session is this" and "did the hub aggregate this" are the two facts that
 * used to differ between screens.
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, Loader2, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDataQuery } from "@/hooks/useDataQuery";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { CoverageHeatmap } from "@/components/data/CoverageHeatmap";
import { dataApi, formatCount, type BarsResponse, type CoverageResponse } from "@/lib/dataClient";
import type { PriceBar } from "@/lib/api";

const INTERVALS = ["1d", "5m", "15m", "30m", "1H", "4H"] as const;

/**
 * Adjustment only exists for daily bars: the lake stores unadjusted prices plus
 * `adj_factor`, and `bars_intraday` has no factor column.  The control is hidden
 * for intraday rather than disabled, so nobody is left guessing why a 5m chart
 * ignores the switch.
 */
const ADJUST_MODES = [
  { value: "none", labelKey: "dataCenter.adjustNone" },
  { value: "qfq", labelKey: "dataCenter.adjustQfq" },
  { value: "hfq", labelKey: "dataCenter.adjustHfq" },
] as const;

type AdjustMode = (typeof ADJUST_MODES)[number]["value"];

function toPriceBars(series: BarsResponse["series"][string] | undefined): PriceBar[] {
  if (!series) return [];
  return series.points
    .filter((point) => point.c != null && point.o != null && point.h != null && point.l != null)
    .map((point) => ({
      time: point.t,
      open: point.o as number,
      high: point.h as number,
      low: point.l as number,
      close: point.c as number,
      volume: point.v ?? 0,
    }));
}

export function MarketPanel({ onOpenDataset }: { onOpenDataset: (name: string) => void }) {
  const { t } = useTranslation();
  const [interval, setInterval] = useState<string>("5m");
  const [symbolInput, setSymbolInput] = useState("");
  const [applied, setApplied] = useState<string[]>([]);
  const [adjust, setAdjust] = useState<AdjustMode>("none");

  const symbolKey = applied.join(",");
  const effectiveAdjust: AdjustMode = interval === "1d" ? adjust : "none";
  const barsQuery = useDataQuery<BarsResponse>(
    `bars:${interval}:${symbolKey}:${effectiveAdjust}`,
    () => dataApi.getBars({ interval, symbols: applied, limit: 4000, adjust: effectiveAdjust }),
  );
  const coverageQuery = useDataQuery<CoverageResponse>(
    `coverage:${interval}:${symbolKey}`,
    () => dataApi.getCoverage({ interval, symbols: applied }),
  );

  const payload = barsQuery.data ?? null;
  const symbols = useMemo(() => Object.keys(payload?.series ?? {}), [payload]);
  const [picked, setPicked] = useState<string | null>(null);
  const active = picked && symbols.includes(picked) ? picked : symbols[0];
  const bars = useMemo(() => toPriceBars(active ? payload?.series[active] : undefined), [payload, active]);

  const apply = () => {
    const parsed = symbolInput
      .split(/[,\s]+/)
      .map((item) => item.trim().toUpperCase())
      .filter(Boolean);
    setApplied(parsed);
    setPicked(null);
  };

  const loading = barsQuery.loading || coverageQuery.loading;
  const dataset = interval === "1d" ? "bars_daily" : "bars_intraday";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
          {t("dataCenter.symbolsLabel")}
          <input
            value={symbolInput}
            onChange={(event) => setSymbolInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") apply();
            }}
            placeholder={t("dataCenter.symbolsPlaceholder")}
            className="w-64 rounded-md border bg-background px-2 py-1.5 text-xs text-foreground outline-none focus:border-primary"
          />
        </label>
        <button
          type="button"
          onClick={apply}
          className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
        >
          {t("dataCenter.applySymbols")}
        </button>

        <div className="flex items-center gap-1 rounded-md border p-0.5">
          {INTERVALS.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => {
                setInterval(item);
                setPicked(null);
              }}
              className={cn(
                "rounded px-2 py-1 text-[11px] transition-colors",
                interval === item
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-muted",
              )}
            >
              {item}
            </button>
          ))}
        </div>

        {interval === "1d" && (
          <div className="flex items-center gap-1 rounded-md border p-0.5">
            {ADJUST_MODES.map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => setAdjust(item.value)}
                className={cn(
                  "rounded px-2 py-1 text-[11px] transition-colors",
                  effectiveAdjust === item.value
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted",
                )}
                title={t(item.labelKey)}
              >
                {t(item.labelKey)}
              </button>
            ))}
          </div>
        )}

        <button
          type="button"
          onClick={() => {
            barsQuery.reload();
            coverageQuery.reload();
          }}
          className="ms-auto inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} aria-hidden />
          {t("dataCenter.refresh")}
        </button>
      </div>

      {payload && (
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
          <span>
            {t("dataCenter.dataDate")} <span className="text-foreground tabular-nums">{payload.as_of.date}</span>
          </span>
          <span>
            {t("dataCenter.barsCoverage", {
              symbols: payload.coverage.symbols,
              bars: formatCount(payload.coverage.bars),
              sessions: payload.coverage.sessions,
            })}
          </span>
          <span>
            {t("dataCenter.sources")}: {payload.sources.length ? payload.sources.join(" / ") : "—"}
          </span>
          {payload.derived_from && (
            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-primary">
              {t("dataCenter.derivedFrom", { interval: payload.derived_from })}
            </span>
          )}
          {payload.adjust !== "none" && (
            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-primary">
              {t(payload.adjust === "qfq" ? "dataCenter.adjustQfq" : "dataCenter.adjustHfq")}
            </span>
          )}
          <button
            type="button"
            onClick={() => onOpenDataset(dataset)}
            className="font-mono text-[11px] text-primary hover:underline"
          >
            {dataset}
          </button>
        </div>
      )}

      {(payload?.warnings.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          {payload?.warnings.join("；")}
        </div>
      )}

      {symbols.length > 1 && (
        <div className="flex flex-wrap items-center gap-1">
          {symbols.map((symbol) => (
            <button
              key={symbol}
              type="button"
              onClick={() => setPicked(symbol)}
              className={cn(
                "rounded-md border px-2 py-1 font-mono text-[11px]",
                active === symbol ? "border-primary bg-primary/10 text-primary" : "hover:bg-muted",
              )}
            >
              {symbol}
            </button>
          ))}
        </div>
      )}

      <div className="rounded-lg border bg-card p-2">
        {barsQuery.loading && bars.length === 0 ? (
          <div className="flex h-[320px] items-center justify-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            {t("dataCenter.loading")}
          </div>
        ) : bars.length === 0 ? (
          <div className="flex h-[240px] flex-col items-center justify-center gap-2 text-xs text-muted-foreground">
            <span>{t("dataCenter.noBars")}</span>
            <button
              type="button"
              onClick={() => onOpenDataset(dataset)}
              className="text-primary hover:underline"
            >
              {t("dataCenter.checkDataset")}
            </button>
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between px-2 py-1">
              <span className="font-mono text-xs">{active}</span>
              <span className="text-[11px] text-muted-foreground">
                {t("dataCenter.barsShown", { bars: bars.length })}
              </span>
            </div>
            <CandlestickChart data={bars} height={460} />
          </>
        )}
      </div>

      <div className="rounded-lg border bg-card p-3">
        {coverageQuery.loading && !coverageQuery.data ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            {t("dataCenter.loading")}
          </div>
        ) : (
          <CoverageHeatmap grid={coverageQuery.data ?? null} />
        )}
      </div>
    </div>
  );
}
