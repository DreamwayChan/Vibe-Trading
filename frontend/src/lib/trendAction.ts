/** Long-only dual-track actions. Missing trend ranks last so old payloads keep order. */

export const TREND_ACTION_ORDER = ["hold", "buy_dip", "sell_rally", "wait", "reduce"] as const;

export type TrendAction = (typeof TREND_ACTION_ORDER)[number];

export function trendActionRank(action: string | null | undefined): number {
  const index = TREND_ACTION_ORDER.indexOf(action as TrendAction);
  return index < 0 ? TREND_ACTION_ORDER.length : index;
}

const TREND_ACTION_I18N = {
  buy_dip: "monitor.forecastActionBuyDip",
  hold: "monitor.forecastActionHold",
  sell_rally: "monitor.forecastActionSellRally",
  reduce: "monitor.forecastActionReduce",
  wait: "monitor.forecastActionWait",
} as const;

/** Native `title` glossary for hold / dip / rally / wait / knife (reduce). */
const TREND_ACTION_HINT_I18N = {
  buy_dip: "monitor.hintBuyDip",
  hold: "monitor.hintHold",
  sell_rally: "monitor.hintSellRally",
  reduce: "monitor.hintReduce",
  wait: "monitor.hintWait",
} as const;

export type TrendActionI18nKey = (typeof TREND_ACTION_I18N)[keyof typeof TREND_ACTION_I18N];
export type TrendActionHintI18nKey = (typeof TREND_ACTION_HINT_I18N)[keyof typeof TREND_ACTION_HINT_I18N];

export function trendActionI18nKey(action: string | null | undefined): TrendActionI18nKey | null {
  if (action === "buy_dip" || action === "hold" || action === "sell_rally" || action === "reduce" || action === "wait") {
    return TREND_ACTION_I18N[action];
  }
  return null;
}

export function trendActionHintI18nKey(action: string | null | undefined): TrendActionHintI18nKey | null {
  if (action === "buy_dip" || action === "hold" || action === "sell_rally" || action === "reduce" || action === "wait") {
    return TREND_ACTION_HINT_I18N[action];
  }
  return null;
}

export function trendActionClass(action: string | null | undefined): string {
  if (action === "buy_dip") return "text-emerald-600 dark:text-emerald-400";
  if (action === "hold") return "text-foreground";
  if (action === "sell_rally" || action === "reduce") {
    return "text-rose-600 dark:text-rose-400";
  }
  return "text-muted-foreground";
}

type Trendish = {
  symbol?: string;
  weight?: number | null;
  trend?: { action?: string } | null;
};

/** hold → buy_dip → sell_rally → wait → reduce, then heavier weight. Stable for missing trend. */
export function sortByTrendAction<T extends Trendish>(rows: T[]): T[] {
  return [...rows].sort((left, right) => {
    const byAction = trendActionRank(left.trend?.action) - trendActionRank(right.trend?.action);
    if (byAction !== 0) return byAction;
    const byWeight = Math.abs(right.weight ?? 0) - Math.abs(left.weight ?? 0);
    if (byWeight !== 0) return byWeight;
    return String(left.symbol || "").localeCompare(String(right.symbol || ""));
  });
}

const REGIME_TREND_I18N = {
  bull: "monitor.forecastTrendBull",
  bear: "monitor.forecastTrendBear",
  chop: "monitor.forecastTrendChop",
} as const;

/** Longer hover text for the index-regime banner (same rules, more detail). */
const REGIME_TREND_HINT_I18N = {
  bull: "monitor.hintTrendBull",
  bear: "monitor.hintTrendBear",
  chop: "monitor.hintTrendChop",
} as const;

export type RegimeTrendI18nKey = (typeof REGIME_TREND_I18N)[keyof typeof REGIME_TREND_I18N];
export type RegimeTrendHintI18nKey = (typeof REGIME_TREND_HINT_I18N)[keyof typeof REGIME_TREND_HINT_I18N];

export function regimeTrendI18nKey(regime: string | null | undefined): RegimeTrendI18nKey | null {
  if (regime === "bull" || regime === "bear" || regime === "chop") {
    return REGIME_TREND_I18N[regime];
  }
  return null;
}

export function regimeTrendHintI18nKey(regime: string | null | undefined): RegimeTrendHintI18nKey | null {
  if (regime === "bull" || regime === "bear" || regime === "chop") {
    return REGIME_TREND_HINT_I18N[regime];
  }
  return null;
}

export type HorizonLeg = "bull" | "chop" | "bear";

export interface IndexHorizons {
  short?: string | null;
  medium?: string | null;
  long?: string | null;
  flip?: string | null;
  close?: number | null;
  ma60?: number | null;
  ma250?: number | null;
}

