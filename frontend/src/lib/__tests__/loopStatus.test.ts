import { describe, expect, it } from "vitest";
import {
  SOAK_SESSIONS,
  costGlance,
  decayFigure,
  paperNav,
  unrealizedPnl,
} from "@/lib/loopStatus";

describe("loop status display math", () => {
  it("reads the cost tax off delta.total_return", () => {
    expect(costGlance(-0.018)).toEqual({ kind: "ate", pct: "1.80%" });
    expect(costGlance(0.005)).toEqual({ kind: "ahead", pct: "0.50%" });
    expect(costGlance(0)).toEqual({ kind: "flat", pct: "0.00%" });
    expect(costGlance(null)).toBeNull();
  });

  it("marks open lots at the stored last price", () => {
    expect(
      unrealizedPnl({
        positions: {
          "600519.SH": {
            last_price: 110,
            lots: [
              { shares: 100, price: 100 },
              { shares: 200, price: 105 },
            ],
          },
        },
      }),
    ).toBeCloseTo(100 * 10 + 200 * 5);
    expect(unrealizedPnl({ positions: {} })).toBeNull();
    expect(unrealizedPnl(null)).toBeNull();
  });

  it("prefers the soak window NAV over a recomputed book", () => {
    const nav = paperNav({
      window: { nav: 1_020_000, session_count: 12 },
      state: { cash: 1, positions: {} },
      report: { nav: 2 },
    });
    expect(nav).toBe(1_020_000);
    expect(SOAK_SESSIONS).toBe(20);
  });

  it("hides decay until the window says it is available", () => {
    expect(decayFigure({ available: false, decay: 0.9, reason: "insufficient_sessions" })).toBeNull();
    expect(decayFigure({ available: true, decay: 0.41 })).toBeCloseTo(0.41);
  });
});
