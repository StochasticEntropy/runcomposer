import { describe, expect, it } from "vitest";

import { fromPayload, initialValues, mergeValues, toPayload, validateValues, wholeMatch } from "./params.ts";
import type { ParameterSpec } from "./types.ts";

const spec = (name: string, kind: ParameterSpec["kind"], extra: Partial<ParameterSpec> = {}): ParameterSpec => ({
  name,
  kind,
  label: name,
  help: "",
  default: null,
  choices: [],
  required: false,
  ...extra,
});

const SPECS = [
  spec("seed", "string", { default: "demo" }),
  spec("retries", "integer", { default: 2 }),
  spec("allow_drift", "boolean", { default: false }),
  spec("browser", "choice", { choices: ["chromium", "firefox"], default: "chromium" }),
  spec("partitions", "multi_choice", { choices: ["chromium", "firefox", "webkit"], default: ["chromium", "firefox"] }),
  spec("note", "string"),
];

describe("form values", () => {
  it("start from the declared defaults", () => {
    expect(initialValues(SPECS)).toEqual({
      seed: "demo",
      retries: "2",
      allow_drift: false,
      browser: "chromium",
      partitions: ["chromium", "firefox"],
      note: "",
    });
  });

  it("lay stored values over the defaults and drop what no longer fits", () => {
    const merged = mergeValues(SPECS, { seed: "b", browser: "safari", partitions: ["webkit", "edge"], gone: "x", allow_drift: true });
    expect(merged).toMatchObject({ seed: "b", browser: "chromium", partitions: ["webkit"], allow_drift: true });
    expect(merged).not.toHaveProperty("gone");
  });

  it("become a typed payload, with nothing sent for an optional field left empty", () => {
    expect(toPayload(SPECS, { ...initialValues(SPECS), retries: " 7 ", note: "" })).toEqual({
      seed: "demo",
      retries: 7,
      allow_drift: false,
      browser: "chromium",
      partitions: ["chromium", "firefox"],
    });
    // Not a number: sent as typed, so the server's refusal lands on the field.
    expect(toPayload(SPECS, { ...initialValues(SPECS), retries: "many" }).retries).toBe("many");
  });

  it("are rebuilt from the parameters of an earlier dispatch", () => {
    expect(fromPayload(SPECS, { seed: "a", retries: 5, partitions: ["firefox"], allow_drift: true })).toMatchObject({
      seed: "a",
      retries: "5",
      partitions: ["firefox"],
      allow_drift: true,
      browser: "chromium",
    });
  });
});

describe("validateValues", () => {
  it("names what can be known to be wrong before asking the server", () => {
    const specs = [
      spec("seed", "string", { required: true }),
      spec("retries", "integer"),
      spec("partitions", "multi_choice", { choices: ["chromium"], required: true }),
      spec("allow_drift", "boolean", { required: true }),
    ];
    expect(validateValues(specs, { seed: " ", retries: "1.5", partitions: [], allow_drift: false })).toEqual({
      seed: "required",
      retries: "integer",
      partitions: "required",
    });
    expect(validateValues(specs, { seed: "a", retries: "-3", partitions: ["chromium"], allow_drift: false })).toEqual({});
    expect(validateValues(specs, { seed: "a", retries: "", partitions: ["chromium"], allow_drift: true })).toEqual({});
  });
});

