import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { MarketPick } from "@/pages/MarketPick";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      getMarketPick: vi.fn(),
      scanMarketPick: vi.fn(),
      listMarketPickHistory: vi.fn(),
      getMarketPickHistoryDay: vi.fn(),
      getSectorPick: vi.fn(),
      updateSectorPick: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

const snapshot = {
  as_of: "2024-01-02",
  scanned_at: "2024-01-02T00:00:00+00:00",
  market_regime: "chop",
  stance: "full",
  reason: "沪深300震荡：金属铜：强势 55%；刀口 10%",
  recommended_bk: "BK1615",
  unique_symbols: 3,
  ohlcv_ok: 3,
  fetch_error_count: 0,
  boards: [
    {
      bk: "BK1615",
      name: "金属铜",
      board_type: "industry",
      classified: 20,
      universe_size: 20,
      rs_share: 0.55,
      knife_share: 0.1,
      rank: 1,
      ret_20d: 0.02,
      catalog_key: "copper",
      catalog_name: "金属铜",
    },
    {
      bk: "BK0001",
      name: "杂板",
      board_type: "concept",
      classified: 12,
      universe_size: 12,
      rs_share: 0.1,
      knife_share: 0.4,
      rank: 2,
      ret_20d: 0.15,
      catalog_key: null,
      catalog_name: null,
    },
  ],
};

function tableBoardNames(): string[] {
  const table = screen.getByRole("table");
  const rows = within(table).getAllByRole("row").slice(1);
  return rows.map((row) => {
    const nameCell = within(row).getAllByRole("cell")[1];
    return nameCell.querySelector(".font-medium")?.textContent ?? "";
  });
}

