#!/usr/bin/env node
/**
 * Regenerate src/i18n/__tests__/i18n-missing-baseline.json.
 *
 * The baseline lists keys that en.json has but a locale does not. It is a
 * shrink-only record of existing translation debt — running this script after
 * a translation lands is how entries leave the file. Do not run it to absorb a
 * *new* missing key: add the translation instead.
 *
 * Usage: node scripts/refresh_i18n_baseline.mjs
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const localesDir = join(root, "src", "i18n", "locales");
const baselinePath = join(root, "src", "i18n", "__tests__", "i18n-missing-baseline.json");

const load = (name) => JSON.parse(readFileSync(join(localesDir, `${name}.json`), "utf8"));

const flatten = (value, prefix = "", out = {}) => {
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (child !== null && typeof child === "object" && !Array.isArray(child)) {
      flatten(child, path, out);
    } else {
      out[path] = child;
    }
  }
  return out;
};

const enKeys = Object.keys(flatten(load("en")));
const output = {
  _comment:
    "Keys present in en.json but not yet translated. This is a shrink-only baseline: adding a key here is a regression, and a key that later exists in the locale must be removed. Regenerate with scripts/refresh_i18n_baseline.mjs.",
};

for (const locale of ["ja", "ko", "ar"]) {
  const present = new Set(Object.keys(flatten(load(locale))));
  output[locale] = enKeys.filter((key) => !present.has(key));
}

writeFileSync(baselinePath, `${JSON.stringify(output, null, 2)}\n`, "utf8");
console.log(
  `baseline written: ${["ja", "ko", "ar"].map((l) => `${l}=${output[l].length}`).join(" ")}`,
);
