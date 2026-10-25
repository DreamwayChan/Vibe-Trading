import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DecisionCard } from "@/components/process/DecisionCard";
import type { ProcessDecision } from "@/lib/api";

const sample: ProcessDecision = {
  action: "wait",
  size_nav: 0,
  reason: "等待，不新开；建议规模 0.0% 净值。",
  vetoed_by: ["mandate:bear"],
  analog_applied: false,
  trace: [
    {
      gate: "mandate",
      title: "过程宪法",
      thought: "沪深300 是空头结构。",
      conclusion: "不新开。",
      passed: false,
      skipped: false,
      veto: "mandate:bear",
    },
  ],
  per_symbol: [],
};

describe("DecisionCard", () => {
  it("shows the official action and a thought/conclusion trace", () => {
    render(<DecisionCard decision={sample} overlayReason="raw overlay" overlayStanceLabel="Full timing" />);
    expect(screen.getByTestId("decision-headline")).toHaveTextContent("Don't buy new names today");
    expect(screen.getByText(/0% means: do not buy new names/i)).toBeInTheDocument();
    expect(screen.getByText(/does not mean the market has entered a bear market/i)).toBeInTheDocument();
    expect(screen.getByText(/等待，不新开/)).toBeInTheDocument();
    expect(screen.getByText(/过程宪法/)).toBeInTheDocument();
    expect(screen.getByText(/沪深300 是空头结构/)).toBeInTheDocument();
    expect(screen.getByText(/不新开。/)).toBeInTheDocument();
  });
});
