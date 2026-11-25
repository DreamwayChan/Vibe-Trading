import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ClipboardCheck,
  Loader2,
  MessageSquareCode,
  Plus,
  ShieldAlert,
  Sparkles,
  Trash2,
  Wallet,
} from "lucide-react";
import {
  api,
  type AdvisoryJournalDraft,
  type MonitorAdvisoryReport,
  type MonitorBoard,
  type MonitorConvictionSummary,
  type MonitorTradePlanSheet,
  type MyPortfolio,
  type PlannedOrderRow,
  type RealPositionItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";

interface PersonalCopilotPanelProps {
  runId: string;
  conviction?: MonitorConvictionSummary | null;
  tradePlan?: MonitorTradePlanSheet | null;
  onBoardUpdated: (board: MonitorBoard) => void;
  onFillJournalDraft: (draft: AdvisoryJournalDraft) => void;
}

function breadthChipClass(state: string | undefined): string {
  if (state === "warm") {
    return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
  }
  if (state === "cold") {
    return "bg-rose-500/15 text-rose-700 dark:text-rose-400";
  }
  return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
}

function breadthLabel(state: string | undefined): string {
  if (state === "warm") return "赚钱效应偏暖";
  if (state === "cold") return "退潮冰点预警";
  return "结构分化期";
}

function planBadgeClass(action: string): string {
  if (action === "stop_exit") {
    return "bg-rose-500/15 text-rose-700 dark:text-rose-400 font-semibold";
  }
  if (action === "signal_exit") {
    return "bg-amber-500/15 text-amber-700 dark:text-amber-400 font-semibold";
  }
  if (action === "buy_focus") {
    return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 font-semibold";
  }
  if (action === "hold_keep") {
    return "bg-sky-500/15 text-sky-700 dark:text-sky-400";
  }
  return "bg-muted text-muted-foreground";
}

function verdictBadgeClass(verdict: string): string {
  if (verdict === "execute_plan") {
    return "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400";
  }
  if (verdict === "probe_small") {
    return "bg-sky-500/15 text-sky-700 dark:text-sky-400";
  }
  if (verdict === "exit_discipline") {
    return "bg-rose-500/15 text-rose-700 dark:text-rose-400";
  }
  return "bg-amber-500/15 text-amber-700 dark:text-amber-400";
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `¥${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function fmtPx(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `¥${v.toFixed(2)}`;
}

function fmtPct(v: number | null | undefined, signed = false): string {
  if (v == null || Number.isNaN(v)) return "—";
  const pct = (v * 100).toFixed(1);
  return signed && v > 0 ? `+${pct}%` : `${pct}%`;
}

export function PersonalCopilotPanel({
  runId,
  conviction,
  tradePlan,
  onBoardUpdated,
  onFillJournalDraft,
}: PersonalCopilotPanelProps) {
  const [portfolio, setPortfolio] = useState<MyPortfolio | null>(null);
  const [portfolioOpen, setPortfolioOpen] = useState(false);
  const [savingPortfolio, setSavingPortfolio] = useState(false);
  const [portfolioMsg, setPortfolioMsg] = useState<string | null>(null);

  // Editable form state for My Portfolio
  const [capitalInput, setCapitalInput] = useState("200000");
  const [cashInput, setCashInput] = useState("");
  const [maxPosInput, setMaxPosInput] = useState("3");
  const [maxStopPctInput, setMaxStopPctInput] = useState("10");
  const [riskPctInput, setRiskPctInput] = useState("1.5");
  const [posRows, setPosRows] = useState<RealPositionItem[]>([]);

  // Daily-close routine state
  const [runningRoutine, setRunningRoutine] = useState(false);
  const [routineDigest, setRoutineDigest] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);

  // On-demand AI Advisory state keyed by symbol
  const [activeAdvisorySym, setActiveAdvisorySym] = useState<string | null>(null);
  const [advisoryNote, setAdvisoryNote] = useState<Record<string, string>>({});
  const [advisoryLoading, setAdvisoryLoading] = useState<string | null>(null);
  const [advisoryBySym, setAdvisoryBySym] = useState<Record<string, MonitorAdvisoryReport>>({});

  const syncFormFromPortfolio = (p: MyPortfolio) => {
    setPortfolio(p);
    setCapitalInput(String(p.total_capital ?? 200000));
    setCashInput(p.available_cash != null ? String(p.available_cash) : "");
    setMaxPosInput(String(p.max_positions ?? 3));
    setMaxStopPctInput(String(Number(((p.max_stop_loss_pct ?? 0.1) * 100).toFixed(1))));
    setRiskPctInput(String(Number(((p.risk_per_trade_pct ?? 0.015) * 100).toFixed(2))));
    setPosRows((p.positions || []).map((item) => ({ ...item })));
  };

  useEffect(() => {
    if (typeof api.getMyPortfolio !== "function") return;
    let cancelled = false;
    Promise.resolve(api.getMyPortfolio())
      .then((res) => {
        if (!cancelled && res) syncFormFromPortfolio(res);
      })
      .catch(() => {
        // Keep defaults if offline on cold start
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleAddPositionRow = () => {
    setPosRows((prev) => [
      ...prev,
      { symbol: "", shares: 100, cost_price: 10, buy_date: "", stop_price: null },
    ]);
  };

  const handleSavePortfolio = async () => {
    setSavingPortfolio(true);
    setPanelError(null);
    setPortfolioMsg(null);
    try {
      const cleaned = posRows
        .map((r) => ({
          symbol: r.symbol.trim().toUpperCase(),
          shares: Math.max(0, Math.floor(Number(r.shares) || 0)),
          cost_price: Number(r.cost_price) || 0,
          buy_date: (r.buy_date || "").trim(),
          stop_price:
            r.stop_price != null && String(r.stop_price).trim() !== "" && Number(r.stop_price) > 0
              ? Number(r.stop_price)
              : null,
        }))
        .filter((r) => r.symbol && r.shares > 0 && r.cost_price > 0);

      const res = await api.updateMyPortfolio({
        total_capital: Math.max(1000, Number(capitalInput) || 200000),
        available_cash: cashInput.trim() === "" ? null : Math.max(0, Number(cashInput) || 0),
        max_positions: Math.max(1, Math.min(10, Math.floor(Number(maxPosInput) || 3))),
        max_stop_loss_pct: Math.max(0.02, Math.min(0.3, (Number(maxStopPctInput) || 10) / 100)),
        risk_per_trade_pct: Math.max(0.002, Math.min(0.1, (Number(riskPctInput) || 1.5) / 100)),
        positions: cleaned,
      });
      syncFormFromPortfolio(res.portfolio);
      onBoardUpdated(res.board);
      setPortfolioMsg("已保存真实账户设置，次日计划卡股数与槽位已同步更新。");
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingPortfolio(false);
    }
  };

  const handleRunDailyClose = async () => {
    if (!runId.trim()) return;
    setRunningRoutine(true);
    setPanelError(null);
    try {
      const res = await api.runMonitorDailyClose({ settle_paper_book: true });
      onBoardUpdated(res.board);
      setRoutineDigest(res.routine.digest_markdown || null);
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunningRoutine(false);
    }
  };

  const handleAskAdvisory = async (symbol: string, useLlm = true) => {
    setActiveAdvisorySym(symbol);
    setAdvisoryLoading(symbol);
    setPanelError(null);
    try {
      const report = await api.requestMonitorAdvisory({
        symbol,
        user_note: advisoryNote[symbol] || "",
        use_llm: useLlm,
      });
      setAdvisoryBySym((prev) => ({ ...prev, [symbol]: report }));
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : String(e));
    } finally {
      setAdvisoryLoading(null);
    }
  };

  const breadth = conviction?.breadth;
  const posActions = tradePlan?.position_actions || [];
  const entryPlans = tradePlan?.entry_plans || [];
  const heldCount = portfolio?.positions?.length ?? tradePlan?.held_count ?? 0;
  const maxPos = portfolio?.max_positions ?? tradePlan?.max_positions ?? 3;

  return (
    <section
      id="personal-copilot-panel"
      data-testid="personal-copilot-panel"
      className="rounded-lg border bg-card p-4 flex flex-col gap-4"
    >
      {/* Header Strip: Market Breadth + Focus Headline + Quick Controls */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold tracking-tight">
              个人实盘操作指南 · 五维共振聚焦
            </h2>
            {breadth && (
              <span
                className={cn(
                  "inline-flex items-center rounded px-2 py-0.5 text-xs font-medium",
                  breadthChipClass(breadth.state),
                )}
              >
                {breadthLabel(breadth.state)}
              </span>
            )}
            <span className="inline-flex items-center rounded bg-muted px-2 py-0.5 text-xs font-mono text-muted-foreground">
              实盘槽位 {heldCount}/{maxPos} 只 · 本金{" "}
              {fmtMoney(portfolio?.total_capital ?? tradePlan?.total_capital ?? 200000)}
            </span>
          </div>
          <p className="text-sm text-foreground/90">
            {conviction?.headline ||
              "点击上方「立即扫描」或右侧「盘后一键复盘」，自动筛出 2~3 只高胜率共振焦点并生成次日挂单计划。"}
          </p>
          {breadth?.summary && (
            <p className="text-xs text-muted-foreground">{breadth.summary}</p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            id="btn-toggle-my-portfolio"
            type="button"
            onClick={() => setPortfolioOpen((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-md border bg-background px-3 py-1.5 text-xs font-medium hover:bg-muted"
          >
            <Wallet className="h-3.5 w-3.5" />
            我的真实持仓 ({heldCount}/{maxPos})
            {portfolioOpen ? (
              <ChevronUp className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" />
            )}
          </button>
          <button
            id="btn-daily-close-routine"
            type="button"
            disabled={runningRoutine || !runId.trim()}
            onClick={() => void handleRunDailyClose()}
            title="顺序执行：监控扫描 + 五维共振聚焦 + 次日计划单生成 + 纸面账本结算"
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            {runningRoutine ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <ClipboardCheck className="h-3.5 w-3.5" />
            )}
            {runningRoutine ? "复盘结算中…" : "16:30 盘后一键复盘"}
          </button>
        </div>
      </div>

      {panelError && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {panelError}
        </div>
      )}

      {/* Collapsible Simple Real-Portfolio Editor */}
      {portfolioOpen && (
        <div
          id="my-portfolio-editor"
          className="rounded-md border bg-muted/20 p-3.5 flex flex-col gap-3"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-sm font-semibold">我的真实账户与风控刻度</h3>
              <p className="text-xs text-muted-foreground">
                输入 6 位代码（如 600519 / 300750）会自动补齐交易所后缀；保存后立即按「以损定仓」重算次日股数。
              </p>
            </div>
            <button
              type="button"
              onClick={handleAddPositionRow}
              className="inline-flex items-center gap-1 rounded border bg-background px-2.5 py-1 text-xs hover:bg-muted"
            >
              <Plus className="h-3.5 w-3.5" />
              添加实盘持仓
            </button>
          </div>

          <div className="grid gap-2.5 sm:grid-cols-5">
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">总投入本金 (¥)</span>
              <input
                type="number"
                value={capitalInput}
                onChange={(e) => setCapitalInput(e.target.value)}
                className="rounded border bg-background px-2.5 py-1.5 font-mono text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">可用现金 (留空自动算)</span>
              <input
                type="number"
                value={cashInput}
                placeholder="自动扣减持仓"
                onChange={(e) => setCashInput(e.target.value)}
                className="rounded border bg-background px-2.5 py-1.5 font-mono text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">最多持股只数 (建议2~3)</span>
              <input
                type="number"
                min={1}
                max={10}
                value={maxPosInput}
                onChange={(e) => setMaxPosInput(e.target.value)}
                className="rounded border bg-background px-2.5 py-1.5 font-mono text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">最大个股止损 (%)</span>
              <input
                type="number"
                step="0.5"
                value={maxStopPctInput}
                onChange={(e) => setMaxStopPctInput(e.target.value)}
                className="rounded border bg-background px-2.5 py-1.5 font-mono text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs">
              <span className="text-muted-foreground">单笔本金风险上限 (%)</span>
              <input
                type="number"
                step="0.25"
                value={riskPctInput}
                onChange={(e) => setRiskPctInput(e.target.value)}
                className="rounded border bg-background px-2.5 py-1.5 font-mono text-sm"
              />
            </label>
          </div>

          {posRows.length > 0 ? (
            <div className="flex flex-col gap-2">
              {posRows.map((row, idx) => (
                <div
                  key={idx}
                  className="grid gap-2 sm:grid-cols-12 items-center rounded border bg-background p-2 text-xs"
                >
                  <input
                    type="text"
                    placeholder="股票代码 (如 300750)"
                    value={row.symbol}
                    onChange={(e) => {
                      const next = [...posRows];
                      next[idx] = { ...next[idx], symbol: e.target.value };
                      setPosRows(next);
                    }}
                    className="sm:col-span-3 rounded border bg-background px-2 py-1 font-mono"
                  />
                  <input
                    type="number"
                    placeholder="持仓股数"
                    value={row.shares}
                    onChange={(e) => {
                      const next = [...posRows];
                      next[idx] = { ...next[idx], shares: Number(e.target.value) };
                      setPosRows(next);
                    }}
                    className="sm:col-span-2 rounded border bg-background px-2 py-1 font-mono"
                  />
                  <input
                    type="number"
                    step="0.01"
                    placeholder="成本价"
                    value={row.cost_price}
                    onChange={(e) => {
                      const next = [...posRows];
                      next[idx] = { ...next[idx], cost_price: Number(e.target.value) };
                      setPosRows(next);
                    }}
                    className="sm:col-span-2 rounded border bg-background px-2 py-1 font-mono"
                  />
                  <input
                    type="date"
                    title="买入日期（当日买入受 T+1 锁仓）"
                    value={row.buy_date || ""}
                    onChange={(e) => {
                      const next = [...posRows];
                      next[idx] = { ...next[idx], buy_date: e.target.value };
                      setPosRows(next);
                    }}
                    className="sm:col-span-2 rounded border bg-background px-2 py-1 font-mono"
                  />
                  <input
                    type="number"
                    step="0.01"
                    placeholder="手工止损价(选填)"
                    value={row.stop_price ?? ""}
                    onChange={(e) => {
                      const next = [...posRows];
                      next[idx] = {
                        ...next[idx],
                        stop_price: e.target.value === "" ? null : Number(e.target.value),
                      };
                      setPosRows(next);
                    }}
                    className="sm:col-span-2 rounded border bg-background px-2 py-1 font-mono"
                  />
                  <button
                    type="button"
                    aria-label="删除持仓行"
                    onClick={() => setPosRows(posRows.filter((_, i) => i !== idx))}
                    className="sm:col-span-1 inline-flex justify-center rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              当前未录入实盘持仓（视为空仓状态，可用 {maxPos} 个开仓槽位）。
            </p>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={savingPortfolio}
              onClick={() => void handleSavePortfolio()}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {savingPortfolio && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              保存账户并更新计划单
            </button>
            {portfolioMsg && (
              <span className="text-xs text-emerald-600 dark:text-emerald-400">
                {portfolioMsg}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Optional Daily Close Digest */}
      {routineDigest && (
        <div className="rounded-md border bg-muted/20 p-3 text-xs flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="font-semibold">盘后一键复盘简报</span>
            <button
              type="button"
              onClick={() => setRoutineDigest(null)}
              className="text-muted-foreground hover:text-foreground"
            >
              收起
            </button>
          </div>
          <pre className="whitespace-pre-wrap font-sans leading-relaxed text-foreground/90">
            {routineDigest}
          </pre>
        </div>
      )}

      {/* Next-Session Actionable Trade Plan Sheet */}
      {tradePlan && (posActions.length > 0 || entryPlans.length > 0) && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
            <div>
              <h3 className="text-sm font-semibold">
                《次日可执行交易计划卡》· 目标交易日 {tradePlan.exec_session}
              </h3>
              <p className="text-xs text-muted-foreground">{tradePlan.summary_note}</p>
            </div>
            <span className="text-xs font-mono text-muted-foreground">
              可用现金 {fmtMoney(tradePlan.effective_cash)} · 剩余槽位 {tradePlan.slots_available}/
              {tradePlan.max_positions}
            </span>
          </div>

          {/* 1. Existing Real Positions Discipline */}
          {posActions.length > 0 && (
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">
                一、 现有实盘持仓处理（先管住旧仓止损与去留）
              </span>
              <div className="grid gap-2.5 md:grid-cols-2">
                {posActions.map((row) => (
                  <OrderPlanCard
                    key={`pos-${row.symbol}`}
                    row={row}
                    advisory={advisoryBySym[row.symbol]}
                    advisoryOpen={activeAdvisorySym === row.symbol}
                    advisoryLoading={advisoryLoading === row.symbol}
                    noteValue={advisoryNote[row.symbol] || ""}
                    onNoteChange={(val) =>
                      setAdvisoryNote((prev) => ({ ...prev, [row.symbol]: val }))
                    }
                    onToggleAdvisory={() => {
                      if (activeAdvisorySym === row.symbol) {
                        setActiveAdvisorySym(null);
                      } else if (advisoryBySym[row.symbol]) {
                        setActiveAdvisorySym(row.symbol);
                      } else {
                        void handleAskAdvisory(row.symbol, true);
                      }
                    }}
                    onRunAdvisory={(useLlm) => void handleAskAdvisory(row.symbol, useLlm)}
                    onFillDraft={onFillJournalDraft}
                  />
                ))}
              </div>
            </div>
          )}

          {/* 2. Top 2-3 Focus New Entry Plans */}
          {entryPlans.length > 0 && (
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground">
                二、 五维共振 Top {conviction?.max_focus ?? 3} 新开仓计划（以损定仓 · 高开不追）
              </span>
              <div className="grid gap-2.5 md:grid-cols-2">
                {entryPlans.map((row) => (
                  <OrderPlanCard
                    key={`entry-${row.symbol}`}
                    row={row}
                    advisory={advisoryBySym[row.symbol]}
                    advisoryOpen={activeAdvisorySym === row.symbol}
                    advisoryLoading={advisoryLoading === row.symbol}
                    noteValue={advisoryNote[row.symbol] || ""}
                    onNoteChange={(val) =>
                      setAdvisoryNote((prev) => ({ ...prev, [row.symbol]: val }))
                    }
                    onToggleAdvisory={() => {
                      if (activeAdvisorySym === row.symbol) {
                        setActiveAdvisorySym(null);
                      } else if (advisoryBySym[row.symbol]) {
                        setActiveAdvisorySym(row.symbol);
                      } else {
                        void handleAskAdvisory(row.symbol, true);
                      }
                    }}
                    onRunAdvisory={(useLlm) => void handleAskAdvisory(row.symbol, useLlm)}
                    onFillDraft={onFillJournalDraft}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

interface OrderPlanCardProps {
  row: PlannedOrderRow;
  advisory?: MonitorAdvisoryReport;
  advisoryOpen: boolean;
  advisoryLoading: boolean;
  noteValue: string;
  onNoteChange: (value: string) => void;
  onToggleAdvisory: () => void;
  onRunAdvisory: (useLlm: boolean) => void;
  onFillDraft: (draft: AdvisoryJournalDraft) => void;
}

function OrderPlanCard({
  row,
  advisory,
  advisoryOpen,
  advisoryLoading,
  noteValue,
  onNoteChange,
  onToggleAdvisory,
  onRunAdvisory,
  onFillDraft,
}: OrderPlanCardProps) {
  const isEntry = row.action === "buy_focus" || row.action === "slot_full_watch" || row.action === "skip_small_delta";

  return (
    <div className="rounded-md border bg-background p-3 flex flex-col gap-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm font-bold">{row.symbol}</span>
          <span className={cn("rounded px-2 py-0.5 text-xs", planBadgeClass(row.action))}>
            {row.title}
          </span>
          {row.stars != null && (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
              {row.stars.toFixed(1)} ★
            </span>
          )}
          {row.board_name && (
            <span className="text-xs text-muted-foreground">{row.board_name}</span>
          )}
        </div>
        <span className="font-mono text-xs text-muted-foreground">
          昨收 {fmtPx(row.ref_close)}
        </span>
      </div>

      {/* Key Quantitative Execution Anchors */}
      {isEntry ? (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 rounded bg-muted/30 p-2 text-xs">
          <div>
            <div className="text-muted-foreground">理想挂单区间</div>
            <div className="font-mono font-medium">
              {fmtPx(row.ideal_buy_low)}~{fmtPx(row.ideal_buy_high)}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">高开放弃线</div>
            <div className="font-mono font-medium text-amber-700 dark:text-amber-400">
              &gt; {fmtPx(row.chase_stop_above)} 不追
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">硬止损价 ({fmtPct(row.stop_distance_pct ? -row.stop_distance_pct : null)})</div>
            <div className="font-mono font-medium text-rose-600 dark:text-rose-400">
              {fmtPx(row.stop_price)}
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">本笔最大亏损</div>
            <div className="font-mono font-medium">
              {row.max_loss_rmb != null
                ? `${fmtMoney(row.max_loss_rmb)} (${fmtPct(row.max_loss_capital_pct)})`
                : "—"}
            </div>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-3 gap-2 rounded bg-muted/30 p-2 text-xs">
          <div>
            <div className="text-muted-foreground">持仓 / 可卖</div>
            <div className="font-mono font-medium">
              {row.held_shares} / {row.sellable_shares} 股
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">成本 / 浮盈亏</div>
            <div
              className={cn(
                "font-mono font-medium",
                (row.unrealized_pnl_pct || 0) >= 0
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-rose-600 dark:text-rose-400",
              )}
            >
              {fmtPx(row.cost_price)} ({fmtPct(row.unrealized_pnl_pct, true)})
            </div>
          </div>
          <div>
            <div className="text-muted-foreground">纪律止损保护位</div>
            <div className="font-mono font-medium text-rose-600 dark:text-rose-400">
              {fmtPx(row.stop_price)}
            </div>
          </div>
        </div>
      )}

      <p className="text-xs leading-relaxed text-foreground/90">{row.broker_tip}</p>
      {row.reason && <p className="text-xs text-muted-foreground">依据：{row.reason}</p>}

      {row.warnings && row.warnings.length > 0 && (
        <div className="flex flex-col gap-1 text-xs text-amber-700 dark:text-amber-400">
          {row.warnings.slice(0, 2).map((w, i) => (
            <div key={i} className="inline-flex items-center gap-1">
              <AlertTriangle className="h-3 w-3 shrink-0" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {/* Action Buttons: On-Demand AI Copilot + Journal Pre-fill */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        <button
          type="button"
          onClick={onToggleAdvisory}
          disabled={advisoryLoading}
          className="inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-medium hover:bg-muted disabled:opacity-50"
        >
          {advisoryLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Sparkles className="h-3.5 w-3.5" />
          )}
          {advisoryOpen ? "收起 AI 参谋" : "AI 结合项目研判"}
        </button>

        {advisory?.journal_draft && (
          <button
            type="button"
            onClick={() => onFillDraft(advisory.journal_draft)}
            className="inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <MessageSquareCode className="h-3.5 w-3.5" />
            一键预填五栏日志
          </button>
        )}
      </div>

      {/* Expandable Pre-Trade AI Copilot Review Drawer */}
      {advisoryOpen && (
        <div className="mt-1 rounded-md border bg-muted/20 p-3 flex flex-col gap-2.5 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={noteValue}
              onChange={(e) => onNoteChange(e.target.value)}
              placeholder="补充你犹豫的问题（如：板块是不是高潮了？盈亏比划算吗？）"
              className="flex-1 min-w-[200px] rounded border bg-background px-2.5 py-1 text-xs"
            />
            <button
              type="button"
              disabled={advisoryLoading}
              onClick={() => onRunAdvisory(true)}
              className="rounded bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {advisoryLoading ? "研判中…" : "重新质询 AI"}
            </button>
          </div>

          {advisory && (
            <>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <span
                    className={cn(
                      "rounded px-2 py-0.5 font-semibold",
                      verdictBadgeClass(advisory.verdict),
                    )}
                  >
                    {advisory.verdict_label}
                  </span>
                  <span className="text-muted-foreground">{advisory.confidence_note}</span>
                </div>
                <span className="text-[11px] text-muted-foreground">
                  引擎：{advisory.mode === "llm" ? "AI 大模型 + 项目全量快照" : "本地规则快照"}
                </span>
              </div>

              <p className="leading-relaxed text-foreground">{advisory.narrative}</p>

              <div className="grid gap-2 sm:grid-cols-2">
                <div className="rounded border bg-background p-2">
                  <div className="font-medium text-emerald-700 dark:text-emerald-400 mb-1 inline-flex items-center gap-1">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    共振支撑证据
                  </div>
                  <ul className="list-disc pl-4 space-y-0.5 text-muted-foreground">
                    {advisory.supporting_points.map((pt, i) => (
                      <li key={i}>{pt}</li>
                    ))}
                  </ul>
                </div>

                <div className="rounded border bg-background p-2">
                  <div className="font-medium text-rose-700 dark:text-rose-400 mb-1 inline-flex items-center gap-1">
                    <ShieldAlert className="h-3.5 w-3.5" />
                    反方挑刺与盲点排查
                  </div>
                  <ul className="list-disc pl-4 space-y-0.5 text-muted-foreground">
                    {advisory.devil_advocate_risks.map((rk, i) => (
                      <li key={i}>{rk}</li>
                    ))}
                  </ul>
                </div>
              </div>

              {advisory.tomorrow_playbook.length > 0 && (
                <div className="rounded border bg-background p-2">
                  <div className="font-medium mb-1">明日开盘应对剧本</div>
                  <ul className="list-decimal pl-4 space-y-0.5 text-muted-foreground">
                    {advisory.tomorrow_playbook.map((step, i) => (
                      <li key={i}>{step}</li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="flex flex-wrap items-center justify-between gap-2 pt-0.5 text-[11px] text-muted-foreground">
                <span>{advisory.boundary_note}</span>
                <button
                  type="button"
                  onClick={() => onFillDraft(advisory.journal_draft)}
                  className="rounded border bg-background px-2 py-0.5 text-foreground hover:bg-muted"
                >
                  认可计划 → 填入下方五栏交易日志
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
