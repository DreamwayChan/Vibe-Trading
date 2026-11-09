/**
 * Display helpers for the Monitor cost-compare and paper-book panels.
 *
 * Numbers come from the existing artifacts. Cost delta is
 * ``metrics.delta`` (ashare_costs − frictionless). Unrealized is the open
 * book's mark minus stored lot cost, the same lots ``BookState`` already
 * persists. Nothing here re-runs a backtest.
 */

export const SOAK_SESSIONS = 20;

export interface CostMetricRow {
  nav?: number | null;
  final_value?: number | null;
  total_return?: number | null;
  max_drawdown?: number | null;
  trade_count?: number | null;
  fill_count?: number | null;
  unfill_count?: number | null;
}

export interface CostCompareReport {
  status?: string;
  production_default?: string;
  delta_definition?: string;
  metrics?: {
    frictionless?: CostMetricRow;
    ashare_costs?: CostMetricRow;
    delta?: CostMetricRow;
  };
}

export interface PaperLot {
  shares?: number;
  price?: number;
}

export interface PaperPosition {
  symbol?: string;
  last_price?: number | null;
  lots?: PaperLot[];
}

export interface PaperBookState {
  cash?: number;
  initial_cash?: number;
  positions?: Record<string, PaperPosition>;
  settled_through?: string | null;
}

export interface PaperBookDecay {
  available?: boolean;
  decay?: number | null;
  decayed?: boolean;
  reason?: string | null;
  recent_sharpe?: number | null;
  earlier_sharpe?: number | null;
}

export interface PaperBookWindow {
  nav?: number | null;
  cash?: number | null;
  market_value?: number | null;
  session_count?: number | null;
  start?: string;
  end?: string;
  return_deviation?: number | null;
  max_return_deviation?: number | null;
  cum_return?: number | null;
  decay?: PaperBookDecay;
  downgraded?: boolean;
  downgrade_reason?: string | null;
}

export interface PaperBookSnapshot {
  state?: PaperBookState | null;
  report?: { nav?: number | null } | null;
  window?: PaperBookWindow | null;
}

export type CostGlance = { kind: "ate" | "ahead" | "flat"; pct: string };

function finite(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && value.trim() === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

/** Absolute percent text, e.g. 0.018 → "1.80%". */
export function formatAbsPct(fraction: number): string {
  return `${(Math.abs(fraction) * 100).toFixed(2)}%`;
}

export function formatPctOrDash(fraction: number | null | undefined): string {
  const number = finite(fraction);
  if (number == null) return "—";
  const pct = number * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

export function formatMoney(value: number | null | undefined): string {
  const number = finite(value);
  if (number == null) return "—";
  return number.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * ``delta.total_return`` is after-cost minus zero-cost.
 * A negative figure is the cost tax ("costs ate X%").
 */
export function costGlance(deltaReturn: number | null | undefined): CostGlance | null {
  const number = finite(deltaReturn);
  if (number == null) return null;
  if (Math.abs(number) < 1e-12) return { kind: "flat", pct: "0.00%" };
  if (number < 0) return { kind: "ate", pct: formatAbsPct(number) };
  return { kind: "ahead", pct: formatAbsPct(number) };
}

/** Open P&L: shares × (last mark − lot price), summed across stored lots. */
export function unrealizedPnl(state: PaperBookState | null | undefined): number | null {
  const positions = state?.positions;
  if (!positions || typeof positions !== "object") return null;
  let total = 0;
  let saw = false;
  for (const pos of Object.values(positions)) {
    if (!pos || !Array.isArray(pos.lots) || pos.lots.length === 0) continue;
    const mark = finite(pos.last_price);
    if (mark == null) continue;
    for (const lot of pos.lots) {
      const shares = finite(lot?.shares);
      const price = finite(lot?.price);
      if (shares == null || price == null) continue;
      total += shares * (mark - price);
      saw = true;
    }
  }
  return saw ? total : null;
}

/**
 * Prefer the window NAV the soak already wrote. Otherwise the daily report.
 * Last resort matches ``book._nav``: cash plus shares × last mark.
 */
export function paperNav(snapshot: PaperBookSnapshot | null | undefined): number | null {
  const windowNav = finite(snapshot?.window?.nav);
  if (windowNav != null) return windowNav;
  const reportNav = finite(snapshot?.report?.nav);
  if (reportNav != null) return reportNav;
  const state = snapshot?.state;
  if (!state) return null;
  const cash = finite(state.cash);
  if (cash == null) return null;
  let market = 0;
  for (const pos of Object.values(state.positions || {})) {
    const mark = finite(pos?.last_price);
    if (mark == null || !Array.isArray(pos?.lots)) continue;
    let shares = 0;
    for (const lot of pos.lots) {
      const count = finite(lot?.shares);
      if (count != null) shares += count;
    }
    market += shares * mark;
  }
  return cash + market;
}

export function decayFigure(decay: PaperBookDecay | null | undefined): number | null {
  if (!decay || decay.available === false) return null;
  return finite(decay.decay);
}
