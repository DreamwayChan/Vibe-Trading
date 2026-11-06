export type JournalField = "thesis" | "invalidation" | "position" | "risk";

export interface JournalValues {
  hypothesis: string;
  thesis: string;
  invalidation: string;
  position: string;
  risk: string;
}

/**
 * Mirrors the backend journal rules. A linked hypothesis can supply the thesis
 * and invalidation, so those two are not flagged while a hypothesis id is set.
 */
export function missingJournalFields(values: JournalValues): JournalField[] {
  const missing: JournalField[] = [];
  const linked = values.hypothesis.trim() !== "";
  if (!linked && !values.thesis.trim()) missing.push("thesis");
  if (!linked && !values.invalidation.trim()) missing.push("invalidation");
  if (!values.position.trim()) missing.push("position");
  if (!values.risk.trim()) missing.push("risk");
  return missing;
}

const REASON_FIELD: Record<string, JournalField> = {
  missing_thesis: "thesis",
  missing_invalidation: "invalidation",
  missing_position: "position",
  missing_risk: "risk",
};

/** Field a backend block-reason code asks the user to fill, if any. */
export function reasonField(code: string): JournalField | null {
  return REASON_FIELD[code] ?? null;
}

export const JOURNAL_FIX_KEY = {
  thesis: "monitor.journalFix.thesis",
  invalidation: "monitor.journalFix.invalidation",
  position: "monitor.journalFix.position",
  risk: "monitor.journalFix.risk",
} as const satisfies Record<JournalField, string>;