describe("MarketPick page", () => {
  beforeEach(() => {
    vi.mocked(api.getMarketPick).mockReset();
    vi.mocked(api.scanMarketPick).mockReset();
    vi.mocked(api.listMarketPickHistory).mockReset();
    vi.mocked(api.getMarketPickHistoryDay).mockReset();
    vi.mocked(api.getSectorPick).mockReset();
    vi.mocked(api.updateSectorPick).mockReset();
    vi.mocked(api.listMarketPickHistory).mockResolvedValue([]);
  });

  it("shows empty state and does not auto-scan", async () => {
    vi.mocked(api.getMarketPick).mockResolvedValue({ result: null, cached: true });
    render(
      <MemoryRouter>
        <MarketPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText(/No snapshot yet/i)).toBeInTheDocument();
    });
    expect(api.scanMarketPick).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Refresh today/i })).toBeInTheDocument();
  });

  it("renders a cached snapshot without scanning", async () => {
    vi.mocked(api.getMarketPick).mockResolvedValue({ result: snapshot, cached: true });
    render(
      <MemoryRouter>
        <MarketPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getAllByText("金属铜").length).toBeGreaterThan(0);
    });
    expect(screen.getByText("杂板")).toBeInTheDocument();
    expect(screen.getByText(/Tape only/i)).toBeInTheDocument();
    expect(api.scanMarketPick).not.toHaveBeenCalled();
  });

  it("shows skip_reason under a board that is not the pick", async () => {
    vi.mocked(api.getMarketPick).mockResolvedValue({
      result: {
        ...snapshot,
        recommended_bk: "BK1494",
        reason: "焦煤：强势 40%。观察第一 医疗研发外包 已跳过（等权均价低于 MA20）",
        boards: [
          {
            bk: "BK1600",
            name: "医疗研发外包",
            board_type: "concept",
            classified: 29,
            universe_size: 29,
            rs_share: 0.48,
            knife_share: 0,
            rank: 1,
            skip_reason: "等权均价低于 MA20",
            catalog_key: "cro",
            catalog_name: "医疗研发外包",
          },
          {
            bk: "BK1494",
            name: "焦煤",
            board_type: "industry",
            classified: 10,
            universe_size: 10,
            rs_share: 0.4,
            knife_share: 0,
            rank: 2,
          },
        ],
      },
      cached: true,
    });
    render(
      <MemoryRouter>
        <MarketPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByText("等权均价低于 MA20")).toBeInTheDocument();
    });
    expect(screen.getAllByText("焦煤").length).toBeGreaterThan(0);
  });

  it("tracks a researched board into sector-pick", async () => {
    vi.mocked(api.getMarketPick).mockResolvedValue({ result: snapshot, cached: true });
    vi.mocked(api.getSectorPick).mockResolvedValue({
      enabled_keys: ["robot"],
      catalog: [],
      result: null,
    });
    vi.mocked(api.updateSectorPick).mockResolvedValue({
      enabled_keys: ["robot", "copper"],
      catalog: [],
      result: null,
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/market-pick"]}>
        <Routes>
          <Route path="/market-pick" element={<MarketPick />} />
          <Route path="/sector-pick" element={<div>sector-pick-page</div>} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Track this board/i })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /Track this board/i }));
    await waitFor(() => {
      expect(api.updateSectorPick).toHaveBeenCalled();
    });
    const body = vi.mocked(api.updateSectorPick).mock.calls[0][0];
    expect(body.enabled_keys).toEqual(expect.arrayContaining(["robot", "copper"]));
    await waitFor(() => {
      expect(screen.getByText("sector-pick-page")).toBeInTheDocument();
    });
  });

  it("sorts the result table by rank, RS, knife, and 20D", async () => {
    vi.mocked(api.getMarketPick).mockResolvedValue({ result: snapshot, cached: true });
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <MarketPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("table")).toBeInTheDocument();
    });

    expect(tableBoardNames()).toEqual(["金属铜", "杂板"]);

    await user.click(screen.getByRole("button", { name: "Sort by Rank" }));
    expect(tableBoardNames()).toEqual(["杂板", "金属铜"]);

    await user.click(screen.getByRole("button", { name: "Sort by RS / hold" }));
    expect(tableBoardNames()).toEqual(["金属铜", "杂板"]);

    await user.click(screen.getByRole("button", { name: "Sort by RS / hold" }));
    expect(tableBoardNames()).toEqual(["杂板", "金属铜"]);

    await user.click(screen.getByRole("button", { name: "Sort by Knife" }));
    expect(tableBoardNames()).toEqual(["杂板", "金属铜"]);

    await user.click(screen.getByRole("button", { name: "Sort by 20D (display)" }));
    expect(tableBoardNames()).toEqual(["杂板", "金属铜"]);

    await user.click(screen.getByRole("button", { name: "Sort by 20D (display)" }));
    expect(tableBoardNames()).toEqual(["金属铜", "杂板"]);
  });

  it("history day reloads that snapshot's ranking", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getMarketPick).mockResolvedValue({ result: snapshot, cached: true });
    vi.mocked(api.listMarketPickHistory).mockResolvedValue([
      {
        as_of: "2024-01-02",
        scanned_at: "2024-01-02T00:00:00+00:00",
        recommended_bk: "BK1615",
        recommended_name: "金属铜",
        stance: "full",
        board_count: 2,
      },
      {
        as_of: "2024-01-01",
        scanned_at: "2024-01-01T00:00:00+00:00",
        recommended_bk: "BK1137",
        recommended_name: "存储芯片",
        stance: "full",
        board_count: 1,
      },
    ]);
    vi.mocked(api.getMarketPickHistoryDay).mockResolvedValue({
      as_of: "2024-01-01",
      scanned_at: "2024-01-01T00:00:00+00:00",
      market_regime: "chop",
      stance: "full",
      reason: "historical memory chip",
      recommended_bk: "BK1137",
      unique_symbols: 1,
      ohlcv_ok: 1,
      boards: [
        {
          bk: "BK1137",
          name: "存储芯片",
          board_type: "concept",
          classified: 10,
          universe_size: 10,
          rs_share: 0.6,
          knife_share: 0.05,
          rank: 1,
          catalog_key: "memory",
          catalog_name: "存储芯片",
        },
      ],
    });

    render(
      <MemoryRouter>
        <MarketPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "2024-01-01" })).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Latest/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "2024-01-01" }));
    await waitFor(() => {
      expect(api.getMarketPickHistoryDay).toHaveBeenCalledWith("2024-01-01");
      expect(screen.getByText("historical memory chip")).toBeInTheDocument();
      expect(screen.getByText("BK1137")).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /Latest/i }));
    await waitFor(() => {
      expect(screen.getByText(/沪深300震荡：金属铜/)).toBeInTheDocument();
    });
  });

  it("refresh today reloads the history list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getMarketPick).mockResolvedValue({ result: snapshot, cached: true });
    vi.mocked(api.listMarketPickHistory)
      .mockResolvedValueOnce([
        {
          as_of: "2024-01-02",
          scanned_at: "2024-01-02T00:00:00+00:00",
          recommended_bk: "BK1615",
          recommended_name: "金属铜",
          stance: "full",
          board_count: 2,
        },
      ])
      .mockResolvedValueOnce([
        {
          as_of: "2024-01-03",
          scanned_at: "2024-01-03T00:00:00+00:00",
          recommended_bk: "BK1137",
          recommended_name: "存储芯片",
          stance: "full",
          board_count: 1,
        },
        {
          as_of: "2024-01-02",
          scanned_at: "2024-01-02T00:00:00+00:00",
          recommended_bk: "BK1615",
          recommended_name: "金属铜",
          stance: "full",
          board_count: 2,
        },
      ]);
    vi.mocked(api.scanMarketPick).mockResolvedValue({
      result: {
        ...snapshot,
        as_of: "2024-01-03",
        scanned_at: "2024-01-03T00:00:00+00:00",
        recommended_bk: "BK1137",
        reason: "rescanned",
        boards: [
          {
            bk: "BK1137",
            name: "存储芯片",
            board_type: "concept",
            classified: 10,
            universe_size: 10,
            rs_share: 0.7,
            knife_share: 0.05,
            rank: 1,
            catalog_key: "memory",
            catalog_name: "存储芯片",
          },
        ],
      },
      cached: false,
    });

    render(
      <MemoryRouter>
        <MarketPick />
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "2024-01-02" })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /Refresh today/i }));
    await waitFor(() => {
      expect(api.scanMarketPick).toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "2024-01-03" })).toBeInTheDocument();
      expect(screen.getByText("rescanned")).toBeInTheDocument();
    });
  });
});
