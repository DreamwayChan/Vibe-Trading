import { useEffect, useMemo, useState } from "react";
import { Loader2, RefreshCw, Scale, ShieldCheck, TriangleAlert, X } from "lucide-react";
import {
  api,
  type DesignRegime,
  type RegimeChecks,
  type RegimeEvidence,
  type RegimePanelProfile,
  type StrategyStyle,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const REGIME_ORDER: DesignRegime[] = ["bull", "slow_bull", "chop", "bear"];

const REGIME_LABEL: Record<string, string> = {
  bull: "快速单边多头",
  slow_bull: "慢牛（温和上行）",
  chop: "震荡",
  bear: "空头",
};

const SOURCE_LABEL: Record<string, string> = {
  auto_inferred: "自动推断",
  manual_override: "人工申报",
  run_id: "命名/设计文件",
  prop_file: "设计文件",
  hypothesis: "事前假设",
  ex_ante_doc: "事前文档",
  design_doc: "设计文档",
  none: "未申报",
  unspecified: "未申报",
};

function regimeLabel(value: string | undefined | null): string {
  if (!value) return "未申报";
  return REGIME_LABEL[value] || value;
}

function sourceLabel(value: string | undefined | null): string {
  if (!value) return "未申报";
  return SOURCE_LABEL[value] || value;
}

function pct(value: number | undefined | null, digits = 0): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function confLabel(style: StrategyStyle): string {
  const raw = style.designed_regime_confidence;
  if (typeof raw !== "number") return "—";
  const level = raw >= 0.7 ? "高" : raw >= 0.45 ? "中" : "低";
  return `${level} ${raw.toFixed(2)}`;
}

/** 生效值与自动推断值不一致时，说明差在哪。 */
function divergence(style: StrategyStyle): string | null {
  const eff = style.designed_regime;
  const auto = style.designed_regime_auto;
  if (!eff || !auto || eff === auto) return null;
  return `自动推断为${regimeLabel(auto)}，当前生效为${regimeLabel(eff)}（${sourceLabel(
    style.designed_regime_source,
  )}）`;
}

/** 板块主市况与申报不一致 → 结构错配。 */
function mismatch(checks: RegimeChecks | undefined, declared: string | undefined): string | null {
  const coverage = checks?.coverage;
  if (!coverage?.dominant_regime || !declared) return null;
  if (coverage.dominant_regime === declared) return null;
  return `板块主市况是${regimeLabel(coverage.dominant_label || coverage.dominant_regime)}，与申报的${regimeLabel(
    declared,
  )} 不一致`;
}

export function RegimeChip({
  style,
  onClick,
}: {
  style: StrategyStyle | null | undefined;
  onClick?: () => void;
}) {
  if (!style) return null;
  const checks = style.designed_regime_evidence?.checks ?? style.designed_regime_checks;
  const share = checks?.coverage?.share;
  const isMismatch = Boolean(mismatch(checks, style.designed_regime));
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "mt-0.5 inline-flex flex-wrap items-center gap-1 rounded border px-1.5 py-0.5 text-[11px]",
        "text-muted-foreground hover:bg-muted/60",
        isMismatch && "border-amber-500/50 text-amber-700 dark:text-amber-400",
      )}
      title="查看设计市况的判定依据"
    >
      <Scale className="h-3 w-3" />
      <span>
        设计市况：{regimeLabel(style.designed_regime)}
        {typeof style.designed_regime_confidence === "number"
          ? ` · 置信 ${confLabel(style)}`
          : ""}
        {` · ${sourceLabel(style.designed_regime_source)}`}
      </span>
      {typeof share === "number" ? <span>{`· 样本占 ${pct(share)}`}</span> : null}
      {isMismatch ? <TriangleAlert className="h-3 w-3" /> : null}
    </button>
  );
}

