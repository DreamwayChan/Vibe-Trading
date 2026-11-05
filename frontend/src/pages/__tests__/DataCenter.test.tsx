import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";
import { invalidateData } from "@/stores/datahub";
import type { DataOverview, DatasetCard, DatasetListResponse } from "@/lib/dataClient";

/**
 * The data center exists to guarantee "one number, one source". These tests
 * pin the two properties that make that true on the client:
 *  1. two mounts asking for the same key share one request (no double fetch);
 *  2. the page renders exactly what the payload says — it does not recount.
 */

const getOverview = vi.fn();
const listDatasets = vi.fn();
const getDataset = vi.fn();
const rebuildCatalog = vi.fn();
const getIngestStatus = vi.fn();
const startIngest = vi.fn();
const cancelIngest = vi.fn();

vi.mock("@/lib/dataClient", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/dataClient")>();
  return {
    ...actual,
    dataApi: {
      getOverview,
      listDatasets,
      getDataset,
      getQuality: vi.fn(),
      rebuildCatalog,
      getIngestStatus,
      startIngest,
      cancelIngest,
      getIngestJob: vi.fn(),
    },
  };
});

const { DataCenter } = await import("@/pages/DataCenter");

function overviewFixture(): DataOverview {
  return {
    as_of: { date: "2024-01-02", settled: true, live: false, reason: "last closed session" },
    generated_at: "2024-01-03T09:00:00",
    strict: false,
    lake: { root: "/tmp/lake", files: 42, bytes: 2048, exists: true },
    counts: { declared: 4, with_data: 3, empty: 1, total_rows: 12345 },
    calendar: { source: "sina", available: true },
    quota: { tokens: 0, endpoints: [], note: "" },
    freshness: [
      {
        name: "bars_daily",
        label: "Daily bars",
        category: "bars",
        status: "ok",
        rows: 1000,
        symbols: 4,
        date_min: "2024-01-02",
        date_max: "2024-12-31",
        lag_days: 0,
        files: 1,
        bytes: 512,
        last_write: "2024-12-31T15:30:00",
        last_source: "lake",
        consumers: ["backtest"],
        retention_days: 1825,
        sla_minutes: 30,
        dirty: false,
      },
      {
        name: "tick_agg",
        label: "Tick aggregates",
        category: "bars",
        status: "stale",
        rows: null,
        symbols: null,
        date_min: null,
        date_max: null,
        lag_days: null,
        files: 0,
        bytes: 0,
        last_write: null,
        last_source: null,
        consumers: [],
        retention_days: null,
        sla_minutes: 30,
        dirty: false,
      },
    ],
    alerts: [
      { dataset: "tick_agg", label: "Tick aggregates", severity: "warning", code: "empty", message: "No rows" },
    ],
  };
}

function cardFixture(): DatasetCard {
  return {
    name: "bars_daily",
    label: "Daily bars",
    category: "bars",
    category_label: "Bars",
    keys: ["symbol", "date"],
    partitions: ["year"],
    columns: [{ name: "open", type: "DOUBLE" }],
    frequency: "1d",
    markets: ["a_share"],
    sources: ["tushare", "lake"],
    consumers: ["backtest"],
    settle_rule: "close",
    retention_days: 1825,
    sla_minutes: 30,
    ingest: "ingest.bars",
    checks: [],
    description: "Daily OHLCV",
    health: overviewFixture().freshness[0],
  };
}

beforeEach(() => {
  invalidateData();
  getOverview.mockReset();
  listDatasets.mockReset();
  getDataset.mockReset();
  rebuildCatalog.mockReset();
  getIngestStatus.mockReset();
  startIngest.mockReset();
  cancelIngest.mockReset();
  getOverview.mockResolvedValue(overviewFixture());
  listDatasets.mockResolvedValue({ as_of: overviewFixture().as_of, datasets: [cardFixture()] } as DatasetListResponse);
  getDataset.mockResolvedValue(null);
  getIngestStatus.mockResolvedValue({
    active: [],
    jobs: [],
    datasets: [
      {
        name: "bars_daily",
        label: "Daily bars",
        category: "bars",
        ingest: "src.data.ingest.bars:daily",
        runnable: true,
        reason: "",
        params: ["start", "symbols"],
        sla_minutes: 40,
        sources: ["tushare"],
      },
    ],
  });
});

