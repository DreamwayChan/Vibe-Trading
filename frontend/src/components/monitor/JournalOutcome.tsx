import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type MonitorJournalEntry } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  row: MonitorJournalEntry;
  onSaved: () => void;
}

function signed(n: number): string {
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

export function JournalOutcome({ row, onSaved }: Props) {
  const { t } = useTranslation();
  const [pnl, setPnl] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (row.outcome) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">{t("monitor.journalOutcomeResult")}</span>
        <span
          className={cn(
            "font-mono font-medium",
            row.outcome.pnl_pct >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400",
          )}
        >
          {signed(row.outcome.pnl_pct)}
        </span>
        {row.outcome.note && <span className="text-muted-foreground">{row.outcome.note}</span>}
      </div>
    );
  }

  const value = Number(pnl);
  const valid = pnl.trim() !== "" && Number.isFinite(value);

  const save = async () => {
    if (!valid || saving) return;
    setSaving(true);
    setError(null);
    try {
      await api.recordMonitorOutcome(row.signal_id, { pnl_pct: value, note: note.trim() || undefined });
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("monitor.journalOutcomeFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        type="number"
        step="any"
        value={pnl}
        onChange={(e) => setPnl(e.target.value)}
        placeholder={t("monitor.journalOutcomePnl")}
        aria-label={t("monitor.journalOutcomePnl")}
        className="w-28 px-2 py-1 rounded-md border bg-background font-mono"
      />
      <input
        type="text"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder={t("monitor.journalOutcomeNote")}
        aria-label={t("monitor.journalOutcomeNote")}
        className="min-w-40 flex-1 px-2 py-1 rounded-md border bg-background"
      />
      <button
        type="button"
        onClick={save}
        disabled={!valid || saving}
        className="rounded-md border px-2 py-1 hover:bg-muted disabled:opacity-50"
      >
        {t("monitor.journalOutcomeSave")}
      </button>
      {error && <span className="text-destructive">{error}</span>}
    </div>
  );
}
