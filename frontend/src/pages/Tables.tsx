import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  FileText,
  Loader2,
  RefreshCw,
  Table2,
  Upload,
  X,
} from "lucide-react";
import {
  ApiError,
  api,
  type ExtractedTable,
  type TableEngineStatus,
  type TableExtractionResult,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const MAX_PDF_BYTES = 50 * 1024 * 1024;
/** Rows rendered before the "show all" toggle kicks in. */
const ROWS_PREVIEW = 20;

const STATEMENT_I18N = {
  income: "tables.statementIncome",
  balance: "tables.statementBalance",
  cashflow: "tables.statementCashflow",
} as const;

type StatementI18nKey = (typeof STATEMENT_I18N)[keyof typeof STATEMENT_I18N];

const QUALITY_I18N = {
  structural: "tables.qualityStructural",
  geometric: "tables.qualityGeometric",
  unknown: "tables.qualityUnknown",
} as const;

type QualityI18nKey = (typeof QUALITY_I18N)[keyof typeof QUALITY_I18N];

function statementI18nKey(hint: string | null): StatementI18nKey | null {
  if (hint === "income" || hint === "balance" || hint === "cashflow") {
    return STATEMENT_I18N[hint];
  }
  return null;
}

function qualityI18nKey(quality: string): QualityI18nKey {
  if (quality === "structural" || quality === "geometric") {
    return QUALITY_I18N[quality];
  }
  return QUALITY_I18N.unknown;
}

const QUALITY_CLASS: Record<string, string> = {
  structural: "border-emerald-500/40 text-emerald-700 dark:text-emerald-400",
  geometric: "border-amber-500/40 text-amber-700 dark:text-amber-400",
  unknown: "border-border text-muted-foreground",
};

function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${Math.round(size / 1024)} KB`;
  return `${size} B`;
}

function pageSpan(pages: number[], t: TFunction): string {
  if (!pages.length) return "";
  if (pages.length === 1) return t("tables.pageSingle", { page: pages[0] });
  return t("tables.pageSpan", { first: pages[0], last: pages[pages.length - 1] });
}

function formatScale(scale: number | null): string {
  if (scale == null) return "";
  if (scale === 1) return "1";
  const exponent = Math.round(Math.log10(scale));
  return `1e${exponent}`;
}

export function Tables() {
  const { t } = useTranslation();
  const [file, setFile] = useState<File | null>(null);
  const [pages, setPages] = useState("");
  const [maxTables, setMaxTables] = useState(20);
  const [includeCsv, setIncludeCsv] = useState(true);
  const [engine, setEngine] = useState<TableEngineStatus | null>(null);
  const [result, setResult] = useState<TableExtractionResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const copyTimer = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getTableEngine()
      .then((status) => {
        if (!cancelled) setEngine(status);
      })
      .catch(() => {
        if (!cancelled) setEngine(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(
    () => () => {
      if (copyTimer.current) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const acceptFile = useCallback(
    (next: File) => {
      if (!next.name.toLowerCase().endsWith(".pdf")) {
        setError(t("tables.errorNotPdf"));
        return;
      }
      if (next.size > MAX_PDF_BYTES) {
        setError(t("tables.errorTooLarge", { limit: formatBytes(MAX_PDF_BYTES) }));
        return;
      }
      setFile(next);
      setResult(null);
      setError(null);
      setExpanded(new Set());
    },
    [t],
  );

  const runExtraction = async () => {
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    try {
      const uploaded = await api.uploadFile(file);
      const next = await api.extractTables({
        file_path: uploaded.file_path,
        pages: pages.trim(),
        max_tables: maxTables,
        include_csv: includeCsv,
      });
      setResult(next);
      setExpanded(new Set());
    } catch (err) {
      setResult(null);
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const copy = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      if (copyTimer.current) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopiedKey(null), 1500);
    } catch {
      setError(t("tables.errorCopy"));
    }
  };

  const downloadCsv = (table: ExtractedTable) => {
    // Prepend a BOM so Excel opens UTF-8 Chinese headers instead of mojibake.
    const blob = new Blob([`\ufeff${table.csv}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${(result?.file || "table").replace(/\.pdf$/i, "")}-${table.id}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const engineOk = engine?.status === "ok";
  const pageCap = engine?.max_pages;

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <div className="flex items-start gap-3">
        <Table2 className="mt-0.5 h-6 w-6 text-primary" />
        <div className="flex-1">
          <h1 className="text-2xl font-bold">{t("tables.title")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("tables.subtitle")}</p>
        </div>
      </div>

      {engine && (
        <div
          className={cn(
            "flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-xs",
            engineOk ? "text-muted-foreground" : "border-destructive/40 bg-destructive/5 text-destructive",
          )}
        >
          {engineOk ? (
            <>
              <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
              <span>
                {t("tables.engineLine", {
                  engine: engine.engine ?? "",
                  version: engine.version ?? "",
                })}
              </span>
              {pageCap != null && <span>· {t("tables.pageCap", { cap: pageCap })}</span>}
              <span>· {t("tables.cacheLine", { state: engine.cache ? t("tables.cacheOn") : t("tables.cacheOff") })}</span>
            </>
          ) : (
            <>
              <AlertTriangle className="h-3.5 w-3.5" />
              <span>{t("tables.engineUnavailable", { error: engine.error ?? "" })}</span>
            </>
          )}
        </div>
      )}

      {/* Drop zone / selected file */}
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const dropped = event.dataTransfer.files?.[0];
          if (dropped) acceptFile(dropped);
        }}
        className={cn(
          "rounded-lg border-2 border-dashed px-4 py-6 transition-colors",
          dragging ? "border-primary bg-primary/5" : "border-border",
        )}
      >
        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={(event) => {
            const picked = event.target.files?.[0];
            if (picked) acceptFile(picked);
            event.target.value = "";
          }}
        />
        {file ? (
          <div className="flex items-center gap-3">
            <FileText className="h-5 w-5 shrink-0 text-primary" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium" title={file.name}>
                {file.name}
              </p>
              <p className="text-xs text-muted-foreground">{formatBytes(file.size)}</p>
            </div>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="rounded-md border px-3 py-1.5 text-xs hover:bg-muted"
            >
              {t("tables.changeFile")}
            </button>
            <button
              type="button"
              onClick={() => {
                setFile(null);
                setResult(null);
              }}
              className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
              title={t("tables.removeFile")}
              aria-label={t("tables.removeFile")}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2 text-center">
            <Upload className="h-6 w-6 text-muted-foreground" />
            <p className="text-sm font-medium">{t("tables.dropTitle")}</p>
            <p className="text-xs text-muted-foreground">{t("tables.dropHint")}</p>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="mt-1 rounded-md border px-3 py-1.5 text-xs hover:bg-muted"
            >
              {t("tables.chooseFile")}
            </button>
          </div>
        )}
      </div>

      {/* Options */}
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-muted-foreground">{t("tables.pagesLabel")}</span>
          <input
            value={pages}
            onChange={(event) => setPages(event.target.value)}
            placeholder={t("tables.pagesPlaceholder")}
            className="w-48 rounded-md border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-muted-foreground">{t("tables.maxTablesLabel")}</span>
          <input
            type="number"
            min={1}
            max={100}
            value={maxTables}
            onChange={(event) => setMaxTables(Math.max(1, Math.min(100, Number(event.target.value) || 1)))}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
          />
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input
            type="checkbox"
            checked={includeCsv}
            onChange={(event) => setIncludeCsv(event.target.checked)}
            className="h-4 w-4"
          />
          {t("tables.includeCsv")}
        </label>
        <button
          type="button"
          onClick={runExtraction}
          disabled={!file || busy || engine?.status === "unavailable"}
          className="ml-auto inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : result ? <RefreshCw className="h-4 w-4" /> : <Table2 className="h-4 w-4" />}
          {busy ? t("tables.extracting") : result ? t("tables.reExtract") : t("tables.extract")}
        </button>
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="break-words">{error}</span>
        </div>
      )}

      {result && (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{result.file}</span>
            <span className={cn("rounded-md border px-2 py-0.5", QUALITY_CLASS[result.quality] ?? QUALITY_CLASS.unknown)}>
              {t(qualityI18nKey(result.quality))}
            </span>
            <span>{t("tables.metaEngine", { engine: result.engine, version: result.engine_version })}</span>
            <span>· {t("tables.metaTables", { count: result.table_count })}</span>
            <span>· {t("tables.metaPages", { count: result.pages_read.length })}</span>
            <span>· {t("tables.metaElapsed", { ms: result.elapsed_ms })}</span>
            <span>
              ·{" "}
              {result.cache === "hit"
                ? t("tables.cacheHit")
                : result.cache === "off"
                  ? t("tables.cacheOff")
                  : t("tables.cacheMiss")}
            </span>
          </div>

          {result.quality === "geometric" && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
              {t("tables.geometricHint")}
            </div>
          )}

          {result.security_warnings && result.security_warnings.length > 0 && (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
              <p className="font-medium">{t("tables.securityWarning", { count: result.security_warnings.length })}</p>
              <ul className="mt-1 list-inside list-disc">
                {result.security_warnings.map((warning, index) => (
                  <li key={`${warning.rule_id ?? "rule"}-${index}`}>
                    {warning.field ? `${warning.field}: ` : ""}
                    {warning.message ?? warning.rule_id ?? ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.note && (
            <div className="rounded-md border px-3 py-2 text-xs text-muted-foreground">{result.note}</div>
          )}

          {result.truncated && !result.note && (
            <div className="rounded-md border px-3 py-2 text-xs text-muted-foreground">{t("tables.truncated")}</div>
          )}

          {result.tables.length === 0 ? (
            <div className="rounded-lg border px-4 py-8 text-center">
              <p className="text-sm font-medium">{t("tables.noTablesTitle")}</p>
              <p className="mt-1 text-xs text-muted-foreground">{t("tables.noTablesHint")}</p>
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              {result.tables.map((table) => {
                const isExpanded = expanded.has(table.id);
                const width = Math.max(table.n_cols, table.columns.length);
                const header = Array.from({ length: width }, (_, index) => table.columns[index] ?? "");
                const visibleRows = isExpanded ? table.rows : table.rows.slice(0, ROWS_PREVIEW);
                const statementKey = statementI18nKey(table.statement_hint);
                return (
                  <div key={table.id} className="overflow-hidden rounded-lg border">
                    <div className="flex flex-wrap items-center gap-2 border-b bg-muted/40 px-3 py-2">
                      <Table2 className="h-4 w-4 shrink-0 text-muted-foreground" />
                      <span className="text-sm font-medium">{table.caption || t("tables.captionFallback")}</span>
                      <span className="text-xs text-muted-foreground">{pageSpan(table.pages, t)}</span>
                      {table.pages.length > 1 && (
                        <span className="rounded-md border border-primary/40 px-1.5 py-0.5 text-[10px] text-primary">
                          {t("tables.stitched", { count: table.pages.length })}
                        </span>
                      )}
                      {table.unit_label && (
                        <span className="rounded-md border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {t("tables.unit", { unit: table.unit_label })}
                          {table.unit_scale != null && table.unit_scale !== 1
                            ? ` · ×${formatScale(table.unit_scale)}`
                            : ""}
                        </span>
                      )}
                      {statementKey && (
                        <span className="rounded-md border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {t(statementKey)}
                        </span>
                      )}
                      <span className="text-xs text-muted-foreground">
                        {t("tables.size", { rows: table.n_rows, cols: table.n_cols })}
                      </span>
                      <div className="ml-auto flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => copy(table.markdown, `${table.id}-md`)}
                          className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] hover:bg-muted"
                        >
                          {copiedKey === `${table.id}-md` ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                          {copiedKey === `${table.id}-md` ? t("tables.copied") : t("tables.copyMarkdown")}
                        </button>
                        <button
                          type="button"
                          onClick={() => downloadCsv(table)}
                          disabled={!table.csv}
                          className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] hover:bg-muted disabled:opacity-40"
                        >
                          <Download className="h-3 w-3" />
                          {t("tables.downloadCsv")}
                        </button>
                      </div>
                    </div>

                    <div className="max-h-[28rem] overflow-auto">
                      <table className="w-full border-collapse text-xs">
                        <thead className="sticky top-0 z-10 bg-muted">
                          <tr>
                            {header.map((label, index) => (
                              <th
                                key={index}
                                className="whitespace-nowrap border px-2 py-1.5 text-start font-medium"
                                title={label}
                              >
                                {label || "—"}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {visibleRows.map((row, rowIndex) => (
                            <tr key={rowIndex} className="even:bg-muted/20">
                              {Array.from({ length: width }, (_, colIndex) => (
                                <td
                                  key={colIndex}
                                  className={cn(
                                    "whitespace-nowrap border px-2 py-1",
                                    table.column_types[colIndex] === "number" && "text-end tabular-nums",
                                  )}
                                >
                                  {row[colIndex] ?? ""}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    {table.rows.length > ROWS_PREVIEW && (
                      <button
                        type="button"
                        onClick={() => toggleExpanded(table.id)}
                        className="flex w-full items-center justify-center gap-1 border-t px-3 py-1.5 text-[11px] text-muted-foreground hover:bg-muted"
                      >
                        {isExpanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                        {isExpanded
                          ? t("tables.showLess")
                          : t("tables.showAll", { shown: ROWS_PREVIEW, total: table.rows.length })}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {!result && !busy && (
        <div className="rounded-lg border px-4 py-6">
          <p className="text-sm font-medium">{t("tables.howtoTitle")}</p>
          <ol className="mt-2 list-inside list-decimal space-y-1 text-xs text-muted-foreground">
            <li>{t("tables.howtoStep1")}</li>
            <li>{t("tables.howtoStep2")}</li>
            <li>{t("tables.howtoStep3")}</li>
          </ol>
        </div>
      )}
    </div>
  );
}
