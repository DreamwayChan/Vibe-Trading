import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { TermHint } from "@/components/process/TermHint";
import {
  horizonGapPct,
  horizonHeadlineI18nKey,
  horizonHowToI18nKey,
  horizonLegClass,
  horizonMeaningI18nKey,
  parseHorizons,
  regimeTrendHintI18nKey,
  regimeTrendI18nKey,
  type IndexHorizons,
} from "@/lib/trendAction";
import { cn } from "@/lib/utils";

const LEG_LABEL = {
  short: "monitor.horizonShort",
  medium: "monitor.horizonMedium",
  long: "monitor.horizonLong",
} as const;

const LEG_HINT = {
  short: "monitor.hintHorizonShort",
  medium: "monitor.hintHorizonMedium",
  long: "monitor.hintHorizonLong",
} as const;

const STATE_LABEL = {
  bull: "monitor.horizonBull",
  chop: "monitor.horizonChop",
  bear: "monitor.horizonBear",
} as const;

function stateKey(leg: string | null | undefined): keyof typeof STATE_LABEL | null {
  if (leg === "bull" || leg === "chop" || leg === "bear") return leg;
  return null;
}

function vsMaLine(
  t: TFunction,
  close: number | null | undefined,
  ma: number | null | undefined,
  labelKey: "monitor.horizonMa60Label" | "monitor.horizonMaYearLabel",
): string | null {
  const gap = horizonGapPct(close, ma);
  if (gap == null) return null;
  const pct = Math.abs(gap).toFixed(1);
  const label = t(labelKey);
  if (gap >= 0) return t("monitor.horizonAboveMa", { label, pct });
  return t("monitor.horizonBelowMa", { label, pct });
}

function HorizonPill({
  id,
  value,
}: {
  id: "short" | "medium" | "long";
  value: string | null | undefined;
}) {
  const { t } = useTranslation();
  const state = stateKey(value);
  const meaningKey = horizonMeaningI18nKey(id, value);
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <TermHint hint={t(LEG_HINT[id])}>
        <span className="text-[11px] text-muted-foreground">{t(LEG_LABEL[id])}</span>
      </TermHint>
      <span className={cn("text-sm font-semibold", horizonLegClass(value))}>
        {state ? t(STATE_LABEL[state]) : "—"}
      </span>
      {meaningKey ? (
        <span className="text-[11px] leading-snug text-muted-foreground">{t(meaningKey)}</span>
      ) : null}
    </div>
  );
}

/**
 * CSI 300 short / medium / long tape in plain language.
 *
 * Labels are jobs (last few days / sizing / is it a bear market), not jargon.
 * The headline is generated in the UI so English boards are not stuck with a
 * Chinese backend `flip` string.
 */
export function HorizonStrip({
  regime,
  horizons,
  className,
}: {
  regime?: string | null;
  horizons?: IndexHorizons | Record<string, unknown> | null;
  className?: string;
}) {
  const { t } = useTranslation();
  const parsed = parseHorizons(horizons);
  if (parsed) {
    const vs60 = vsMaLine(t, parsed.close, parsed.ma60, "monitor.horizonMa60Label");
    const vsYear = vsMaLine(t, parsed.close, parsed.ma250, "monitor.horizonMaYearLabel");
    const tapeBits = [
      parsed.close != null ? t("monitor.horizonClose", { close: parsed.close.toFixed(0) }) : null,
      vs60,
      vsYear,
    ].filter((bit): bit is string => Boolean(bit));
    return (
      <div
        data-testid="horizon-strip"
        className={cn("mt-2 rounded-md border bg-muted/40 px-2.5 py-2", className)}
      >
        <p className="text-[11px] text-muted-foreground">{t("monitor.horizonStripTitle")}</p>
        <p data-testid="horizon-headline" className="mt-1 text-sm font-medium leading-snug">
          {t(horizonHeadlineI18nKey(parsed))}
        </p>
        <div className="mt-2 grid grid-cols-3 gap-3">
          <HorizonPill id="short" value={parsed.short} />
          <HorizonPill id="medium" value={parsed.medium} />
          <HorizonPill id="long" value={parsed.long} />
        </div>
        {tapeBits.length > 0 ? (
          <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
            {tapeBits.join(" · ")}
          </p>
        ) : null}
        <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
          {t(horizonHowToI18nKey(parsed))}
        </p>
      </div>
    );
  }

  const regimeKey = regimeTrendI18nKey(regime);
  const regimeHintKey = regimeTrendHintI18nKey(regime);
  if (!regimeKey) return null;
  return (
    <p
      data-testid="horizon-strip"
      className={cn(
        "text-xs leading-relaxed mt-2 rounded-md border px-2.5 py-2 bg-muted/40",
        className,
      )}
    >
      <span className="font-medium text-foreground">
        <TermHint hint={regimeHintKey ? t(regimeHintKey) : ""}>
          {t("monitor.forecastTrendTitle")}
          {" · "}
          {t(regimeKey)}
        </TermHint>
      </span>
    </p>
  );
}
