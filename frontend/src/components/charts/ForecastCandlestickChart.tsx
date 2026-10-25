import { useEffect, useMemo, useRef } from "react";
import i18n from "@/i18n";
import type { MonitorForecastItem } from "@/lib/api";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";

const MA_COLORS = ["#f59e0b", "#8b5cf6", "#3b82f6"];
const VISIBLE_HIST = 40;
/** Forecast MA lines are not in the legend; they follow the up/down scenario toggles. */
export const UP_SCENARIO_MA = ["MA5↑", "MA10↑", "MA20↑"] as const;
export const DOWN_SCENARIO_MA = ["MA5↓", "MA10↓", "MA20↓"] as const;

/** Satellite MA series hidden/shown with a scenario legend click. */
export function scenarioSatelliteSeries(
  legendName: string,
  legendUp: string,
  legendDown: string,
): readonly string[] {
  if (legendName === legendUp) return UP_SCENARIO_MA;
  if (legendName === legendDown) return DOWN_SCENARIO_MA;
  return [];
}

export interface ForecastChartModel {
  dates: string[];
  historyLen: number;
  historyCandle: Array<Array<number | null>>;
  upCustom: number[][];
  downCustom: number[][];
  maHist: { ma5: Array<number | null>; ma10: Array<number | null>; ma20: Array<number | null> };
  maUp: { ma5: Array<number | null>; ma10: Array<number | null>; ma20: Array<number | null> };
  maDown: { ma5: Array<number | null>; ma10: Array<number | null>; ma20: Array<number | null> };
  splitIndex: number;
  zoomStart: number;
}

function pad(values: Array<number | null> | undefined, len: number): Array<number | null> {
  const src = Array.isArray(values) ? values : [];
  const out: Array<number | null> = src.slice(0, len);
  while (out.length < len) out.push(null);
  return out;
}

function stitchForecastMa(
  histLen: number,
  futureLen: number,
  lastHist: number | null | undefined,
  future: Array<number | null> | undefined,
): Array<number | null> {
  const out: Array<number | null> = Array(histLen + futureLen).fill(null);
  // Connect at the last realized bar so the line does not jump a gap.
  if (histLen > 0 && lastHist != null) out[histLen - 1] = lastHist;
  const src = Array.isArray(future) ? future : [];
  for (let i = 0; i < futureLen; i++) {
    out[histLen + i] = src[i] ?? null;
  }
  return out;
}

/** Flatten history + dual scenario bars into ECharts-ready aligned series. */
export function buildForecastChartModel(item: Pick<MonitorForecastItem, "history" | "up_bars" | "down_bars" | "ma">): ForecastChartModel {
  const history = item.history || [];
  const upBars = item.up_bars || [];
  const downBars = item.down_bars || [];
  const futureDates = upBars.map((b) => b.time);
  const dates = [...history.map((b) => b.time), ...futureDates];
  const historyLen = history.length;
  const futureLen = futureDates.length;
  const historyCandle: Array<Array<number | null>> = [
    ...history.map((b) => [b.open, b.close, b.low, b.high]),
    ...futureDates.map(() => [null, null, null, null]),
  ];
  const upCustom = upBars.map((b, i) => [historyLen + i, b.open, b.close, b.low, b.high]);
  const downCustom = downBars.map((b, i) => [historyLen + i, b.open, b.close, b.low, b.high]);
  const histMa = item.ma?.history;
  const maHist = {
    ma5: pad(histMa?.ma5, historyLen).concat(Array(futureLen).fill(null)),
    ma10: pad(histMa?.ma10, historyLen).concat(Array(futureLen).fill(null)),
    ma20: pad(histMa?.ma20, historyLen).concat(Array(futureLen).fill(null)),
  };
  const last5 = histMa?.ma5?.[historyLen - 1] ?? null;
  const last10 = histMa?.ma10?.[historyLen - 1] ?? null;
  const last20 = histMa?.ma20?.[historyLen - 1] ?? null;
  const maUp = {
    ma5: stitchForecastMa(historyLen, futureLen, last5, item.ma?.up?.ma5),
    ma10: stitchForecastMa(historyLen, futureLen, last10, item.ma?.up?.ma10),
    ma20: stitchForecastMa(historyLen, futureLen, last20, item.ma?.up?.ma20),
  };
  const maDown = {
    ma5: stitchForecastMa(historyLen, futureLen, last5, item.ma?.down?.ma5),
    ma10: stitchForecastMa(historyLen, futureLen, last10, item.ma?.down?.ma10),
    ma20: stitchForecastMa(historyLen, futureLen, last20, item.ma?.down?.ma20),
  };
  const visible = Math.min(historyLen, VISIBLE_HIST) + futureLen;
  const zoomStart = dates.length === 0 ? 0 : Math.max(0, 100 - (visible / dates.length) * 100);
  return {
    dates,
    historyLen,
    historyCandle,
    upCustom,
    downCustom,
    maHist,
    maUp,
    maDown,
    splitIndex: Math.max(0, historyLen - 1),
    zoomStart,
  };
}

