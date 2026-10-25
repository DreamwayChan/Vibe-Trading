import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { SectorPick } from "@/pages/SectorPick";

vi.mock("@/lib/api", () => ({
  api: {
    getSectorPick: vi.fn(),
    updateSectorPick: vi.fn(),
    scanSectorPick: vi.fn(),
    previewSectorPickHoldings: vi.fn(),
    listSectorPickHistory: vi.fn(),
    getSectorPickHistoryDay: vi.fn(),
    listSectorPickHistorySeries: vi.fn(),
  },
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: false, toggle: vi.fn() }),
}));

import { api } from "@/lib/api";

const catalog = [
  {
    key: "robot",
    name: "人形机器人",
    panel: "BK1184",
    confirm_ids: ["gtja191_124"],
    use_max_veto: false,
    run_id: "run_robot",
    prop_file: "prop.md",
    variant: "f0_prime",
    run_title: "F0'最优确认-人形机器人-124+178",
    run_missing: false,
  },
  {
    key: "copper",
    name: "金属铜",
    panel: "BK1615",
    confirm_ids: ["gtja191_081"],
    use_max_veto: false,
    run_id: "run_copper",
    prop_file: "prop.md",
    variant: "f0_prime",
    run_title: "F0'最优确认-金属铜BK1615-081/160",
    run_missing: false,
  },
];

