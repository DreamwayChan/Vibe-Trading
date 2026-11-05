/**
 * Data-center API client.
 *
 * Mirrors the backend `/data/*` contract in `agent/src/api/data_routes.py`.
 * Kept separate from `api.ts` only for size: it reuses the same `request`
 * helper, so auth headers and error shaping stay identical.
 */

import { request } from "@/lib/api";

export type FreshnessStatus = "ok" | "pending" | "lag" | "stale" | "empty" | "unknown";

export interface DataAsOf {
  date: string;
  settled: boolean;
  live: boolean;
  reason: string;
}

export interface Freshness {
  name: string;
  label: string;
  category: string;
  status: FreshnessStatus;
  rows: number | null;
  symbols: number | null;
  date_min: string | null;
  date_max: string | null;
  lag_days: number | null;
  files: number | null;
  bytes: number | null;
  last_write: string | null;
  last_source: string | null;
  consumers: string[];
  retention_days: number | null;
  sla_minutes: number;
  dirty: boolean;
}

export interface LakeInfo {
  root: string;
  files: number;
  bytes: number;
  exists: boolean;
}

export interface QuotaEndpoint {
  api: string;
  per_token: number;
  tokens: number;
  remaining: number;
}

export interface QuotaInfo {
  tokens: number;
  endpoints: QuotaEndpoint[];
  note: string;
}

export interface DataAlert {
  dataset: string;
  label: string;
  severity: "warning" | "info" | string;
  code: string;
  message: string;
}

export interface DataOverview {
  as_of: DataAsOf;
  generated_at: string;
  strict: boolean;
  lake: LakeInfo;
  counts: {
    declared: number;
    with_data: number;
    empty: number;
    total_rows: number;
  };
  calendar: { source: string; available: boolean };
  quota: QuotaInfo;
  freshness: Freshness[];
  alerts: DataAlert[];
}

export interface DatasetColumn {
  name: string;
  type: string;
}

export interface DatasetSpec {
  name: string;
  label: string;
  category: string;
  category_label: string;
  keys: string[];
  partitions: string[];
  columns: DatasetColumn[];
  frequency: string;
  markets: string[];
  sources: string[];
  consumers: string[];
  settle_rule: string;
  retention_days: number | null;
  sla_minutes: number;
  ingest: string | null;
  checks: { id: string; predicate: string }[];
  description: string;
}

export interface DatasetCard extends DatasetSpec {
  health: Freshness;
}

export interface DatasetListResponse {
  as_of: DataAsOf;
  datasets: DatasetCard[];
}

export interface DatasetPartition {
  name: string;
  rows: number | null;
  files: number;
  bytes: number;
}

export interface QualityCheck {
  id: string;
  violations: number | null;
  error?: string;
}

export interface DatasetDetail {
  spec: DatasetSpec;
  manifest: Record<string, unknown> | null;
  health: Freshness;
  partitions: DatasetPartition[];
  quality: { dup_keys?: number | null; checks?: QualityCheck[]; note?: string };
  schema_drift: { missing?: string[]; extra?: string[] };
}

export interface QualityResponse {
  as_of: DataAsOf;
  alerts: DataAlert[];
}

export interface BarPoint {
  t: string;
  o: number | null;
  h: number | null;
  l: number | null;
  c: number | null;
  v: number | null;
}

export interface SymbolSeries {
  bars: number;
  points: BarPoint[];
}

export interface BarsCoverage {
  symbols: number;
  bars: number;
  sessions: number;
  first: string | null;
  last: string | null;
}

export interface BarsResponse {
  as_of: DataAsOf;
  interval: string;
  adjust: string;
  derived_from: string | null;
  sources: string[];
  coverage: BarsCoverage;
  series: Record<string, SymbolSeries>;
  warnings: string[];
}

export interface CoverageBucket {
  key: string;
  sessions: number;
}

export interface CoverageSymbol {
  symbol: string;
  covered: number;
}

export interface CoverageCell {
  symbol: string;
  bucket: string;
  covered: number;
  sessions: number;
}

export interface CoverageResponse {
  as_of: DataAsOf;
  interval: string;
  granularity: "session" | "month" | string;
  buckets: CoverageBucket[];
  symbols: CoverageSymbol[];
  cells: CoverageCell[];
  truncated: boolean;
  warnings: string[];
}