describe("text, mapping and pattern", () => {
  const specs = [
    spec("build", "string", { pattern: "\\d{4}\\.\\d{1,2}" }),
    spec("notes", "text"),
    spec("variables", "mapping", { pattern: "[a-z0-9-]+", default: { locale: "de" } }),
    spec("extra", "mapping"),
  ];

  it("a mapping is edited as rows and sent as an object", () => {
    const values = initialValues(specs);
    expect(values.variables).toEqual([{ key: "locale", value: "de" }]);
    expect(values.extra).toEqual([]);
    expect(toPayload(specs, { ...values, build: "2026.11", notes: "first line\nsecond line" })).toEqual({
      build: "2026.11",
      notes: "first line\nsecond line",
      variables: { locale: "de" },
    });
    const edited = { ...values, variables: [{ key: " locale ", value: "en" }, { key: "", value: "" }, { key: "currency", value: "eur" }] };
    expect(toPayload(specs, edited).variables).toEqual({ locale: "en", currency: "eur" });
    // Emptying a mapping that has a default sends the empty mapping, not nothing.
    expect(toPayload(specs, { ...values, variables: [] }).variables).toEqual({});
    expect(toPayload(specs, values)).not.toHaveProperty("extra");
  });

  it("is rebuilt from an earlier dispatch and survives a stored draft", () => {
    expect(fromPayload(specs, { variables: { locale: "fr", currency: "eur" }, notes: "as before" })).toMatchObject({
      variables: [{ key: "locale", value: "fr" }, { key: "currency", value: "eur" }],
      notes: "as before",
    });
    expect(mergeValues(specs, { variables: [{ key: "a", value: "b" }], extra: "not rows" })).toMatchObject({
      variables: [{ key: "a", value: "b" }],
      extra: [],
    });
  });

  it("checks a pattern against the whole value, per value of a mapping", () => {
    const base = initialValues(specs);
    expect(validateValues(specs, { ...base, build: "2026.11" })).toEqual({});
    expect(validateValues(specs, { ...base, build: "build 2026.11" })).toEqual({ build: "pattern" });
    expect(validateValues(specs, { ...base, build: "" })).toEqual({});
    expect(validateValues(specs, { ...base, variables: [{ key: "locale", value: "DE!" }] })).toEqual({ variables: "pattern" });
    expect(validateValues(specs, { ...base, variables: [{ key: "a", value: "x" }, { key: "a", value: "y" }] })).toEqual({ variables: "duplicate" });
    expect(validateValues(specs, { ...base, variables: [{ key: "", value: "x" }] })).toEqual({ variables: "duplicate" });
    expect(validateValues([spec("extra", "mapping", { required: true })], { extra: [] })).toEqual({ extra: "required" });
  });

  it("checks the names of a mapping against their own pattern, before the values", () => {
    const switches = [spec("switches", "mapping", { key_pattern: "[A-Z][A-Za-z-]+", pattern: "on|off" })];
    const check = (rows: { key: string; value: string }[]) => validateValues(switches, { switches: rows });
    expect(check([{ key: "Checkout-Express", value: "on" }])).toEqual({});
    expect(check([{ key: "checkout express", value: "on" }])).toEqual({ switches: "key" });
    // The whole name must have the form, not a part of it.
    expect(check([{ key: "Checkout Express", value: "on" }])).toEqual({ switches: "key" });
    // A name of the wrong form is said before a value of the wrong form in the same field.
    expect(check([{ key: "checkout", value: "maybe" }])).toEqual({ switches: "key" });
    expect(check([{ key: "Checkout", value: "maybe" }])).toEqual({ switches: "pattern" });
    // A row that was added and left empty is no row.
    expect(check([{ key: "Checkout", value: "on" }, { key: "", value: "" }])).toEqual({});
    // A name pattern the browser cannot read is left to the server.
    expect(validateValues([spec("switches", "mapping", { key_pattern: "(?P<name>x)" })], { switches: [{ key: "anything", value: "v" }] })).toEqual({});
  });

  it("a one-line string may not hold a line break; a pattern the browser cannot read is left to the server", () => {
    expect(validateValues([spec("build", "string")], { build: "a\nb" })).toEqual({ build: "line" });
    expect(wholeMatch("(?P<year>\\d{4})")).toBeNull();
    expect(validateValues([spec("build", "string", { pattern: "(?P<year>\\d{4})" })], { build: "anything" })).toEqual({});
    expect(wholeMatch("a|b")?.test("ab")).toBe(false);
    expect(wholeMatch("a|b")?.test("b")).toBe(true);
  });
});
