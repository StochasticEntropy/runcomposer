// A runner declares its per-run parameters as data (DESIGN.md §6.2); this is
// the form logic over that declaration: the values a form starts with, what
// is sent, and what can be said to be wrong before asking the server. The
// server stays the authority — its refusal names the parameter and lands on
// the same field.

import type { ParameterSpec } from "./types.ts";

export type ParamValues = Record<string, unknown>;
export type ParamProblem = "required" | "integer" | "choice";

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
    else if ((spec.kind === "string" || spec.kind === "integer") && typeof kept === "string") values[spec.name] = kept;
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
    else if (spec.kind === "integer") {
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
    stored[spec.name] = spec.kind === "integer" ? String(value) : value;
  }
  return mergeValues(specs, stored);
}

export function validateValues(specs: ParameterSpec[], values: ParamValues): Record<string, ParamProblem> {
  const problems: Record<string, ParamProblem> = {};
  for (const spec of specs) {
    const value = values[spec.name];
    const text = typeof value === "string" ? value.trim() : "";
    if (spec.kind === "integer" && text !== "" && !/^-?\d+$/.test(text)) problems[spec.name] = "integer";
    else if (spec.kind === "choice" && text !== "" && !spec.choices.includes(text)) problems[spec.name] = "choice";
    else if (spec.required) {
      const missing = spec.kind === "multi_choice" ? !Array.isArray(value) || value.length === 0 : spec.kind !== "boolean" && text === "";
      if (missing) problems[spec.name] = "required";
    }
  }
  return problems;
}