function dashedCandleRenderer(offsetFrac: number, upColor: string, downColor: string) {
  // ECharts custom series: draw hollow dashed bodies so future bars read as projections.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (_params: any, api: any) => {
    const idx = api.value(0);
    const open = api.value(1);
    const close = api.value(2);
    const low = api.value(3);
    const high = api.value(4);
    const xCenter = api.coord([idx, open])[0];
    const yOpen = api.coord([idx, open])[1];
    const yClose = api.coord([idx, close])[1];
    const yLow = api.coord([idx, low])[1];
    const yHigh = api.coord([idx, high])[1];
    const band = api.size([1, 0])[0];
    const bodyW = Math.max(3, band * 0.28);
    const x = xCenter + offsetFrac * band;
    const color = close >= open ? upColor : downColor;
    const top = Math.min(yOpen, yClose);
    const height = Math.max(Math.abs(yClose - yOpen), 1);
    return {
      type: "group",
      children: [
        {
          type: "line",
          shape: { x1: x, y1: yHigh, x2: x, y2: yLow },
          style: { stroke: color, lineDash: [4, 3], lineWidth: 1 },
        },
        {
          type: "rect",
          shape: { x: x - bodyW / 2, y: top, width: bodyW, height },
          style: {
            stroke: color,
            fill: "transparent",
            lineDash: [4, 3],
            lineWidth: 1.2,
          },
        },
      ],
    };
  };
}

interface Props {
  item: MonitorForecastItem;
  height?: number;
}

