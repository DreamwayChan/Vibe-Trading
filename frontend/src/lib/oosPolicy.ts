/** Product policy B: a validation-gate pass and out-of-sample review are separate.

The only implemented review is a tail split of the same equity curve.
A payload that claims any other kind, including a market holdout, is not
shown as a review.
*/

export type GateStatus = "passed" | "failed" | "off" | "unknown";

export type OosReview =
  | { reviewed: false }
  | { reviewed: true; kind: "equity_curve_tail"; passed: boolean | null };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function gateStatusFromGate(gate: Record<string, unknown> | null | undefined): GateStatus {
  if (!isRecord(gate)) return "unknown";
  const checks = gate.checks;
  if (isRecord(checks) && checks.enabled === false) return "off";
  if (gate.passed === true) return "passed";
  if (gate.passed === false) return "failed";
  return "unknown";
}

function tailReview(passed: unknown): OosReview {
  return {
    reviewed: true,
    kind: "equity_curve_tail",
    passed: typeof passed === "boolean" ? passed : null,
  };
}

export function oosReviewFromGate(gate: Record<string, unknown> | null | undefined): OosReview {
  if (!isRecord(gate)) return { reviewed: false };

  const policy = gate.oos_policy;
  if (isRecord(policy)) {
    if (policy.reviewed === true && policy.kind === "equity_curve_tail") {
      return tailReview(policy.passed);
    }
    return { reviewed: false };
  }

  const thresholds = gate.thresholds;
  const checks = gate.checks;
  if (
    isRecord(thresholds) &&
    thresholds.require_out_of_sample === true &&
    isRecord(checks) &&
    isRecord(checks.out_of_sample) &&
    "passed" in checks.out_of_sample
  ) {
    return tailReview(checks.out_of_sample.passed);
  }
  return { reviewed: false };
}

/** True when validation.json itself carries the tail-split block. */
export function validationArtifactHasTail(data: { out_of_sample?: unknown } | null | undefined): boolean {
  return isRecord(data?.out_of_sample);
}
