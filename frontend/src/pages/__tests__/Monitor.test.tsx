import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { Monitor } from "@/pages/Monitor";

vi.mock("@/components/charts/ForecastCandlestickChart", () => ({
  ForecastCandlestickChart: () => <div data-testid="forecast-chart" />,
  buildForecastChartModel: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getMonitor: vi.fn(),
    listRuns: vi.fn(),
    renameRun: vi.fn(),
    deleteRun: vi.fn(),
    updateMonitor: vi.fn(),
    scanMonitor: vi.fn(),
    resetMonitor: vi.fn(),
    listMonitorHistory: vi.fn(),
    getMonitorHistoryDay: vi.fn(),
    forecastMonitor: vi.fn(),
    listMonitorForecastSaves: vi.fn(),
    getMonitorForecastSave: vi.fn(),
    saveMonitorForecast: vi.fn(),
    getRun: vi.fn(),
    getRunCode: vi.fn(),
  },
}));

import { api } from "@/lib/api";

describe("Monitor page", () => {
  beforeEach(() => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      suggestions: [
        {
          symbol: "600519.SH",
          action: "buy",
          position: "long",
          reason: "signal crossed into long",
          as_of: "2024-01-02",
          data_as_of: "2024-01-02",
          price: 1688.5,
        },
      ],
      holdings: [
        {
          symbol: "600519.SH",
          weight: 0.1,
          action: "enter",
          price: 1688.5,
          data_as_of: "2024-01-02",
        },
      ],
      ops: { entered: ["600519.SH"], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.listRuns).mockResolvedValue([
      {
        run_id: "run_1",
        status: "success",
        created_at: "2024-01-01",
        prompt: "demo",
        title: "创新药",
        total_return: 0.0935,
        excess_return: -0.2585,
        sharpe: 0.284,
        max_drawdown: -0.27,
        has_signal_engine: true,
      },
    ]);
    vi.mocked(api.listMonitorHistory).mockResolvedValue([]);
    vi.mocked(api.listMonitorForecastSaves).mockResolvedValue([]);
    vi.mocked(api.forecastMonitor).mockResolvedValue({
      as_of: "2024-01-02",
      horizon: 10,
      holding_count: 1,
      evaluated_count: 1,
      capped: false,
      bias: "up",
      items: [
        {
          symbol: "600519.SH",
          weight: 0.1,
          price: 1688.5,
          up_prob: 0.62,
          down_prob: 0.38,
          style_score: 0.7,
          assessment: {
            bias: "up",
            style_bucket: "high",
            ma_structure: "bull",
            analog_count: 12,
            up_count: 8,
            down_count: 4,
            confidence: 0.55,
          },
          history: [
            { time: "2024-01-02", open: 1680, high: 1690, low: 1675, close: 1688.5, volume: 1 },
          ],
          up_bars: Array.from({ length: 10 }, (_, i) => ({
            time: `2024-01-${String(3 + i).padStart(2, "0")}`,
            open: 1688,
            high: 1700,
            low: 1680,
            close: 1695,
            volume: 0,
          })),
          down_bars: Array.from({ length: 10 }, (_, i) => ({
            time: `2024-01-${String(3 + i).padStart(2, "0")}`,
            open: 1688,
            high: 1688,
            low: 1660,
            close: 1670,
            volume: 0,
          })),
          ma: {
            history: { ma5: [1688], ma10: [null], ma20: [null] },
            up: { ma5: Array(10).fill(1690), ma10: Array(10).fill(1688), ma20: Array(10).fill(1685) },
            down: { ma5: Array(10).fill(1675), ma10: Array(10).fill(1680), ma20: Array(10).fill(1685) },
          },
        },
      ],
    });
    vi.mocked(api.getRun).mockResolvedValue({
      status: "success",
      run_id: "run_1",
      prompt: "Buy when RSI < 30",
      metrics: {
        final_value: 110000,
        total_return: 0.1,
        annual_return: 0.2,
        max_drawdown: -0.05,
        sharpe: 1.5,
        win_rate: 0.55,
        trade_count: 12,
      },
      run_card: {
        backtest: {
          codes: ["600519.SH"],
          start_date: "2024-01-01",
          end_date: "2024-06-01",
        },
      },
    });
    vi.mocked(api.getRunCode).mockResolvedValue({
      "signal_engine.py": "class SignalEngine:\n    pass\n",
    });
  });

  it("renders suggestions from GET /monitor", async () => {
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getAllByText("600519.SH").length).toBeGreaterThan(0);
    });
    expect(api.listRuns).toHaveBeenCalledWith(50, { strategyReady: true });
    expect(screen.getByText("signal crossed into long")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "buy" })).toBeInTheDocument();
    expect(screen.getByText("Long")).toBeInTheDocument();
    expect(screen.getAllByText(/1[,.]688/).length).toBeGreaterThan(0);
    expect(screen.getByText("Current strategy holdings")).toBeInTheDocument();
    expect(screen.getByText(/Entered/)).toBeInTheDocument();
    expect(screen.getByText("Tape")).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: /ret \+9\.35%.*ex -25\.85%.*sharpe \+0\.28/ }),
    ).toBeInTheDocument();
  });

  it("shows tape action on holdings and sorts hold ahead of reduce", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: ["AAA.SZ", "BBB.SZ"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      suggestions: [],
      holdings: [
        {
          symbol: "AAA.SZ",
          weight: 0.4,
          action: "hold",
          price: 10,
          data_as_of: "2024-01-02",
          trend: {
            action: "reduce",
            market_regime: "chop",
            stock_regime: "bear",
            swing: "mid",
            fade_rallies: true,
            reason: "cut",
          },
        },
        {
          symbol: "BBB.SZ",
          weight: 0.1,
          action: "enter",
          price: 20,
          data_as_of: "2024-01-02",
          trend: {
            action: "hold",
            market_regime: "chop",
            stock_regime: "bull",
            swing: "mid",
            fade_rallies: false,
            reason: "rs",
          },
        },
      ],
      ops: { entered: ["BBB.SZ"], exited: [], held: ["AAA.SZ"] },
      schedule: null,
    });

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("Tape")).toBeInTheDocument();
    });
    expect(screen.getByText("Reduce")).toBeInTheDocument();
    const table = screen.getByText("Tape").closest("table");
    expect(table).toBeTruthy();
    const symbols = within(table as HTMLElement)
      .getAllByText(/AAA\.SZ|BBB\.SZ/)
      .map((node) => node.textContent);
    expect(symbols.indexOf("BBB.SZ")).toBeLessThan(symbols.indexOf("AAA.SZ"));
  });

  it("scan passes refresh_report only when the checkbox is checked", async () => {
    const user = userEvent.setup();
    const store: Record<string, string> = {};
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => (key in store ? store[key] : null),
      setItem: (key: string, value: string) => {
        store[key] = String(value);
      },
      removeItem: (key: string) => {
        delete store[key];
      },
      clear: () => {
        for (const key of Object.keys(store)) delete store[key];
      },
    });
    vi.mocked(api.updateMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: null,
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.scanMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /scan now/i })).toBeEnabled();
    });

    const refreshBox = screen.getByRole("checkbox", { name: /also refresh report/i });
    expect(refreshBox).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: /scan now/i }));
    await waitFor(() => {
      expect(api.scanMonitor).toHaveBeenCalledWith({ refresh_report: false });
    });

    await user.click(refreshBox);
    expect(refreshBox).toBeChecked();
    expect(store["vibe.monitor.refreshReport"]).toBe("1");

    vi.mocked(api.scanMonitor).mockClear();
    await user.click(screen.getByRole("button", { name: /scan now/i }));
    await waitFor(() => {
      expect(api.scanMonitor).toHaveBeenCalledWith({ refresh_report: true });
    });
  });

  it("does not list chat-only runs returned without strategyReady filtering", async () => {
    // Backend should already filter; assert the page asks for strategy-ready runs
    // and only renders what listRuns returns (no fallback to unfiltered chat runs).
    vi.mocked(api.listRuns).mockResolvedValue([
      {
        run_id: "run_with_engine",
        status: "success",
        created_at: "2024-01-01",
        prompt: "backtest",
        has_signal_engine: true,
      },
    ]);
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: "20260727_132619_15_ad36af" },
      last_scan_at: null,
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(api.listRuns).toHaveBeenCalledWith(50, { strategyReady: true });
    });
    const options = screen.getAllByRole("option").map((el) => el.textContent || "");
    expect(options.some((label) => label.includes("run_with_engine"))).toBe(true);
    expect(options.some((label) => label.includes("20260727_132619_15_ad36af"))).toBe(true);
    // Chat-only runs must not be mixed into the strategy-ready list from listRuns.
    expect(options.some((label) => label.includes("微信上随便问问"))).toBe(false);
  });

  it("switching strategy loads that board's symbols and suggestions", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listRuns).mockResolvedValue([
      { run_id: "run_1", status: "success", created_at: "2024-01-01", prompt: "demo" },
      { run_id: "run_2", status: "success", created_at: "2024-01-02", prompt: "other" },
    ]);
    vi.mocked(api.updateMonitor).mockImplementation(async (body) => {
      const rid = body.strategy?.run_id || "";
      if (rid === "run_2") {
        return {
          symbols: ["000858.SZ"],
          strategy: { type: "run_dir", run_id: "run_2" },
          last_scan_at: "2024-01-03T00:00:00+00:00",
          suggestions: [
            {
              symbol: "000858.SZ",
              action: "sell",
              position: "flat",
              reason: "from run 2 board",
              as_of: "2024-01-03",
            },
          ],
          holdings: [],
          ops: { entered: [], exited: [], held: [] },
          schedule: null,
        };
      }
      return {
        symbols: ["600519.SH"],
        strategy: { type: "run_dir", run_id: rid || "run_1" },
        last_scan_at: "2024-01-02T00:00:00+00:00",
        suggestions: [
          {
            symbol: "600519.SH",
            action: "buy",
            position: "long",
            reason: "signal crossed into long",
            as_of: "2024-01-02",
          },
        ],
        holdings: [],
        ops: { entered: [], exited: [], held: [] },
        schedule: null,
      };
    });

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("signal crossed into long")).toBeInTheDocument();
    });

    await user.selectOptions(screen.getByRole("combobox"), "run_2");

    await waitFor(() => {
      expect(screen.getByText("from run 2 board")).toBeInTheDocument();
    });
    expect(screen.getByDisplayValue(/000858\.SZ/)).toBeInTheDocument();
    // First call persists the previous board; second selects the new strategy board.
    expect(api.updateMonitor).toHaveBeenCalledWith(
      expect.objectContaining({ strategy: { type: "run_dir", run_id: "run_2" } }),
    );
  });

  it("opens strategy details for the selected run", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /view strategy/i })).toBeEnabled();
    });

    await user.click(screen.getByRole("button", { name: /view strategy/i }));

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument();
    });
    expect(api.getRun).toHaveBeenCalledWith("run_1", { chart_payload: "summary" });
    expect(api.getRunCode).toHaveBeenCalledWith("run_1");
    expect(await screen.findByText("Buy when RSI < 30")).toBeInTheDocument();
    expect(screen.getByText(/class SignalEngine/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open full run/i })).toHaveAttribute(
      "href",
      "/runs/run_1",
    );
  });

  it("maintains strategy sources by deleting a run from the list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listRuns).mockResolvedValue([
      {
        run_id: "run_1",
        status: "success",
        created_at: "2024-01-01",
        prompt: "demo",
        title: "创新药",
      },
      {
        run_id: "run_2",
        status: "success",
        created_at: "2024-01-02",
        prompt: "other",
        title: "质量确认",
      },
    ]);
    vi.mocked(api.deleteRun).mockResolvedValue({ status: "deleted", run_id: "run_2" });
    vi.mocked(api.updateMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: null },
      last_scan_at: null,
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.deleteRun).mockClear();
    vi.mocked(api.updateMonitor).mockClear();

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /manage sources/i })).toBeEnabled();
    });

    await user.click(screen.getByRole("button", { name: /manage sources/i }));
    const maintainDialog = await screen.findByRole("dialog", { name: /manage strategy sources/i });
    expect(maintainDialog).toBeInTheDocument();
    expect(within(maintainDialog).getByText("run_2")).toBeInTheDocument();

    // Delete the non-active run — should not clear the current board.
    const deleteButtons = within(maintainDialog).getAllByTitle(/delete this run/i);
    expect(deleteButtons).toHaveLength(2);
    await user.click(deleteButtons[1]);

    expect(await screen.findByText("Delete backtest run?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^delete$/i }));

    await waitFor(() => {
      expect(api.deleteRun).toHaveBeenCalledWith("run_2");
    });
    expect(api.updateMonitor).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(within(maintainDialog).queryByText("run_2")).not.toBeInTheDocument();
    });
  });

  it("renames a strategy source title from the maintain modal", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listRuns).mockResolvedValue([
      {
        run_id: "run_1",
        status: "success",
        created_at: "2024-01-01",
        prompt: "demo",
        title: "创新药",
        total_return: 0.0935,
        excess_return: -0.2585,
        sharpe: 0.284,
        max_drawdown: -0.27,
      },
      {
        run_id: "run_2",
        status: "success",
        created_at: "2024-01-02",
        prompt: "other",
        title: "质量确认",
      },
    ]);
    vi.mocked(api.renameRun).mockResolvedValue({
      status: "updated",
      run_id: "run_2",
      title: "工业母机",
    });

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /manage sources/i })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: /manage sources/i }));
    const maintainDialog = await screen.findByRole("dialog", { name: /manage strategy sources/i });

    const renameButtons = within(maintainDialog).getAllByTitle(/rename title/i);
    expect(renameButtons).toHaveLength(2);
    await user.click(renameButtons[1]);

    const input = within(maintainDialog).getByLabelText(/rename title/i);
    expect(input).toHaveValue("质量确认");
    await user.clear(input);
    await user.type(input, "工业母机");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(api.renameRun).toHaveBeenCalledWith("run_2", "工业母机");
    });
    await waitFor(() => {
      expect(within(maintainDialog).getByText(/工业母机/)).toBeInTheDocument();
    });
  });

  it("resets strategy history after confirm and applies the fresh scan board", async () => {
    const user = userEvent.setup();
    vi.mocked(api.updateMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.resetMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-03T00:00:00+00:00",
      suggestions: [
        {
          symbol: "600519.SH",
          action: "buy",
          position: "long",
          reason: "fresh day-one after reset",
          as_of: "2024-01-03",
        },
      ],
      holdings: [
        {
          symbol: "600519.SH",
          weight: 0.25,
          action: "enter",
          price: 1700,
          data_as_of: "2024-01-03",
        },
      ],
      ops: { entered: ["600519.SH"], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.listMonitorHistory).mockResolvedValue([]);

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /reset history/i })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: /reset history/i }));

    expect(
      await screen.findByText(/Reset this strategy.s scan history\?/i),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^reset$/i }));

    await waitFor(() => {
      expect(api.resetMonitor).toHaveBeenCalled();
    });
    expect(await screen.findByText("fresh day-one after reset")).toBeInTheDocument();
  });

  it("enables forecast compare when the board has active holdings", async () => {
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /forecast compare/i })).toBeEnabled();
    });
  });

  it("opens the forecast panel and requests analog paths for the live board", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /forecast compare/i })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: /forecast compare/i }));
    expect(await screen.findByRole("dialog", { name: /forecast compare/i })).toBeInTheDocument();
    await waitFor(() => {
      expect(api.forecastMonitor).toHaveBeenCalledWith({ as_of: null });
    });
    expect(api.listMonitorForecastSaves).toHaveBeenCalled();
    expect(await screen.findByTestId("forecast-chart")).toBeInTheDocument();
    expect(screen.getByText(/Book tilt: higher odds of a 10-day rise/i)).toBeInTheDocument();
    expect(screen.getByText(/12 independent analogs/i)).toBeInTheDocument();
    expect(screen.getByText(/confidence 55%/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save result/i })).toBeEnabled();
  });

  it("saves the live forecast and can reopen a stored result", async () => {
    const user = userEvent.setup();
    const saved = {
      id: "20240102T000000Z",
      saved_at: "2024-01-02T00:00:00+00:00",
      run_id: "run_1",
      as_of: "2024-01-02",
      horizon: 10,
      holding_count: 1,
      evaluated_count: 1,
      capped: false,
      bias: "down" as const,
      items: [
        {
          symbol: "600519.SH",
          weight: 0.1,
          price: 1600,
          up_prob: 0.3,
          down_prob: 0.7,
          style_score: 0.4,
          assessment: {
            bias: "down",
            style_bucket: "medium",
            ma_structure: "bear",
            analog_count: 8,
            up_count: 2,
            down_count: 6,
            confidence: 0.22,
          },
          history: [
            { time: "2024-01-02", open: 1610, high: 1612, low: 1590, close: 1600, volume: 1 },
          ],
          up_bars: [],
          down_bars: [],
          ma: {
            history: { ma5: [1600], ma10: [null], ma20: [null] },
            up: { ma5: [], ma10: [], ma20: [] },
            down: { ma5: [], ma10: [], ma20: [] },
          },
        },
      ],
    };
    vi.mocked(api.saveMonitorForecast).mockResolvedValue(saved);
    vi.mocked(api.getMonitorForecastSave).mockResolvedValue(saved);

    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /forecast compare/i })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: /forecast compare/i }));
    const saveBtn = await screen.findByRole("button", { name: /save result/i });
    await user.click(saveBtn);
    await waitFor(() => {
      expect(api.saveMonitorForecast).toHaveBeenCalled();
    });
    const savedPill = await screen.findByRole("button", { name: /2024-01-02/ });
    await user.click(savedPill);
    await waitFor(() => {
      expect(api.getMonitorForecastSave).toHaveBeenCalledWith("20240102T000000Z");
    });
    expect(await screen.findByText(/Viewing a saved result/i)).toBeInTheDocument();
    expect(screen.getByText(/Book tilt: higher odds of a 10-day decline/i)).toBeInTheDocument();
  });

  it("keeps forecast compare enabled without holdings so saved results can be opened", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /forecast compare/i })).toBeEnabled();
    });
  });
});