export function ForecastCandlestickChart({ item, height = 320 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof echarts.init> | null>(null);
  const { dark } = useDarkMode();
  const model = useMemo(() => buildForecastChartModel(item), [item]);

  useEffect(() => {
    if (!containerRef.current || model.dates.length === 0) return;
    const chart = echarts.init(containerRef.current);
    chartRef.current = chart;
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(containerRef.current);
    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, [model.dates.length, dark]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || model.dates.length === 0) return;
    const t = getChartTheme();
    const splitLabel = model.dates[model.splitIndex];
    const legendK = i18n.t("monitor.forecastLegendK");
    const legendUp = i18n.t("monitor.forecastLegendUp");
    const legendDown = i18n.t("monitor.forecastLegendDown");

    // Keep forecast MA lines glued to the scenario legend items. Those MA
    // series stay off the legend itself so MA5/10/20 still mean historical.
    chart.off("legendselectchanged");
    chart.on("legendselectchanged", (raw: unknown) => {
      const params = raw as { name?: string; selected?: Record<string, boolean> };
      const name = params?.name || "";
      const satellites = scenarioSatelliteSeries(name, legendUp, legendDown);
      if (!satellites.length) return;
      const show = params?.selected?.[name] !== false;
      for (const seriesName of satellites) {
        chart.dispatchAction({ type: show ? "legendSelect" : "legendUnSelect", name: seriesName });
      }
    });

    chart.setOption(
      {
        backgroundColor: "transparent",
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "cross" },
          backgroundColor: t.tooltipBg,
          borderColor: t.tooltipBorder,
          textStyle: { color: t.tooltipText, fontSize: 11 },
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          formatter: (params: any) => {
            if (!Array.isArray(params) || !params.length) return "";
            let html = `<b>${params[0].axisValue}</b>`;
            for (const p of params) {
              if (p.seriesName === legendK && Array.isArray(p.value)) {
                const [open, close, low, high] = p.value;
                html += `<br/>O ${Number(open).toFixed(2)} H ${Number(high).toFixed(2)} L ${Number(low).toFixed(2)} C ${Number(close).toFixed(2)}`;
              } else if ((p.seriesName === legendUp || p.seriesName === legendDown) && Array.isArray(p.value)) {
                const [, open, close, low, high] = p.value;
                html += `<br/>${p.marker} ${p.seriesName}: O ${Number(open).toFixed(2)} C ${Number(close).toFixed(2)} (L ${Number(low).toFixed(2)} / H ${Number(high).toFixed(2)})`;
              } else if (p.value != null && !Array.isArray(p.value)) {
                html += `<br/>${p.marker} ${p.seriesName}: ${Number(p.value).toFixed(2)}`;
              }
            }
            return html;
          },
        },
        legend: {
          data: [legendK, legendUp, legendDown, "MA5", "MA10", "MA20"],
          textStyle: { color: t.textColor, fontSize: 10 },
          top: 0,
          type: "scroll",
          itemWidth: 12,
          itemHeight: 8,
        },
        grid: { left: 8, right: 12, top: 28, bottom: 28, containLabel: true },
        xAxis: {
          type: "category",
          data: model.dates,
          axisLine: { lineStyle: { color: t.axisColor } },
          axisLabel: { color: t.textColor, fontSize: 10 },
          boundaryGap: true,
        },
        yAxis: {
          scale: true,
          splitLine: { lineStyle: { color: t.gridColor } },
          axisLabel: { color: t.textColor, fontSize: 10 },
        },
        dataZoom: [
          { type: "inside", start: model.zoomStart, end: 100 },
          { type: "slider", start: model.zoomStart, end: 100, bottom: 4, height: 16 },
        ],
        series: [
          {
            name: legendK,
            type: "candlestick",
            data: model.historyCandle,
            itemStyle: {
              color: t.upColor,
              color0: t.downColor,
              borderColor: t.upColor,
              borderColor0: t.downColor,
            },
            markLine: splitLabel
              ? {
                  silent: true,
                  symbol: "none",
                  lineStyle: { type: "dashed", color: t.axisColor, width: 1 },
                  data: [{ xAxis: splitLabel }],
                  label: { formatter: "", show: false },
                }
              : undefined,
          },
          {
            name: legendUp,
            type: "custom",
            renderItem: dashedCandleRenderer(-0.18, t.upColor, t.downColor),
            data: model.upCustom,
            encode: { x: 0, y: [1, 2, 3, 4] },
            z: 3,
          },
          {
            name: legendDown,
            type: "custom",
            renderItem: dashedCandleRenderer(0.18, t.upColor, t.downColor),
            data: model.downCustom,
            encode: { x: 0, y: [1, 2, 3, 4] },
            z: 3,
          },
          { name: "MA5", type: "line", data: model.maHist.ma5, symbol: "none", lineStyle: { width: 1, color: MA_COLORS[0] } },
          { name: "MA10", type: "line", data: model.maHist.ma10, symbol: "none", lineStyle: { width: 1, color: MA_COLORS[1] } },
          { name: "MA20", type: "line", data: model.maHist.ma20, symbol: "none", lineStyle: { width: 1, color: MA_COLORS[2] } },
          { name: UP_SCENARIO_MA[0], type: "line", data: model.maUp.ma5, symbol: "none", lineStyle: { width: 1, color: t.upColor } },
          { name: UP_SCENARIO_MA[1], type: "line", data: model.maUp.ma10, symbol: "none", lineStyle: { width: 1, type: "dashed", color: t.upColor } },
          { name: UP_SCENARIO_MA[2], type: "line", data: model.maUp.ma20, symbol: "none", lineStyle: { width: 1, type: "dotted", color: t.upColor } },
          { name: DOWN_SCENARIO_MA[0], type: "line", data: model.maDown.ma5, symbol: "none", lineStyle: { width: 1, color: t.downColor } },
          { name: DOWN_SCENARIO_MA[1], type: "line", data: model.maDown.ma10, symbol: "none", lineStyle: { width: 1, type: "dashed", color: t.downColor } },
          { name: DOWN_SCENARIO_MA[2], type: "line", data: model.maDown.ma20, symbol: "none", lineStyle: { width: 1, type: "dotted", color: t.downColor } },
        ],
      },
      true,
    );
  }, [model, dark]);

  if (model.dates.length === 0) {
    return <div className="text-muted-foreground text-sm p-4">{i18n.t("charts.noPriceData")}</div>;
  }

  return <div ref={containerRef} style={{ height }} />;
}