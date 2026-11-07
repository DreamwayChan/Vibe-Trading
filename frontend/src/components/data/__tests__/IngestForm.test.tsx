import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { IngestForm, formFields, parseSymbols } from "@/components/data/IngestForm";
import type { IngestDatasetOption } from "@/lib/dataClient";

/**
 * The form is generated from the backend's parameter list, so these tests pin
 * two things: that the derivation follows the list instead of a hard-coded
 * per-dataset rule, and that a posted body contains only what the user filled
 * in — an empty string turning into ``start=""`` would make the ingester read a
 * blank window as "no range".
 */

function option(params: string[]): IngestDatasetOption {
  return {
    name: "bars_intraday",
    label: "Intraday bars",
    category: "bars",
    ingest: "bars_intraday",
    runnable: true,
    reason: "",
    params,
    sla_minutes: 60,
    sources: ["baostock"],
  };
}

const FULL = ["end", "interval", "limit", "refresh", "start", "symbols"];

describe("formFields", () => {
  it("follows the ingester's parameter list", () => {
    expect(formFields(option(FULL))).toEqual({
      range: true,
      interval: true,
      symbols: true,
      limit: true,
      refresh: true,
    });
  });

  it("hides what the ingester does not accept", () => {
    expect(formFields(option(["end", "refresh", "start"]))).toEqual({
      range: true,
      interval: false,
      symbols: false,
      limit: false,
      refresh: true,
    });
  });
});

describe("parseSymbols", () => {
  it("accepts commas, spaces and full-width punctuation", () => {
    expect(parseSymbols("600519.sh, 000001.SZ  920001.bj")).toEqual([
      "600519.SH",
      "000001.SZ",
      "920001.BJ",
    ]);
    expect(parseSymbols("600519.SH，000001.SZ")).toEqual(["600519.SH", "000001.SZ"]);
  });

  it("treats blank input as no universe", () => {
    expect(parseSymbols("   ")).toEqual([]);
    expect(parseSymbols("")).toEqual([]);
  });
});

describe("IngestForm", () => {
  it("stays out of the way until asked", () => {
    render(<IngestForm option={option(FULL)} starting={false} onSubmit={vi.fn()} />);

    expect(screen.queryByText("Start date")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Range backfill"));
    expect(screen.getByText("Start date")).toBeInTheDocument();
  });

  it("posts only the fields that were filled in", () => {
    const onSubmit = vi.fn();
    render(<IngestForm option={option(FULL)} starting={false} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("Range backfill"));

    fireEvent.change(screen.getByLabelText("Start date"), {
      target: { value: "2025-01-01" },
    });
    fireEvent.change(screen.getByLabelText("End date"), {
      target: { value: "2025-03-31" },
    });
    fireEvent.change(screen.getByPlaceholderText("600519.SH, 000001.SZ"), {
      target: { value: "600519.SH, 000001.SZ" },
    });
    fireEvent.change(screen.getByLabelText("Symbol cap"), { target: { value: "10" } });
    fireEvent.click(screen.getByLabelText(/Refetch covered range/));
    fireEvent.click(screen.getByRole("button", { name: "Start backfill" }));

    expect(onSubmit).toHaveBeenCalledWith({
      start: "2025-01-01",
      end: "2025-03-31",
      interval: "5m",
      symbols: ["600519.SH", "000001.SZ"],
      limit: 10,
      refresh: true,
    });
  });

  it("leaves out what the user did not touch", () => {
    const onSubmit = vi.fn();
    render(<IngestForm option={option(FULL)} starting={false} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("Range backfill"));
    fireEvent.click(screen.getByRole("button", { name: "Start backfill" }));

    // An empty body is the "fill whatever is missing" request, not a blank range.
    expect(onSubmit).toHaveBeenCalledWith({ interval: "5m" });
  });

  it("refuses a reversed window before it reaches the server", () => {
    const onSubmit = vi.fn();
    render(<IngestForm option={option(FULL)} starting={false} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText("Range backfill"));

    fireEvent.change(screen.getByLabelText("Start date"), {
      target: { value: "2025-03-31" },
    });
    fireEvent.change(screen.getByLabelText("End date"), {
      target: { value: "2025-01-01" },
    });

    expect(screen.getByText("The start date is after the end date")).toBeInTheDocument();
    const run = screen.getByRole("button", { name: "Start backfill" });
    expect(run).toBeDisabled();
    fireEvent.click(run);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("renders nothing for a dataset with nothing to tune", () => {
    const { container } = render(
      <IngestForm option={option([])} starting={false} onSubmit={vi.fn()} />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
