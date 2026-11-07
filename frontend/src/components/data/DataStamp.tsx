import { useTranslation } from "react-i18next";
import { CalendarClock, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DataAsOf } from "@/lib/dataClient";

/**
 * The one timestamp every screen shows.
 *
 * `as_of` is resolved server-side (settlement cut-off, holidays) so this chip
 * can never disagree with another page: it renders the payload, it does not
 * decide what "today" means.
 */
export function DataStamp({
  asOf,
  strict,
  className,
}: {
  asOf?: DataAsOf | null;
  strict?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  if (!asOf) return null;

  return (
    <div className={cn("flex flex-wrap items-center gap-2 text-xs", className)}>
      <span
        className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-muted-foreground"
        title={asOf.reason}
      >
        <CalendarClock className="h-3.5 w-3.5" aria-hidden />
        {t("dataCenter.dataDate")}
        <span className="tabular-nums text-foreground">{asOf.date}</span>
      </span>
      <span
        className={cn(
          "rounded-md px-2 py-1 font-medium",
          asOf.settled
            ? "bg-muted text-muted-foreground"
            : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
        )}
      >
        {asOf.settled ? t("dataCenter.settled") : t("dataCenter.intraday")}
      </span>
      {strict && (
        <span className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2 py-1 font-medium text-primary">
          <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
          {t("dataCenter.strictOn")}
        </span>
      )}
    </div>
  );
}
