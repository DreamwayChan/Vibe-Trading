import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";
import type { SectorPickCatalogItem, SectorPickHistorySeries } from "@/lib/api";

/** Distinct line colors so 19 boards stay separable on one chart. */
const PALETTE = [
  "#2563eb",
  "#dc2626",
  "#16a34a",
  "#d97706",
  "#7c3aed",
  "#0891b2",
  "#db2777",
  "#65a30d",
  "#ea580c",
  "#4f46e5",
  "#0d9488",
  "#c026d3",
  "#ca8a04",
  "#0284c7",
  "#e11d48",
  "#15803d",
  "#9333ea",
  "#b45309",
  "#334155",
];

interface Props {
  series: SectorPickHistorySeries | null;
  catalog: SectorPickCatalogItem[];
  selectedAsOf?: string | null;
  onSelectDay?: (asOf: string) => void;
  height?: number;
}

export type DayLookup = Record<
  string,
  Record<
    string,
    {
      rank: number | null;
      ret_20d: number | null;
      confirm_pass_rate: number | null;
      timing_mult: number | null;
      isWinner: boolean;
    }
  >
>;

export type BoardTooltipLabels = {
  rank: string;
  mom: string;
  confirm: string;
  timing: string;
  winner: string;
};

function colorFor(index: number): string {
  return PALETTE[index % PALETTE.length];
}

/** Rank and momentum are two series that share one legend name. */
export function seriesIdsForBoard(key: string): [string, string] {
  return [`rank:${key}`, `mom:${key}`];
}

export function boardKeyFromSeriesName(
  name: string,
  keys: string[],
  nameByKey: Map<string, string>,
): string | undefined {
  return keys.find((key) => (nameByKey.get(key) || key) === name);
}

/** Rank/mom series ids are `rank:key` / `mom:key`; legend highlight uses the display name. */
export function boardKeyFromHighlight(
  params: { seriesName?: string; seriesId?: string; name?: string },
  keys: string[],
  nameByKey: Map<string, string>,
): string | undefined {
  const id = String(params.seriesId || "");
  const colon = id.indexOf(":");
  if (colon > 0) {
    const prefix = id.slice(0, colon);
    const key = id.slice(colon + 1);
    if ((prefix === "rank" || prefix === "mom") && keys.includes(key)) return key;
  }
  return boardKeyFromSeriesName(String(params.seriesName || params.name || ""), keys, nameByKey);
}

/**
 * Style both grids the same way. ECharts emphasis can only keep one series
 * "in focus", so we set opacity/width ourselves instead of dispatching highlight.
 */
export function boardPairLineStyle(
  colorIndex: number,
  seriesKey: string,
  focusKey: string | null,
): { color: string; width: number; opacity: number } {
  const color = colorFor(colorIndex);
  if (focusKey == null) return { color, width: 2, opacity: 1 };
  const on = seriesKey === focusKey;
  return { color, width: on ? 3.2 : 1.5, opacity: on ? 1 : 0.22 };
}

/** Pick the board whose value on this date is closest to the pointer's Y. */
export function findNearestBoardKey(
  keys: string[],
  lookup: DayLookup,
  asOf: string,
  yValue: number,
  metric: "rank" | "mom",
): string | null {
  let best: string | null = null;
  let bestDist = Infinity;
  for (const key of keys) {
    const cell = lookup[asOf]?.[key];
    const raw =
      metric === "rank"
        ? cell?.rank
        : cell?.ret_20d == null
          ? null
          : cell.ret_20d * 100;
    if (raw == null) continue;
    const dist = Math.abs(Number(raw) - yValue);
    if (dist < bestDist) {
      bestDist = dist;
      best = key;
    }
  }
  return best;
}

export function formatIdleTooltipHtml(
  asOf: string,
  winnerName: string,
  winnerLabel: string,
): string {
  let html = `<b>${asOf}</b>`;
  if (winnerName) html += `<br/>${winnerLabel}: <b>${winnerName}</b>`;
  return html;
}

