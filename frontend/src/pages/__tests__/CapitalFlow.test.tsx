import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { CapitalFlow } from "@/pages/CapitalFlow";
import { movingAverage } from "@/components/capital/EtfOiChart";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      getEtfOi: vi.fn(),
      getCapitalUniverse: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";
import type { CapitalEtfOiReport } from "@/lib/api";

const report: CapitalEtfOiReport = {
  as_of: "2026-10-28",
  built_at: "2026-10-28T14:00:00+00:00",
  window: 120,
  points: [
    {
      date: "2026-10-27",
      aum: 367.57e9,
      index_close: 4049.55,
      index_mcap: 4.83e13,
      oi_pct: 0.762,
      net_flow: 6.79e9,
      nav_pnl: -1.2e9,
      flow_bp: 1.41,
      fund_count: 14,
      missing_count: 0,
      provisional: false,
    },
    {
      date: "2026-10-28",
      aum: 372.786e9,
      index_close: 4083.87,
      index_mcap: 4.867e13,
      oi_pct: 0.7659554,
      net_flow: null,
      nav_pnl: 5.2e9,
      flow_bp: null,
      fund_count: 14,
      missing_count: 2,
      provisional: true,
    },
  ],
  funds: [
    {
      code: "510300",
      name: "沪深300ETF华泰柏瑞",
      exchange: "SH",
      shares: 363.8e8,
      shares_prev: 362.4e8,
      shares_change: 1.4e8,
      nav: 4.1602,
      aum: 1513.4e8,
      net_flow: 5.76e8,
      group_weight: 0.406,
      stale: true,
    },
    {
      code: "159919",
      name: "沪深300ETF嘉实",
      exchange: "SZ",
      shares: 104e8,
      shares_prev: 104e8,
      shares_change: 0,
      nav: 4.34,
      aum: 451.6e8,
      net_flow: 0,
      group_weight: 0.121,
      stale: false,
    },
  ],
  stats: {
    oi_pct: 0.7659554,
    oi_change_bp: 0.43,
    aum: 372.786e9,
    aum_change: 5.22e9,
    net_flow: null,
    flow_5d: 81.23e9,
    flow_20d: 141.86e9,
    flow_window: 138.93e9,
    flow_5d_bp: 16.69,
    flow_20d_bp: 29.15,
    flow_z: 0.46,
    inflow_streak: 9,
    oi_percentile: 98.78,
    regime: "surge",
    score: 76.7,
    observed_days: 2,
    flow_days: 1,
  },
  mcap: {
    date: "2026-10-28",
    weights_date: "2026-09-30",
    index_close: 4083.87,
    per_point: 11917501348.43,
    total: 4.867e13,
    mode: "circ_mv",
    total_circ_mv: 4.867e13,
    total_weight_implied: 4.474e13,
    constituents: 300,
    covered: 299,
  },
  methodology: {
    formulas: {
      oi: "ETF合计规模 ÷ 沪深300流通市值 × 100%",
      aum: "Σ(ETF份额 × 单位净值)",
      net_flow: "(份额_t − 份额_t−1) × 净值_t−1",
      nav_pnl: "份额_t−1 × (净值_t − 净值_t−1)",
      mcap: "每点市值 × 指数收盘",
    },
    caveats: ["分母按最新的成分股快照标定为每点市值"],
    sources: [{ name: "上交所 ETF 份额", url: "https://www.sse.com.cn/" }],
  },
  warnings: [],
  provisional: true,
  universe_size: 14,
  cached: false,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <CapitalFlow />
    </MemoryRouter>,
  );
}

describe("CapitalFlow page", () => {
  it("renders the ETF-OI headline metrics from the API", async () => {
    vi.mocked(api.getEtfOi).mockResolvedValue(report);
    renderPage();

    expect(await screen.findByTestId("kpi-oi")).toHaveTextContent("0.766%");
    expect(await screen.findByTestId("kpi-oi")).toHaveTextContent("+0.4bp");
    const aum = screen.getByTestId("kpi-aum");
    expect(aum).toHaveTextContent("3,728");
    // The latest day has no published shares, so the flow KPI must not invent one.
    expect(screen.getByTestId("kpi-flow")).toHaveTextContent("pending");
    expect(screen.getByTestId("kpi-flow20")).toHaveTextContent("+1,418.6");
    expect(screen.getByTestId("kpi-index")).toHaveTextContent("4083.87");
  });

  it("shows the regime, streak and the pending-share warning", async () => {
    vi.mocked(api.getEtfOi).mockResolvedValue(report);
    renderPage();

    const card = await screen.findByTestId("state-card");
    expect(card).toHaveTextContent("Accelerating inflow");
    expect(card).toHaveTextContent("77");
    expect(card).toHaveTextContent("9 straight subscription days");
    expect(screen.getByText(/Latest share file not published yet/)).toBeInTheDocument();
  });

  it("lists every tracked fund with its group weight", async () => {
    vi.mocked(api.getEtfOi).mockResolvedValue(report);
    renderPage();

    const first = await screen.findByText("沪深300ETF华泰柏瑞");
    const row = first.closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("510300")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("40.6%")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("1,513.4")).toBeInTheDocument();
    expect(screen.getByText("沪深300ETF嘉实")).toBeInTheDocument();
  });

  it("reveals the methodology formulas on demand", async () => {
    vi.mocked(api.getEtfOi).mockResolvedValue(report);
    renderPage();

    const toggle = await screen.findByRole("button", { name: /Methodology/i });
    expect(screen.queryByText(/ETF合计规模 ÷ 沪深300流通市值/)).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(screen.getByText(/ETF合计规模 ÷ 沪深300流通市值/)).toBeInTheDocument();
    expect(screen.getByText("分母按最新的成分股快照标定为每点市值")).toBeInTheDocument();
  });

  it("surfaces a load error instead of an empty page", async () => {
    vi.mocked(api.getEtfOi).mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText(/Failed to load capital-flow data/)).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });
});

describe("movingAverage", () => {
  it("returns null until the window is full and skips gaps", () => {
    expect(movingAverage([1, 2, 3, 4], 2)).toEqual([null, 1.5, 2.5, 3.5]);
    expect(movingAverage([1, null, 3], 2)).toEqual([null, null, 2]);
    expect(movingAverage([2, 4, 6], 3)).toEqual([null, null, 4]);
  });
});
