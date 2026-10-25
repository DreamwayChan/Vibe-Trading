import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { ProcessDecision, ProcessTraceStep } from "@/lib/api";
import { TermHint } from "@/components/process/TermHint";
import { cn } from "@/lib/utils";

// Literal maps so `t()` receives typed i18n keys, not widened `string`.
const ACTION_I18N = {
  wait: "process.actionWait",
  hold: "process.actionHold",
  enter: "process.actionEnter",
  add: "process.actionAdd",
  trim: "process.actionTrim",
  exit: "process.actionExit",
} as const;

const ACTION_HINT = {
  wait: "process.hintActionWait",
  hold: "process.hintActionHold",
  enter: "process.hintActionEnter",
  add: "process.hintActionAdd",
  trim: "process.hintActionTrim",
  exit: "process.hintActionExit",
} as const;

const VETO_HINT = {
  "mandate:bear": "process.hintVetoBear",
  "mandate:provisional": "process.hintVetoProvisional",
  "structure:index_bull": "process.hintVetoIndexBull",
  "structure:index_bear": "process.hintVetoIndexBear",
  "structure:fade": "process.hintVetoFade",
  "f0:none": "process.hintVetoF0None",
  "f0:half_watch": "process.hintVetoHalf",
  "analog:down": "process.hintVetoAnalog",
  "size:zero": "process.hintVetoSize",
} as const;

type ActionI18nKey = (typeof ACTION_I18N)[keyof typeof ACTION_I18N];
type ActionHintI18nKey = (typeof ACTION_HINT)[keyof typeof ACTION_HINT];
type VetoHintI18nKey = (typeof VETO_HINT)[keyof typeof VETO_HINT] | "process.hintVetoGeneric";

function actionI18nKey(action: string): ActionI18nKey {
  if (action in ACTION_I18N) {
    return ACTION_I18N[action as keyof typeof ACTION_I18N];
  }
  return "process.actionWait";
}

function actionHintI18nKey(action: string): ActionHintI18nKey {
  if (action in ACTION_HINT) {
    return ACTION_HINT[action as keyof typeof ACTION_HINT];
  }
  return "process.hintActionWait";
}

function vetoHintI18nKey(veto: string): VetoHintI18nKey {
  if (veto in VETO_HINT) {
    return VETO_HINT[veto as keyof typeof VETO_HINT];
  }
  return "process.hintVetoGeneric";
}

function vetoHint(t: TFunction, veto: string): string {
  const key = vetoHintI18nKey(veto);
  // Unknown tags interpolate the raw code; known layers have dedicated copy.
  if (key === "process.hintVetoGeneric") {
    return t(key, { veto });
  }
  return t(key);
}

function formatNav(size: number): string {
  if (!Number.isFinite(size)) return "—";
  return `${(size * 100).toFixed(1)}%`;
}

export function DecisionCard({
  decision,
  overlayReason,
  overlayStanceLabel,
}: {
  decision: ProcessDecision;
  overlayReason?: string | null;
  overlayStanceLabel?: string | null;
}) {
  const { t } = useTranslation();
  const action = (decision.action || "wait").toLowerCase();
  const actionLabel = t(actionI18nKey(action));
  const actionHint = t(actionHintI18nKey(action));
  const blocked = (decision.per_symbol || []).filter(
    (row) => row.blocked && (row.raw_action === "enter" || row.raw_action === "add"),
  );
  const trace = decision.trace || [];
  const passedCount = trace.filter((step) => step.passed && !step.skipped).length;
  const sizeIsZero = Number.isFinite(decision.size_nav) && decision.size_nav <= 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 data-testid="decision-headline" className="text-lg font-semibold">
          <TermHint hint={actionHint}>{actionLabel}</TermHint>
        </h2>
        <span className="text-xs rounded-md border px-2 py-0.5 cursor-help">
          <TermHint hint={t("process.hintSizeNav")}>
            {t("process.sizeNav", { pct: formatNav(decision.size_nav) })}
          </TermHint>
        </span>
      </div>
      {sizeIsZero ? (
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">{t("process.sizeNavZeroNote")}</p>
          <p className="text-xs text-muted-foreground">{t("process.sizeNavZeroNotBear")}</p>
        </div>
      ) : null}
      <p className="text-sm text-muted-foreground">{decision.reason}</p>
      {(decision.vetoed_by || []).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {(decision.vetoed_by || []).map((veto) => (
            <span
              key={veto}
              className="text-[11px] rounded-md border px-2 py-0.5 text-muted-foreground"
            >
              <TermHint hint={vetoHint(t, veto)}>
                {t("process.vetoLayer")}: {veto}
              </TermHint>
            </span>
          ))}
        </div>
      )}
      {blocked.length > 0 && (
        <p className="text-xs text-muted-foreground">
          <TermHint hint={t("process.hintBlockedNames")}>{t("process.blockedNames")}</TermHint>
          {": "}
          {blocked.map((row) => row.symbol).join("、")}
        </p>
      )}
      {trace.length > 0 && (
        <details className="rounded-md border bg-muted/30 px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">
            <TermHint hint={t("process.hintTrace")}>
              {t("process.traceTitle", { count: trace.length, passed: passedCount })}
            </TermHint>
          </summary>
          <ol className="mt-3 space-y-3 border-l border-border pl-3">
            {trace.map((step, index) => (
              <TraceRow key={`${step.gate}-${index}`} step={step} index={index} />
            ))}
          </ol>
        </details>
      )}
      {(overlayReason || overlayStanceLabel) && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">
            <TermHint hint={t("process.hintOverlays")}>{t("process.overlaysTitle")}</TermHint>
          </summary>
          <div className="mt-2 space-y-1">
            {overlayStanceLabel ? (
              <p>
                {t("process.overlayStance")}: {overlayStanceLabel}
              </p>
            ) : null}
            {overlayReason ? <p>{overlayReason}</p> : null}
          </div>
        </details>
      )}
    </div>
  );
}

function TraceRow({ step, index }: { step: ProcessTraceStep; index: number }) {
  const { t } = useTranslation();
  const tone = step.skipped
    ? "text-muted-foreground"
    : step.passed
      ? "text-foreground"
      : "text-rose-700 dark:text-rose-400";
  const badge = step.skipped
    ? t("process.traceSkipped")
    : step.passed
      ? t("process.tracePassed")
      : t("process.traceVetoed");
  return (
    <li className="space-y-1">
      <div className={cn("text-xs font-medium", tone)}>
        {index + 1}. {step.title}
        <span className="ml-2 font-normal text-muted-foreground">{badge}</span>
      </div>
      <p className="text-sm leading-relaxed">
        <span className="text-muted-foreground">{t("process.thoughtLabel")}：</span>
        {step.thought}
      </p>
      <p className="text-sm leading-relaxed">
        <span className="text-muted-foreground">{t("process.conclusionLabel")}：</span>
        {step.conclusion}
      </p>
    </li>
  );
}
