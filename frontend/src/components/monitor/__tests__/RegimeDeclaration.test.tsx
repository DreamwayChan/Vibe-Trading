import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RegimeChip, RegimePanelModal } from "@/components/monitor/RegimeDeclaration";
import type { StrategyStyle } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  api: {
    getRunRegime: vi.fn(),
    patchRunRegime: vi.fn(),
    inferRunRegime: vi.fn(),
  },
}));

import { api } from "@/lib/api";

const STYLE: StrategyStyle = {
  style_label: "横截面·周频·6等权·MA20择时",
  designed_regime: "chop",
  designed_regime_label: "震荡",
  designed_regime_source: "auto_inferred",
  designed_regime_confidence: 0.3,
  designed_regime_auto: "chop",
  designed_regime_note: "机制层 MA20 半仓 + 低吸高抛；板块层快牛日占 68%",
  designed_regime_evidence: {
    method: "mechanics_x_panel_v1",
    panel: {
      window: ["2024-01-03", "2026-08-20"],
      panel_symbols: 27,
      labeled_days: 498,
      trend_r2: 0.66,
      trend_slope_ann: 0.28,
      annual_vol: 0.43,
      panel_max_drawdown: -0.42,
      ma20_crosses_per_year: 24.9,
      regime_share: { bull: 0.68, slow_bull: 0.09, chop: 0.15, bear: 0.08 },
    },
    factors: [
      {
        id: "mechanics.timing",
        label: "板块择时",
        weight: 0.26,
        value: "ma20_half",
        scores: { bull: 0.26, slow_bull: 0.66, chop: 0.8, bear: 0.4 },
        note: "跌破 MA20 降到半仓",
      },
    ],
    verdict: {
      designed_regime: "chop",
      confidence: 0.3,
      tie: false,
      runner_up: "slow_bull",
      runner_up_score: 0.7,
    },
    effective: { designed_regime: "chop", source: "auto_inferred" },
    checks: {
      coverage: {
        regime: "chop",
        share: 0.15,
        dominant_regime: "bull",
        dominant_label: "快速单边多头",
        ok: false,
        note: "申报市况在样本内占 15%，板块主市况为快速单边多头（68%）",
      },
      empirical: {
        used_for_decision: false,
        available: true,
        metric: "avg_daily_bp",
        best_regime: "bull",
        best_label: "快速单边多头",
        declared_rank: 3,
        consistent: false,
        note: "该策略在快速单边多头的日子里日均收益最高（33 bp/日）",
        by_regime: {
          bull: { days: 150, avg_daily_bp: 33, cum_return: 0.77, ann_return: 1.5, sharpe: 3.5 },
          chop: { days: 80, avg_daily_bp: 12, cum_return: 0.1, ann_return: 0.3, sharpe: 1.1 },
        },
      },
    },
  },
};

describe("RegimeChip", () => {
  it("renders the declaration, confidence, source and sample coverage", () => {
    render(<RegimeChip style={STYLE} />);
    const chip = screen.getByRole("button");
    expect(chip.textContent).toContain("设计市况：震荡");
    expect(chip.textContent).toContain("置信 低 0.30");
    expect(chip.textContent).toContain("自动推断");
    expect(chip.textContent).toContain("样本占 15%");
  });

  it("renders nothing when the run has no declaration", () => {
    const { container } = render(<RegimeChip style={null} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("RegimePanelModal", () => {
  beforeEach(() => {
    vi.mocked(api.getRunRegime).mockResolvedValue({ run_id: "r1", strategy_style: STYLE });
    vi.mocked(api.patchRunRegime).mockResolvedValue({
      run_id: "r1",
      strategy_style: { ...STYLE, designed_regime: "slow_bull", designed_regime_source: "manual_override" },
    });
    vi.mocked(api.inferRunRegime).mockResolvedValue({ run_id: "r1", strategy_style: STYLE });
  });

  it("shows the verdict, the mismatch warning and the per-factor evidence", async () => {
    render(<RegimePanelModal runId="r1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("判定依据（逐项打分）")).toBeTruthy());
    expect(screen.getByText(/结构错配/)).toBeTruthy();
    expect(screen.getByText(/样本覆盖不足/)).toBeTruthy();
    expect(screen.getAllByText(/事后归因/).length).toBeGreaterThan(0);
    expect(screen.getByText("板块择时")).toBeTruthy();
    // The evidence table explains why the strategy's own P&L cannot move the label.
    expect(screen.getByText(/策略自身的收益、回撤、胜率不参与判定/)).toBeTruthy();
  });

  it("requires a reason before saving a manual override", async () => {
    const user = userEvent.setup();
    render(<RegimePanelModal runId="r1" onClose={() => {}} />);
    await waitFor(() => expect(api.getRunRegime).toHaveBeenCalled());

    await user.selectOptions(screen.getByLabelText("手工调整"), "slow_bull");
    const save = screen.getByRole("button", { name: "保存申报" });
    expect((save as HTMLButtonElement).disabled).toBe(true);

    await user.type(screen.getByPlaceholderText(/必填/), "板块订单周期上行");
    expect((save as HTMLButtonElement).disabled).toBe(false);
    await user.click(save);

    await waitFor(() =>
      expect(api.patchRunRegime).toHaveBeenCalledWith("r1", "slow_bull", "板块订单周期上行"),
    );
  });
});
