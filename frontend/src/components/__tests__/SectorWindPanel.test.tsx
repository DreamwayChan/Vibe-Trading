import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SectorWindPanel } from "@/components/SectorWindPanel";

vi.mock("@/lib/api", () => ({
  api: { getSectorWindCards: vi.fn() },
}));

import { api } from "@/lib/api";

describe("SectorWindPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders wind state and factor cards after a scan", async () => {
    vi.mocked(api.getSectorWindCards).mockResolvedValue({
      sectors: [
        {
          sector: "robot",
          wind: { state: "main", score: 1.23, actionable: true, validation: { n: 100, ic: 0.08, top_mean: 0.01, top_hit: 0.6, passed: true } },
          cards: [
            {
              sector: "robot",
              factor: "gtja191_124",
              entry: "",
              horizon_days: 5,
              shrunk_ic: 0.05,
              ic_in: 0.06,
              ic_out: 0.04,
              t_out: 2.1,
              applies_when: { wind_states: ["launch", "main"] },
              retire_when: { rolling_ic_window: 20, rolling_ic_below: 0, wind_states: ["fade", "dormant"] },
            },
          ],
        },
        { sector: "copper", status: "no_data", cards: [] },
      ],
    });
    render(<SectorWindPanel keys={["robot", "copper"]} />);
    await userEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("gtja191_124")).toBeInTheDocument());
    expect(api.getSectorWindCards).toHaveBeenCalledWith(["robot", "copper"]);
    expect(screen.getByText("robot")).toBeInTheDocument();
    expect(screen.getByText("no_data")).toBeInTheDocument();
    expect(screen.getByText("Research only")).toBeInTheDocument();
    expect(screen.getByText("Not tradable")).toBeInTheDocument();
  });

  it("shows the error when the request fails", async () => {
    vi.mocked(api.getSectorWindCards).mockRejectedValue(new Error("boom"));
    render(<SectorWindPanel />);
    await userEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument());
  });
});
