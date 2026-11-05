/**
 * Shared data-hub cache.
 *
 * Every screen reads from here, so two panels asking for the same key get the
 * same object and only one network call — that is the client-side half of the
 * "one data source" rule. Keys are plain strings (`overview`, `datasets`,
 * `dataset:bars_daily`) so a page can invalidate a whole family by prefix.
 */

import { create } from "zustand";

export interface CacheEntry {
  data: unknown;
  at: number;
}

interface DataHubState {
  cache: Record<string, CacheEntry>;
  inflight: Record<string, Promise<unknown>>;
  put: (key: string, data: unknown) => void;
  drop: (key: string) => void;
  invalidate: (prefix?: string) => void;
  begin: (key: string, promise: Promise<unknown>) => void;
  end: (key: string) => void;
}

export const useDataHub = create<DataHubState>((set) => ({
  cache: {},
  inflight: {},
  put: (key, data) =>
    set((state) => ({ cache: { ...state.cache, [key]: { data, at: Date.now() } } })),
  drop: (key) =>
    set((state) => {
      const next = { ...state.cache };
      delete next[key];
      return { cache: next };
    }),
  invalidate: (prefix) =>
    set((state) => {
      if (!prefix) return { cache: {} };
      const next: Record<string, CacheEntry> = {};
      for (const [key, value] of Object.entries(state.cache)) {
        if (!key.startsWith(prefix)) next[key] = value;
      }
      return { cache: next };
    }),
  begin: (key, promise) =>
    set((state) => ({ inflight: { ...state.inflight, [key]: promise } })),
  end: (key) =>
    set((state) => {
      const next = { ...state.inflight };
      delete next[key];
      return { inflight: next };
    }),
}));

/** Default freshness window for catalog reads; ingest cadence is minutes. */
export const DEFAULT_TTL_MS = 30_000;

export function peekCache<T>(key: string, ttl = DEFAULT_TTL_MS): T | undefined {
  const entry = useDataHub.getState().cache[key];
  if (!entry) return undefined;
  if (Date.now() - entry.at > ttl) return undefined;
  return entry.data as T;
}

/**
 * Load through the shared cache with in-flight de-duplication.
 *
 * Six panels opening at once produce one request: the second caller joins the
 * pending promise instead of starting its own.
 */
export async function loadShared<T>(
  key: string,
  fetcher: () => Promise<T>,
  opts: { ttl?: number; force?: boolean } = {},
): Promise<T> {
  const { ttl = DEFAULT_TTL_MS, force = false } = opts;
  const state = useDataHub.getState();

  if (!force) {
    const cached = peekCache<T>(key, ttl);
    if (cached !== undefined) return cached;
  }

  const pending = state.inflight[key] as Promise<T> | undefined;
  if (pending && !force) return pending;

  const promise = fetcher()
    .then((data) => {
      useDataHub.getState().put(key, data);
      return data;
    })
    .finally(() => {
      useDataHub.getState().end(key);
    });

  useDataHub.getState().begin(key, promise as Promise<unknown>);
  return promise;
}

export function invalidateData(prefix?: string): void {
  useDataHub.getState().invalidate(prefix);
}
