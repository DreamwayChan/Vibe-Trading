/**
 * `useDataQuery` — subscribe a component to the shared data-hub cache.
 *
 * Usage: `const { data, loading, error, reload } = useDataQuery("overview", dataApi.getOverview)`.
 * The key is the cache identity: two components using the same key share one
 * request and one object instance, which is what keeps panels consistent.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { DEFAULT_TTL_MS, loadShared, useDataHub } from "@/stores/datahub";

export interface DataQueryResult<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  updatedAt: number | undefined;
  reload: () => void;
}

export function useDataQuery<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  opts: { ttl?: number; enabled?: boolean } = {},
): DataQueryResult<T> {
  const { ttl = DEFAULT_TTL_MS, enabled = true } = opts;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const cachedAt = useDataHub((state) => (key ? state.cache[key]?.at : undefined));
  const [reloadToken, setReloadToken] = useState(0);
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(key) && enabled);

  useEffect(() => {
    if (!key || !enabled) {
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    // A manual reload must bypass the TTL; anything after it may reuse cache.
    loadShared<T>(key, () => fetcherRef.current(), { ttl, force: reloadToken > 0 })
      .then((next) => {
        if (!alive) return;
        setData(next);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [key, enabled, ttl, reloadToken]);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  return { data, error, loading, updatedAt: cachedAt, reload };
}
