import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ValidationPanel } from "@/components/charts/ValidationPanel";
import type { ValidationData } from "@/lib/api";

const gateFigures: ValidationData = {
  monte_carlo: {
    actual_sharpe: 1.2,
    actual_max_dd: -0.1,
    p_value_sharpe: 0.01,
    p_value_max_dd: 0.2,
    simulated_sharpe_mean: 0.1,
    simulated_sharpe_std: 0.4,
    simulated_sharpe_p5: -0.5,
    simulated_sharpe_p95: 0.8,
    n_simulations: 1000,
    n_trades: 20,
  },
};

describe("ValidationPanel out-of-sample policy", () => {
  it("states the gate and that OOS was not reviewed as two sentences", () => {
    render(<ValidationPanel data={gateFigures} />);
    const gate = screen.getByTestId("validation-gate-line");
    const oos = screen.getByTestId("validation-oos-line");
    expect(gate).toHaveTextContent(
      "The validation gate is Monte Carlo, bootstrap, and walk-forward on the cost-aware run.",
    );
    expect(oos).toHaveTextContent("OOS not reviewed.");
    expect(oos.textContent).not.toBe(gate.textContent);
    expect(screen.queryByTestId("validation-oos-tail")).not.toBeInTheDocument();
  });

  it("labels a recorded tail as the same equity curve", () => {
    render(
      <ValidationPanel
        data={{
          ...gateFigures,
          out_of_sample: {
            oos_fraction: 0.3,
            is_sharpe: 1.4,
            oos_sharpe: 0.9,
            degradation: 0.2,
          },
        }}
      />,
    );
    expect(screen.getByTestId("validation-gate-line")).toHaveTextContent("validation gate");
    expect(screen.getByTestId("validation-oos-line")).toHaveTextContent(
      "tail split of the same equity curve, not an independent market holdout",
    );
    expect(screen.queryByText("OOS not reviewed.")).not.toBeInTheDocument();
    expect(screen.getByTestId("validation-oos-tail")).toHaveTextContent("Tail Sharpe");
    expect(screen.getByTestId("validation-oos-tail")).toHaveTextContent("0.90");
  });

  it("keeps the two sentences when the artifact is empty", () => {
    render(<ValidationPanel data={{}} />);
    expect(screen.getByTestId("validation-gate-line")).toBeInTheDocument();
    expect(screen.getByTestId("validation-oos-line")).toHaveTextContent("OOS not reviewed.");
    expect(screen.getByText("No validation data available.")).toBeInTheDocument();
  });
});