function FactorRows({ evidence }: { evidence: RegimeEvidence }) {
  const factors = evidence.factors || [];
  if (factors.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {factors.map((f) => {
        const best = Object.entries(f.scores || {}).sort((a, b) => b[1] - a[1])[0];
        return (
          <div key={f.id} className="rounded-md border px-2 py-1.5">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <span className="text-xs font-medium">{f.label}</span>
                <span className="ms-1.5 text-[11px] text-muted-foreground">{f.value}</span>
              </div>
              <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                权重 {f.weight.toFixed(2)}
              </span>
            </div>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{f.note}</p>
            <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-muted-foreground">
              {REGIME_ORDER.map((r) => (
                <span
                  key={r}
                  className={cn(
                    "font-mono",
                    best && best[0] === r && "font-semibold text-foreground",
                  )}
                >
                  {regimeLabel(r)} {Number(f.scores?.[r] ?? 0).toFixed(2)}
                </span>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ShareBar({ shares }: { shares?: Partial<Record<DesignRegime, number>> }) {
  if (!shares) return null;
  return (
    <div className="space-y-1">
      <div className="flex h-2 overflow-hidden rounded-full bg-muted">
        {REGIME_ORDER.map((r) => {
          const v = Number(shares[r] || 0);
          if (v <= 0) return null;
          return (
            <div
              key={r}
              style={{ width: `${v * 100}%` }}
              className={cn(
                r === "bull" && "bg-rose-500/70",
                r === "slow_bull" && "bg-amber-500/70",
                r === "chop" && "bg-sky-500/70",
                r === "bear" && "bg-emerald-500/70",
              )}
              title={`${regimeLabel(r)} ${pct(v)}`}
            />
          );
        })}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
        {REGIME_ORDER.map((r) => (
          <span key={r}>
            {regimeLabel(r)} {pct(Number(shares[r] || 0))}
          </span>
        ))}
      </div>
    </div>
  );
}

function CheckRow({
  tone,
  title,
  detail,
}: {
  tone: "ok" | "warn" | "info";
  title: string;
  detail: string;
}) {
  return (
    <div
      className={cn(
        "flex gap-1.5 rounded-md border px-2 py-1.5 text-[11px] leading-relaxed",
        tone === "warn" && "border-amber-500/50 bg-amber-500/5",
        tone === "ok" && "border-emerald-500/40 bg-emerald-500/5",
      )}
    >
      {tone === "warn" ? (
        <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0 text-amber-600 dark:text-amber-400" />
      ) : (
        <ShieldCheck className="mt-0.5 h-3 w-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
      )}
      <div className="min-w-0">
        <span className="font-medium text-foreground">{title}</span>
        <span className="text-muted-foreground"> {detail}</span>
      </div>
    </div>
  );
}

export function RegimePanelModal({
  runId,
  initialStyle,
  onClose,
  onSaved,
}: {
  runId: string;
  initialStyle?: StrategyStyle | null;
  onClose: () => void;
  onSaved?: (style: StrategyStyle) => void;
}) {
  const [style, setStyle] = useState<StrategyStyle | null>(initialStyle ?? null);
  const [loading, setLoading] = useState(!initialStyle);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [choice, setChoice] = useState<string>(initialStyle?.designed_regime || "auto");
  const [reason, setReason] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.getRunRegime(runId);
        if (!cancelled) {
          setStyle(res.strategy_style);
          setChoice(res.strategy_style.designed_regime || "auto");
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "读取申报失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const evidence = style?.designed_regime_evidence;
  const checks: RegimeChecks | undefined = evidence?.checks ?? style?.designed_regime_checks;
  const panel: Partial<RegimePanelProfile> | undefined =
    evidence?.panel ?? style?.designed_regime_panel;
  const coverage = checks?.coverage;
  const empirical = checks?.empirical;
  const prior = checks?.prior;
  const mismatchNote = useMemo(
    () => mismatch(checks, style?.designed_regime),
    [checks, style?.designed_regime],
  );
  const divergenceNote = style ? divergence(style) : null;
  const isManual = style?.designed_regime_source === "manual_override";

  async function saveOverride() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.patchRunRegime(runId, choice, reason.trim());
      setStyle(res.strategy_style);
      setReason("");
      setChoice(res.strategy_style.designed_regime || "auto");
      onSaved?.(res.strategy_style);
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function reinfer() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.inferRunRegime(runId);
      setStyle(res.strategy_style);
      setChoice(res.strategy_style.designed_regime || "auto");
      onSaved?.(res.strategy_style);
    } catch (e) {
      setError(e instanceof Error ? e.message : "重新推断失败");
    } finally {
      setBusy(false);
    }
  }

  // 与当前生效值相同的选择是空操作：不给按钮，避免提交一条无意义的申报。
  const isNoop =
    !style || choice === (style.designed_regime || "auto") || (choice === "auto" && !isManual);
  const needsReason = !isNoop && choice !== "auto";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="设计市况申报"
        className="relative flex max-h-[88vh] w-full max-w-2xl flex-col rounded-xl border bg-background shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">设计市况申报</h2>
            <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">{runId}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={() => void reinfer()}
              disabled={busy}
              className="inline-flex items-center gap-1 rounded-lg border px-2 py-1.5 text-xs hover:bg-muted disabled:opacity-40"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />
              重新推断
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
          {loading ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 读取申报…
            </div>
          ) : null}

          {error ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 px-2 py-1.5 text-xs text-destructive">
              {error}
            </div>
          ) : null}

          {style ? (
            <>
              <div className="rounded-lg border px-3 py-2">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="text-base font-semibold">
                    {regimeLabel(style.designed_regime)}
                  </span>
                  <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {sourceLabel(style.designed_regime_source)}
                  </span>
                  {typeof style.designed_regime_confidence === "number" ? (
                    <span className="text-[11px] text-muted-foreground">
                      置信 {confLabel(style)}
                    </span>
                  ) : null}
                </div>
                <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                  {style.designed_regime_note || "—"}
                </p>
                {divergenceNote ? (
                  <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
                    {divergenceNote}
                  </p>
                ) : null}
              </div>

              {mismatchNote ? (
                <CheckRow tone="warn" title="结构错配：" detail={`${mismatchNote}。`} />
              ) : null}
              {coverage?.note ? (
                <CheckRow
                  tone={coverage.ok ? "ok" : "warn"}
                  title={coverage.ok ? "样本覆盖：" : "样本覆盖不足："}
                  detail={coverage.note}
                />
              ) : null}
              {empirical?.available && empirical.note ? (
                <CheckRow
                  tone={empirical.consistent ? "ok" : "warn"}
                  title="事后归因（不参与判定）："
                  detail={empirical.note}
                />
              ) : null}
              {prior?.available && prior.note ? (
                <CheckRow
                  tone={prior.agree ? "ok" : "warn"}
                  title="事前申报对照："
                  detail={prior.note}
                />
              ) : null}

              {panel ? (
                <div className="space-y-1.5">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <h3 className="text-xs font-semibold">板块自身市况（等权合成指数）</h3>
                    <span className="text-[10px] text-muted-foreground">
                      {panel.window?.[0]} ~ {panel.window?.[1]} · {panel.panel_symbols ?? 0} 只 ·{" "}
                      {panel.labeled_days ?? 0} 个可判定交易日
                    </span>
                  </div>
                  <ShareBar shares={panel.regime_share} />
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
                    <span>趋势拟合 R² {panel.trend_r2 ?? "—"}</span>
                    <span>年化斜率 {pct(panel.trend_slope_ann, 1)}</span>
                    <span>年化波动 {pct(panel.annual_vol)}</span>
                    <span>板块最大回撤 {pct(panel.panel_max_drawdown)}</span>
                    <span>MA20 年均穿越 {panel.ma20_crosses_per_year ?? "—"} 次</span>
                  </div>
                </div>
              ) : null}

              {empirical?.by_regime ? (
                <div>
                  <h3 className="mb-1 text-xs font-semibold">分市况实测表现（仅核对）</h3>
                  <table className="w-full text-[11px]">
                    <thead className="text-muted-foreground">
                      <tr className="text-left">
                        <th className="py-0.5 font-normal">市况</th>
                        <th className="py-0.5 text-right font-normal">天数</th>
                        <th className="py-0.5 text-right font-normal">日均 bp</th>
                        <th className="py-0.5 text-right font-normal">累计</th>
                        <th className="py-0.5 text-right font-normal">夏普</th>
                      </tr>
                    </thead>
                    <tbody>
                      {REGIME_ORDER.filter((r) => empirical.by_regime?.[r]).map((r) => {
                        const row = empirical.by_regime![r];
                        return (
                          <tr key={r} className="border-t">
                            <td className="py-0.5">{regimeLabel(r)}</td>
                            <td className="py-0.5 text-right font-mono">{row.days}</td>
                            <td className="py-0.5 text-right font-mono">
                              {row.avg_daily_bp.toFixed(1)}
                            </td>
                            <td className="py-0.5 text-right font-mono">
                              {pct(row.cum_return)}
                            </td>
                            <td className="py-0.5 text-right font-mono">{row.sharpe.toFixed(2)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : null}

              {evidence?.factors?.length ? (
                <div>
                  <h3 className="mb-1 text-xs font-semibold">判定依据（逐项打分）</h3>
                  <FactorRows evidence={evidence} />
                </div>
              ) : null}

              {evidence?.confidence_notes?.length ? (
                <ul className="list-disc ps-4 text-[10px] text-muted-foreground">
                  {evidence.confidence_notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              ) : null}

              <p className="rounded-md bg-muted/40 px-2 py-1.5 text-[10px] leading-relaxed text-muted-foreground">
                判定只吃两类输入：策略结构（择时、离场规则、调仓节奏、池宽度）与板块行情本身。
                策略自身的收益、回撤、胜率不参与判定，只在「事后归因」里披露，用来核对申报和事实是否一致。
                判定结果不参与任何准入校验，也不改变可交易池。
              </p>
            </>
          ) : null}
        </div>

        <div className="shrink-0 space-y-2 border-t px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="regime-choice" className="text-[11px] text-muted-foreground">
              手工调整
            </label>
            <select
              id="regime-choice"
              value={choice}
              onChange={(e) => setChoice(e.target.value)}
              className="rounded-md border bg-background px-2 py-1 text-xs"
            >
              <option value="auto">自动推断（撤销手工申报）</option>
              {REGIME_ORDER.map((r) => (
                <option key={r} value={r}>
                  {regimeLabel(r)}
                </option>
              ))}
            </select>
            <span className="text-[10px] text-muted-foreground">
              {isManual ? "当前为人工申报，重新推断不会覆盖它" : "自动推断结果，可被人工改判"}
            </span>
          </div>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            placeholder={needsReason ? "必填：为什么认为自动判断不对（会写入申报记录）" : "可选备注"}
            className="w-full resize-none rounded-md border bg-background px-2 py-1 text-xs"
          />
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border px-3 py-1.5 text-xs hover:bg-muted"
            >
              关闭
            </button>
            <button
              type="button"
              disabled={busy || isNoop || (needsReason && !reason.trim())}
              onClick={() => void saveOverride()}
              className={cn(
                "rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground",
                "disabled:opacity-40",
              )}
            >
              {choice === "auto" ? "恢复自动推断" : "保存申报"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
