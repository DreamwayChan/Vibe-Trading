import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PersonalCopilotPanel } from "@/components/monitor/PersonalCopilotPanel";

vi.mock("@/lib/api", () => ({
  api: {
    getMyPortfolio: vi.fn(),
    updateMyPortfolio: vi.fn(),
    requestMonitorAdvisory: vi.fn(),
    runMonitorDailyClose: vi.fn(),
  },
}));

import { api } from "@/lib/api";

describe("PersonalCopilotPanel", () => {
  beforeEach(() => {
    vi.mocked(api.getMyPortfolio).mockResolvedValue({
      total_capital: 200000,
      available_cash: null,
      effective_cash: 170000,
      max_positions: 3,
      max_stop_loss_pct: 0.1,
      risk_per_trade_pct: 0.015,
      min_trade_amount: 10000,
      min_rebalance_delta_pct: 0.03,
      positions: [{ symbol: "000001.SZ", shares: 2000, cost_price: 15.0 }],
    });
    vi.mocked(api.requestMonitorAdvisory).mockResolvedValue({
      symbol: "300750.SZ",
      as_of: "2026-02-04",
      generated_at: "2026-02-04T16:30:00Z",
      mode: "rule_fallback",
      advisory_only: true,
      verdict: "execute_plan",
      verdict_label: "共振充分 · 可按计划限价定仓执行",
      confidence_note: "五维共振 4.3/5.0 星",
      supporting_points: ["主线板块顺风", "贴近MA20低吸买点"],
      devil_advocate_risks: ["高开超3.5%勿追"],
      tomorrow_playbook: ["早盘回踩区间挂限价单"],
      narrative: "胜率与赔率匹配，严格挂止损条件单。",
      context_digest: {},
      journal_draft: {
        symbol: "300750.SZ",
        observation: "五维共振 4.3 星",
        hypothesis: "因为主线顺风，因此波段看多",
        logic: "回踩均线低吸",
        validation_plan: "计成本回测+纸面跟踪",
        invalidation: "price < 165.00",
        thesis: "电池主线低吸",
        planned_position: "200 股",
        planned_risk: "最大亏损 ¥3,000",
        invalidation_price: 165.0,
        invalidation_side: "below",
      },
      boundary_note: "仅作交易前交叉质询参考",
    });
  });

  it("renders breadth, trade plan cards, AI copilot drawer, and fills journal draft", async () => {
    const onBoardUpdated = vi.fn();
    const onFillJournalDraft = vi.fn();
    const user = userEvent.setup();

    render(
      <PersonalCopilotPanel
        runId="run_demo"
        conviction={{
          as_of: "2026-02-04",
          max_focus: 3,
          focus_symbols: ["300750.SZ"],
          stance: "actionable",
          headline: "五维共振通过：精力聚焦 Top 1 —— 300750.SZ。",
          breadth: {
            as_of: "2026-02-04",
            sample_size: 4800,
            sufficient_sample: true,
            state: "warm",
            stance_cap: "full",
            score: 0.78,
            summary: "赚钱效应偏暖：上涨占比 64% · 涨跌停 52:2",
          },
          items: [],
        }}
        tradePlan={{
          as_of: "2026-02-04",
          exec_session: "2026-02-05",
          total_capital: 200000,
          effective_cash: 170000,
          max_positions: 3,
          held_count: 1,
          slots_available: 2,
          max_stop_loss_pct: 0.1,
          risk_per_trade_pct: 0.015,
          summary_note: "聚焦 1 只高胜率共振标的",
          position_actions: [],
          entry_plans: [
            {
              symbol: "300750.SZ",
              action: "buy_focus",
              title: "共振焦点买入 · 200 股",
              ref_close: 180.0,
              planned_shares: 200,
              planned_amount: 36000,
              capital_weight: 0.18,
              ideal_buy_low: 177.3,
              ideal_buy_high: 182.7,
              chase_stop_above: 186.3,
              stop_price: 165.0,
              stop_distance_pct: 0.0833,
              target_price: 210.0,
              reward_risk_ratio: 2.0,
              max_loss_rmb: 3000,
              max_loss_capital_pct: 0.015,
              held_shares: 0,
              sellable_shares: 0,
              stars: 4.3,
              board_name: "电池",
              broker_tip: "理想挂单区间 ¥177.30~¥182.70 买入 200 股",
              reason: "主线强顺风",
              warnings: [],
            },
          ],
        }}
        onBoardUpdated={onBoardUpdated}
        onFillJournalDraft={onFillJournalDraft}
      />,
    );

    expect(screen.getByText("赚钱效应偏暖")).toBeInTheDocument();
    expect(screen.getByText("300750.SZ")).toBeInTheDocument();
    expect(screen.getByText("共振焦点买入 · 200 股")).toBeInTheDocument();

    // Open AI Copilot drawer
    await user.click(screen.getByRole("button", { name: /AI 结合项目研判/ }));
    await waitFor(() => {
      expect(screen.getByText("共振充分 · 可按计划限价定仓执行")).toBeInTheDocument();
    });

    // Click pre-fill 5-field journal button
    await user.click(screen.getByRole("button", { name: /认可计划 → 填入下方五栏交易日志/ }));
    expect(onFillJournalDraft).toHaveBeenCalledTimes(1);
    expect(onFillJournalDraft.mock.calls[0][0].symbol).toBe("300750.SZ");
  });
});