export type IngestJobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface IngestReport {
  dataset: string;
  rows: number;
  sessions: number;
  symbols: number;
  source: string;
  start: string | null;
  end: string | null;
  skipped: number;
  notes: string[];
}

export interface IngestJob {
  id: string;
  dataset: string;
  params: Record<string, unknown>;
  status: IngestJobStatus;
  message: string;
  progress: number;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  seconds: number | null;
  report: IngestReport | null;
  error: string | null;
  /** Resume point: how far the last unit got, and out of how many. */
  cursor: { session?: string; done?: number; total?: number } | null;
  rows: number;
  done: number | null;
  total: number | null;
}

export interface IngestDatasetOption {
  name: string;
  label: string;
  category: string;
  ingest: string | null;
  runnable: boolean;
  reason: string;
  params: string[];
  sla_minutes: number;
  sources: string[];
}

export interface IngestStatusResponse {
  active: IngestJob[];
  jobs: IngestJob[];
  datasets: IngestDatasetOption[];
}

export interface IngestRequest {
  dataset: string;
  start?: string;
  end?: string;
  symbols?: string[];
  limit?: number;
  interval?: string;
  refresh?: boolean;
}

export const dataApi = {  getOverview: (opts: { live?: boolean; refresh?: boolean } = {}) => {
    const params = new URLSearchParams();
    if (opts.live) params.set("live", "true");
    if (opts.refresh) params.set("refresh", "true");
    const qs = params.toString();
    return request<DataOverview>(`/data/overview${qs ? `?${qs}` : ""}`);
  },
  listDatasets: () => request<DatasetListResponse>("/data/datasets"),
  getDataset: (name: string, refresh = false) =>
    request<DatasetDetail>(
      `/data/datasets/${encodeURIComponent(name)}${refresh ? "?refresh=true" : ""}`,
    ),
  getQuality: () => request<QualityResponse>("/data/quality"),
  getBars: (opts: {
    symbols?: string[];
    interval?: string;
    start?: string;
    end?: string;
    limit?: number;
    /** none 不复权 / qfq 前复权 / hfq 后复权 — derived by the hub from adj_factor. */
    adjust?: string;
  } = {}) =>
    request<BarsResponse>(
      `/data/bars?${seriesParams(opts)}${opts.adjust && opts.adjust !== "none" ? `&adjust=${encodeURIComponent(opts.adjust)}` : ""}`,
    ),
  getCoverage: (opts: {
    symbols?: string[];
    interval?: string;
    start?: string;
    end?: string;
  } = {}) => request<CoverageResponse>(`/data/coverage?${seriesParams(opts)}`),
  getIngestStatus: () => request<IngestStatusResponse>("/data/ingest"),
  getIngestJob: (id: string) =>
    request<IngestJob>(`/data/ingest/jobs/${encodeURIComponent(id)}`),
  startIngest: (body: IngestRequest) =>
    request<IngestJob>("/data/ingest", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Cooperative: the job stops at its next symbol/session and reports a cursor,
  // so the next run continues instead of re-fetching.
  cancelIngest: (id: string) =>
    request<IngestJob>(`/data/ingest/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),
  rebuildCatalog: (datasets?: string[]) =>
    request<{ rebuilt: string[]; rows: Record<string, number | null> }>("/data/catalog/rebuild", {
      method: "POST",
      body: JSON.stringify({ datasets: datasets ?? null }),
    }),
};

/** Shared query-string builder for the two series endpoints. */
function seriesParams(opts: {
  symbols?: string[];
  interval?: string;
  start?: string;
  end?: string;
  limit?: number;
}): string {
  const params = new URLSearchParams();
  if (opts.symbols?.length) params.set("symbols", opts.symbols.join(","));
  if (opts.interval) params.set("interval", opts.interval);
  if (opts.start) params.set("start", opts.start);
  if (opts.end) params.set("end", opts.end);
  if (opts.limit) params.set("limit", String(opts.limit));
  return params.toString();
}

/** Human-readable byte size (binary units, one decimal above KB). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

/** Thousands separator with no decimals — row counts are magnitudes, not money. */
export function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return Math.round(value).toLocaleString("en-US");
}
