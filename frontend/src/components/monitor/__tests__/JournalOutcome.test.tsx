import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JournalOutcome } from "@/components/monitor/JournalOutcome";
import type { MonitorJournalEntry } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: { recordMonitorOutcome: vi.fn() } }));
import { api } from "@/lib/api";

const row: MonitorJournalEntry = {
  signal_id: "sig_1",
  timestamp: "2026-01-02T00:00:00Z",
  run_id: "run_1",
  hypothesis_id: "",
  thesis: "t",
  invalidation: "i",
  planned_position: "p",
  planned_risk: "r",
  symbol: "",
  tradable: true,
  block_reasons: [],
};

describe("JournalOutcome", () => {
  beforeEach(() => vi.mocked(api.recordMonitorOutcome).mockReset());

  it("records a result for an open entry", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    vi.mocked(api.recordMonitorOutcome).mockResolvedValue(row);
    render(<JournalOutcome row={row} onSaved={onSaved} />);

    const save = screen.getByRole("button", { name: "Record result" });
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText("Result % (e.g. -3.5)"), "-3.5");
    await user.type(screen.getByLabelText("What happened? (optional)"), "stopped out");
    await user.click(save);

    expect(api.recordMonitorOutcome).toHaveBeenCalledWith("sig_1", { pnl_pct: -3.5, note: "stopped out" });
    expect(onSaved).toHaveBeenCalled();
  });

  it("shows the saved result instead of the form", () => {
    render(
      <JournalOutcome
        row={{ ...row, outcome: { pnl_pct: 4, note: "took profit", recorded_at: "2026-01-05T00:00:00Z" } }}
        onSaved={vi.fn()}
      />,
    );
    expect(screen.getByText("+4.00%")).toBeInTheDocument();
    expect(screen.getByText("took profit")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
