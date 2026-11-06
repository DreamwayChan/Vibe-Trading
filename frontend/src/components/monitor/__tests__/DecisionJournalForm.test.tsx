import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DecisionJournalForm } from "@/components/monitor/DecisionJournalForm";
import { missingJournalFields } from "@/lib/journalValidation";

const base = { busy: false, saving: false, error: null };

describe("DecisionJournalForm", () => {
  it("explains the journal before a strategy is bound", () => {
    render(<DecisionJournalForm {...base} bound={false} onSave={vi.fn()} />);
    expect(screen.getByTestId("journal-empty")).toHaveTextContent("Bind a strategy");
    expect(screen.queryByRole("button", { name: /save decision/i })).toBeNull();
  });

  it("shows per-field help with an example and a what-to-write message after blur", async () => {
    const user = userEvent.setup();
    render(<DecisionJournalForm {...base} bound onSave={vi.fn()} />);
    expect(screen.getByText(/Gap-ups in large caps fade within five sessions\. *$/, { selector: "span" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();

    const invalidation = screen.getByLabelText("Invalidation");
    await user.click(invalidation);
    await user.tab();
    expect(screen.getByRole("alert")).toHaveTextContent(/Add an invalidation/);
    expect(screen.getByText(/marked not tradable/)).toBeInTheDocument();
  });

  it("fills a complete valid entry from the example panel and submits it", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    render(<DecisionJournalForm {...base} bound onSave={onSave} />);
    await user.click(screen.getByText("Show an example entry"));
    await user.click(screen.getByRole("button", { name: "Use this example" }));
    expect(screen.queryByText(/marked not tradable/)).toBeNull();
    await user.click(screen.getByRole("button", { name: /save decision/i }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        thesis: "Gap-ups in large caps fade within five sessions.",
        invalidation: "Close below the 20-day low.",
        position: "2% of NAV",
        risk: "Max loss 0.5% of NAV, stop at -4%",
      }),
    );
  });
});

describe("missingJournalFields", () => {
  it("does not flag thesis/invalidation when a hypothesis is linked", () => {
    expect(
      missingJournalFields({ hypothesis: "h1", thesis: "", invalidation: "", position: "", risk: "" }),
    ).toEqual(["position", "risk"]);
  });
});
