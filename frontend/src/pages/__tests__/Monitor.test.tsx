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
    recordMonitorSignal: vi.fn(),
    listMonitorJournal: vi.fn(),
    journalSummary: vi.fn(),
    recordJournalOutcome: vi.fn(),
    listMonitorHistory: vi.fn(),
    getMonitorHistoryDay: vi.fn(),
    forecastMonitor: vi.fn(),
    listMonitorForecastSaves: vi.fn(),
    getMonitorForecastSave: vi.fn(),
    saveMonitorForecast: vi.fn(),
    getRun: vi.fn(),
    getRunCode: vi.fn(),
    getCostCompare: vi.fn(),
    postCostCompare: vi.fn(),
    getPaperBook: vi.fn(),
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
    vi.mocked(api.getCostCompare).mockRejectedValue(Object.assign(new Error("missing"), { status: 404 }));
    vi.mocked(api.getPaperBook).mockRejectedValue(Object.assign(new Error("missing"), { status: 404 }));
    vi.mocked(api.postCostCompare).mockResolvedValue({
      metrics: {
        frictionless: { total_return: 0.1, nav: 110000, max_drawdown: -0.04 },
        ashare_costs: { total_return: 0.082, nav: 108200, max_drawdown: -0.05 },
        delta: { total_return: -0.018, nav: -1800, max_drawdown: -0.01 },
      },
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

  it("shows A-share risk markers on a suggestion", async () => {
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
          price: 1688.5,
          tradable: false,
          block_reasons: ["ashare_limit_up", "ashare_one_word"],
          risk_flags: ["limit_up", "one_word"],
        },
      ],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    expect(await screen.findByText("limit-up")).toBeInTheDocument();
    expect(screen.getByText("one-word")).toBeInTheDocument();
  });

  it("shows the tradable-pool strip and blocks a buy that failed the journal", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      tradable: false,
      tradable_reasons: ["missing_invalidation"],
      bars_source: "lake",
      suggestions: [
        {
          symbol: "600519.SH",
          action: "buy",
          position: "long",
          reason: "signal crossed into long",
          as_of: "2024-01-02",
          price: 1688.5,
          tradable: false,
          block_reasons: ["missing_invalidation"],
        },
      ],
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
      expect(screen.getByText("Not in the tradable pool")).toBeInTheDocument();
    });
    expect(screen.getByText("missing invalidation")).toBeInTheDocument();
    expect(screen.getByText("Not tradable")).toBeInTheDocument();
    expect(screen.getByText("Bars: data center")).toBeInTheDocument();
  });

  it("names an uncausal scan as the reason the pool stays closed", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "optimal" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      tradable: false,
      tradable_reasons: ["uncausal_scan"],
      suggestions: [
        {
          symbol: "600519.SH",
          action: "buy",
          position: "long",
          reason: "ranked first",
          as_of: "2024-01-02",
          price: 10,
          tradable: false,
          block_reasons: ["uncausal_scan"],
        },
      ],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    expect(await screen.findAllByText("uncausal scan cannot enter the tradable pool")).not.toHaveLength(0);
  });

  it("explains a closed pool with the observed value and threshold", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      tradable: false,
      tradable_reasons: ["monte_carlo_p_value", "costs_not_applied"],
      bars_source: "lake",
      validation_gate: {
        passed: false,
        reasons: ["monte_carlo_p_value", "costs_not_applied"],
        cost_aware: false,
        checks: {
          enabled: true,
          costs: { passed: false },
          monte_carlo: { passed: false, metric: "p_value_sharpe", observed: 0.165, threshold: 0.1 },
          bootstrap: { passed: true, metric: "ci_lower", observed: 0.4, threshold: 0 },
          walk_forward: { passed: true, metric: "consistency_rate", observed: 0.8, threshold: 0.6 },
        },
      },
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

    expect(
      await screen.findByText(
        "Weak statistical evidence: Monte Carlo p=0.165, needs ≤0.100 · Backtest applied no trading costs, so the result is not admissible",
      ),
    ).toBeInTheDocument();
    // The codes those notes explain are dropped so the strip says it once.
    expect(screen.queryByText("Monte Carlo p-value above threshold")).not.toBeInTheDocument();
    expect(screen.queryByText("backtest is not cost-aware")).not.toBeInTheDocument();
    const gateLine = screen.getByTestId("gate-pass-line");
    const oosLine = screen.getByTestId("oos-policy-line");
    expect(gateLine).toHaveTextContent("Validation gate did not pass.");
    expect(oosLine).toHaveTextContent("OOS not reviewed.");
    expect(gateLine.compareDocumentPosition(oosLine) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps a gate pass and “OOS not reviewed” as two sentences", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      tradable: true,
      tradable_reasons: [],
      validation_gate: {
        passed: true,
        reasons: [],
        checks: { enabled: true },
        thresholds: { require_out_of_sample: false },
        oos_policy: { reviewed: false, kind: "not_reviewed" },
      },
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

    expect(await screen.findByText("In the tradable pool")).toBeInTheDocument();
    expect(screen.getByTestId("gate-pass-line")).toHaveTextContent("Validation gate passed.");
    expect(screen.getByTestId("oos-policy-line")).toHaveTextContent("OOS not reviewed.");
    expect(screen.getByText(/A gate pass does not mean out-of-sample was reviewed/)).toBeInTheDocument();
  });

  it("names a required tail split and does not call it a holdout review", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      tradable: true,
      tradable_reasons: [],
      validation_gate: {
        passed: true,
        reasons: [],
        checks: {
          enabled: true,
          out_of_sample: { passed: true, observed: 0.2, threshold: 0.5 },
        },
        thresholds: { require_out_of_sample: true },
        oos_policy: { reviewed: true, kind: "equity_curve_tail", passed: true },
      },
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

    const oos = await screen.findByTestId("oos-policy-line");
    expect(oos).toHaveTextContent(
      "Out-of-sample review used a tail split of the same equity curve, not an independent market holdout.",
    );
    expect(screen.queryByText("OOS not reviewed.")).not.toBeInTheDocument();
    expect(screen.getByTestId("gate-pass-line")).toHaveTextContent("Validation gate passed.");
  });

  it("lists decision-journal history for the bound strategy", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: null,
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.listMonitorJournal).mockResolvedValue([
      {
        signal_id: "sig_1",
        timestamp: "2026-01-02T00:00:00Z",
        run_id: "run_1",
        hypothesis_id: "",
        thesis: "Gap reversal fades",
        invalidation: "",
        planned_position: "2% NAV",
        planned_risk: "0.5% NAV",
        symbol: "AAPL",
        tradable: false,
        block_reasons: ["missing_invalidation"],
      },
      {
        signal_id: "sig_0",
        timestamp: "2025-09-26T00:00:00Z",
        run_id: "run_1",
        hypothesis_id: "",
        thesis: "Old thesis",
        invalidation: "close above high",
        planned_position: "2% NAV",
        planned_risk: "0.5% NAV",
        symbol: "",
        tradable: true,
        block_reasons: [],
        stale: true,
        age_days: 41,
      },
    ]);
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );
    const history = await screen.findByTestId("journal-history");
    expect(await within(history).findByText("Gap reversal fades")).toBeInTheDocument();
    expect(within(history).getByText("Missing fields")).toBeInTheDocument();
    expect(within(history).getByText("missing invalidation")).toBeInTheDocument();
    // The badge describes field completeness, not pool admission: a complete
    // row can still be past its expiry window.
    expect(within(history).getByText("Fields complete")).toBeInTheDocument();
    expect(within(history).getByText("Expired 41d")).toBeInTheDocument();
    expect(within(history).getByText("Research")).toBeInTheDocument();
    const researchRow = within(history).getByText("Gap reversal fades").closest("li");
    const tradableRow = within(history).getByText("Old thesis").closest("li");
    expect(researchRow).toHaveAttribute("data-lane", "research");
    expect(tradableRow).toHaveAttribute("data-lane", "tradable");
    expect(researchRow?.className).toContain("border-dashed");
    expect(tradableRow?.className).not.toContain("border-dashed");
  });

  it("wires cost-compare and the paper-book window, and splits research from tradable", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: ["600519.SH"],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: "2024-01-02T00:00:00+00:00",
      tradable: true,
      suggestions: [
        {
          symbol: "600519.SH",
          action: "buy",
          position: "long",
          reason: "cleared the gate",
          as_of: "2024-01-02",
          price: 100,
          tradable: true,
        },
        {
          symbol: "000001.SZ",
          action: "buy",
          position: "flat",
          reason: "still a hypothesis",
          as_of: "2024-01-02",
          price: 10,
          tradable: false,
          block_reasons: ["missing_invalidation"],
        },
      ],
      holdings: [
        { symbol: "600519.SH", weight: 0.1, action: "hold", price: 100 },
        { symbol: "000001.SZ", weight: 0.05, action: "hold", price: 10 },
      ],
      journal_alerts: ["000001.SZ"],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    vi.mocked(api.getCostCompare).mockResolvedValue({
      metrics: {
        frictionless: { total_return: 0.1, nav: 110000, max_drawdown: -0.04 },
        ashare_costs: { total_return: 0.082, nav: 108200, max_drawdown: -0.05 },
        delta: { total_return: -0.018, nav: -1800, max_drawdown: -0.01 },
      },
    });
    vi.mocked(api.getPaperBook).mockResolvedValue({
      state: {
        cash: 900000,
        positions: {
          "600519.SH": { last_price: 110, lots: [{ shares: 100, price: 100 }] },
        },
      },
      window: {
        nav: 1011000,
        session_count: 12,
        return_deviation: 0.032,
        decay: { available: true, decay: 0.41, decayed: false },
      },
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("cost-compare-glance")).toHaveTextContent("Costs ate 1.80%");
    expect(api.getCostCompare).toHaveBeenCalledWith("run_1");
    expect(api.getPaperBook).toHaveBeenCalledWith("run_1");
    const paper = screen.getByTestId("paper-book-glance");
    expect(paper).toHaveTextContent("Soak day 12/20");
    expect(paper).toHaveTextContent("deviation 3.20%");
    expect(paper).toHaveTextContent("decay 0.41");
    expect(paper).toHaveTextContent("Tradable");
    expect(screen.getByTestId("paper-book-panel")).toHaveTextContent(/1[,]?011[,]?000\.00|1011000\.00/);
    expect(screen.getByTestId("paper-book-panel")).toHaveTextContent("Unrealized");

    const research = screen.getByTestId("research-board");
    const tradable = screen.getByTestId("tradable-signals");
    expect(within(research).getByText("000001.SZ")).toBeInTheDocument();
    expect(within(research).getByText("Research only")).toBeInTheDocument();
    expect(research.className).toContain("border-dashed");
    expect(within(tradable).getByText("600519.SH")).toBeInTheDocument();
    expect(within(tradable).getByText("cleared the gate")).toBeInTheDocument();
    expect(tradable.className).not.toContain("border-dashed");

    await user.click(screen.getByRole("button", { name: "Compare costs" }));
    expect(api.postCostCompare).toHaveBeenCalledWith("run_1");
  });

  it("fills the journal form from the example gallery", async () => {
    vi.mocked(api.getMonitor).mockResolvedValue({
      symbols: [],
      strategy: { type: "run_dir", run_id: "run_1" },
      last_scan_at: null,
      suggestions: [],
      holdings: [],
      ops: { entered: [], exited: [], held: [] },
      schedule: null,
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Monitor />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", { name: "Show example" }));
    const gallery = await screen.findByTestId("journal-sample");
    expect(within(gallery).getByText(/beat expectations/)).toBeInTheDocument();

    await user.click(within(gallery).getByRole("button", { name: "Fill the form" }));
    expect(screen.getByLabelText("Symbol (optional)")).toHaveValue("600519");
    expect(screen.getByLabelText("Invalidation")).toHaveValue(
      "Wholesale price breaks below 2100, or channel inventory climbs back above 3 months",
    );

    // Strategy-level sample covers the whole strategy, so the symbol clears.
    await user.click(within(gallery).getByRole("button", { name: "Strategy-level" }));
    await user.click(within(gallery).getByRole("button", { name: "Fill the form" }));
    expect(screen.getByLabelText("Symbol (optional)")).toHaveValue("");
    expect(screen.getByLabelText("Planned position")).toHaveValue(
      "Gross exposure capped at 60%, max 8% per name",
    );

    await user.click(within(gallery).getByRole("button", { name: "Anomaly" }));
    expect(screen.getByTestId("journal-sample-mechanism")).toHaveTextContent("does not report a return");
    await user.click(within(gallery).getByRole("button", { name: "Fill the form" }));
    const observation = screen.getByLabelText("Observation (facts only)") as HTMLInputElement;
    const causal = screen.getByLabelText("Causal hypothesis (because / therefore)") as HTMLInputElement;
    expect(observation.value).toContain("limit-up price");
    expect(causal.value.startsWith("Because ")).toBe(true);
    expect(observation.value.toLowerCase()).not.toContain("because");

    await user.click(within(gallery).getByRole("button", { name: "Behavioral bias" }));
    await user.click(within(gallery).getByRole("button", { name: "Fill the form" }));
    const weekend = screen.getByLabelText("Causal hypothesis (because / therefore)") as HTMLInputElement;
    const logic = screen.getByLabelText("Logic") as HTMLTextAreaElement | HTMLInputElement;
    expect(weekend.value).toContain("weekend");
    expect(logic.value).toContain("loss aversion");
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
