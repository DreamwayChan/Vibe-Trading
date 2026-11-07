import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";
import type { CapitalPoint } from "@/lib/api";

/** Trading-day moving average, skipping gaps instead of poisoning them. */
export function movingAverage(values: (number | null)[], window: number): (number | null)[] {
  const out: (number | null)[] = [];
  let sum = 0;
  const queue: number[] = [];
  for (const value of values) {
    if (value == null || !Number.isFinite(value)) {
      out.push(null);
      continue;
    }
    queue.push(value);
    sum += value;
    if (queue.length > window) sum -= queue.shift() as number;
    out.push(queue.length === window ? sum / window : null);
  }
  return out;
}

interface Props {
  points: CapitalPoint[];
  height?: number;
  /** Overlay the benchmark index on the right axis. */
  showIndex?: boolean;
}

/**
 * Two-panel capital-flow chart:
 *   top    — ETF-OI ratio (%) with its 20-day average, plus the CSI 300 level
 *   bottom — daily net subscription (亿元), red for inflow / green for outflow
 */
export function EtfOiChart({ points, height = 420, showIndex = true }: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const { dark } = useDarkMode();

  const built = useMemo(() => {
    const dates = points.map((p) => p.date);
    const oi = points.map((p) => (p.oi_pct == null ? null : Number(p.oi_pct.toFixed(4))));
    const oiMa = movingAverage(oi, 20);
    const index = points.map((p) => p.index_close ?? null);
    const flow = points.map((p) => (p.net_flow == null ? null : Number((p.net_flow / 1e8).toFixed(2))));
    return { dates, oi, oiMa, index, flow, lookup: new Map(points.map((p) => [p.date, p])) };
  }, [points]);

  useEffect(() => {
    const el = ref.current;
    if (!el || built.dates.length === 0) return;
    // jsdom / collapsed layouts have no paint box — skip rather than crash ECharts.
    if (el.clientWidth < 8 || el.clientHeight < 8) return;
    const theme = getChartTheme();
    let chart: ReturnType<typeof echarts.init>;
    try {
      chart = echarts.init(el);
    } catch {
      return;
    }

    const inflowColor = theme.upColor;
    const outflowColor = theme.downColor;

    chart.setOption({
      backgroundColor: "transparent",
      animationDuration: 240,
      axisPointer: { link: [{ xAxisIndex: [0, 1] }] },
      legend: {
        top: 0,
        left: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: theme.textColor, fontSize: 11 },
        data: [
          t("capitalFlow.chartOi"),
          t("capitalFlow.chartOiMa"),
          ...(showIndex ? [t("capitalFlow.chartIndex")] : []),
          t("capitalFlow.chartFlow"),
        ],
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross", label: { backgroundColor: theme.axisColor } },
        backgroundColor: theme.tooltipBg,
        borderColor: theme.tooltipBorder,
        textStyle: { color: theme.tooltipText, fontSize: 11 },
        confine: true,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (params: any) => {
          if (!Array.isArray(params) || !params.length) return "";
          const day = String(params[0].axisValue || "");
          const point = built.lookup.get(day);
          if (!point) return day;
          const yi = (value?: number | null) =>
            value == null ? "—" : `${(value / 1e8).toFixed(1)}`;
          const rows: string[] = [`<b>${day}</b>`];
          if (point.provisional) {
            rows.push(`<span style="color:${theme.warningColor}">${t("capitalFlow.pendingBadge")}</span>`);
          }
          rows.push(
            `${t("capitalFlow.oiShort")} <b>${point.oi_pct == null ? "—" : point.oi_pct.toFixed(3)}%</b>`,
          );
          if (point.index_close != null) {
            rows.push(`${t("capitalFlow.indexShort")} ${point.index_close.toFixed(2)}`);
          }
          rows.push(`${t("capitalFlow.aumShort")} ${yi(point.aum)} ${t("capitalFlow.unitYi")}`);
          if (point.net_flow != null) {
            const color = point.net_flow >= 0 ? inflowColor : outflowColor;
            rows.push(
              `${t("capitalFlow.netFlowShort")} <b style="color:${color}">${point.net_flow >= 0 ? "+" : ""}${yi(point.net_flow)}</b> ${t("capitalFlow.unitYi")}`,
            );
          } else {
            rows.push(`${t("capitalFlow.netFlowShort")} —`);
          }
          if (point.nav_pnl != null) {
            rows.push(`${t("capitalFlow.navPnlShort")} ${yi(point.nav_pnl)} ${t("capitalFlow.unitYi")}`);
          }
          return rows.join("<br/>");
        },
      },
      grid: [
        { left: 10, right: 12, top: 46, height: "48%", containLabel: true },
        { left: 10, right: 12, top: "68%", height: "22%", containLabel: true },
      ],
      xAxis: [
        {
          type: "category",
          data: built.dates,
          gridIndex: 0,
          boundaryGap: false,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisLabel: { color: theme.textColor, fontSize: 10, show: false },
          axisTick: { show: false },
        },
        {
          type: "category",
          data: built.dates,
          gridIndex: 1,
          boundaryGap: true,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisLabel: { color: theme.textColor, fontSize: 10 },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          type: "value",
          name: t("capitalFlow.oiShort"),
          nameTextStyle: { color: theme.textColor, fontSize: 10, align: "left" },
          gridIndex: 0,
          scale: true,
          splitLine: { lineStyle: { color: theme.gridColor } },
          axisLabel: {
            color: theme.textColor,
            fontSize: 10,
            formatter: (value: number) => `${value.toFixed(2)}%`,
          },
        },
        {
          type: "value",
          name: t("capitalFlow.indexShort"),
          nameTextStyle: { color: theme.textColor, fontSize: 10, align: "right" },
          gridIndex: 0,
          scale: true,
          position: "right",
          splitLine: { show: false },
          axisLabel: {
            color: theme.textColor,
            fontSize: 10,
            formatter: (value: number) => value.toFixed(0),
          },
        },
        {
          type: "value",
          name: t("capitalFlow.flowAxis"),
          nameTextStyle: { color: theme.textColor, fontSize: 10, align: "left" },
          gridIndex: 1,
          splitLine: { lineStyle: { color: theme.gridColor } },
          axisLabel: {
            color: theme.textColor,
            fontSize: 10,
            formatter: (value: number) => `${value}`,
          },
        },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
        {
          type: "slider",
          xAxisIndex: [0, 1],
          height: 14,
          bottom: 4,
          borderColor: theme.gridColor,
          fillerColor: "rgba(37,99,235,0.12)",
          handleStyle: { color: theme.axisColor },
          textStyle: { color: theme.textColor, fontSize: 9 },
        },
      ],
      series: [
        {
          id: "oi",
          name: t("capitalFlow.chartOi"),
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: built.oi,
          connectNulls: true,
          symbol: "none",
          lineStyle: { width: 2.2, color: "#2563eb" },
          itemStyle: { color: "#2563eb" },
          areaStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(37,99,235,0.22)" },
                { offset: 1, color: "rgba(37,99,235,0.01)" },
              ],
            },
          },
          z: 6,
        },
        {
          id: "oi-ma",
          name: t("capitalFlow.chartOiMa"),
          type: "line",
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: built.oiMa,
          connectNulls: true,
          symbol: "none",
          lineStyle: { width: 1.3, color: "#f59e0b", type: "dashed" },
          itemStyle: { color: "#f59e0b" },
          z: 5,
        },
        ...(showIndex
          ? [
              {
                id: "index",
                name: t("capitalFlow.chartIndex"),
                type: "line" as const,
                xAxisIndex: 0,
                yAxisIndex: 1,
                data: built.index,
                connectNulls: true,
                symbol: "none",
                lineStyle: { width: 1.1, color: theme.textColor, opacity: 0.55 },
                itemStyle: { color: theme.textColor },
                z: 3,
              },
            ]
          : []),
        {
          id: "flow",
          name: t("capitalFlow.chartFlow"),
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: built.flow.map((value) => ({
            value,
            itemStyle: {
              color:
                value == null
                  ? "transparent"
                  : value >= 0
                    ? inflowColor
                    : outflowColor,
              opacity: value == null ? 0 : 0.85,
            },
          })),
          barMaxWidth: 12,
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: false },
            lineStyle: { color: theme.axisColor, width: 1, type: "solid", opacity: 0.6 },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    });

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(el);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [built, dark, showIndex, t]);

  if (built.dates.length === 0) return null;
  return <div ref={ref} style={{ height }} data-testid="etf-oi-chart" />;
}
