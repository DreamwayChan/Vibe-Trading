import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { invalidateData } from "@/stores/datahub";
import type { BarsResponse, CoverageResponse } from "@/lib/dataClient";

/**
 * The market tab is the first screen that plots lake data, so the tests pin the
 * properties that keep it honest: every read is labelled with the session and
 * provenance the hub returned, a coarse interval says it was aggregated, and
 * "no data" is an explicit answer rather than an empty chart.
 */

const getBars = vi.fn();
const getCoverage = vi.fn();

vi.mock("@/lib/dataClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/dataClient")>();
  return {
    ...actual,
    dataApi: { getBars, getCoverage, getOverview: vi.fn(), listDatasets: vi.fn(), getDataset: vi.fn(), getQuality: vi.fn(), rebuildCatalog: vi.fn() },
  };
});

// The chart owns echarts and a canvas; the assertions here are about the data
// envelope, so the plot itself is stubbed.
vi.mock("@/components/charts/CandlestickChart", () => ({
  CandlestickChart: ({ data }: { data: unknown[] }) => (
    <div data-testid="candlestick">{data.length} bars</div>
  ),
}));

const { MarketPanel } = await import("@/components/data/MarketPanel");

function barsPayload(overrides: Partial<BarsResponse> = {}): BarsResponse {
  return {
    as_of: { date: "2024-01-02", settled: true, live: false, reason: "已收盘结算" },
    interval: "5m",
    adjust: "none",
    derived_from: null,
    sources: ["tencent"],
    coverage: { symbols: 1, bars: 2, sessions: 1, first: "2024-01-02T09:35:00", last: "2024-01-02T09:40:00" },
    series: {
      "600519.SH": {
        bars: 2,
        points: [
          { t: "2024-01-02 09:35", o: 10, h: 10.5, l: 9.9, c: 10.4, v: 100 },
          { t: "2024-01-02 09:40", o: 10.4, h: 10.6, l: 10.3, c: 10.5, v: 120 },
        ],
      },
    },
    warnings: [],
    ...overrides,
  };
}

function coveragePayload(): CoverageResponse {
  return {
    as_of: { date: "2024-01-02", settled: true, live: false, reason: "" },
    interval: "5m",
    granularity: "session",
    buckets: [
      { key: "2024-01-02", sessions: 1 },
      { key: "2024-01-03", sessions: 1 },
    ],
    symbols: [{ symbol: "600519.SH", covered: 1 }],
    cells: [
      { symbol: "600519.SH", bucket: "2024-01-02", covered: 1, sessions: 1 },
      { symbol: "600519.SH", bucket: "2024-01-03", covered: 0, sessions: 1 },
    ],
    truncated: false,
    warnings: [],
  };
}

beforeEach(() => {
  invalidateData();
  getBars.mockReset().mockResolvedValue(barsPayload());
  getCoverage.mockReset().mockResolvedValue(coveragePayload());
});

describe("MarketPanel", () => {
  it("plots the bars the hub returned and labels them with the session", async () => {
    render(<MarketPanel onOpenDataset={() => {}} />);

    expect(await screen.findByTestId("candlestick")).toHaveTextContent("2 bars");
    expect(screen.getByText("2024-01-02")).toBeInTheDocument();
    expect(screen.getByText(/tencent/)).toBeInTheDocument();
  });

  it("says when a coarse interval was aggregated locally", async () => {
    getBars.mockResolvedValue(
      barsPayload({ interval: "1H", derived_from: "5m", warnings: ["该周期未入库，已由 5m 本地按交易时段聚合"] }),
    );

    render(<MarketPanel onOpenDataset={() => {}} />);

    expect(await screen.findByText(/aggregated from 5m/)).toBeInTheDocument();
    expect(screen.getByText(/5m 本地按交易时段聚合/)).toBeInTheDocument();
  });

  it("treats an empty lake as an answer, not a blank chart", async () => {
    getBars.mockResolvedValue(
      barsPayload({ series: {}, coverage: { symbols: 0, bars: 0, sessions: 0, first: null, last: null }, warnings: ["湖内没有该周期与区间的数据"] }),
    );
    getCoverage.mockResolvedValue({ ...coveragePayload(), buckets: [], cells: [], symbols: [] });

    render(<MarketPanel onOpenDataset={() => {}} />);

    expect(await screen.findByText("No bars stored for this interval")).toBeInTheDocument();
    expect(screen.getByText("Check dataset health")).toBeInTheDocument();
  });

  it("re-reads when the interval changes", async () => {
    render(<MarketPanel onOpenDataset={() => {}} />);
    await waitFor(() => expect(getBars).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "1d" }));

    await waitFor(() => expect(getBars).toHaveBeenCalledTimes(2));
    expect(getBars.mock.calls[1][0]).toMatchObject({ interval: "1d" });
  });

  it("offers adjustment for daily bars only", async () => {
    // Intraday bars carry no factor, so the switch would be a lie there.
    render(<MarketPanel onOpenDataset={() => {}} />);
    await waitFor(() => expect(getBars).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "QFQ" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "1d" }));

    expect(await screen.findByRole("button", { name: "QFQ" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "HFQ" })).toBeInTheDocument();
  });

  it("asks the hub for the adjusted series instead of adjusting locally", async () => {
    getBars.mockResolvedValue({ ...barsPayload(), interval: "1d" });
    render(<MarketPanel onOpenDataset={() => {}} />);
    await waitFor(() => expect(getBars).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "1d" }));
    await waitFor(() => expect(getBars).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "QFQ" }));

    await waitFor(() => expect(getBars).toHaveBeenCalledTimes(3));
    expect(getBars.mock.calls[2][0]).toMatchObject({ interval: "1d", adjust: "qfq" });
  });
});
