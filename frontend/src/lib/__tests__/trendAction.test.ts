import { describe, expect, it } from "vitest";
import {
  horizonHeadlineI18nKey,
  horizonMeaningI18nKey,
  parseHorizons,
  sortByTrendAction,
  trendActionHintI18nKey,
  trendActionI18nKey,
  trendActionRank,
} from "@/lib/trendAction";

describe("trendAction helpers", () => {
  it("ranks hold before reduce and missing trend last", () => {
    expect(trendActionRank("hold")).toBeLessThan(trendActionRank("buy_dip"));
    expect(trendActionRank("reduce")).toBeGreaterThan(trendActionRank("wait"));
    expect(trendActionRank(undefined)).toBeGreaterThan(trendActionRank("reduce"));
  });

  it("sorts mixed rows without crashing when trend is missing", () => {
    const ordered = sortByTrendAction([
      { symbol: "AAA.SZ", weight: 0.4, trend: { action: "reduce" } },
      { symbol: "BBB.SZ", weight: 0.1, trend: { action: "hold" } },
      { symbol: "CCC.SZ", weight: 0.2 },
    ]);
    expect(ordered.map((row) => row.symbol)).toEqual(["BBB.SZ", "AAA.SZ", "CCC.SZ"]);
  });

  it("returns null i18n keys for unknown actions", () => {
    expect(trendActionI18nKey("hold")).toBe("monitor.forecastActionHold");
    expect(trendActionI18nKey(undefined)).toBeNull();
    expect(trendActionI18nKey("enter")).toBeNull();
  });

  it("maps dual-track actions to glossary hint keys", () => {
    expect(trendActionHintI18nKey("hold")).toBe("monitor.hintHold");
    expect(trendActionHintI18nKey("buy_dip")).toBe("monitor.hintBuyDip");
    expect(trendActionHintI18nKey("reduce")).toBe("monitor.hintReduce");
    expect(trendActionHintI18nKey(undefined)).toBeNull();
  });
});

describe("horizon copy helpers", () => {
  it("picks the medium+long soft-bear headline", () => {
    const parsed = parseHorizons({
      short: "chop",
      medium: "bear",
      long: "bear",
      close: 4548,
      ma60: 4729,
      ma250: 4671,
    });
    expect(parsed).not.toBeNull();
    expect(horizonHeadlineI18nKey(parsed!)).toBe("monitor.horizonHeadlineMediumLongBear");
    expect(horizonMeaningI18nKey("medium", "bear")).toBe("monitor.horizonMeaningMediumBear");
    expect(horizonMeaningI18nKey("long", "bear")).toBe("monitor.horizonMeaningLongBear");
  });
});
