import { describe, expect, it } from "vitest";
import {
  DOWN_SCENARIO_MA,
  UP_SCENARIO_MA,
  buildForecastChartModel,
  scenarioSatelliteSeries,
} from "@/components/charts/ForecastCandlestickChart";
import type { MonitorForecastItem } from "@/lib/api";

function bar(time: string, close: number) {
  return { time, open: close - 1, high: close + 1, low: close - 2, close, volume: 0 };
}

describe("buildForecastChartModel", () => {
  it("aligns history, dual scenario bars, and split MA series", () => {
    const item: Pick<MonitorForecastItem, "history" | "up_bars" | "down_bars" | "ma"> = {
      history: [bar("2024-01-01", 10), bar("2024-01-02", 11)],
      up_bars: [bar("2024-01-03", 12), bar("2024-01-04", 13)],
      down_bars: [bar("2024-01-03", 9), bar("2024-01-04", 8)],
      ma: {
        history: { ma5: [null, 10.5], ma10: [null, null], ma20: [null, null] },
        up: { ma5: [11, 12], ma10: [10, 11], ma20: [9, 10] },
        down: { ma5: [10, 9], ma10: [10, 10], ma20: [10, 10] },
      },
    };
    const model = buildForecastChartModel(item);
    expect(model.dates).toEqual(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]);
    expect(model.historyLen).toBe(2);
    expect(model.historyCandle).toHaveLength(4);
    expect(model.historyCandle[0]).toEqual([9, 10, 8, 11]);
    expect(model.upCustom[0][0]).toBe(2);
    expect(model.downCustom[1][0]).toBe(3);
    expect(model.maHist.ma5).toEqual([null, 10.5, null, null]);
    expect(model.maUp.ma5[1]).toBe(10.5);
    expect(model.maUp.ma5[2]).toBe(11);
    expect(model.maDown.ma5[3]).toBe(9);
  });

  it("returns empty dates when there is no price history", () => {
    const model = buildForecastChartModel({
      history: [],
      up_bars: [],
      down_bars: [],
      ma: {
        history: { ma5: [], ma10: [], ma20: [] },
        up: { ma5: [], ma10: [], ma20: [] },
        down: { ma5: [], ma10: [], ma20: [] },
      },
    });
    expect(model.dates).toEqual([]);
    expect(model.upCustom).toEqual([]);
  });

  it("maps scenario legend clicks to the matching forecast MA series", () => {
    expect(scenarioSatelliteSeries("上涨情景", "上涨情景", "下跌情景")).toEqual([...UP_SCENARIO_MA]);
    expect(scenarioSatelliteSeries("下跌情景", "上涨情景", "下跌情景")).toEqual([...DOWN_SCENARIO_MA]);
    expect(scenarioSatelliteSeries("MA5", "上涨情景", "下跌情景")).toEqual([]);
  });
});