export function formatBoardTooltipHtml(opts: {
  asOf: string;
  key: string;
  marker: string;
  nameByKey: Map<string, string>;
  titleByKey: Map<string, string>;
  lookup: DayLookup;
  winnerByDate: Record<string, string>;
  labels: BoardTooltipLabels;
}): string {
  const { asOf, key, marker, nameByKey, titleByKey, lookup, winnerByDate, labels } = opts;
  const winnerKey = winnerByDate[asOf];
  const winnerName = winnerKey ? nameByKey.get(winnerKey) || winnerKey : "";
  const name = nameByKey.get(key) || key;
  const cell = lookup[asOf]?.[key];
  const rank = cell?.rank == null ? "—" : String(cell.rank);
  const mom = cell?.ret_20d == null ? "—" : `${(cell.ret_20d * 100).toFixed(1)}%`;
  const confirm =
    cell?.confirm_pass_rate == null ? "—" : `${(cell.confirm_pass_rate * 100).toFixed(0)}%`;
  const timing = cell?.timing_mult == null ? "—" : cell.timing_mult.toFixed(1);
  const title = titleByKey.get(key) || "";
  let html = formatIdleTooltipHtml(asOf, winnerName, labels.winner);
  html += `<br/>${marker} <b>${name}</b>`;
  if (title) html += ` <span style="opacity:.7">${title}</span>`;
  html += `<br/>&nbsp;&nbsp;${labels.rank} ${rank} · ${labels.mom} ${mom} · ${labels.confirm} ${confirm} · ${labels.timing} ${timing}`;
  return html;
}

