import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Reports } from "../Reports";

const apiMock = vi.hoisted(() => ({
  listRuns: vi.fn(),
  deleteRun: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMock,
}));

describe("Reports page", () => {
  beforeEach(() => {
    apiMock.listRuns.mockReset();
    apiMock.deleteRun.mockReset();
  });

  it("lists backtest reports newest first with Full Report links and skips non-report runs", async () => {
    apiMock.listRuns.mockResolvedValue([
      {
        run_id: "old-report",
        status: "success",
        created_at: "2026-06-01T00:00:00Z",
        prompt: "Old report",
        codes: ["MSFT"],
        total_return: 0.05,
        sharpe: 1.1,
      },
      {
        run_id: "chat-only",
        status: "success",
        created_at: "2026-06-03T00:00:00Z",
        prompt: "No metrics",
        codes: [],
      },
      {
        run_id: "new-report",
        status: "success",
        created_at: "2026-06-04T00:00:00Z",
        prompt: "New report",
        codes: ["AAPL"],
        total_return: 0.12,
        sharpe: 1.8,
      },
    ]);

    render(<Reports />, { wrapper: MemoryRouter });

    expect(await screen.findByText("Backtest Report Library")).toBeInTheDocument();
    expect(apiMock.listRuns).toHaveBeenCalledWith(100);
    expect(screen.queryByText("chat-only")).not.toBeInTheDocument();
    const reportRunLinks = screen.getAllByRole("link", { name: /-report$/ });
    expect(reportRunLinks[0]).toHaveAttribute("href", "/runs/new-report");
    expect(reportRunLinks[1]).toHaveAttribute("href", "/runs/old-report");
    const fullReportLinks = screen.getAllByRole("link", { name: "Full Report" });
    expect(fullReportLinks[0]).toHaveAttribute("href", "/runs/new-report");
    expect(fullReportLinks[1]).toHaveAttribute("href", "/runs/old-report");
  });

  it("filters reports by search text", async () => {
    apiMock.listRuns.mockResolvedValue([
      {
        run_id: "aapl-report",
        status: "success",
        created_at: "2026-06-04T00:00:00Z",
        prompt: "Apple strategy",
        codes: ["AAPL"],
        total_return: 0.12,
      },
      {
        run_id: "msft-report",
        status: "success",
        created_at: "2026-06-03T00:00:00Z",
        prompt: "Microsoft strategy",
        codes: ["MSFT"],
        total_return: 0.08,
      },
    ]);

    render(<Reports />, { wrapper: MemoryRouter });
    await screen.findByText("aapl-report");

    fireEvent.change(screen.getByPlaceholderText("Search run id, prompt, symbol, status..."), {
      target: { value: "MSFT" },
    });

    expect(screen.queryByText("aapl-report")).not.toBeInTheDocument();
    expect(screen.getByText("msft-report")).toBeInTheDocument();
  });

  it("deletes a report only after the destructive confirm dialog is accepted", async () => {
    apiMock.listRuns.mockResolvedValue([
      {
        run_id: "keep-report",
        status: "success",
        created_at: "2026-06-04T00:00:00Z",
        prompt: "Keep me",
        total_return: 0.12,
      },
      {
        run_id: "stale-report",
        status: "success",
        created_at: "2026-06-03T00:00:00Z",
        prompt: "Delete me",
        total_return: 0.01,
      },
    ]);
    apiMock.deleteRun.mockResolvedValue({ status: "deleted", run_id: "stale-report" });

    render(<Reports />, { wrapper: MemoryRouter });
    await screen.findByText("stale-report");

    // The row button opens a confirm dialog and does NOT delete by itself.
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    expect(deleteButtons).toHaveLength(2);
    fireEvent.click(deleteButtons[1]);
    expect(apiMock.deleteRun).not.toHaveBeenCalled();
    expect(await screen.findByText("Delete backtest report?")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    await waitFor(() => expect(apiMock.deleteRun).toHaveBeenCalledWith("stale-report"));
    await waitFor(() => expect(screen.queryByText("stale-report")).not.toBeInTheDocument());
    expect(screen.getByText("keep-report")).toBeInTheDocument();
  });

  it("keeps the row and reports the error when deletion fails", async () => {
    apiMock.listRuns.mockResolvedValue([
      {
        run_id: "locked-report",
        status: "success",
        created_at: "2026-06-04T00:00:00Z",
        prompt: "Locked",
        total_return: 0.12,
      },
    ]);
    apiMock.deleteRun.mockRejectedValue(new Error("Run is in use"));

    render(<Reports />, { wrapper: MemoryRouter });
    await screen.findByText("locked-report");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Run is in use");
    expect(screen.getByText("locked-report")).toBeInTheDocument();
  });
});
