import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ForecastPanel } from "@/components/monitor/ForecastPanel";

vi.mock("@/lib/api", () => ({
  api: {
    forecastMonitor: vi.fn(),
    listMonitorForecastSaves: vi.fn(),
    saveMonitorForecast: vi.fn(),
    getMonitorForecastSave: vi.fn(),
  },
}));

vi.mock("@/components/charts/ForecastCandlestickChart", () => ({
  ForecastCandlestickChart: () => <div data-testid="forecast-chart" />,
}));

import { api } from "@/lib/api";

const emptyItem = (symbol: string, extra: Record<string, unknown> = {}) => ({
  symbol,
  weight: 0.1,
  price: 10,
  up_prob: 0.5,
  down_prob: 0.5,
  assessment: { bias: "neutral", called: false, style_bucket: "low", ma_structure: "mixed", analog_count: 0, up_count: 0, down_count: 0, confidence: 0 },
  ...extra,
});

describe("ForecastPanel dual-track overlay", () => {
  beforeEach(() => {
    vi.mocked(api.listMonitorForecastSaves).mockResolvedValue([]);
  });

  it("renders a legacy report with no trend field", async () => {
    vi.mocked(api.forecastMonitor).mockResolvedValue({
      as_of: "2026-08-20",
      horizon: 10,
      holding_count: 1,
      evaluated_count: 1,
      capped: false,
      bias: "neutral",
      called_count: 0,
      items: [emptyItem("000001.SZ")],
    });

    render(<ForecastPanel asOf={null} onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByText("000001.SZ")).toBeInTheDocument();
    });
    expect(screen.queryByText(/Long-horizon rule/i)).not.toBeInTheDocument();
  });

  it("shows the index-bull banner and sorts hold before reduce", async () => {
    vi.mocked(api.forecastMonitor).mockResolvedValue({
      as_of: "2026-08-20",
      horizon: 10,
      holding_count: 2,
      evaluated_count: 2,
      capped: false,
      bias: "neutral",
      called_count: 0,
      trend: { market_regime: "bull", mom120: 0.04, rule: "no adds" },
      items: [
        emptyItem("AAA.SZ", { trend: { action: "reduce", market_regime: "bull", stock_regime: "bear", swing: "mid", fade_rallies: true, reason: "cut" } }),
        emptyItem("BBB.SZ", { trend: { action: "hold", market_regime: "bull", stock_regime: "bull", swing: "mid", fade_rallies: false, reason: "rs" } }),
      ],
    });

    render(<ForecastPanel asOf={null} onClose={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByText(/do not add this book/i)).toBeInTheDocument();
    });
    const symbols = screen.getAllByText(/AAA\.SZ|BBB\.SZ/).map((node) => node.textContent);
    expect(symbols.indexOf("BBB.SZ")).toBeLessThan(symbols.indexOf("AAA.SZ"));
  });
});
