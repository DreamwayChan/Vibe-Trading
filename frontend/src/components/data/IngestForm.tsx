import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, Loader2, Play } from "lucide-react";
import { cn } from "@/lib/utils";
import type { IngestDatasetOption, IngestRequest } from "@/lib/dataClient";

/** An ingest body without the dataset name, which the caller already knows. */
export type IngestBody = Omit<IngestRequest, "dataset">;

/**
 * Which controls this dataset can actually honour.
 *
 * Derived from ``option.params`` — the backend lists the ingester's own
 * signature there — instead of hard-coding "bars_intraday takes a range".  A
 * hard-coded rule rots the moment an ingester gains or loses a parameter, and
 * it would silently post a field the backend then drops.
 */
export function formFields(option: IngestDatasetOption | undefined) {
  const params = new Set(option?.params ?? []);
  return {
    range: params.has("start") || params.has("end"),
    interval: params.has("interval"),
    symbols: params.has("symbols"),
    limit: params.has("limit"),
    refresh: params.has("refresh"),
  };
}

/** baostock's archive only carries these sizes; ``1h`` normalises to ``60m``. */
const INTERVALS = ["5m", "15m", "30m", "1h"];

/** Split on commas or whitespace, upper-case, drop empties. */
export function parseSymbols(text: string): string[] {
  return text
    .split(/[\s,，;；]+/)
    .map((part) => part.trim().toUpperCase())
    .filter(Boolean);
}

export function IngestForm({
  option,
  starting,
  disabled,
  onSubmit,
}: {
  option: IngestDatasetOption | undefined;
  starting: boolean;
  disabled?: boolean;
  onSubmit: (body: IngestBody) => void;
}) {
  const { t } = useTranslation();
  const fields = useMemo(() => formFields(option), [option]);
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [interval, setInterval] = useState(INTERVALS[0]);
  const [symbols, setSymbols] = useState("");
  const [limit, setLimit] = useState("");
  const [refresh, setRefresh] = useState(false);

  // Only the range can be wrong before it reaches the server: a reversed window
  // fetches nothing and looks like "already covered", which is the most
  // expensive way to learn about a typo.
  const badRange = Boolean(start && end && start > end);
  const parsed = parseSymbols(symbols);

  if (!fields.range && !fields.symbols && !fields.limit && !fields.refresh && !fields.interval) {
    return null;
  }

  const submit = () => {
    if (badRange) return;
    const body: IngestBody = {};
    if (start) body.start = start;
    if (end) body.end = end;
    if (fields.interval && interval) body.interval = interval;
    if (parsed.length) body.symbols = parsed;
    if (fields.limit && limit) body.limit = Number(limit);
    if (fields.refresh && refresh) body.refresh = true;
    onSubmit(body);
  };

  return (
    <div className="mt-2 border-t pt-2">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
      >
        <ChevronDown
          className={cn("h-3 w-3 transition-transform", open && "rotate-180")}
          aria-hidden
        />
        {t("dataCenter.ingestAdvanced")}
      </button>

      {open && (
        <div className="mt-2 flex flex-col gap-2">
          <p className="text-[11px] text-muted-foreground">{t("dataCenter.ingestAdvancedHint")}</p>
          <div className="grid gap-2 sm:grid-cols-2">
            {fields.range && (
              <>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[11px] text-muted-foreground">
                    {t("dataCenter.ingestStart")}
                  </span>
                  <input
                    type="date"
                    value={start}
                    onChange={(event) => setStart(event.target.value)}
                    className="rounded-md border bg-background px-2 py-1 text-[11px]"
                  />
                </label>
                <label className="flex flex-col gap-0.5">
                  <span className="text-[11px] text-muted-foreground">
                    {t("dataCenter.ingestEnd")}
                  </span>
                  <input
                    type="date"
                    value={end}
                    onChange={(event) => setEnd(event.target.value)}
                    className="rounded-md border bg-background px-2 py-1 text-[11px]"
                  />
                </label>
              </>
            )}
            {fields.interval && (
              <label className="flex flex-col gap-0.5">
                <span className="text-[11px] text-muted-foreground">
                  {t("dataCenter.ingestInterval")}
                </span>
                <select
                  value={interval}
                  onChange={(event) => setInterval(event.target.value)}
                  className="rounded-md border bg-background px-2 py-1 text-[11px]"
                >
                  {INTERVALS.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {fields.limit && (
              <label className="flex flex-col gap-0.5">
                <span className="text-[11px] text-muted-foreground">
                  {t("dataCenter.ingestLimit")}
                </span>
                <input
                  type="number"
                  min={1}
                  value={limit}
                  onChange={(event) => setLimit(event.target.value)}
                  className="rounded-md border bg-background px-2 py-1 text-[11px] tabular-nums"
                />
              </label>
            )}
            {fields.symbols && (
              <label className="flex flex-col gap-0.5 sm:col-span-2">
                <span className="text-[11px] text-muted-foreground">
                  {t("dataCenter.ingestSymbols")}
                </span>
                <input
                  type="text"
                  value={symbols}
                  placeholder="600519.SH, 000001.SZ"
                  onChange={(event) => setSymbols(event.target.value)}
                  className="rounded-md border bg-background px-2 py-1 font-mono text-[11px]"
                />
                <span className="text-[11px] text-muted-foreground">
                  {parsed.length
                    ? t("dataCenter.ingestSymbolsCount", { n: parsed.length })
                    : t("dataCenter.ingestSymbolsHint")}
                </span>
              </label>
            )}
          </div>

          {fields.refresh && (
            <label className="flex items-center gap-1.5 text-[11px]">
              <input
                type="checkbox"
                checked={refresh}
                onChange={(event) => setRefresh(event.target.checked)}
              />
              <span>{t("dataCenter.ingestRefresh")}</span>
              <span className="text-muted-foreground">{t("dataCenter.ingestRefreshHint")}</span>
            </label>
          )}

          {badRange && (
            <p className="text-[11px] text-destructive">{t("dataCenter.ingestRangeInvalid")}</p>
          )}

          <div>
            <button
              type="button"
              onClick={submit}
              disabled={starting || disabled || badRange}
              className="inline-flex items-center gap-1 rounded-md border border-primary/40 bg-primary/5 px-2 py-1 text-[11px] text-primary hover:bg-primary/10 disabled:opacity-50"
            >
              {starting ? (
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              ) : (
                <Play className="h-3 w-3" aria-hidden />
              )}
              {t("dataCenter.ingestRun")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
