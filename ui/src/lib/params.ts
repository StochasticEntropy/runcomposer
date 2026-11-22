// A runner declares its per-run parameters as data (DESIGN.md §6.2); this is
// the form logic over that declaration: the values a form starts with, what
// is sent, and what can be said to be wrong before asking the server. The
// server stays the authority — its refusal names the parameter and lands on
// the same field.

import type { ParameterSpec } from "./types.ts";

export type ParamValues = Record<string, unknown>;
export type ParamProblem = "required" | "integer" | "choice" | "pattern" | "key" | "duplicate" | "line";

/** A mapping is edited as rows, so a key can be empty or doubled while it is typed. */
export interface MappingRow {
  key: string;
  value: string;
}

const rowsOf = (value: unknown): MappingRow[] =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? Object.entries(value as Record<string, unknown>).map(([key, entry]) => ({ key, value: String(entry) }))
    : [];

const isRows = (value: unknown): value is MappingRow[] =>
  Array.isArray(value) && value.every((row) => row !== null && typeof row === "object" && "key" in row && "value" in row);

/** The pattern as the server applies it: the WHOLE value must match. Null when
 * the browser cannot compile it — then the server alone decides. */
export function wholeMatch(pattern: string | null | undefined): RegExp | null {
  if (!pattern) return null;
  try {
    return new RegExp(`^(?:${pattern})$`, "u");
  } catch {
    try {
      return new RegExp(`^(?:${pattern})$`);
    } catch {
      return null;
    }
  }
}

export function initialValue(spec: ParameterSpec): unknown {
  switch (spec.kind) {
    case "boolean":
      return spec.default === true;
    case "multi_choice":
      return Array.isArray(spec.default) ? spec.default.filter((entry) => spec.choices.includes(String(entry))) : [];
    case "integer":
      return typeof spec.default === "number" ? String(spec.default) : "";
    case "choice":
      return typeof spec.default === "string" && spec.choices.includes(spec.default) ? spec.default : "";
    case "mapping":
      return rowsOf(spec.default);
    default:
      return typeof spec.default === "string" ? spec.default : "";
  }
}

export const initialValues = (specs: ParameterSpec[]): ParamValues =>
  Object.fromEntries(specs.map((spec) => [spec.name, initialValue(spec)]));

/** Stored values laid over the defaults; anything the declaration no longer knows is dropped. */
export function mergeValues(specs: ParameterSpec[], stored: ParamValues | undefined): ParamValues {
  const values = initialValues(specs);
  for (const spec of specs) {
    const kept = stored?.[spec.name];
    if (kept === undefined) continue;
    if (spec.kind === "boolean" && typeof kept === "boolean") values[spec.name] = kept;
    else if (spec.kind === "multi_choice" && Array.isArray(kept))
      values[spec.name] = kept.filter((entry) => spec.choices.includes(String(entry)));
    else if (spec.kind === "choice" && typeof kept === "string" && (kept === "" || spec.choices.includes(kept)))
      values[spec.name] = kept;
    else if (spec.kind === "mapping" && isRows(kept)) values[spec.name] = kept;
    else if ((spec.kind === "string" || spec.kind === "text" || spec.kind === "integer") && typeof kept === "string") values[spec.name] = kept;
  }
  return values;
}

/** What a dispatch carries: typed values, with nothing sent for an optional field left empty. */
export function toPayload(specs: ParameterSpec[], values: ParamValues): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const spec of specs) {
    const value = values[spec.name];
    if (spec.kind === "boolean") payload[spec.name] = value === true;
    else if (spec.kind === "multi_choice") payload[spec.name] = Array.isArray(value) ? value : [];
    else if (spec.kind === "mapping") {
      const rows = (isRows(value) ? value : []).filter((row) => row.key.trim() !== "");
      // An emptied mapping is sent as empty when it had a default — so the default does not come back.
      if (rows.length > 0 || rowsOf(spec.default).length > 0) {
        payload[spec.name] = Object.fromEntries(rows.map((row) => [row.key.trim(), row.value]));
      }
    } else if (spec.kind === "integer") {
      const text = String(value ?? "").trim();
      if (text !== "") payload[spec.name] = /^-?\d+$/.test(text) ? Number(text) : text;
    } else {
      const text = String(value ?? "");
      if (text !== "") payload[spec.name] = text;
    }
  }
  return payload;
}

/** Values of an earlier dispatch → form values (for "run again with …"). */
export function fromPayload(specs: ParameterSpec[], payload: Record<string, unknown>): ParamValues {
  const stored: ParamValues = {};
  for (const spec of specs) {
    const value = payload[spec.name];
    if (value === undefined || value === null) continue;
    stored[spec.name] = spec.kind === "integer" ? String(value) : spec.kind === "mapping" ? rowsOf(value) : value;
  }
  return mergeValues(specs, stored);
}

export function validateValues(specs: ParameterSpec[], values: ParamValues): Record<string, ParamProblem> {
  const problems: Record<string, ParamProblem> = {};
  for (const spec of specs) {
    const value = values[spec.name];
    const text = typeof value === "string" ? value.trim() : "";
    const pattern = wholeMatch(spec.pattern);
    if (spec.kind === "mapping") {
      const rows = (isRows(value) ? value : []).filter((row) => row.key.trim() !== "" || row.value !== "");
      const keys = rows.map((row) => row.key.trim());
      const keyPattern = wholeMatch(spec.key_pattern);
      if (keys.some((key) => key === "") || new Set(keys).size !== keys.length) problems[spec.name] = "duplicate";
      // Names first: a name of the wrong form is the more basic mistake of a row.
      else if (keyPattern && keys.some((key) => !keyPattern.test(key))) problems[spec.name] = "key";
      else if (pattern && rows.some((row) => !pattern.test(row.value))) problems[spec.name] = "pattern";
      else if (spec.required && rows.length === 0) problems[spec.name] = "required";
      continue;
    }
    if (spec.kind === "integer" && text !== "" && !/^-?\d+$/.test(text)) problems[spec.name] = "integer";
    else if (spec.kind === "choice" && text !== "" && !spec.choices.includes(text)) problems[spec.name] = "choice";
    else if (spec.kind === "string" && typeof value === "string" && /[\r\n]/.test(value)) problems[spec.name] = "line";
    else if ((spec.kind === "string" || spec.kind === "text") && typeof value === "string" && value !== "" && pattern && !pattern.test(value))
      problems[spec.name] = "pattern";
    else if (spec.required) {
      const missing = spec.kind === "multi_choice" ? !Array.isArray(value) || value.length === 0 : spec.kind !== "boolean" && text === "";
      if (missing) problems[spec.name] = "required";
    }
  }
  return problems;
}
