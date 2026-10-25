import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

describe("Vite API proxy config", () => {
  const configPath = path.resolve(__dirname, "../../vite.config.ts");
  const config = fs.readFileSync(configPath, "utf8");

  it("proxies channel runtime endpoints", () => {
    expect(config).toContain('"/channels"');
  });

  it("proxies settings endpoints", () => {
    expect(config).toContain('"/settings/llm"');
    expect(config).toContain('"/settings/data-sources"');
  });

  it("proxies authentication endpoints", () => {
    expect(config).toContain('"/auth"');
  });

  it("proxies monitor SPA/API twin with HTML Accept bypass", () => {
    expect(config).toContain('"/monitor"');
    expect(config).toContain("apiProxyWithHtmlFallback");
  });

  it("proxies sector-pick SPA/API twin with HTML Accept bypass", () => {
    expect(config).toContain('"/sector-pick"');
  });

  it("proxies market-pick SPA/API twin with HTML Accept bypass", () => {
    expect(config).toContain('"/market-pick"');
  });
});
