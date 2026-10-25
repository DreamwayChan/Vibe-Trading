import { authHeaders, withAuthTicket } from "@/lib/apiAuth";

const BASE = "";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_REQUIRED_MESSAGE =
  "Remote API access requires an API key. Add it in Settings, or run the backend on localhost for local-only use.";

export function isAuthRequiredError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

export interface CorrelationResponse {
  labels: string[];
  matrix: number[][];
}

export interface RegimeEpisode {
  start: string;
  end: string | null;
}

export interface CorrelationRegimeResponse {
  labels: string[];
  dates: string[];
  density: (number | null)[];
  smoothed: (number | null)[];
  fused: number[];
  episodes: RegimeEpisode[];
  params: {
    days: number;
    corr_window: number;
    edge_threshold: number;
    smooth_window: number;
    enter_threshold: number;
    exit_threshold: number;
  };
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    detail = body.detail || body.message || detail;
  } catch { /* ignore */ }
  if (res.status === 401 || res.status === 403) {
    detail = AUTH_REQUIRED_MESSAGE;
  }
  return new ApiError(detail, res.status);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  // Accept: application/json keeps SPA/API path twins (/monitor, /correlation,
  // /sector-pick, /market-pick, /runs/{id}) from returning the cached HTML shell
  // or Vite/FastAPI deep-link fallback that keys off text/html navigation.
  const mergedHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
    ...authHeaders(),
  };
  if (headers) {
    new Headers(headers).forEach((value, key) => {
      mergedHeaders[key] = value;
    });
  }
  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: mergedHeaders,
  });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  const text = await res.text();
  if (!text) return {} as T;

  const contentType = res.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const preview = text.slice(0, 80).replace(/\s+/g, " ");
    throw new ApiError(
      `Expected JSON from ${path}, got ${contentType || "unknown content type"}: ${preview}`,
      res.status,
    );
  }

  return JSON.parse(text) as T;
}

export interface UploadResult {
  status: string;
  file_path: string;
  filename: string;
}

