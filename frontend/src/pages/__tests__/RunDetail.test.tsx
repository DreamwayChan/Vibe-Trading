import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { RunDetail } from "../RunDetail";
import type { RunData } from "@/lib/api";

const apiMock = vi.hoisted(() => ({
  getRun: vi.fn(),
  getRunCode: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/components/charts/CandlestickChart", () => ({
  CandlestickChart: () => <div data-testid="candlestick-chart" />,
}));
vi.mock("@/components/charts/EquityChart", () => ({
  EquityChart: () => <div data-testid="equity-chart" />,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderRunDetail(path = "/runs/old") {
  const router = createMemoryRouter(
    [{ path: "/runs/:runId", element: <RunDetail /> }],
    { initialEntries: [path] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

describe("RunDetail page", () => {
  beforeEach(() => {
    apiMock.getRun.mockReset();
    apiMock.getRunCode.mockReset();
  });

  it("does not let an older route load replace the current run or code", async () => {
    const oldRun = deferred<RunData>();
    const oldCode = deferred<Record<string, string>>();
    const newRun = deferred<RunData>();
    const newCode = deferred<Record<string, string>>();

    apiMock.getRun.mockImplementation((runId: string) => runId === "old" ? oldRun.promise : newRun.promise);
    apiMock.getRunCode.mockImplementation((runId: string) => runId === "old" ? oldCode.promise : newCode.promise);

    const router = renderRunDetail();
    await act(async () => { await router.navigate("/runs/new"); });

    await act(async () => {
      newRun.resolve({ status: "success", run_id: "new", prompt: "New run" });
      newCode.resolve({ "new.py": "NEW_CODE" });
      await Promise.all([newRun.promise, newCode.promise]);
    });
    expect(await screen.findByText("New run")).toBeInTheDocument();

    await act(async () => {
      oldRun.resolve({ status: "success", run_id: "old", prompt: "Old run" });
      oldCode.resolve({ "old.py": "OLD_CODE" });
      await Promise.all([oldRun.promise, oldCode.promise]);
    });

    expect(screen.getByText("New run")).toBeInTheDocument();
    expect(screen.queryByText("Old run")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Code" }));
    expect(await screen.findByText("NEW_CODE")).toBeInTheDocument();
    expect(screen.queryByText("OLD_CODE")).not.toBeInTheDocument();
  });

  it("loads charts for traded symbols only when clicking load-all", async () => {
    apiMock.getRun.mockImplementation((runId: string, params: Record<string, string> = {}) => {
      if (params.chart_payload === "summary") {
        return Promise.resolve({
          status: "success",
          run_id: runId,
          prompt: "Trade run",
          // Autoload a non-traded picker symbol so load-all order is unambiguous.
          chart_symbols: ["ZZZ", "LOW", "HIGH", "CCC"],
          by_symbol: [
            // Deliberately unordered vs contribution so the UI must re-rank.
            { code: "LOW", count: 1, win_rate: 1, total_pnl: 1, avg_pnl: 1, avg_return_pct: 1, avg_holding_days: 1, contribution_pct: 0.2 },
            { code: "HIGH", count: 1, win_rate: 1, total_pnl: 4, avg_pnl: 4, avg_return_pct: 4, avg_holding_days: 1, contribution_pct: 0.8 },
          ],
          trade_log: [{ code: "LOW" }, { code: "HIGH" }],
        });
      }
      return Promise.resolve({
        status: "success",
        run_id: runId,
        chart_symbols: ["ZZZ", "LOW", "HIGH", "CCC"],
        price_series: {
          [params.chart_symbol || ""]: [
            { time: "2024-01-01", open: 1, high: 2, low: 1, close: 1.5, volume: 10 },
          ],
        },
      });
    });
    apiMock.getRunCode.mockResolvedValue({});

    renderRunDetail("/runs/trade-run");
    expect(await screen.findByText("Trade run")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Load all traded symbols/i }));

    await waitFor(() => {
      const chartCalls = apiMock.getRun.mock.calls
        .filter(([, params]) => params?.chart_symbol)
        .map(([, params]) => params.chart_symbol);
      // Initial autoload is ZZZ; load-all then follows contribution high → low.
      expect(chartCalls).toEqual(["ZZZ", "HIGH", "LOW"]);
      expect(chartCalls).not.toContain("CCC");
    });
  });

  it("ignores a chart response that finishes after the route changes", async () => {
    const oldChart = deferred<RunData>();
    apiMock.getRun.mockImplementation((runId: string, params: Record<string, string>) => {
      if (runId === "old" && params.chart_payload === "summary") {
        return Promise.resolve({ status: "success", run_id: "old", prompt: "Old run", chart_symbols: ["OLD"] });
      }
      if (runId === "old" && params.chart_symbol === "OLD") return oldChart.promise;
      return Promise.resolve({ status: "success", run_id: "new", prompt: "New run" });
    });
    apiMock.getRunCode.mockResolvedValue({});

    const router = renderRunDetail();
    expect(await screen.findByText("Old run")).toBeInTheDocument();
    await waitFor(() => {
      expect(apiMock.getRun).toHaveBeenCalledWith("old", { chart_symbol: "OLD" });
    });

    await act(async () => { await router.navigate("/runs/new"); });
    expect(await screen.findByText("New run")).toBeInTheDocument();

    await act(async () => {
      oldChart.resolve({
        status: "success",
        run_id: "old",
        chart_symbols: ["OLD"],
        trade_log: [{ note: "OLD TRADE" }],
      });
      await oldChart.promise;
    });

    expect(screen.getByText("New run")).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "OLD" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Trades" }));
    expect(screen.queryByText("OLD TRADE")).not.toBeInTheDocument();
  });
});
