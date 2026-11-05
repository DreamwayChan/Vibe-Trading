import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { FreshnessStatus } from "@/lib/dataClient";

const TONE: Record<string, string> = {
  ok: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  pending: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  lag: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  stale: "bg-rose-500/10 text-rose-600 dark:text-rose-400",
  empty: "bg-muted text-muted-foreground",
  unknown: "bg-muted text-muted-foreground",
};

/** Literal keys instead of a template string: keeps i18n keys type-checked. */
const LABEL_KEY = {
  ok: "dataCenter.statusOk",
  pending: "dataCenter.statusPending",
  lag: "dataCenter.statusLag",
  stale: "dataCenter.statusStale",
  empty: "dataCenter.statusEmpty",
  unknown: "dataCenter.statusUnknown",
} as const;

/** Freshness badge shared by the overview grid and the datasets table. */
export function StatusPill({
  status,
  className,
  label,
}: {
  status: FreshnessStatus | string;
  className?: string;
  label?: string;
}) {
  const { t } = useTranslation();
  const text =
    label ?? t(LABEL_KEY[status as keyof typeof LABEL_KEY] ?? LABEL_KEY.unknown);
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap",
        TONE[status] ?? TONE.unknown,
        className,
      )}
    >
      {text}
    </span>
  );
}
