import { beforeEach, describe, expect, it, vi } from "vitest";
import { importWithChunkReload } from "../lazyWithReload";

describe("importWithChunkReload", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("returns the module and clears any prior reload marker", async () => {
    sessionStorage.setItem("vt:chunk-reload", "1");
    const mod = await importWithChunkReload(async () => ({ default: "ok" }));
    expect(mod).toEqual({ default: "ok" });
    expect(sessionStorage.getItem("vt:chunk-reload")).toBeNull();
  });

  it("reloads once on the first import failure", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });

    const pending = importWithChunkReload(async () => {
      throw new TypeError("Failed to fetch dynamically imported module");
    });

    // Give the rejected factory a turn to schedule reload; the promise
    // intentionally never settles while the page is about to reload.
    await Promise.resolve();
    await Promise.resolve();

    expect(reload).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem("vt:chunk-reload")).toBe("1");
    // Do not await pending — it hangs by design until navigation.
    void pending;
  });

  it("rethrows on a second failure instead of reload-looping", async () => {
    sessionStorage.setItem("vt:chunk-reload", "1");
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });

    await expect(
      importWithChunkReload(async () => {
        throw new TypeError("Failed to fetch dynamically imported module");
      }),
    ).rejects.toThrow(/Failed to fetch dynamically imported module/);

    expect(reload).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("vt:chunk-reload")).toBeNull();
  });
});
