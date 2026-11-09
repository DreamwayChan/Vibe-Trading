import { describe, expect, it } from "vitest";
import {
  gateStatusFromGate,
  oosReviewFromGate,
  validationArtifactHasTail,
} from "@/lib/oosPolicy";

describe("oos policy", () => {
  it("keeps a gate pass and an unreviewed tail as two facts", () => {
    const gate = {
      passed: true,
      checks: { enabled: true, monte_carlo: { passed: true } },
      thresholds: { require_out_of_sample: false },
      oos_policy: { reviewed: false, kind: "not_reviewed" },
    };
    expect(gateStatusFromGate(gate)).toBe("passed");
    expect(oosReviewFromGate(gate)).toEqual({ reviewed: false });
  });

  it("does not treat tail numbers as a review while the flag is off", () => {
    const gate = {
      passed: true,
      thresholds: { require_out_of_sample: false },
      checks: {
        enabled: true,
        out_of_sample: { passed: true, observed: 0.1 },
      },
    };
    expect(oosReviewFromGate(gate).reviewed).toBe(false);
  });

  it("labels a required tail as the same equity curve, never a holdout", () => {
    const gate = {
      passed: true,
      oos_policy: { reviewed: true, kind: "equity_curve_tail", passed: true },
    };
    expect(oosReviewFromGate(gate)).toEqual({
      reviewed: true,
      kind: "equity_curve_tail",
      passed: true,
    });
    expect(oosReviewFromGate({
      passed: false,
      oos_policy: { reviewed: true, kind: "holdout", passed: true },
    })).toEqual({ reviewed: false });
  });

  it("reads a legacy gate that required the tail check", () => {
    expect(oosReviewFromGate({
      passed: false,
      thresholds: { require_out_of_sample: true },
      checks: { out_of_sample: { passed: false, reason: "missing_out_of_sample" } },
    })).toEqual({ reviewed: true, kind: "equity_curve_tail", passed: false });
  });

  it("says the gate is off without inventing a pass", () => {
    expect(gateStatusFromGate({ passed: true, checks: { enabled: false } })).toBe("off");
    expect(gateStatusFromGate(null)).toBe("unknown");
    expect(oosReviewFromGate(null)).toEqual({ reviewed: false });
  });

  it("shows the tail block only when the artifact has one", () => {
    expect(validationArtifactHasTail({ monte_carlo: {} })).toBe(false);
    expect(validationArtifactHasTail({ out_of_sample: { degradation: 0.2 } })).toBe(true);
    expect(validationArtifactHasTail(null)).toBe(false);
  });
});