describe("DataCenter page", () => {
  it("renders the payload's own numbers rather than recomputing them", async () => {
    render(<DataCenter />);

    expect(await screen.findByText("Data Center")).toBeInTheDocument();
    // Row total comes straight from counts.total_rows, formatted — not summed.
    expect(await screen.findByText("12,345")).toBeInTheDocument();
    await waitFor(() => expect(getOverview).toHaveBeenCalledTimes(1));
    expect(rebuildCatalog).not.toHaveBeenCalled();
  });

  it("shares one overview request between two mounts", async () => {
    render(
      <>
        <DataCenter />
        <DataCenter />
      </>,
    );

    await waitFor(() => expect(screen.getAllByText("Data Center")).toHaveLength(2));
    await waitFor(() => expect(getOverview).toHaveBeenCalledTimes(1));
  });

  it("loads the dataset list only when its tab opens", async () => {
    render(<DataCenter />);
    await waitFor(() => expect(getOverview).toHaveBeenCalled());

    expect(listDatasets).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
    });

    await waitFor(() => expect(listDatasets).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Daily bars")).toBeInTheDocument();
  });

  it("queues a backfill for the selected dataset", async () => {
    getDataset.mockResolvedValue({
      spec: cardFixture(),
      manifest: null,
      health: cardFixture().health,
      partitions: [],
      quality: { dup_keys: 0, checks: [] },
      schema_drift: { missing: [], extra: [] },
    });
    startIngest.mockResolvedValue({
      id: "job1",
      dataset: "bars_daily",
      params: {},
      status: "queued",
      message: "Queued",
      progress: 0,
      queued_at: "2024-01-03T09:00:00",
      started_at: null,
      finished_at: null,
      seconds: null,
      report: null,
      error: null,
    });

    render(<DataCenter />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
    });
    await act(async () => {
      fireEvent.click(await screen.findByText("Daily bars"));
    });

    const backfill = await screen.findByRole("button", { name: "Backfill" });
    await act(async () => {
      fireEvent.click(backfill);
    });

    // No range is sent on purpose: the backend resolves the window from the
    // lake watermark, so the button means the same thing on every dataset.
    await waitFor(() => expect(startIngest).toHaveBeenCalledWith({ dataset: "bars_daily" }));
  });

  it("sends the window the operator typed instead of only the dataset name", async () => {
    getDataset.mockResolvedValue({
      spec: cardFixture(),
      manifest: null,
      health: cardFixture().health,
      partitions: [],
      quality: { dup_keys: 0, checks: [] },
      schema_drift: { missing: [], extra: [] },
    });
    startIngest.mockResolvedValue({
      id: "job1",
      dataset: "bars_daily",
      params: {},
      status: "queued",
      message: "Queued",
      progress: 0,
      queued_at: "2024-01-03T09:00:00",
      started_at: null,
      finished_at: null,
      seconds: null,
      report: null,
      error: null,
    });

    render(<DataCenter />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
    });
    await act(async () => {
      fireEvent.click(await screen.findByText("Daily bars"));
    });

    // The one-click button and the range form are different promises; this is
    // the only assertion that the form's body actually reaches the API.
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Range backfill" }));
    });
    fireEvent.change(await screen.findByLabelText("Start date"), {
      target: { value: "2025-01-01" },
    });
    fireEvent.change(screen.getByLabelText("End date"), {
      target: { value: "2025-03-31" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Start backfill" }));
    });

    await waitFor(() =>
      expect(startIngest).toHaveBeenCalledWith({
        dataset: "bars_daily",
        start: "2025-01-01",
        end: "2025-03-31",
      }),
    );
  });

  it("can cancel a running backfill and shows where it stopped", async () => {
    const running = {
      id: "job1",
      dataset: "bars_daily",
      params: {},
      status: "running" as const,
      message: "日线 1200/5576",
      progress: 0.21,
      queued_at: "2024-01-03T09:00:00",
      started_at: "2024-01-03T09:00:01",
      finished_at: null,
      seconds: null,
      report: null,
      error: null,
      cursor: { done: 1200, total: 5576 },
      rows: 0,
      done: 1200,
      total: 5576,
    };
    getIngestStatus.mockResolvedValue({
      active: [running],
      jobs: [running],
      datasets: [],
    });
    cancelIngest.mockResolvedValue({ ...running, status: "cancelled", message: "已请求取消" });

    render(<DataCenter />);

    // The progress the backend published, not a percentage the client guessed.
    expect(await screen.findByText("1200/5576 done")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    });

    expect(cancelIngest).toHaveBeenCalledWith("job1");
  });
});
