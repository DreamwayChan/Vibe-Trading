import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { HorizonStrip } from "@/components/process/HorizonStrip";

describe("HorizonStrip", () => {
  it("shows a plain-language headline and job labels when the blob is present", () => {
    render(
      <HorizonStrip
        regime="bear"
        horizons={{
          short: "chop",
          medium: "bear",
          long: "bear",
          close: 4548,
          ma60: 4729,
          ma250: 4671,
        }}
      />,
    );
    expect(screen.getByTestId("horizon-strip")).toBeInTheDocument();
    expect(screen.getByText("Last few days")).toBeInTheDocument();
    expect(screen.getByText("Positioning")).toBeInTheDocument();
    expect(screen.getByText("Bear market?")).toBeInTheDocument();
    expect(screen.getByTestId("horizon-headline")).toHaveTextContent(/not that a bear market is confirmed/i);
    expect(screen.getByText("Do not buy new names today")).toBeInTheDocument();
    expect(screen.getByText(/below the year line, still not a bear-market verdict/i)).toBeInTheDocument();
    expect(screen.getByText(/3\.8% below the 60-day line/i)).toBeInTheDocument();
  });

  it("falls back to the one-line medium rule when horizons are missing", () => {
    render(<HorizonStrip regime="bull" />);
    expect(screen.getByText(/do not add this book/i)).toBeInTheDocument();
  });
});
