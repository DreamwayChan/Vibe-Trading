import { describe, expect, it } from "vitest";
import {
  boardKeyFromHighlight,
  boardPairLineStyle,
  findNearestBoardKey,
  formatBoardTooltipHtml,
  formatIdleTooltipHtml,
  seriesIdsForBoard,
  type DayLookup,
} from "@/components/charts/SectorPickHistoryChart";

const nameByKey = new Map([
  ["food", "食品饮料"],
  ["chip", "存储芯片"],
]);

const lookup: DayLookup = {
  "2026-08-29": {
    food: {
      rank: 10,
      ret_20d: 0.082,
      confirm_pass_rate: 0.6,
      timing_mult: 1.2,
      isWinner: false,
    },
    chip: {
      rank: 2,
      ret_20d: 0.31,
      confirm_pass_rate: 0.9,
      timing_mult: 1.5,
      isWinner: true,
    },
  },
};

describe("SectorPickHistoryChart pairing", () => {
  it("maps one board to both rank and momentum series ids", () => {
    expect(seriesIdsForBoard("food")).toEqual(["rank:food", "mom:food"]);
  });

  it("keeps the focused board fully opaque on both grids", () => {
    const on = boardPairLineStyle(0, "food", "food");
    const off = boardPairLineStyle(1, "chip", "food");
    expect(on.opacity).toBe(1);
    expect(on.width).toBeGreaterThan(off.width);
    expect(off.opacity).toBeLessThan(1);
    expect(off.opacity).toBeGreaterThan(0.1);
  });

  it("resolves a highlight payload from series id or legend name", () => {
    expect(boardKeyFromHighlight({ seriesId: "rank:food" }, ["food", "chip"], nameByKey)).toBe("food");
    expect(boardKeyFromHighlight({ seriesId: "mom:chip" }, ["food", "chip"], nameByKey)).toBe("chip");
    expect(boardKeyFromHighlight({ seriesName: "食品饮料" }, ["food", "chip"], nameByKey)).toBe("food");
  });

  it("picks the nearest board on the rank grid and the momentum grid", () => {
    expect(findNearestBoardKey(["food", "chip"], lookup, "2026-08-29", 9, "rank")).toBe("food");
    expect(findNearestBoardKey(["food", "chip"], lookup, "2026-08-29", 3, "rank")).toBe("chip");
    expect(findNearestBoardKey(["food", "chip"], lookup, "2026-08-29", 10, "mom")).toBe("food");
    expect(findNearestBoardKey(["food", "chip"], lookup, "2026-08-29", 30, "mom")).toBe("chip");
  });

  it("keeps the idle tooltip to the date and winner only", () => {
    const html = formatIdleTooltipHtml("2026-08-29", "存储芯片", "当日推荐");
    expect(html).toContain("2026-08-29");
    expect(html).toContain("存储芯片");
    expect(html).not.toContain("排名");
    expect(html).not.toContain("食品饮料");
  });

  it("renders a single-board tooltip with rank and momentum together", () => {
    const html = formatBoardTooltipHtml({
      asOf: "2026-08-29",
      key: "food",
      marker: "●",
      nameByKey,
      titleByKey: new Map([["food", "F0最优确认"]]),
      lookup,
      winnerByDate: { "2026-08-29": "chip" },
      labels: {
        rank: "排名",
        mom: "20日动量",
        confirm: "确认覆盖",
        timing: "择时",
        winner: "当日推荐",
      },
    });
    expect(html).toContain("食品饮料");
    expect(html).toContain("F0最优确认");
    expect(html).toContain("排名 10");
    expect(html).toContain("20日动量 8.2%");
    expect(html).toContain("当日推荐: <b>存储芯片</b>");
    expect(html).not.toContain("排名 2");
    expect(html).not.toContain("31.0%");
  });
});