/** i18n keys for the always-visible "what this means" line under each pill. */
const HORIZON_MEANING = {
  short: {
    bull: "monitor.horizonMeaningShortBull",
    chop: "monitor.horizonMeaningShortChop",
    bear: "monitor.horizonMeaningShortBear",
  },
  medium: {
    bull: "monitor.horizonMeaningMediumBull",
    chop: "monitor.horizonMeaningMediumChop",
    bear: "monitor.horizonMeaningMediumBear",
  },
  long: {
    bull: "monitor.horizonMeaningLongBull",
    chop: "monitor.horizonMeaningLongChop",
    bear: "monitor.horizonMeaningLongBear",
  },
} as const;

export type HorizonMeaningI18nKey =
  (typeof HORIZON_MEANING)[keyof typeof HORIZON_MEANING][HorizonLeg];

/** Combination headlines live in the UI so English boards are not stuck with a Chinese `flip`. */
const HORIZON_HEADLINE = {
  mediumLongBear: "monitor.horizonHeadlineMediumLongBear",
  mediumBear: "monitor.horizonHeadlineMediumBear",
  mediumBull: "monitor.horizonHeadlineMediumBull",
  shortBearChop: "monitor.horizonHeadlineShortBearChop",
  chop: "monitor.horizonHeadlineChop",
} as const;

const HORIZON_HOW_TO = {
  mediumLongBear: "monitor.horizonHowToMediumLongBear",
  mediumBear: "monitor.horizonHowToMediumBear",
  mediumBull: "monitor.horizonHowToMediumBull",
  shortBearChop: "monitor.horizonHowToShortBearChop",
  chop: "monitor.horizonHowToChop",
} as const;

export type HorizonHeadlineI18nKey = (typeof HORIZON_HEADLINE)[keyof typeof HORIZON_HEADLINE];
export type HorizonHowToI18nKey = (typeof HORIZON_HOW_TO)[keyof typeof HORIZON_HOW_TO];

function asHorizonLeg(value: string | null | undefined): HorizonLeg | null {
  if (value === "bull" || value === "chop" || value === "bear") return value;
  return null;
}

function readNum(rec: Record<string, unknown>, key: string): number | null {
  const value = rec[key];
  if (typeof value === "number" && Number.isFinite(value) && value > 0) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return null;
}

/** Read the short/medium/long blob off a scan payload or overlays_raw. */
export function parseHorizons(raw: unknown): IndexHorizons | null {
  if (!raw || typeof raw !== "object") return null;
  const rec = raw as Record<string, unknown>;
  const short = typeof rec.short === "string" ? rec.short.trim().toLowerCase() : "";
  const medium = typeof rec.medium === "string" ? rec.medium.trim().toLowerCase() : "";
  const long = typeof rec.long === "string" ? rec.long.trim().toLowerCase() : "";
  if (!short && !medium && !long) return null;
  return {
    short: short || null,
    medium: medium || null,
    long: long || null,
    flip: typeof rec.flip === "string" && rec.flip.trim() ? rec.flip.trim() : null,
    close: readNum(rec, "close"),
    ma60: readNum(rec, "ma60"),
    ma250: readNum(rec, "ma250"),
  };
}

export function horizonLegClass(leg: string | null | undefined): string {
  if (leg === "bull") return "text-emerald-700 dark:text-emerald-400";
  if (leg === "bear") return "text-rose-700 dark:text-rose-400";
  return "text-muted-foreground";
}

export function horizonMeaningI18nKey(
  id: "short" | "medium" | "long",
  value: string | null | undefined,
): HorizonMeaningI18nKey | null {
  const state = asHorizonLeg(value);
  if (!state) return null;
  return HORIZON_MEANING[id][state];
}

function horizonCombo(
  horizons: IndexHorizons,
): keyof typeof HORIZON_HEADLINE {
  if (horizons.medium === "bear" && horizons.long === "bear") return "mediumLongBear";
  if (horizons.medium === "bear") return "mediumBear";
  if (horizons.medium === "bull") return "mediumBull";
  if (horizons.short === "bear") return "shortBearChop";
  return "chop";
}

/** One-sentence verdict for the three-leg tape. Independent of backend `flip`. */
export function horizonHeadlineI18nKey(horizons: IndexHorizons): HorizonHeadlineI18nKey {
  return HORIZON_HEADLINE[horizonCombo(horizons)];
}

/** What would change the call — shown under the headline, not instead of it. */
export function horizonHowToI18nKey(horizons: IndexHorizons): HorizonHowToI18nKey {
  return HORIZON_HOW_TO[horizonCombo(horizons)];
}

/** Percent gap from close to a moving average; null when either side is missing. */
export function horizonGapPct(close: number | null | undefined, ma: number | null | undefined): number | null {
  if (close == null || ma == null || close <= 0 || ma <= 0) return null;
  return (close / ma - 1) * 100;
}