async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/upload`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

function appendQueryParam(url: string, key: string, value: string): string {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
}

export const api = {
  uploadFile,
  getCorrelation: (codes: string, days: number, method: "pearson" | "spearman") =>
    request<CorrelationResponse>(
      `/correlation?codes=${encodeURIComponent(codes)}&days=${encodeURIComponent(String(days))}&method=${encodeURIComponent(method)}`,
    ),
  getCorrelationRegime: (codes: string, days: number) =>
    request<CorrelationRegimeResponse>(
      `/correlation/regime?codes=${encodeURIComponent(codes)}&days=${encodeURIComponent(String(days))}`,
    ),
  listRuns: (limit?: number, opts?: { strategyReady?: boolean }) => {
    const params = new URLSearchParams();
    if (limit != null) params.set("limit", String(limit));
    // Monitor strategy binding requires code/signal_engine.py — exclude chat-only runs.
    if (opts?.strategyReady) params.set("strategy_ready", "true");
    const qs = params.toString();
    return request<RunListItem[]>(`/runs${qs ? `?${qs}` : ""}`);
  },
  /** Rename a backtest run display title (Monitor strategy-source list). */
  renameRun: (runId: string, title: string) =>
    request<{ status: string; run_id: string; title: string }>(
      `/runs/${encodeURIComponent(runId)}`,
      { method: "PATCH", body: JSON.stringify({ title }) },
    ),
  /** Permanently remove a backtest run directory (+ its monitor board/history). */
  deleteRun: (runId: string) =>
    request<{ status: string; run_id: string }>(`/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }),
  getMonitor: () => request<MonitorBoard>(`/monitor`),
  updateMonitor: (body: UpdateMonitorRequest) =>
    request<MonitorBoard>(`/monitor`, { method: "PUT", body: JSON.stringify(body) }),
  scanMonitor: (body: { refresh_report?: boolean } = {}) =>
    request<MonitorBoard>(`/monitor/scan`, {
      method: "POST",
      body: JSON.stringify({ refresh_report: Boolean(body.refresh_report) }),
    }),
  getMarketPick: () => request<MarketPickBoard>(`/market-pick`),
  scanMarketPick: (body: { force?: boolean } = {}) =>
    request<MarketPickBoard>(`/market-pick/scan`, {
      method: "POST",
      body: JSON.stringify({ force: Boolean(body.force) }),
    }),
  listMarketPickHistory: (limit = 30) =>
    request<MarketPickHistorySummary[]>(`/market-pick/history?limit=${limit}`),
  getMarketPickHistoryDay: (asOf: string) =>
    request<MarketPickResult>(`/market-pick/history/${encodeURIComponent(asOf)}`),
  getSectorPick: () => request<SectorPickBoard>(`/sector-pick`),
  updateSectorPick: (body: { enabled_keys: string[] }) =>
    request<SectorPickBoard>(`/sector-pick`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  scanSectorPick: (body: { enabled_keys?: string[] } = {}) =>
    request<SectorPickBoard>(`/sector-pick/scan`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  previewSectorPickHoldings: (key: string) =>
    request<SectorPickBoard>(`/sector-pick/holdings`, {
      method: "POST",
      body: JSON.stringify({ key }),
    }),
  listSectorPickHistory: (limit = 30) =>
    request<SectorPickHistorySummary[]>(`/sector-pick/history?limit=${limit}`),
  getSectorPickHistoryDay: (asOf: string) =>
    request<SectorPickResult>(`/sector-pick/history/${encodeURIComponent(asOf)}`),
  listSectorPickHistorySeries: (limit = 30) =>
    request<SectorPickHistorySeries>(`/sector-pick/history/series?limit=${limit}`),
  /** Wipe this strategy's holdings history and rescan today as day one. */
  resetMonitor: () =>
    request<MonitorBoard>(`/monitor/reset`, { method: "POST", body: JSON.stringify({}) }),
  forecastMonitor: (body: { as_of?: string | null } = {}) =>
    request<MonitorForecastReport>(`/monitor/forecast`, {
      method: "POST",
      body: JSON.stringify({ as_of: body.as_of ?? null }),
    }),
  listMonitorForecastSaves: (limit = 50, runId?: string) => {
    const q = new URLSearchParams();
    q.set("limit", String(limit));
    if (runId) q.set("run_id", runId);
    return request<MonitorForecastSaveSummary[]>(`/monitor/forecast/saves?${q.toString()}`);
  },
  getMonitorForecastSave: (snapshotId: string, runId?: string) => {
    const q = new URLSearchParams();
    if (runId) q.set("run_id", runId);
    const qs = q.toString();
    return request<MonitorForecastSave>(
      `/monitor/forecast/saves/${encodeURIComponent(snapshotId)}${qs ? `?${qs}` : ""}`,
    );
  },
  saveMonitorForecast: (report: MonitorForecastReport) =>
    request<MonitorForecastSave>(`/monitor/forecast/saves`, {
      method: "POST",
      body: JSON.stringify(report),
    }),
  listMonitorHistory: (limit = 30, runId?: string) => {
    const q = new URLSearchParams();
    q.set("limit", String(limit));
    if (runId) q.set("run_id", runId);
    return request<MonitorHistorySummary[]>(`/monitor/history?${q.toString()}`);
  },
  getMonitorHistoryDay: (asOf: string, runId?: string) => {
    const q = new URLSearchParams();
    if (runId) q.set("run_id", runId);
    const qs = q.toString();
    return request<MonitorHistoryDetail>(
      `/monitor/history/${encodeURIComponent(asOf)}${qs ? `?${qs}` : ""}`,
    );
  },
  getRun: (id: string, params: RunDetailParams = {}) => {
    const q = new URLSearchParams();
    if (params.chart_payload) q.set("chart_payload", params.chart_payload);
    if (params.chart_symbol) q.set("chart_symbol", params.chart_symbol);
    const qs = q.toString();
    return request<RunData>(`/runs/${id}${qs ? `?${qs}` : ""}`);
  },
  getRunCode: (id: string) => request<Record<string, string>>(`/runs/${id}/code`),
  getRunPine: (id: string) => request<PineScriptResult>(`/runs/${id}/pine`),
  listSessions: () => request<SessionItem[]>("/sessions"),
  createSession: (title?: string) => request<SessionItem>("/sessions", { method: "POST", body: JSON.stringify({ title: title || "" }) }),
  deleteSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "DELETE" }),
  renameSession: (sid: string, title: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  sendMessage: (sid: string, content: string) => request<{ message_id: string; attempt_id: string }>(`/sessions/${sid}/messages`, { method: "POST", body: JSON.stringify({ content }) }),
  cancelSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}/cancel`, { method: "POST" }),
  getSessionMessages: (sid: string) => request<MessageItem[]>(`/sessions/${sid}/messages`),
  createGoal: (sid: string, body: CreateGoalRequest) =>
    request<GoalSnapshot>(`/sessions/${sid}/goal`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getGoal: (sid: string) => request<GoalSnapshot>(`/sessions/${sid}/goal`),
  updateGoal: (sid: string, body: UpdateGoalRequest) =>
    request<UpdateGoalResponse>(`/sessions/${sid}/goal`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  addGoalEvidence: (sid: string, body: AddGoalEvidenceRequest) =>
    request<AddGoalEvidenceResponse>(`/sessions/${sid}/goal/evidence`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateGoalStatus: (sid: string, body: UpdateGoalStatusRequest) =>
    request<UpdateGoalStatusResponse>(`/sessions/${sid}/goal/status`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  // Returns the bare stream URL (no auth in the query string). The SSE ticket
  // is minted per connect/reconnect inside useSSE (tickets are single-use, so
  // baking one into a cached URL would break reconnection).
  sseUrl: (sid: string, options?: { replay?: "active" }) => {
    let url = `${BASE}/sessions/${sid}/events`;
    if (options?.replay) url = appendQueryParam(url, "replay", options.replay);
    return url;
  },

  // Swarm API
  listSwarmPresets: () => request<SwarmPreset[]>("/swarm/presets"),
  createSwarmRun: (preset_name: string, user_vars: Record<string, string>) =>
    request<{ id: string; status: string }>("/swarm/runs", {
      method: "POST",
      body: JSON.stringify({ preset_name, user_vars }),
    }),
  listSwarmRuns: () => request<SwarmRunSummary[]>("/swarm/runs"),
  getSwarmRun: (id: string) => request<Record<string, unknown>>(`/swarm/runs/${id}`),
  swarmSseUrl: (id: string) => withAuthTicket(`${BASE}/swarm/runs/${id}/events`),
  cancelSwarmRun: (id: string) =>
    request<{ status: string }>(`/swarm/runs/${id}/cancel`, { method: "POST" }),
  retrySwarmRun: (id: string) =>
    request<{ id: string; status: string; preset_name: string }>(`/swarm/runs/${id}/retry`, { method: "POST" }),
  getLLMSettings: () => request<LLMSettings>("/settings/llm"),
  updateLLMSettings: (settings: UpdateLLMSettingsRequest) =>
    request<LLMSettings>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getDataSourceSettings: () => request<DataSourceSettings>("/settings/data-sources"),
  updateDataSourceSettings: (settings: UpdateDataSourceSettingsRequest) =>
    request<DataSourceSettings>("/settings/data-sources", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getChannelStatus: () => request<ChannelRuntimeStatus>("/channels/status"),
  startChannels: () => request<ChannelRuntimeActionResponse>("/channels/start", { method: "POST" }),
  stopChannels: () => request<ChannelRuntimeActionResponse>("/channels/stop", { method: "POST" }),
  runChannelPairingCommand: (body: ChannelPairingCommandRequest) =>
    request<ChannelPairingCommandResponse>("/channels/pairing/command", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Alpha Zoo API
  listAlphas: (params: AlphaListParams = {}) => {
    const q = new URLSearchParams();
    if (params.zoo) q.set("zoo", params.zoo);
    if (params.theme) q.set("theme", params.theme);
    if (params.universe) q.set("universe", params.universe);
    if (params.alive_status) q.set("alive_status", params.alive_status);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<AlphaListResponse>(`/alpha/list${qs ? `?${qs}` : ""}`);
  },
  getAlpha: (alphaId: string) =>
    request<AlphaDetailResponse>(`/alpha/${encodeURIComponent(alphaId)}`),
  createAlphaBench: (body: AlphaBenchRequest) =>
    request<{ status: string; job_id: string }>("/alpha/bench", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaBenchStreamUrl: (jobId: string) =>
    withAuthTicket(`${BASE}/alpha/bench/${encodeURIComponent(jobId)}/stream`),
  createAlphaCompare: (body: AlphaCompareRequest) =>
    request<{ status: string; job_id: string }>("/alpha/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaCompareStreamUrl: (jobId: string) =>
    withAuthTicket(`${BASE}/alpha/compare/${encodeURIComponent(jobId)}/stream`),

  // Connector runtime channel — privileged surface actions (NOT agent tools).
  // commit is the ONLY action that writes a mandate; halt trips the kill switch.
  commitMandate: (body: CommitMandateRequest) =>
    request<CommitMandateResponse>("/mandate/commit", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  haltLive: (session_id?: string, broker?: string, reason?: string) =>
    request<HaltLiveResponse>("/live/halt", {
      method: "POST",
      body: JSON.stringify({ session_id, broker, reason }),
    }),
  // Read the persistent runtime status across all authorized brokers (SPEC §7.5).
  // Polled by the RunnerStatus panel; a plain authenticated GET, never a chat message.
  getLiveStatus: (signal?: AbortSignal) => request<LiveStatus>("/live/status", { signal }),
  verifyConnector: (profileId: string) =>
    request<ConnectorVerifyResponse>(`/live/connectors/${encodeURIComponent(profileId)}/verify?force=true`, {
      method: "POST",
    }),
  authorizeLive: (broker: string) =>
    request<LiveAuthorizeResponse>("/live/authorize", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  // Start/stop the persistent runner (SPEC §7.5). Privileged surface actions, not agent tools.
  startLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/start", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  stopLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/stop", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
};

// --- Swarm types ---

export interface SwarmPreset {
  name: string;
  title: string;
  description: string;
  agent_count: number;
  variables: { name: string; description: string; required: boolean }[];
}

export interface SwarmRunSummary {
  id: string;
  preset_name: string;
  status: string;
  created_at: string;
  task_count: number;
  completed_count: number;
}

export interface LLMProviderOption {
  name: string;
  label: string;
  api_key_env?: string | null;
  base_url_env: string;
  default_model: string;
  default_base_url: string;
  base_url_options?: string[];
  api_key_required: boolean;
  auth_type?: string;
  login_command?: string | null;
}

export interface LLMSettings {
  provider: string;
  model_name: string;
  base_url: string;
  api_key_env?: string | null;
  api_key_configured: boolean;
  api_key_hint?: string | null;
  api_key_required: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
  sse_timeout_seconds: number;
  env_path: string;
  providers: LLMProviderOption[];
}

export interface UpdateLLMSettingsRequest {
  provider: string;
  model_name: string;
  base_url: string;
  api_key?: string;
  clear_api_key?: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort?: string;
}

export interface DataSourceSettings {
  tushare_token_configured: boolean;
  tushare_token_hint?: string | null;
  tushare_token_count?: number;
  baostock_supported: boolean;
  baostock_installed: boolean;
  baostock_message: string;
  env_path: string;
}

export interface UpdateDataSourceSettingsRequest {
  tushare_token?: string;
  tushare_tokens?: string;
  clear_tushare_token?: boolean;
}

export interface ChannelAdapterStatus {
  name: string;
  display_name: string;
  configured: boolean;
  enabled: boolean;
  available: boolean;
  loaded: boolean;
  running: boolean;
  error?: string;
  install_hint?: string;
}

export interface ChannelRuntimeStatus {
  running: boolean;
  inbound_queue: number;
  outbound_queue: number;
  session_count: number;
  channels: Record<string, ChannelAdapterStatus>;
}

export interface ChannelRuntimeActionResponse extends ChannelRuntimeStatus {
  status: string;
}

export interface ChannelPairingCommandRequest {
  channel: string;
  command: string;
}

export interface ChannelPairingCommandResponse {
  channel: string;
  reply: string;
}

// --- Types matching backend API contracts ---

export interface RunListItem {
  run_id: string;
  status: string;
  created_at: string;
  prompt?: string;
  /** Live label: session title, else run title, else prompt. */
  title?: string;
  total_return?: number;
  excess_return?: number;
  max_drawdown?: number;
  sharpe?: number;
  codes?: string[];
  start_date?: string;
  end_date?: string;
  /** True when the run has code/signal_engine.py (Monitor can bind it). */
  has_signal_engine?: boolean;
}

/** Strategy pointer for the signal monitor board. */
export interface MonitorStrategyRef {
  type: "run_dir" | "shadow";
  run_id?: string | null;
  shadow_id?: string | null;
}

/** One buy/sell/hold suggestion from POST /monitor/scan. */
export interface MonitorSuggestion {
  symbol: string;
  action: "buy" | "sell" | "hold" | string;
  /** Strategy-implied stance from latest signal (not broker holdings). */
  position?: "long" | "flat" | "short" | "unknown" | string;
  reason: string;
  /** Monitoring decision date (scan day). */
  as_of: string;
  /** Latest OHLCV/signal bar actually used (may lag as_of). */
  data_as_of?: string | null;
  /** Latest close from the OHLCV bar used in the scan. */
  price?: number | null;
  /** True when today's bar was synthesized from a live quote. */
  provisional?: boolean;
  confidence?: number | null;
  signal?: number | null;
}

/** One strategy-implied holding row from the latest (or historical) scan. */
export interface MonitorHolding {
  symbol: string;
  weight: number;
  action: "enter" | "exit" | "hold" | "add" | "trim" | string;
  price?: number | null;
  data_as_of?: string | null;
  provisional?: boolean;
  prev_weight?: number | null;
  /** Dual-track plan. Missing on older holdings snapshots. */
  trend?: MonitorForecastTrendPlan | null;
}

/** Day-over-day portfolio churn vs the previous as_of snapshot. */
export interface MonitorDayOps {
  entered: string[];
  exited: string[];
  held: string[];
}

/** Official process instruction after Mandate + overlay compose. */
export interface ProcessTraceStep {
  gate: "mandate" | "structure" | "f0" | "analog" | "size" | string;
  title: string;
  thought: string;
  conclusion: string;
  passed?: boolean;
  skipped?: boolean;
  veto?: string | null;
}

export interface ProcessSymbolDecision {
  symbol: string;
  action: string;
  weight: number;
  raw_weight: number;
  raw_action: string;
  structure_action?: string | null;
  analog_bias?: string | null;
  blocked?: boolean;
  block_reason?: string | null;
}

export interface ProcessDecision {
  action: "wait" | "hold" | "enter" | "add" | "trim" | "exit" | string;
  size_nav: number;
  reason: string;
  as_of?: string | null;
  mandate_hash?: string;
  analog_applied?: boolean;
  vetoed_by?: string[];
  per_symbol?: ProcessSymbolDecision[];
  trace?: ProcessTraceStep[];
  overlays_raw?: Record<string, unknown>;
}

/**
 * Response of GET/PUT /monitor and POST /monitor/scan.
 * One board per strategy source: symbols + cached scan results are not shared.
 */
export interface MonitorBoard {
  symbols: string[];
  strategy: MonitorStrategyRef;
  last_scan_at?: string | null;
  suggestions: MonitorSuggestion[];
  holdings?: MonitorHolding[];
  ops?: MonitorDayOps;
  schedule?: string | null;
  decision?: ProcessDecision | null;
}

export interface MonitorHistorySummary {
  as_of: string;
  scanned_at: string;
  run_id: string;
  provisional?: boolean;
  holding_count: number;
  entered_count: number;
  exited_count: number;
  held_count: number;
  universe_size?: number;
  fetch_error_count?: number;
}

export interface MonitorHistoryDetail {
  as_of: string;
  scanned_at: string;
  run_id: string;
  provisional?: boolean;
  holdings: MonitorHolding[];
  ops: MonitorDayOps;
  suggestions?: MonitorSuggestion[];
  universe_size?: number;
  fetch_error_count?: number;
  decision?: ProcessDecision | null;
}

/** One OHLC bar on a monitor forecast chart (realized or projected). */
export interface MonitorForecastBar {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export interface MonitorForecastMaBundle {
  ma5: Array<number | null>;
  ma10: Array<number | null>;
  ma20: Array<number | null>;
}

export interface MonitorForecastAssessment {
  bias: "up" | "down" | "neutral" | string;
  style_bucket: "high" | "medium" | "low" | string;
  ma_structure: "bull" | "bear" | "mixed" | string;
  analog_count: number;
  up_count: number;
  down_count: number;
  /** Vote conviction × style match; older saved snapshots may omit this. */
  confidence?: number;
  /** False = show scenario paths only, no directional call. Missing on old saves. */
  called?: boolean;
  /** CSI 300 reverse veto: market_disagree. Legacy saves may still send market_abstain. */
  market_veto?: string | null;
}

export interface MonitorForecastItem {
  symbol: string;
  weight: number;
  price?: number | null;
  up_prob: number;
  down_prob: number;
  style_score: number;
  assessment: MonitorForecastAssessment;
  history: MonitorForecastBar[];
  up_bars: MonitorForecastBar[];
  down_bars: MonitorForecastBar[];
  ma: {
    history: MonitorForecastMaBundle;
    up: MonitorForecastMaBundle;
    down: MonitorForecastMaBundle;
  };
  error?: string | null;
  /** Long-only major-trend plan. Missing on older saved forecasts. */
  trend?: MonitorForecastTrendPlan | null;
}

export interface MonitorForecastTrendPlan {
  market_regime: "bull" | "chop" | "bear" | string;
  stock_regime: "bull" | "chop" | "bear" | string;
  swing: "oversold" | "mid" | "overbought" | string;
  action: "wait" | "buy_dip" | "hold" | "sell_rally" | "reduce" | string;
  fade_rallies: boolean;
  reason: string;
}

export interface IndexHorizons {
  short?: string | null;
  medium?: string | null;
  long?: string | null;
  flip?: string | null;
  close?: number | null;
  ma5?: number | null;
  ma10?: number | null;
  ma20?: number | null;
  ma60?: number | null;
  ma120?: number | null;
  ma250?: number | null;
  mom120?: number | null;
}

export interface MonitorForecastTrendBook {
  market_regime?: "bull" | "chop" | "bear" | string | null;
  mom120?: number | null;
  rule?: string;
  horizons?: IndexHorizons | null;
}

/** POST /monitor/forecast — analog up/down paths for active holdings. */
export interface MonitorForecastReport {
  as_of?: string | null;
  horizon: number;
  holding_count: number;
  evaluated_count: number;
  capped: boolean;
  bias: "up" | "down" | "neutral" | string;
  /** Names that issued a directional call; 0 → abstain at book level. */
  called_count?: number;
  /** CSI 300 analog used as a strong reverse veto. */
  market?: MonitorForecastMarket | null;
  /** Long-only major-trend overlay. Missing on older saved forecasts. */
  trend?: MonitorForecastTrendBook | null;
  items: MonitorForecastItem[];
  decision?: ProcessDecision | null;
}

export interface MonitorForecastMarket {
  symbol: string;
  up_prob: number;
  down_prob: number;
  style_score: number;
  confidence: number;
  bias: string;
  called: boolean;
  /** True only when CSI 300 is lopsided, confident, and a large analog move. */
  decisive?: boolean;
  /** Favored-side 10-day analog return (signed). */
  impact?: number;
  analog_count: number;
  error?: string | null;
  /** MA20/60/120 + 120d structure. Missing on older saved forecasts. */
  regime?: "bull" | "chop" | "bear" | string | null;
  mom120?: number | null;
  horizons?: IndexHorizons | null;
}

export interface MonitorForecastSaveSummary {
  id: string;
  saved_at: string;
  run_id: string;
  as_of?: string | null;
  bias: string;
  holding_count: number;
  evaluated_count: number;
}

export interface MonitorForecastSave extends MonitorForecastReport {
  id: string;
  saved_at: string;
  run_id: string;
}

/** One curated 「最优」 sector recipe from GET /sector-pick. */
export interface SectorPickCatalogItem {
  key: string;
  name: string;
  panel: string;
  confirm_ids: string[];
  use_max_veto: boolean;
  run_id: string;
  prop_file: string;
  variant: "f0_prime" | "f1_max" | string;
  /** Monitor strategy-source title (req.json rename). Board ``name`` is unchanged. */
  run_title?: string | null;
  run_missing?: boolean;
}

export interface SectorPickScore {
  key: string;
  name: string;
  panel: string;
  run_id: string;
  variant: string;
  enabled: boolean;
  timing_mult?: number | null;
  ew_ma20_ratio?: number | null;
  ret_20d?: number | null;
  confirm_pass_rate?: number | null;
  live_score?: number | null;
  rank?: number | null;
  universe_size?: number;
  fetch_error_count?: number;
  excess_return?: number | null;
  sharpe?: number | null;
  error?: string | null;
  holding_count?: number | null;
  max_weight?: number | null;
  book_healthy?: boolean | null;
  skip_reason?: string | null;
  rs_n?: number;
  dip_n?: number;
  knife_n?: number;
  wait_n?: number;
  sell_n?: number;
  rs_share?: number | null;
  knife_share?: number | null;
}

export interface SectorPickResult {
  as_of: string;
  scanned_at: string;
  recommended_key?: string | null;
  holdings_key?: string | null;
  stance: "full" | "half_watch" | "none" | string;
  reason: string;
  sectors: SectorPickScore[];
  holdings: MonitorHolding[];
  ops?: MonitorDayOps;
  holdings_run_id?: string | null;
  provisional?: boolean;
  market_regime?: "bull" | "chop" | "bear" | string | null;
  mom120?: number | null;
  decision?: ProcessDecision | null;
  locked_key?: string | null;
  map_key?: string | null;
  challenger_key?: string | null;
  challenger_days?: number;
  challenger_as_of?: string | null;
  lock_event?: string | null;
  lock_reason?: string | null;
  horizons?: IndexHorizons | null;
}

export interface SectorPickBoard {
  enabled_keys: string[];
  catalog: SectorPickCatalogItem[];
  result?: SectorPickResult | null;
}

export interface SectorPickHistorySummary {
  as_of: string;
  scanned_at: string;
  recommended_key?: string | null;
  holdings_key?: string | null;
  stance?: string;
  provisional?: boolean;
  holding_count: number;
  entered_count: number;
  exited_count: number;
  held_count: number;
}

export interface SectorPickHistorySeriesPoint {
  key: string;
  name: string;
  rank?: number | null;
  ret_20d?: number | null;
  confirm_pass_rate?: number | null;
  timing_mult?: number | null;
  live_score?: number | null;
}

export interface SectorPickHistorySeriesDay {
  as_of: string;
  recommended_key?: string | null;
  stance?: string;
  sectors: SectorPickHistorySeriesPoint[];
}

export interface SectorPickHistorySeries {
  days: SectorPickHistorySeriesDay[];
}

export interface MarketPickBoardScore {
  bk: string;
  name: string;
  board_type: string;
  classified?: number;
  universe_size?: number;
  rs_n?: number;
  dip_n?: number;
  knife_n?: number;
  wait_n?: number;
  sell_n?: number;
  rs_share?: number | null;
  knife_share?: number | null;
  live_score?: number | null;
  rank?: number | null;
  ret_20d?: number | null;
  timing_mult?: number | null;
  ew_ma20_ratio?: number | null;
  skip_reason?: string | null;
  catalog_key?: string | null;
  catalog_name?: string | null;
}

export interface MarketPickResult {
  as_of: string;
  scanned_at: string;
  market_regime?: "bull" | "chop" | "bear" | string | null;
  mom120?: number | null;
  stance?: string;
  reason?: string;
  recommended_bk?: string | null;
  map_bk?: string | null;
  unique_symbols?: number;
  ohlcv_ok?: number;
  fetch_error_count?: number;
  boards: MarketPickBoardScore[];
  horizons?: IndexHorizons | null;
}

export interface MarketPickBoard {
  result?: MarketPickResult | null;
  cached?: boolean;
}

export interface MarketPickHistorySummary {
  as_of: string;
  scanned_at: string;
  recommended_bk?: string | null;
  recommended_name?: string | null;
  map_bk?: string | null;
  stance?: string;
  market_regime?: string | null;
  board_count: number;
  unique_symbols?: number;
  ohlcv_ok?: number;
  fetch_error_count?: number;
}

/** PUT /monitor — changing strategy switches boards; symbols apply to the target board only. */
export interface UpdateMonitorRequest {
  symbols?: string[];
  strategy?: MonitorStrategyRef;
  schedule?: string | null;
}

export interface RunDetailParams {
  chart_payload?: "summary";
  chart_symbol?: string;
}

export interface PriceBar {
  time: string;
  timestamp?: string;
  code?: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  time: string;
  timestamp?: string;
  code?: string;
  side: "BUY" | "SELL";
  price: number;
  qty?: number;
  reason?: string;
  text?: string;
}

export interface EquityPoint {
  time: string;
  equity: string | number;
  drawdown: string | number;
}

export interface ValidationData {
  monte_carlo?: {
    actual_sharpe: number;
    actual_max_dd: number;
    p_value_sharpe: number;
    p_value_max_dd: number;
    simulated_sharpe_mean: number;
    simulated_sharpe_std: number;
    simulated_sharpe_p5: number;
    simulated_sharpe_p95: number;
    n_simulations: number;
    n_trades: number;
    error?: string;
  };
  bootstrap?: {
    observed_sharpe: number;
    ci_lower: number;
    ci_upper: number;
    median_sharpe: number;
    prob_positive: number;
    confidence: number;
    n_bootstrap: number;
    error?: string;
  };
  walk_forward?: {
    n_windows: number;
    windows: Array<{
      window: number;
      start: string;
      end: string;
      return: number;
      sharpe: number;
      max_dd: number;
      trades: number;
      win_rate: number;
    }>;
    profitable_windows: number;
    consistency_rate: number;
    return_mean: number;
    return_std: number;
    sharpe_mean: number;
    sharpe_std: number;
    error?: string;
  };
}

export interface SymbolPnLRow {
  code: string;
  count: number;
  win_rate: number;
  total_pnl: number;
  avg_pnl: number;
  avg_return_pct: number;
  avg_holding_days: number;
  contribution_pct: number;
}

export interface ExitReasonRow {
  reason: string;
  count: number;
  total_pnl: number;
  avg_pnl: number;
  win_rate: number;
}

export interface StrategyAssessmentCheck {
  id: string;
  label: string;
  status: "pass" | "warn" | "fail" | "skip" | string;
  detail: string;
  score: number;
}

export interface StrategyAssessment {
  score: number;
  grade: "excellent" | "good" | "fair" | "poor" | string;
  summary: string;
  checks: StrategyAssessmentCheck[];
  highlights?: {
    top_winners?: Array<Record<string, string | number>>;
    top_losers?: Array<Record<string, string | number>>;
    holding_buckets?: Array<{ bucket: string; count: number; total_pnl: number }>;
    symbol_count?: number;
    exit_count?: number;
  };
}

export interface RunData {
  status: string;
  run_id: string;
  prompt?: string;
  elapsed_seconds?: number;
  run_directory?: string;
  run_stage?: string;
  run_context?: Record<string, unknown>;

  metrics?: BacktestMetrics;
  artifacts?: ArtifactInfo[];
  run_card?: RunCard;
  validation?: ValidationData;
  by_symbol?: SymbolPnLRow[];
  by_exit_reason?: ExitReasonRow[];
  strategy_assessment?: StrategyAssessment;

  chart_symbols?: string[];
  price_series?: Record<string, PriceBar[]>;
  indicator_series?: Record<string, Record<string, IndicatorPoint[]>>;
  trade_markers?: TradeMarker[];
  equity_curve?: EquityPoint[];
  trade_log?: Array<Record<string, string>>;
  run_logs?: Array<{ source?: string; line_number?: number; message?: string }>;
}

export interface RunCard {
  schema_version?: string;
  generated_at?: string;
  run_dir?: string;
  backtest?: Record<string, unknown>;
  reproducibility?: Record<string, unknown>;
  data_sources?: string[];
  metrics?: Record<string, unknown>;
  validation?: unknown;
  warnings?: string[];
  artifacts?: RunCardArtifact[];
  [key: string]: unknown;
}

export interface RunCardArtifact {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface BacktestMetrics {
  final_value: number;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  [key: string]: number;
}


export interface IndicatorPoint {
  time: string;
  value: number;
}

export interface ArtifactInfo {
  name: string;
  path: string;
  type: string;
  size: number;
  exists: boolean;
}

export interface PineScriptResult {
  exists: boolean;
  content: string | null;
}

export interface SessionItem {
  session_id: string;
  title?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  last_attempt_id?: string;
}

// --- Goal types ---

export type GoalStatus =
  | "active"
  | "paused"
  | "waiting_user"
  | "needs_refresh"
  | "insufficient_evidence"
  | "compliance_blocked"
  | "blocked"
  | "budget_limited"
  | "usage_limited"
  | "complete"
  | "cancelled"
  | "superseded";

export type GoalRiskTier =
  | "research_general"
  | "market_specific_short_term"
  | "personalized_advice_or_position_sizing";

export interface GoalRecord {
  goal_id: string;
  session_id: string;
  status: GoalStatus;
  objective: string;
  ui_summary: string;
  source: string;
  protocol: string;
  risk_tier: GoalRiskTier;
  token_budget?: number | null;
  tokens_used: number;
  turn_budget?: number | null;
  turns_used: number;
  time_budget_seconds?: number | null;
  time_used_seconds: number;
  budget_wrapup_sent: boolean;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  recap?: string | null;
}

export interface GoalClaim {
  claim_id: string;
  goal_id: string;
  session_id: string;
  claim_type: string;
  text: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface GoalCriterion {
  criterion_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  required: boolean;
  status: string;
  freshness_requirement?: string | null;
  protocol_step?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GoalEvidence {
  evidence_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe: string[];
  benchmark: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  retrieved_at: string;
  data_as_of?: string | null;
  freshness_status: string;
  verification_status: string;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids: string[];
  created_at: string;
}

export interface GoalSnapshot {
  goal: GoalRecord;
  claims: GoalClaim[];
  criteria: GoalCriterion[];
  evidence: GoalEvidence[];
  evidence_count: number;
}

export interface CreateGoalRequest {
  objective: string;
  criteria?: string[];
  ui_summary?: string;
  protocol?: string;
  risk_tier?: GoalRiskTier;
  token_budget?: number;
  turn_budget?: number;
  time_budget_seconds?: number;
}

export interface AddGoalEvidenceRequest {
  goal_id: string;
  expected_goal_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type?: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe?: string[];
  benchmark?: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions?: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  data_as_of?: string | null;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids?: string[];
}

export interface UpdateGoalRequest {
  goal_id: string;
  expected_goal_id: string;
  objective?: string;
  ui_summary?: string;
}

export interface UpdateGoalResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

export interface AddGoalEvidenceResponse {
  evidence: GoalEvidence;
  snapshot: GoalSnapshot;
}

export interface GoalAuditRowRequest {
  criterion_id: string;
  result: string;
  evidence_ids?: string[];
  notes?: string;
}

export interface UpdateGoalStatusRequest {
  goal_id: string;
  expected_goal_id: string;
  status: GoalStatus;
  audit?: GoalAuditRowRequest[];
  recap?: string | null;
}

export interface UpdateGoalStatusResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

// --- Alpha Zoo types ---

/** Last full-zoo bench survival: alive / invalid(失效) / unknown(未评测). */
export type AlphaAliveStatus = "alive" | "invalid" | "unknown";

export interface AlphaListParams {
  zoo?: string;
  theme?: string;
  universe?: string;
  /** Filter by persisted bench survival status. */
  alive_status?: AlphaAliveStatus;
  limit?: number;
}

export interface AlphaSummary {
  id: string;
  zoo: string;
  theme: string[];
  universe: string[];
  nickname?: string;
  decay_horizon?: number | null;
  min_warmup_bars?: number | null;
  requires_sector?: boolean;
  /** Latest catalogue survival from the last full-zoo bench. */
  alive_status?: AlphaAliveStatus;
  /** Raw bench bucket: alive | reversed | dead. */
  bench_category?: "alive" | "reversed" | "dead" | null;
  bench_universe?: string | null;
  bench_period?: string | null;
  bench_updated_at?: string | null;
}

export interface AlphaListResponse {
  status: string;
  alphas: AlphaSummary[];
  total: number;
  returned: number;
  truncated: boolean;
}

export interface AlphaDetail {
  id: string;
  zoo: string;
  module_path?: string;
  meta: Record<string, unknown>;
  alive_status?: AlphaAliveStatus;
  bench_category?: "alive" | "reversed" | "dead" | null;
  bench_universe?: string | null;
  bench_period?: string | null;
  bench_updated_at?: string | null;
}

export interface AlphaDetailResponse {
  status: string;
  alpha: AlphaDetail;
  source_code: string;
}

export interface AlphaBenchRequest {
  zoo: string;
  universe: string;
  period: string;
  top?: number;
}

export interface AlphaBenchTopRow {
  id: string;
  ic_mean: number;
  ir: number;
  theme: string[];
  formula_latex: string;
  category: "alive" | "reversed" | "dead";
}

export interface AlphaBenchResult {
  alive: number;
  reversed: number;
  dead: number;
  skipped?: number;
  top5_by_ir: AlphaBenchTopRow[];
  /** Every survivor (IR-sorted), not capped — for the full alive table. */
  alive_list?: AlphaBenchTopRow[];
  dead_examples: AlphaBenchTopRow[];
  by_theme: Record<string, { alive: number; reversed: number; dead: number }>;
}

export interface AlphaCompareRequest {
  alpha_ids: string[];
  universe: string;
  period: string;
  /** One of: ir | ic_mean | ic_positive_ratio | ic_count (default ir). */
  sort?: string;
}

export interface AlphaCompareRow {
  rank: number;
  id: string;
  zoo: string;
  ic_mean: number;
  ic_std: number;
  ir: number;
  ic_positive_ratio: number;
  ic_count: number;
  /** `delta_<sort>_vs_best` — gap to the top-ranked alpha on the active metric. */
  [deltaKey: string]: number | string;
}

export interface AlphaCompareSkip {
  id: string;
  reason: string;
}

export interface AlphaCompareResult {
  universe: string;
  period: string;
  sort: string;
  n_compared: number;
  n_skipped: number;
  winner: string;
  ranking: AlphaCompareRow[];
  skipped: AlphaCompareSkip[];
}

// --- Connector runtime channel types ---

/** One mandate profile inside a `mandate.proposal` event (SPEC Consent §1). */
export interface MandateProfile {
  ordinal: number;
  label: string;
  /** Concrete ticker list, or a structural universe descriptor (e.g. "tech_sector"). */
  universe: string[] | string;
  max_order_usd: number;
  daily_trade_cap: number;
  /** "none" for cash-only, otherwise a leverage descriptor/multiple. */
  leverage: string | number;
  instruments: string[];
  notes?: string;
}

/** Account block of a `mandate.proposal` event. */
export interface MandateProposalAccount {
  broker: string;
  type: string;
  funded_by: string;
}

/** Payload of the `mandate.proposal` SSE event (SPEC Consent §1). */
export interface MandateProposal {
  type?: string;
  proposal_id: string;
  session_id?: string;
  intent_normalized?: string;
  account?: MandateProposalAccount;
  ceilings_ref?: string;
  profiles: MandateProfile[];
  funding_note?: string;
  halt_note?: string;
  /** Present only when this proposal was triggered by a mandate breach (SPEC Consent §3). */
  reauth_for?: { breach_id?: string } | null;
}

/** Payload of the `mandate.committed` SSE event (SPEC Consent §1 COMMIT). */
export interface MandateCommitted {
  proposal_id?: string;
  mandate_id?: string;
  consent_record_id?: string;
  selected_ordinal?: number;
  broker?: string;
  /** Resolved limits, surfaced for the compact active-mandate badge. */
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

/** Payload of the `live.halted` SSE event (SPEC Consent §4). */
export interface LiveHalted {
  broker?: string | null;
  tripped_at?: string;
  by?: string;
  reason?: string;
}

/** Payload of the `live.action` SSE event (SPEC Consent §5 audit notify). */
export interface LiveAction {
  audit_id?: string;
  ts?: string;
  kind: string;
  intent_normalized?: string;
  outcome?: string;
  broker?: string;
  remote_tool?: string;
  error?: string | null;
}

export interface CommitMandateRequest {
  broker: string;
  proposal_id: string;
  selected_ordinal: number;
  /** Present only on the adjust path (SPEC Consent §3); null otherwise. */
  adjustments?: Record<string, unknown> | null;
  /** Explicit affirmative consent; the surface sets it on the user's click. */
  consent_ack: boolean;
  session_id?: string;
  account_ref?: string;
  lifetime_days?: number;
}

export interface CommitMandateResponse {
  mandate_id: string;
  consent_record_id: string;
  selected_ordinal?: number;
  broker?: string;
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

export interface HaltLiveResponse {
  halted: boolean;
  broker?: string | null;
  reason: string;
  sentinel: string;
}

export interface LiveAuthorizeRequest {
  broker: string;
}

export interface LiveAuthorizeResponse {
  broker: string;
  connector_profile: string;
  oauth_token_present: boolean;
  instruction: string;
  note?: string;
}

/** Mandate limits surfaced inside a `GET /live/status` broker entry (SPEC §7.5). */
export interface LiveMandateLimits {
  max_order_notional_usd?: number;
  max_total_exposure_usd?: number;
  max_leverage?: number;
  max_trades_per_day?: number;
  allowed_instruments?: string[];
  account_funding_usd?: number;
  [key: string]: unknown;
}

/** Active mandate block of a `GET /live/status` broker entry. */
export interface LiveMandateStatus {
  broker?: string;
  mandate_id?: string;
  account_ref?: string;
  created_at?: string;
  limits?: LiveMandateLimits;
  /** ISO timestamp the mandate auto-expires (SPEC §7.5 #7 proactive expiry). */
  expires_at?: string;
  expires_in_seconds?: number | null;
  expired?: boolean;
}

/** Runner liveness block of a `GET /live/status` broker entry (SPEC §7.5 #3). */
export interface LiveRunnerLiveness {
  broker?: string;
  alive: boolean;
  /** Unix epoch seconds of the last heartbeat tick; null if the runner never started. */
  last_tick?: number | string | null;
  last_tick_age_seconds?: number | null;
}

export interface LiveBrokerAuthStatus {
  broker: string;
  oauth_token_present: boolean;
  is_live_broker: boolean;
  /** Optional during rolling upgrades from OAuth-only runtime responses. */
  profile_id?: string | null;
  transport?: string | null;
  connection_state?: string | null;
  configured?: boolean | null;
  credential_source?: string | null;
  sdk_installed?: boolean | null;
  last_checked_at?: string | null;
  environment_identity?: string | null;
  readonly?: boolean | null;
  capabilities?: string[] | null;
  error_code?: string | null;
  error?: string | null;
}

export interface ConnectorVerifyResponse {
  status?: string;
  profile_id?: string | null;
  transport?: string | null;
  connection_state?: string | null;
  configured?: boolean | null;
  credential_source?: string | null;
  sdk_installed?: boolean | null;
  last_checked_at?: string | null;
  environment_identity?: string | null;
  readonly?: boolean | null;
  capabilities?: string[] | null;
  error_code?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

/** One broker entry in the `GET /live/status` response. */
export interface LiveBrokerStatus {
  auth: LiveBrokerAuthStatus;
  mandate?: LiveMandateStatus | null;
  runner: LiveRunnerLiveness;
  halted: boolean;
}

/** Response of `GET /live/status` (SPEC §7.5 runner status panel + C2). */
export interface LiveStatus {
  brokers: LiveBrokerStatus[];
  global_halted: boolean;
}

/** Response of `POST /live/runner/start|stop`. */
export interface LiveRunnerResponse {
  broker: string;
  started?: boolean;
  already_running?: boolean;
  stopped?: boolean;
  was_running?: boolean;
}

export interface MessageItem {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
  linked_attempt_id?: string;
  metadata?: Record<string, unknown>;
}
