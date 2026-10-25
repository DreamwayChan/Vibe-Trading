import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TermHint } from "@/components/process/TermHint";

describe("TermHint", () => {
  it("does not set a native title that would duplicate the hover card", () => {
    render(
      <TermHint hint="只允许做多。">
        等待
      </TermHint>,
    );
    const term = screen.getByText("等待");
    expect(term.closest("[title]")).toBeNull();
    expect(screen.getByRole("tooltip", { hidden: true })).toHaveTextContent("只允许做多。");
  });
});
