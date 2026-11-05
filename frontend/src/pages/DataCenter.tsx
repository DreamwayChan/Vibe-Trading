/**
 * Data center — the single console for every dataset the product reads.
 *
 * Two rules shape this page:
 *  1. It renders what the hub says, it never computes market facts itself.
 *     Every number here comes from `/data/*`, so no panel can disagree.
 *  2. Reads go through the shared `useDataQuery` cache, so opening this page
 *     and a feature page at once still costs one request per key.
 *
 * Phase 0 exposes the catalog (overview + datasets). Market/sector/capital/task
 * tabs land in later phases and will read the same hub, not new endpoints.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Database, Loader2, RadioTower, RefreshCw, RotateCw, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDataQuery } from "@/hooks/useDataQuery";
import { invalidateData } from "@/stores/datahub";
import { DataStamp } from "@/components/data/DataStamp";
import { DatasetDetailCard, DatasetTable } from "@/components/data/DatasetTable";
import { IngestStatusBar } from "@/components/data/IngestStatusBar";
import type { IngestBody } from "@/components/data/IngestForm";
import { MarketPanel } from "@/components/data/MarketPanel";
import { OverviewPanel } from "@/components/data/OverviewPanel";
import { StatusPill } from "@/components/data/StatusPill";
import {
  dataApi,
  type DataOverview,
  type DatasetListResponse,
  type IngestJob,
  type IngestStatusResponse,
} from "@/lib/dataClient";

type Tab = "overview" | "market" | "datasets";

const TABS = [
  { key: "overview", labelKey: "dataCenter.tabOverview" },
  { key: "market", labelKey: "dataCenter.tabMarket" },
  { key: "datasets", labelKey: "dataCenter.tabDatasets" },
] as const;

export function DataCenter() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("overview");
  const [selected, setSelected] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildAll, setRebuildAll] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  // `live` changes the cache key on purpose: an intraday read is a different
  // payload (settled=false) and must never be served from the settled cache.
  const overviewQuery = useDataQuery<DataOverview>(
    live ? "overview:live" : "overview",
    () => dataApi.getOverview({ live }),
  );
  const datasetsQuery = useDataQuery<DatasetListResponse>("datasets", dataApi.listDatasets, {
    enabled: tab === "datasets",
  });
  const detailQuery = useDataQuery(
    selected ? `dataset:${selected}` : null,
    () => dataApi.getDataset(selected as string),
    { enabled: Boolean(selected) && tab === "datasets" },
  );
  // Ingest status is polled while a job runs, so this cache entry is always read
  // fresh rather than served from the shared TTL.
  const ingestQuery = useDataQuery<IngestStatusResponse>("ingest", dataApi.getIngestStatus, {
    ttl: 0,
  });
  const [startingIngest, setStartingIngest] = useState(false);
  const [ingestDataset, setIngestDataset] = useState<string | null>(null);
  // Remember which finished job we have already acted on: reloading the
  // datasets table once per completed job, not once per poll.
  const handledJobRef = useRef<string | null>(null);

  const asOf = overviewQuery.data?.as_of ?? datasetsQuery.data?.as_of ?? null;
  const cards = useMemo(() => datasetsQuery.data?.datasets ?? [], [datasetsQuery.data]);
  const activeJobs = ingestQuery.data?.active ?? [];
  const ingestOptions = useMemo(() => {
    const map = new Map<string, IngestStatusResponse["datasets"][number]>();
    for (const option of ingestQuery.data?.datasets ?? []) map.set(option.name, option);
    return map;
  }, [ingestQuery.data]);
  const lastJob = ingestQuery.data?.jobs?.[0] ?? null;
  const jobFor = useCallback(
    (name: string | null) => {
      if (!name) return null;
      return (
        activeJobs.find((job) => job.dataset === name) ??
        (lastJob?.dataset === name ? lastJob : null)
      );
    },
    [activeJobs, lastJob],
  );

  // Poll while anything is running; stop as soon as the queue is empty.
  useEffect(() => {
    if (activeJobs.length === 0) return;
    const timer = window.setTimeout(() => ingestQuery.reload(), 1500);
    return () => window.clearTimeout(timer);
  }, [activeJobs, ingestQuery]);

  // A finished job changes rows/coverage, so refresh what the page shows once.
  useEffect(() => {
    if (!lastJob || activeJobs.length > 0) return;
    if (lastJob.status !== "done" || handledJobRef.current === lastJob.id) return;
    handledJobRef.current = lastJob.id;
    invalidateData("overview");
    invalidateData("datasets");
    invalidateData(`dataset:${lastJob.dataset}`);
    overviewQuery.reload();
    datasetsQuery.reload();
    if (selected) detailQuery.reload();
  }, [lastJob, activeJobs, overviewQuery, datasetsQuery, detailQuery, selected]);

  const refreshAll = useCallback(() => {
    overviewQuery.reload();
    if (tab === "datasets") {
      datasetsQuery.reload();
      if (selected) detailQuery.reload();
    }
  }, [overviewQuery, datasetsQuery, detailQuery, tab, selected]);

  const runRebuild = useCallback(
    async (datasets?: string[]) => {
      setActionError(null);
      if (datasets?.length) setRebuilding(true);
      else setRebuildAll(true);
      try {
        await dataApi.rebuildCatalog(datasets);
        invalidateData("overview");
        invalidateData("datasets");
        if (datasets?.length) invalidateData(`dataset:${datasets[0]}`);
        overviewQuery.reload();
        if (tab === "datasets") {
          datasetsQuery.reload();
          if (selected) detailQuery.reload();
        }
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        setRebuilding(false);
        setRebuildAll(false);
      }
    },
    [overviewQuery, datasetsQuery, detailQuery, tab, selected],
  );

  const selectDataset = useCallback((name: string) => {
    setSelected(name);
    setTab("datasets");
  }, []);

  const runIngest = useCallback(
    async (name: string, body: IngestBody = {}) => {
      setActionError(null);
      setStartingIngest(true);
      setIngestDataset(name);
      try {
        // Empty body means "fill whatever the lake is missing", which is what
        // the one-click button promises; an explicit body is a range backfill.
        await dataApi.startIngest({ dataset: name, ...body });
        invalidateData("ingest");
        ingestQuery.reload();
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        setStartingIngest(false);
      }
    },
    [ingestQuery],
  );

  const cancelIngest = useCallback(
    async (job: IngestJob) => {
      setActionError(null);
      try {
        // Cooperative cancel: the run stops at its next symbol and the cursor
        // stays, so the button "补齐" continues instead of starting over.
        await dataApi.cancelIngest(job.id);
        ingestQuery.reload();
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    },
    [ingestQuery],
  );

  const error = overviewQuery.error ?? datasetsQuery.error ?? detailQuery.error ?? actionError;
  const loading =
    overviewQuery.loading || (tab === "datasets" && (datasetsQuery.loading || detailQuery.loading));

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5 p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <Database className="h-5 w-5 text-primary" aria-hidden />
            {t("dataCenter.title")}
          </h1>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted-foreground">
            {t("dataCenter.subtitle")}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <DataStamp asOf={asOf} strict={overviewQuery.data?.strict} />
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => setLive((value) => !value)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition-colors",
                live
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-muted",
              )}
              title={t("dataCenter.liveHint")}
            >
              {live ? <RadioTower className="h-3.5 w-3.5" aria-hidden /> : <ShieldCheck className="h-3.5 w-3.5" aria-hidden />}
              {live ? t("dataCenter.liveOn") : t("dataCenter.liveOff")}
            </button>
            <button
              type="button"
              onClick={refreshAll}
              className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} aria-hidden />
              {t("dataCenter.refresh")}
            </button>
            <button
              type="button"
              onClick={() => runRebuild()}
              disabled={rebuildAll}
              className="inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs hover:bg-muted disabled:opacity-50"
              title={t("dataCenter.rebuildAllHint")}
            >
              <RotateCw className={cn("h-3.5 w-3.5", rebuildAll && "animate-spin")} aria-hidden />
              {t("dataCenter.rebuildAll")}
            </button>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-1 border-b pb-0">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm transition-colors",
              tab === item.key
                ? "border-primary font-medium text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t(item.labelKey)}
          </button>
        ))}
        {overviewQuery.data && (
          <span className="ms-auto pb-1.5 text-[11px] text-muted-foreground">
            {t("dataCenter.updatedAt", {
              time: overviewQuery.data.generated_at.replace("T", " ").slice(0, 19),
            })}
          </span>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <IngestStatusBar
        activeJobs={activeJobs}
        lastJob={lastJob}
        onOpenDataset={selectDataset}
        onCancel={cancelIngest}
      />

      {overviewQuery.loading && !overviewQuery.data && tab === "overview" ? (
        <div className="flex h-[40vh] items-center justify-center gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          {t("dataCenter.loading")}
        </div>
      ) : tab === "overview" ? (
        <OverviewPanel overview={overviewQuery.data ?? null} onSelectDataset={selectDataset} />
      ) : tab === "market" ? (
        <MarketPanel onOpenDataset={selectDataset} />
      ) : (
        <div className="flex flex-col gap-4">
          {datasetsQuery.loading && cards.length === 0 ? (
            <div className="flex h-[30vh] items-center justify-center gap-2 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              {t("dataCenter.loading")}
            </div>
          ) : (
            <DatasetTable cards={cards} selected={selected} onSelect={setSelected} />
          )}

          {selected && (
            <DatasetDetailCard
              detail={detailQuery.data ?? null}
              loading={detailQuery.loading}
              rebuilding={rebuilding}
              onClose={() => setSelected(null)}
              onRebuild={() => runRebuild([selected])}
              ingest={{
                option: ingestOptions.get(selected),
                job: jobFor(selected),
                starting: startingIngest && ingestDataset === selected,
                onStart: () => runIngest(selected),
                onStartWith: (body) => runIngest(selected, body),
                onCancel: cancelIngest,
              }}
            />
          )}

          <p className="text-[11px] leading-relaxed text-muted-foreground">
            {t("dataCenter.datasetsHint")}
          </p>
        </div>
      )}

      {overviewQuery.data && (
        <div className="flex flex-wrap items-center gap-3 border-t pt-3 text-[11px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            {t("dataCenter.footerDeclared", { count: overviewQuery.data.counts.declared })}
          </span>
          {overviewQuery.data.freshness
            .filter((item) => item.status === "stale" || item.status === "lag")
            .slice(0, 6)
            .map((item) => (
              <button
                key={item.name}
                type="button"
                onClick={() => selectDataset(item.name)}
                className="inline-flex items-center gap-1 hover:text-foreground"
              >
                {item.label}
                <StatusPill status={item.status} />
              </button>
            ))}
        </div>
      )}
    </div>
  );
}