export function SectorPickHistoryChart({
  series,
  catalog,
  selectedAsOf,
  onSelectDay,
  height = 460,
}: Props) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  const { dark } = useDarkMode();

  const built = useMemo(() => {
    const days = series?.days || [];
    const dates = days.map((day) => day.as_of);
    const nameByKey = new Map(catalog.map((row) => [row.key, row.name]));
    const titleByKey = new Map(catalog.map((row) => [row.key, row.run_title || ""]));
    const order = catalog.map((row) => row.key);
    const seen = new Set<string>();
    const keys: string[] = [];
    for (const key of order) {
      if (days.some((day) => day.sectors.some((row) => row.key === key))) {
        keys.push(key);
        seen.add(key);
      }
    }
    for (const day of days) {
      for (const row of day.sectors) {
        if (!seen.has(row.key)) {
          keys.push(row.key);
          seen.add(row.key);
          if (!nameByKey.has(row.key)) nameByKey.set(row.key, row.name);
        }
      }
    }

    const lookup: DayLookup = {};
    for (const day of days) {
      lookup[day.as_of] = {};
      for (const row of day.sectors) {
        lookup[day.as_of][row.key] = {
          rank: row.rank ?? null,
          ret_20d: row.ret_20d ?? null,
          confirm_pass_rate: row.confirm_pass_rate ?? null,
          timing_mult: row.timing_mult ?? null,
          isWinner: day.recommended_key === row.key,
        };
      }
    }

    const winnerByDate: Record<string, string> = {};
    for (const day of days) {
      if (day.recommended_key) winnerByDate[day.as_of] = day.recommended_key;
    }

    return { dates, keys, nameByKey, titleByKey, lookup, winnerByDate };
  }, [series, catalog]);

  useEffect(() => {
    const el = ref.current;
    if (!el || built.dates.length === 0) return;
    // jsdom and collapsed layouts have no paint box — skip rather than crash ECharts.
    if (el.clientWidth < 8 || el.clientHeight < 8) return;
    const theme = getChartTheme();
    let chart: ReturnType<typeof echarts.init>;
    try {
      chart = echarts.init(el);
    } catch {
      return;
    }
    const rankLabel = t("sectorPick.chartRank");
    const momLabel = t("sectorPick.chartMom");
    const confirmLabel = t("sectorPick.chartConfirm");
    const timingLabel = t("sectorPick.chartTiming");
    const winnerLabel = t("sectorPick.chartWinner");
    const labels: BoardTooltipLabels = {
      rank: rankLabel,
      mom: momLabel,
      confirm: confirmLabel,
      timing: timingLabel,
      winner: winnerLabel,
    };

    const lineSeries = (kind: "rank" | "mom", key: string, index: number) => {
      const color = colorFor(index);
      return {
        id: `${kind}:${key}`,
        name: built.nameByKey.get(key) || key,
        type: "line" as const,
        xAxisIndex: kind === "rank" ? 0 : 1,
        yAxisIndex: kind === "rank" ? 0 : 1,
        data:
          kind === "rank"
            ? built.dates.map((asOf) => built.lookup[asOf]?.[key]?.rank ?? null)
            : built.dates.map((asOf) => {
                const value = built.lookup[asOf]?.[key]?.ret_20d;
                return value == null ? null : Number((value * 100).toFixed(2));
              }),
        connectNulls: false,
        symbol: built.dates.length <= 2 ? "circle" : "emptyCircle",
        symbolSize: 6,
        lineStyle: { width: 2, color, opacity: 1 },
        itemStyle: { color, opacity: 1 },
        // Default emphasis would fade every other series, including this board's pair.
        legendHoverLink: true,
        emphasis: { disabled: true },
        z: 2,
        markLine:
          kind === "rank" && index === 0 && selectedAsOf
            ? {
                silent: true,
                symbol: "none",
                label: { show: false },
                lineStyle: { color: theme.axisColor, type: "dashed", width: 1 },
                data: [{ xAxis: selectedAsOf }],
              }
            : undefined,
      };
    };

    const rankSeries = built.keys.map((key, index) => lineSeries("rank", key, index));
    const momSeries = built.keys.map((key, index) => lineSeries("mom", key, index));

    // Declared before setOption so the tooltip formatter can read the current board.
    let focusedKey: string | null = null;
    let legendLocked = false;
    let lastAsOf = built.dates[built.dates.length - 1] || "";

    chart.setOption({
      backgroundColor: "transparent",
      axisPointer: {
        link: [{ xAxisIndex: [0, 1] }],
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: theme.tooltipBg,
        borderColor: theme.tooltipBorder,
        textStyle: { color: theme.tooltipText, fontSize: 11 },
        confine: true,
        extraCssText: "pointer-events:none;",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        formatter: (params: any) => {
          if (!Array.isArray(params) || !params.length) return "";
          const asOf = String(params[0].axisValue || "");
          const winnerKey = built.winnerByDate[asOf];
          const winnerName = winnerKey ? built.nameByKey.get(winnerKey) || winnerKey : "";
          if (!focusedKey) {
            return formatIdleTooltipHtml(asOf, winnerName, winnerLabel);
          }
          const marker =
            params.find(
              (point: { seriesId?: string; marker?: string }) =>
                point.seriesId === `rank:${focusedKey}` || point.seriesId === `mom:${focusedKey}`,
            )?.marker ||
            params[0].marker ||
            "";
          return formatBoardTooltipHtml({
            asOf,
            key: focusedKey,
            marker,
            nameByKey: built.nameByKey,
            titleByKey: built.titleByKey,
            lookup: built.lookup,
            winnerByDate: built.winnerByDate,
            labels,
          });
        },
      },
      legend: {
        type: "scroll",
        data: built.keys.map((key) => built.nameByKey.get(key) || key),
        textStyle: { color: theme.textColor, fontSize: 11 },
        top: 4,
      },
      grid: [
        { left: 12, right: 16, top: 48, height: "38%", containLabel: true },
        { left: 12, right: 16, top: "62%", height: "28%", containLabel: true },
      ],
      xAxis: [
        {
          type: "category",
          data: built.dates,
          gridIndex: 0,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisLabel: { color: theme.textColor, fontSize: 10 },
        },
        {
          type: "category",
          data: built.dates,
          gridIndex: 1,
          axisLine: { lineStyle: { color: theme.axisColor } },
          axisLabel: { color: theme.textColor, fontSize: 10 },
        },
      ],
      yAxis: [
        {
          type: "value",
          name: rankLabel,
          inverse: true,
          min: 1,
          minInterval: 1,
          gridIndex: 0,
          nameTextStyle: { color: theme.textColor, fontSize: 11 },
          splitLine: { lineStyle: { color: theme.gridColor } },
          axisLabel: { color: theme.textColor, fontSize: 10 },
        },
        {
          type: "value",
          name: momLabel,
          gridIndex: 1,
          nameTextStyle: { color: theme.textColor, fontSize: 11 },
          splitLine: { lineStyle: { color: theme.gridColor } },
          axisLabel: {
            color: theme.textColor,
            fontSize: 10,
            formatter: (value: number) => `${value}%`,
          },
        },
      ],
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
      series: [...rankSeries, ...momSeries],
    });

    chart.on("click", (params) => {
      const asOf = String(
        (params as { name?: string; axisValue?: string }).name ||
          (params as { axisValue?: string }).axisValue ||
          "",
      );
      if (asOf && onSelectDay) onSelectDay(asOf);
    });

    const applyBoardStyles = (focusKey: string | null) => {
      chart.setOption({
        series: built.keys.flatMap((key, index) => {
          const lineStyle = boardPairLineStyle(index, key, focusKey);
          const itemStyle = { color: lineStyle.color, opacity: lineStyle.opacity };
          const z = focusKey != null && key === focusKey ? 8 : 2;
          return [
            { id: `rank:${key}`, lineStyle, itemStyle, z },
            { id: `mom:${key}`, lineStyle, itemStyle, z },
          ];
        }),
      });
    };

    const selectBoard = (key: string, locked: boolean) => {
      legendLocked = locked;
      if (focusedKey === key) return;
      focusedKey = key;
      applyBoardStyles(key);
    };

    const clearFocus = () => {
      if (!focusedKey && !legendLocked) return;
      focusedKey = null;
      legendLocked = false;
      applyBoardStyles(null);
    };

    // Legend hover in ECharts 6 is a highlight action, not legendmouseover.
    chart.on("highlight", (raw) => {
      const params = raw as { seriesName?: string; seriesId?: string; name?: string; seriesIndex?: number };
      const key = boardKeyFromHighlight(params, built.keys, built.nameByKey);
      if (!key) return;
      const fromLegend = params.seriesIndex == null;
      selectBoard(key, fromLegend || legendLocked);
    });

    chart.on("mouseover", (params) => {
      const event = params as { componentType?: string; seriesName?: string; seriesId?: string };
      if (event.componentType !== "series") return;
      const key = boardKeyFromHighlight(event, built.keys, built.nameByKey);
      if (!key) return;
      selectBoard(legendLocked && focusedKey ? focusedKey : key, legendLocked);
    });

    chart.on("updateAxisPointer", (raw) => {
      const event = raw as { axesInfo?: Array<{ value?: unknown }> };
      const value = event.axesInfo?.[0]?.value;
      if (value != null && value !== "") lastAsOf = String(value);
    });

    const onPointerMove = (event: { offsetX?: number; offsetY?: number; zrX?: number; zrY?: number }) => {
      const x = Number(event.zrX ?? event.offsetX);
      const y = Number(event.zrY ?? event.offsetY);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      const pixel: [number, number] = [x, y];
      let inRank = false;
      let inMom = false;
      try {
        inRank = chart.containPixel({ gridIndex: 0 }, pixel);
        inMom = chart.containPixel({ gridIndex: 1 }, pixel);
      } catch {
        return;
      }
      if (!inRank && !inMom) return;
      if (legendLocked) return;
      const gridIndex = inRank ? 0 : 1;
      let converted: unknown;
      try {
        converted = chart.convertFromPixel({ gridIndex }, pixel);
      } catch {
        return;
      }
      const yValue = Array.isArray(converted) ? Number(converted[1]) : Number(converted);
      if (!Number.isFinite(yValue)) return;
      const key = findNearestBoardKey(
        built.keys,
        built.lookup,
        lastAsOf,
        yValue,
        inRank ? "rank" : "mom",
      );
      if (key) selectBoard(key, false);
    };

    const zr = chart.getZr();
    zr.on("mousemove", onPointerMove);
    el.addEventListener("mouseleave", clearFocus);

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(el);
    return () => {
      zr.off("mousemove", onPointerMove);
      el.removeEventListener("mouseleave", clearFocus);
      observer.disconnect();
      chart.dispose();
    };
  }, [built, dark, onSelectDay, selectedAsOf, t]);

  if (!series || built.dates.length === 0) return null;

  return (
    <div className="flex flex-col gap-1.5">
      <div>
        <h2 className="text-lg font-semibold">{t("sectorPick.chartTitle")}</h2>
        <p className="text-xs text-muted-foreground mt-0.5">
          {built.dates.length < 2 ? t("sectorPick.chartNeedMore") : t("sectorPick.chartHint")}
        </p>
      </div>
      <div ref={ref} style={{ height }} />
    </div>
  );
}
