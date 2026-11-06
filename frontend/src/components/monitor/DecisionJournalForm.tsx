import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Lightbulb, Loader2, Save } from "lucide-react";
import { JOURNAL_FIX_KEY, levelIssue, missingJournalFields, type JournalField, type JournalValues } from "@/lib/journalValidation";
import { cn } from "@/lib/utils";

export interface JournalSubmit extends JournalValues {
  symbol: string;
  levelSide: "" | "below" | "above";
  levelPrice: string;
}

interface Props {
  bound: boolean;
  busy: boolean;
  saving: boolean;
  error: string | null;
  onSave: (values: JournalSubmit) => Promise<void> | void;
}

const EMPTY: JournalSubmit = { hypothesis: "", symbol: "", thesis: "", invalidation: "", position: "", risk: "", levelSide: "", levelPrice: "" };
type FieldKey = Exclude<keyof JournalSubmit, "levelSide" | "levelPrice">;

const FIELDS = {
  hypothesis: { label: "monitor.journalHypothesis", help: "monitor.journalHelpHypothesis" },
  symbol: { label: "monitor.journalSymbol", help: "monitor.journalHelpSymbol" },
  thesis: { label: "monitor.journalThesis", help: "monitor.journalHelpThesis" },
  invalidation: { label: "monitor.journalInvalidation", help: "monitor.journalHelpInvalidation" },
  position: { label: "monitor.journalPosition", help: "monitor.journalHelpPosition" },
  risk: { label: "monitor.journalRisk", help: "monitor.journalHelpRisk" },
} as const satisfies Record<FieldKey, { label: string; help: string }>;

const EXAMPLE_ROWS = [
  { label: "monitor.journalThesis", value: "monitor.journalExampleThesis" },
  { label: "monitor.journalInvalidation", value: "monitor.journalExampleInvalidation" },
  { label: "monitor.journalPosition", value: "monitor.journalExamplePosition" },
  { label: "monitor.journalRisk", value: "monitor.journalExampleRisk" },
] as const;

const LEVEL_SIDES = [
  { value: "", label: "monitor.journalLevelNone" },
  { value: "below", label: "monitor.journalLevelBelow" },
  { value: "above", label: "monitor.journalLevelAbove" },
] as const;

const LEVEL_KEY = {
  incomplete: "monitor.journalLevelFix.incomplete",
  number: "monitor.journalLevelFix.number",
  symbol: "monitor.journalLevelFix.symbol",
} as const;

const INPUT = "w-full px-3 py-2 rounded-md border bg-background text-sm";