describe("SectorPick page", () => {
  beforeEach(() => {
    vi.mocked(api.getSectorPick).mockResolvedValue({
      enabled_keys: ["robot", "copper"],
      catalog,
      result: {
        as_of: "2024-01-02",
        scanned_at: "2024-01-02T00:00:00+00:00",
        recommended_key: "robot",
        holdings_key: "robot",
        stance: "full",
        reason: "人形机器人：板块等权均价站上 MA20",
        decision: {
          action: "hold",
          size_nav: 0.2,
          reason: "续持，不加仓；建议规模 20.0% 净值。",
          vetoed_by: ["structure:fade"],
          analog_applied: false,
          trace: [
            {
              gate: "mandate",
              title: "过程宪法",
              thought: "只允许做多。",
              conclusion: "宪法允许往下走。",
              passed: true,
              skipped: false,
            },
          ],
          per_symbol: [],
        },
        sectors: [
          {
            key: "robot",
            name: "人形机器人",
            panel: "BK1184",
            run_id: "run_robot",
            variant: "f0_prime",
            enabled: true,
            timing_mult: 1,
            ret_20d: 0.05,
            confirm_pass_rate: 0.4,
            rank: 1,
            excess_return: 0.12,
          },
          {
            key: "copper",
            name: "金属铜",
            panel: "BK1615",
            run_id: "run_copper",
            variant: "f0_prime",
            enabled: true,
            timing_mult: 0.5,
            ret_20d: -0.02,
            confirm_pass_rate: 0.2,
            rank: 2,
          },
        ],
        holdings: [
          {
            symbol: "002472.SZ",
            weight: 0.16,
            action: "enter",
            price: 12.3,
          },
        ],
        ops: { entered: ["002472.SZ"], exited: [], held: [] },
        holdings_run_id: "run_robot",
      },
    });
    vi.mocked(api.listSectorPickHistory).mockResolvedValue([
      {
        as_of: "2024-01-02",
        scanned_at: "2024-01-02T00:00:00+00:00",
        recommended_key: "robot",
        holding_count: 1,
        entered_count: 1,
        exited_count: 0,
        held_count: 0,
      },
    ]);
    vi.mocked(api.listSectorPickHistorySeries).mockResolvedValue({
      days: [
        {
          as_of: "2024-01-02",
          recommended_key: "robot",
          stance: "full",
          sectors: [
            { key: "robot", name: "人形机器人", rank: 1, ret_20d: 0.05 },
            { key: "copper", name: "金属铜", rank: 2, ret_20d: -0.02 },
          ],
        },
      ],
    });
  });

  it("renders recommended sector and holdings from GET /sector-pick", async () => {
    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getAllByText("人形机器人").length).toBeGreaterThan(0);
    });
    expect(screen.getByTestId("decision-headline")).toHaveTextContent("Hold");
    expect(screen.getByText(/板块等权均价站上 MA20/)).toBeInTheDocument();
    expect(screen.getByText("002472.SZ")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "人形机器人" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "金属铜" })).toBeChecked();
    expect(screen.getByText("2024-01-02")).toBeInTheDocument();
    expect(screen.getAllByText("F0'最优确认-人形机器人-124+178").length).toBeGreaterThan(0);
    expect(screen.getByText("Rank and momentum")).toBeInTheDocument();
  });

  it("keeps the locked theme when map rank-1 is a different board", async () => {
    vi.mocked(api.getSectorPick).mockResolvedValue({
      enabled_keys: ["robot", "copper"],
      catalog,
      result: {
        as_of: "2026-08-18",
        scanned_at: "2026-08-18T00:00:00+00:00",
        recommended_key: "copper",
        holdings_key: "copper",
        locked_key: "copper",
        map_key: "robot",
        challenger_key: "robot",
        challenger_days: 1,
        lock_event: "held",
        stance: "full",
        reason: "金属铜：板块等权均价站上 MA20。锁定续持 金属铜；观察第一 人形机器人",
        decision: {
          action: "hold",
          size_nav: 0.2,
          reason: "续持，不加仓；建议规模 20.0% 净值。",
          vetoed_by: [],
          analog_applied: false,
          trace: [],
          per_symbol: [],
        },
        sectors: [
          {
            key: "robot",
            name: "人形机器人",
            panel: "BK1184",
            run_id: "run_robot",
            variant: "f0_prime",
            enabled: true,
            rank: 1,
            live_score: 1.5,
          },
          {
            key: "copper",
            name: "金属铜",
            panel: "BK1615",
            run_id: "run_copper",
            variant: "f0_prime",
            enabled: true,
            rank: 2,
            live_score: 1.0,
          },
        ],
        holdings: [{ symbol: "000630.SZ", weight: 0.16, action: "hold" }],
        ops: { entered: [], exited: [], held: ["000630.SZ"] },
        holdings_run_id: "run_copper",
      },
    });
    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getAllByText("金属铜").length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText("Locked").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Map #1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/人形机器人 leading 1\/2 days/)).toBeInTheDocument();
    expect(screen.getByText("000630.SZ")).toBeInTheDocument();
  });

  it("skips a residual index leader and still highlights the recommended board", async () => {
    vi.mocked(api.getSectorPick).mockResolvedValue({
      enabled_keys: ["robot", "copper"],
      catalog,
      result: {
        as_of: "2026-08-18",
        scanned_at: "2026-08-18T00:00:00+00:00",
        recommended_key: "copper",
        holdings_key: "copper",
        stance: "full",
        reason: "金属铜：板块等权均价站上 MA20。指数第一 人形机器人 已跳过（策略残仓：持仓 1 只，单票 100%）",
        sectors: [
          {
            key: "robot",
            name: "人形机器人",
            panel: "BK1184",
            run_id: "run_robot",
            variant: "f0_prime",
            enabled: true,
            rank: 1,
            ret_20d: 0.19,
            holding_count: 1,
            max_weight: 1,
            book_healthy: false,
            skip_reason: "策略残仓：持仓 1 只，单票 100%（需至少 3 只且单票≤50%）",
          },
          {
            key: "copper",
            name: "金属铜",
            panel: "BK1615",
            run_id: "run_copper",
            variant: "f0_prime",
            enabled: true,
            rank: 2,
            ret_20d: 0.16,
            holding_count: 6,
            max_weight: 0.1667,
            book_healthy: true,
          },
        ],
        holdings: [{ symbol: "000630.SZ", weight: 0.1667, action: "hold" }],
        ops: { entered: [], exited: [], held: ["000630.SZ"] },
      },
    });

    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("000630.SZ")).toBeInTheDocument();
    });
    expect(screen.getByText(/指数第一 人形机器人 已跳过/)).toBeInTheDocument();
    expect(screen.getByText(/Residual book/)).toBeInTheDocument();
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.map((el) => el.getAttribute("aria-label"))).toEqual(["人形机器人", "金属铜"]);
  });

  it("orders the candidate table by live rank", async () => {
    vi.mocked(api.getSectorPick).mockResolvedValue({
      enabled_keys: ["robot", "copper"],
      catalog,
      result: {
        as_of: "2024-01-02",
        scanned_at: "2024-01-02T00:00:00+00:00",
        recommended_key: "copper",
        holdings_key: "copper",
        stance: "full",
        reason: "金属铜：板块等权均价站上 MA20",
        sectors: [
          {
            key: "robot",
            name: "人形机器人",
            panel: "BK1184",
            run_id: "run_robot",
            variant: "f0_prime",
            enabled: true,
            rank: 2,
          },
          {
            key: "copper",
            name: "金属铜",
            panel: "BK1615",
            run_id: "run_copper",
            variant: "f0_prime",
            enabled: true,
            rank: 1,
          },
        ],
        holdings: [],
        ops: { entered: [], exited: [], held: [] },
      },
    });

    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("checkbox", { name: "金属铜" })).toBeInTheDocument();
    });
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes.map((el) => el.getAttribute("aria-label"))).toEqual(["金属铜", "人形机器人"]);
  });

  it("history day reloads that snapshot's holdings", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getSectorPickHistoryDay).mockResolvedValue({
      as_of: "2024-01-02",
      scanned_at: "2024-01-02T00:00:00+00:00",
      recommended_key: "copper",
      holdings_key: "copper",
      stance: "full",
      reason: "historical copper",
      sectors: [],
      holdings: [{ symbol: "000630.SZ", weight: 0.2, action: "hold" }],
      ops: { entered: [], exited: [], held: ["000630.SZ"] },
    });

    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("2024-01-02")).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: "2024-01-02" }));
    await waitFor(() => {
      expect(api.getSectorPickHistoryDay).toHaveBeenCalledWith("2024-01-02");
      expect(screen.getByText("000630.SZ")).toBeInTheDocument();
    });
  });

  it("scan posts enabled keys", async () => {
    const user = userEvent.setup();
    vi.mocked(api.scanSectorPick).mockResolvedValue({
      enabled_keys: ["robot"],
      catalog,
      result: {
        as_of: "2024-01-03",
        scanned_at: "2024-01-03T00:00:00+00:00",
        recommended_key: "robot",
        holdings_key: "robot",
        stance: "full",
        reason: "rescanned",
        sectors: [],
        holdings: [],
        ops: { entered: [], exited: [], held: [] },
      },
    });

    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /scan now/i })).toBeEnabled();
    });
    await user.click(screen.getByRole("checkbox", { name: "金属铜" }));
    await user.click(screen.getByRole("button", { name: /scan now/i }));
    await waitFor(() => {
      expect(api.scanSectorPick).toHaveBeenCalledWith({ enabled_keys: ["robot"] });
    });
  });

  it("shows the CSI 300 banner and sorts holdings by tape action", async () => {
    vi.mocked(api.getSectorPick).mockResolvedValue({
      enabled_keys: ["robot"],
      catalog,
      result: {
        as_of: "2026-08-20",
        scanned_at: "2026-08-20T00:00:00+00:00",
        recommended_key: "robot",
        holdings_key: "robot",
        stance: "half_watch",
        reason: "沪深300震荡：低吸高抛。",
        market_regime: "chop",
        mom120: -0.028,
        sectors: [
          {
            key: "robot",
            name: "人形机器人",
            panel: "BK1184",
            run_id: "run_robot",
            variant: "f0_prime",
            enabled: true,
            rank: 1,
            rs_share: 0.2,
            knife_share: 0.4,
          },
        ],
        holdings: [
          { symbol: "AAA.SZ", weight: 0.4, action: "hold", trend: { action: "reduce", market_regime: "chop", stock_regime: "bear", swing: "mid", fade_rallies: true, reason: "cut" } },
          { symbol: "BBB.SZ", weight: 0.1, action: "enter", trend: { action: "hold", market_regime: "chop", stock_regime: "bull", swing: "mid", fade_rallies: false, reason: "rs" } },
        ],
      },
    });

    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText(/range-bound/i)).toBeInTheDocument();
    });
    expect(screen.getByText("RS / hold")).toBeInTheDocument();
    expect(screen.getByText("Knife")).toBeInTheDocument();
    const symbols = screen.getAllByText(/AAA\.SZ|BBB\.SZ/).map((node) => node.textContent);
    expect(symbols.indexOf("BBB.SZ")).toBeLessThan(symbols.indexOf("AAA.SZ"));
  });

    it("still renders a legacy payload with no market_regime or trend", async () => {
    render(
      <MemoryRouter>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("002472.SZ")).toBeInTheDocument();
    });
    expect(screen.queryByText(/CSI 300 chop/i)).not.toBeInTheDocument();
    expect(screen.getByText("Tape")).toBeInTheDocument();
  });

  it("deep-links ?key= into holdings preview", async () => {
    vi.mocked(api.previewSectorPickHoldings).mockResolvedValue({
      enabled_keys: ["robot", "copper"],
      catalog,
      result: {
        as_of: "2024-01-02",
        scanned_at: "2024-01-02T00:00:00+00:00",
        recommended_key: "robot",
        holdings_key: "copper",
        stance: "full",
        reason: "preview copper",
        sectors: [],
        holdings: [{ symbol: "000630.SZ", weight: 0.2, action: "hold" }],
        ops: { entered: [], exited: [], held: ["000630.SZ"] },
      },
    });
    render(
      <MemoryRouter initialEntries={["/sector-pick?key=copper"]}>
        <SectorPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(api.previewSectorPickHoldings).toHaveBeenCalledWith("copper");
    });
    await waitFor(() => {
      expect(screen.getByText("000630.SZ")).toBeInTheDocument();
    });
  });
});
