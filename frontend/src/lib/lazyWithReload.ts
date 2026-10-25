import { lazy, type ComponentType, type LazyExoticComponent } from "react";

const CHUNK_RELOAD_KEY = "vt:chunk-reload";

/**
 * Run a dynamic import factory, recovering once from stale Vite chunk URLs
 * after a frontend rebuild by forcing a full page reload.
 *
 * Exported for unit tests; ``lazyWithReload`` is the React-facing wrapper.
 */
export async function importWithChunkReload<T>(
  factory: () => Promise<T>,
): Promise<T> {
  try {
    const mod = await factory();
    // Successful load means any prior reload marker is stale — clear it
    // so a later deploy can recover again in this tab.
    sessionStorage.removeItem(CHUNK_RELOAD_KEY);
    return mod;
  } catch (error) {
    const alreadyReloaded = sessionStorage.getItem(CHUNK_RELOAD_KEY) === "1";
    if (!alreadyReloaded) {
      sessionStorage.setItem(CHUNK_RELOAD_KEY, "1");
      window.location.reload();
      // Hold the promise open until the reload tears the page down.
      return await new Promise<T>(() => {});
    }
    sessionStorage.removeItem(CHUNK_RELOAD_KEY);
    throw error;
  }
}

/**
 * React.lazy wrapper that recovers from stale Vite chunk URLs after a
 * frontend rebuild: the first dynamic-import failure triggers a one-shot
 * full page reload so the browser picks up the new hashed asset names.
 * A second failure surfaces the original error (avoids reload loops).
 */
export function lazyWithReload<T extends ComponentType<unknown>>(
  factory: () => Promise<{ default: T }>,
): LazyExoticComponent<T> {
  return lazy(() => importWithChunkReload(factory));
}
