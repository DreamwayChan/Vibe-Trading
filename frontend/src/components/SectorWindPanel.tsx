import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Wind } from "lucide-react";
import { api, type SectorWindSector } from "@/lib/api";
import { cn } from "@/lib/utils";

const ACTIONABLE = new Set(["launch", "main"]);

function fmt(v: number | null | undefined, digits = 3): string {
  return v == null || Number.isNaN(v) ? "—" : v.toFixed(digits);
}

export function SectorWindPanel({ keys }: { keys?: string[] }) {
  const { t } = useTranslation();
  const [sectors, setSectors] = useState<SectorWindSector[] | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      const res = await api.getSectorWindCards(keys);
      setSectors(res.sectors);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="rounded-lg border p-4 flex flex-col gap-3" data-testid="sector-wind-panel">
      <div className="flex items-start gap-3">
        <Wind className="h-5 w-5 text-primary mt-0.5" />
        <div className="flex-1">
          <h2 className="text-lg font-semibold inline-flex flex-wrap items-center gap-2">
            <span>{t("sectorWind.title")}</span>
            <span
              className="text-[10px] rounded border border-dashed border-amber-500/70 px-1 py-0.5 font-medium text-amber-800 dark:text-amber-300"
              title={t("sectorWind.notTradableHint")}
            >
              {t("sectorWind.researchOnly")}
            </span>
          </h2>
          <p className="text-xs text-muted-foreground mt-1">{t("sectorWind.hint")}</p>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50"
        >
          {running && <Loader2 className="h-4 w-4 animate-spin" />}
          {running ? t("sectorWind.running") : t("sectorWind.run")}
        </button>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {sectors && sectors.length === 0 && (
        <p className="text-sm text-muted-foreground">{t("sectorWind.empty")}</p>
      )}

      {sectors?.map((row) => {
        const state = row.wind?.state;
        const val = row.wind?.validation;
        return (
          <div key={row.sector} className="rounded-md border p-3 flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="font-medium">{row.sector}</span>
              {state && (
                <span
                  className={cn(
                    "rounded px-2 py-0.5 text-xs",
                    ACTIONABLE.has(state) ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
                  )}
                >
                  {t(`sectorWind.state.${state}`, { defaultValue: state })}
                </span>
              )}
              {row.wind?.score != null && (
                <span className="font-mono text-xs">
                  {t("sectorWind.score")} {fmt(row.wind.score, 2)}
                </span>
              )}
              {val && (
                <span className="font-mono text-xs text-muted-foreground">
                  {t("sectorWind.windIc")} {fmt(val.ic)} ·{" "}
                  {val.passed ? t("sectorWind.validated") : t("sectorWind.notValidated")}
                </span>
              )}
              {(row.error || (row.status && row.status !== "ok" && !row.wind)) && (
                <span className="text-xs text-destructive">{row.error || row.status}</span>
              )}
            </div>

            {row.cards.length === 0 ? (
              <p className="text-xs text-muted-foreground">{t("sectorWind.noCards")}</p>
            ) : (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-2">{t("sectorWind.factor")}</th>
                    <th className="py-1 pr-2">{t("sectorWind.shrunkIc")}</th>
                    <th className="py-1 pr-2">{t("sectorWind.icOut")}</th>
                    <th className="py-1 pr-2">{t("sectorWind.tOut")}</th>
                    <th className="py-1">{t("sectorWind.retire")}</th>
                  </tr>
                </thead>
                <tbody>
                  {row.cards.map((c) => (
                    <tr key={c.factor} className="border-t">
                      <td className="py-1 pr-2 font-mono">
                        <span>{c.factor}</span>
                        <span
                          className="ml-1 whitespace-nowrap text-[10px] rounded border border-dashed border-amber-500/70 px-1 py-0.5 text-amber-800 dark:text-amber-300"
                          title={t("sectorWind.notTradableHint")}
                        >
                          {t("sectorWind.notTradable")}
                        </span>
                      </td>
                      <td className="py-1 pr-2 font-mono">{fmt(c.shrunk_ic, 4)}</td>
                      <td className="py-1 pr-2 font-mono">{fmt(c.ic_out, 4)}</td>
                      <td className="py-1 pr-2 font-mono">{fmt(c.t_out, 2)}</td>
                      <td className="py-1">
                        {t("sectorWind.retireRule", {
                          window: c.retire_when.rolling_ic_window,
                          states: c.retire_when.wind_states.join("/"),
                        })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}

      <p className="text-xs text-muted-foreground">{t("sectorWind.disclaimer")}</p>
    </section>
  );
}
