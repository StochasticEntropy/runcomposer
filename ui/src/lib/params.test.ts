import { describe, expect, it } from "vitest";

import { fromPayload, initialValues, mergeValues, toPayload, validateValues } from "./params.ts";
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