export function DecisionJournalForm({ bound, busy, saving, error, onSave }: Props) {
  const { t } = useTranslation();
  const [values, setValues] = useState<JournalSubmit>(EMPTY);
  const [touched, setTouched] = useState<Set<JournalField>>(new Set());
  const [attempted, setAttempted] = useState(false);

  if (!bound) {
    return (
      <div
        className="rounded-md border border-dashed bg-muted/30 p-4 text-sm flex flex-col gap-1"
        data-testid="journal-empty"
      >
        <span className="font-medium">{t("monitor.journalEmptyTitle")}</span>
        <span className="text-muted-foreground">{t("monitor.journalEmptyBody")}</span>
      </div>
    );
  }

  const missing = missingJournalFields(values);
  const level = levelIssue(values.levelSide, values.levelPrice, values.symbol);
  const shown = (field: JournalField) => missing.includes(field) && (attempted || touched.has(field));
  const set = (key: keyof JournalSubmit) => (e: { target: { value: string } }) =>
    setValues((prev) => ({ ...prev, [key]: e.target.value }));
  const touch = (field: JournalField) => () => setTouched((prev) => new Set(prev).add(field));

  const field = (
    key: FieldKey,
    kind: JournalField | null,
    className = "",
    mono = false,
  ) => {
    const { label: labelKey, help: helpKey } = FIELDS[key];
    const invalid = kind !== null && shown(kind);
    return (
      <label className={cn("flex flex-col gap-1", className)}>
        <span className="text-xs font-medium">{t(labelKey)}</span>
        <input
          type="text"
          value={values[key]}
          onChange={set(key)}
          onBlur={kind ? touch(kind) : undefined}
          aria-label={t(labelKey)}
          aria-invalid={invalid || undefined}
          className={cn(INPUT, mono && "font-mono", invalid && "border-amber-500")}
        />
        <span className="text-xs text-muted-foreground">{t(helpKey)}</span>
        {invalid && kind && (
          <span className="text-xs text-amber-700 dark:text-amber-400" role="alert">
            {t(JOURNAL_FIX_KEY[kind])}
          </span>
        )}
      </label>
    );
  };

  const useExample = () => {
    setValues({
      hypothesis: "",
      symbol: "",
      thesis: t("monitor.journalExampleThesis"),
      invalidation: t("monitor.journalExampleInvalidation"),
      position: t("monitor.journalExamplePosition"),
      risk: t("monitor.journalExampleRisk"),
      levelSide: "",
      levelPrice: "",
    });
  };

  const submit = async () => {
    setAttempted(true);
    await onSave(values);
  };

  return (
    <div className="flex flex-col gap-3">
      <details className="rounded-md border bg-muted/30 px-3 py-2 text-xs" data-testid="journal-example">
        <summary className="cursor-pointer font-medium inline-flex items-center gap-1">
          <Lightbulb className="h-3.5 w-3.5" />
          {t("monitor.journalExampleToggle")}
        </summary>
        <dl className="mt-2 grid gap-1 sm:grid-cols-[auto,1fr] sm:gap-x-3">
          {EXAMPLE_ROWS.map((row) => (
            <div key={row.label} className="contents">
              <dt className="font-medium">{t(row.label)}</dt>
              <dd className="text-muted-foreground">{t(row.value)}</dd>
            </div>
          ))}
        </dl>
        <button
          type="button"
          onClick={useExample}
          className="mt-2 rounded-md border px-2 py-1 hover:bg-muted"
        >
          {t("monitor.journalExampleUse")}
        </button>
      </details>
      <div className="grid gap-3 sm:grid-cols-2">
        {field("hypothesis", null)}
        {field("symbol", null, "", true)}
        {field("thesis", "thesis", "sm:col-span-2")}
        {field("invalidation", "invalidation", "sm:col-span-2")}
        {field("position", "position")}
        {field("risk", "risk")}
      </div>
      <div className="flex flex-col gap-1">
        <span className="text-xs font-medium">{t("monitor.journalLevel")}</span>
        <div className="flex flex-wrap items-center gap-2">
          <div role="radiogroup" aria-label={t("monitor.journalLevelSide")} className="flex flex-wrap gap-3 text-sm">
            {LEVEL_SIDES.map((side) => (
              <label key={side.value || "none"} className="inline-flex items-center gap-1">
                <input
                  type="radio"
                  name="journal-level-side"
                  checked={values.levelSide === side.value}
                  onChange={() => setValues((prev) => ({ ...prev, levelSide: side.value }))}
                />
                {t(side.label)}
              </label>
            ))}
          </div>
          <input
            type="number"
            min="0"
            step="any"
            value={values.levelPrice}
            onChange={(e) => setValues((prev) => ({ ...prev, levelPrice: e.target.value }))}
            aria-label={t("monitor.journalLevelPrice")}
            className={cn(INPUT, "w-32 font-mono")}
          />
        </div>
        <span className="text-xs text-muted-foreground">{t("monitor.journalHelpLevel")}</span>
        {level && (
          <span className="text-xs text-amber-700 dark:text-amber-400" role="alert">
            {t(LEVEL_KEY[level])}
          </span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={submit}
          disabled={saving || busy || level !== null}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
        >
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          {saving ? t("monitor.journalSaving") : t("monitor.journalSubmit")}
        </button>
        {missing.length > 0 && (
          <span className="text-xs text-muted-foreground">{t("monitor.journalWillDowngrade")}</span>
        )}
        {error && <span className="text-sm text-destructive">{error}</span>}
      </div>
    </div>
  );
}
