// The UI ships EN and DE. A key the source uses and a locale lacks would be
// printed as the key itself — quietly enough to reach a release. So: every
// key the source spells must exist in both files, and every value the SERVER
// can send for a translated family (a run state, an error code, an event
// kind, …) must have a text in both. The server's values are read from the
// Python sources, so a state or code added there fails here until it is
// translated.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { lookup } from "./lib/i18n.ts";
import de from "./locales/de.json";
import en from "./locales/en.json";

const SRC = import.meta.dirname;
const PACKAGE = join(SRC, "..", "..", "src", "runcomposer");

function files(directory: string, keep: (name: string) => boolean): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return files(path, keep);
    return keep(name) ? [path] : [];
  });
}

function leafKeys(node: unknown, prefix = ""): string[] {
  if (node !== null && typeof node === "object") {
    return Object.entries(node).flatMap(([name, child]) => leafKeys(child, prefix ? `${prefix}.${name}` : name));
  }
  return [prefix];
}

const sources = files(SRC, (name) => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)).map((path) => readFileSync(path, "utf-8"));
const python = (relative: string) => readFileSync(join(PACKAGE, relative), "utf-8");
const pythonTree = files(PACKAGE, (name) => name.endsWith(".py")).map((path) => readFileSync(path, "utf-8"));
/** The strings of a Python tuple assignment, found by the pattern of its first line. */
const tuple = (text: string, assignment: RegExp): string[] => {
  const body = assignment.exec(text)?.[1] ?? "";
  return [...body.matchAll(/"([^"]+)"/g)].map((match) => match[1]!);
};
const RUN_STATES = /^RUN_STATES = \(([^)]*)\)/ms;
const COMPLETIONS = /^COMPLETIONS = \(([^)]*)\)/ms;
const REPORTABLE = /^REPORTABLE_STATUSES = \(([^)]*)\)/ms;

const LOCALES = { en, de } as Record<string, Record<string, unknown>>;

describe("locale files", () => {
  it("carry exactly the same keys", () => {
    expect(leafKeys(de).sort()).toEqual(leafKeys(en).sort());
  });

  it("leave no message empty", () => {
    for (const [name, messages] of Object.entries(LOCALES)) {
      for (const key of leafKeys(messages)) {
        const value = lookup(messages, key);
        expect(typeof value === "string" && value.trim() !== "", `${name}: ${key}`).toBe(true);
      }
    }
  });

  it("keep the placeholders of a message the same in both languages", () => {
    const placeholders = (text: unknown) => [...String(text).matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
    for (const key of leafKeys(en)) {
      expect(placeholders(lookup(de, key)), key).toEqual(placeholders(lookup(en, key)));
    }
  });
});

describe("keys the source uses", () => {
  // t("a.b") / tOr("a.b", …) / a ternary of two literals inside t(…)
  const literal = /\bt(?:Or)?\(\s*(?:[\w.]+\s*\?\s*)?"([\w.-]+)"(?:\s*:\s*"([\w.-]+)")?/g;
  const used = new Set<string>();
  for (const text of sources) for (const match of text.matchAll(literal)) for (const key of match.slice(1)) if (key) used.add(key);

  it("finds them at all", () => {
    expect(used.size).toBeGreaterThan(250);
    expect(used.has("filter.rootGroup") && used.has("filter.group")).toBe(true);
  });

  it("exist in every locale", () => {
    for (const [name, messages] of Object.entries(LOCALES)) {
      const missing = [...used].filter((key) => lookup(messages, key) === undefined);
      expect(missing, `missing from ${name}`).toEqual([]);
    }
  });

  it("are the only keys there are — nothing translated that nothing shows", () => {
    // Families addressed by a computed key: `runState.${state}` and friends.
    const families = new Set<string>();
    for (const text of sources) for (const match of text.matchAll(/\bt(?:Or)?\(\s*`([\w.]+)\.\$\{/g)) families.add(match[1]!);
    const unused = leafKeys(en).filter((key) => {
      const base = key.replace(/\.(one|other)$/, "");
      return !used.has(key) && !used.has(base) && ![...families].some((family) => key.startsWith(`${family}.`));
    });
    expect(unused).toEqual([]);
  });
});

describe("values the server sends", () => {
  const lifecycle = python("core/lifecycle.py");
  const model = python("core/model.py");
  const events = [...python("core/events.py").matchAll(/^[A-Z_]+ = "([a-z_.]+)"$/gm)].map((match) => match[1]!);
  const codes = new Set<string>(["invalid-request", "not-found", "method-not-allowed", "internal-error", "network", "client-error"]);
  const reasons = new Set<string>();
  for (const text of pythonTree) {
    for (const match of text.matchAll(/^\s+code = "([a-z-]+)"$/gm)) codes.add(match[1]!);
    for (const match of text.matchAll(/\breason="([a-z-]+)"/g)) reasons.add(match[1]!);
  }
  codes.delete("error"); // the abstract base class; nothing answers with it
  for (const reason of reasons) codes.add(reason); // an ingest refusal answers with its reason as the code

  const families: [family: string, values: string[]][] = [
    ["runState", tuple(lifecycle, RUN_STATES)],
    ["completion", tuple(lifecycle, COMPLETIONS)],
    ["verdict", [...tuple(model, REPORTABLE), "MISSING"]],
    ["event", events],
    ["error.code", [...codes]],
    ["inbox.reason", [...reasons]],
    ["health", ["ok", "degraded", "down", "unknown"]],
    ["outcome", ["new", "duplicate", "replaced"]],
    ["runner.problem", ["required", "integer", "choice"]],
    ["filter.parse", ["empty", "unexpected-end", "unexpected-operator", "missing-operator", "unbalanced-open", "unbalanced-close", "unterminated-quote"]],
  ];

  it("were found in the Python sources", () => {
    expect(tuple(lifecycle, RUN_STATES)).toContain("AWAITING_RESULTS");
    expect(tuple(lifecycle, COMPLETIONS)).toContain("INCOMPLETE");
    expect(events).toContain("run.cancel_requested");
    expect(codes.has("dispatch-refused") && codes.has("invalid-filter") && codes.has("taxonomy-invalid")).toBe(true);
    expect(reasons.has("sha-mismatch") && reasons.has("unsolicited")).toBe(true);
  });

  it.each(families)("%s: every value has a text in both languages", (family, values) => {
    expect(values.length).toBeGreaterThan(0);
    for (const [name, messages] of Object.entries(LOCALES)) {
      const missing = values.filter((value) => typeof lookup(messages, `${family}.${value}`) !== "string");
      expect(missing, `${family} in ${name}`).toEqual([]);
    }
  });
});
