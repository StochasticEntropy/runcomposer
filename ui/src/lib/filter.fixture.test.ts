// The shared conformance cases of the filter's text form: the same file the
// server's reader is tested against (tests/test_filter_text.py). One
// grammar, two readers — and every case runs on both sides, none skipped.

import { describe, expect, it } from "vitest";

import cases from "../../../tests/fixtures/filter-text.json";
import { FilterParseError, codePointPosition, formatFilter, parseFilter, utf16Offset } from "./filter.ts";
import type { FilterAst } from "./types.ts";

interface ErrorCase {
  text: string;
  error: { problem: string; position: number; token?: string };
}

const parseCases = cases.parse as { text: string; ast: FilterAst | null }[];
const formatCases = cases.format as { ast: FilterAst | null; text: string }[];
const errorCases = cases.errors as ErrorCase[];
const label = (value: unknown) => JSON.stringify(value);

function errorOf(text: string): FilterParseError {
  try {
    parseFilter(text);
  } catch (error) {
    if (error instanceof FilterParseError) return error;
    throw error;
  }
  throw new Error(`${label(text)} was read without an error`);
}

describe("the shared cases", () => {
  it("are all there and none is set aside for one reader", () => {
    expect(parseCases.length).toBeGreaterThan(40);
    expect(formatCases.length).toBeGreaterThan(25);
    expect(errorCases.length).toBeGreaterThan(25);
    const all: object[] = [...parseCases, ...formatCases, ...errorCases];
    expect(all.filter((entry) => "ui" in entry)).toEqual([]);
  });

  describe("parse: reading the text gives the AST", () => {
    for (const entry of parseCases) {
      it(label(entry.text), () => {
        expect(parseFilter(entry.text)).toEqual(entry.ast);
      });
    }
  });

  describe("format: writing the AST gives its canonical text, which reads back", () => {
    for (const entry of formatCases) {
      it(label(entry.ast), () => {
        expect(formatFilter(entry.ast)).toBe(entry.text);
        expect(parseFilter(entry.text)).toEqual(entry.ast);
      });
    }
  });

  describe("errors: the problem, where it is in code points, and the word found there", () => {
    for (const entry of errorCases) {
      it(label(entry.text), () => {
        const error = errorOf(entry.text);
        expect({ problem: error.problem, position: error.position }).toEqual({ problem: entry.error.problem, position: entry.error.position });
        if (entry.error.token !== undefined) expect(error.token).toBe(entry.error.token);
        // The UTF-16 offset for a caret is the same place.
        expect(codePointPosition(entry.text, error.offset)).toBe(error.position);
        expect(utf16Offset(entry.text, error.position)).toBe(error.offset);
      });
    }
  });
});
