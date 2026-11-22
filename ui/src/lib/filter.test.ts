import { describe, expect, it } from "vitest";

import {
  codePointPosition,
  errorPath,
  FilterParseError,
  formatFilter,
  isEverything,
  leaves,
  MATCH_ALL,
  parseFilter,
  quotePattern,
  utf16Offset,
  withoutPath,
} from "./filter.ts";
import type { FilterAst } from "./types.ts";

const problemOf = (text: string): string => {
  try {
    parseFilter(text);
  } catch (error) {
    if (error instanceof FilterParseError) return error.problem;
    throw error;
  }
  return "no error";
};

describe("formatFilter", () => {
  it("writes the expression the way the server's own formatter does", () => {
    const ast: FilterAst = {
      op: "AND",
      items: [{ op: "OR", items: ["Payments", "prefix:Checkout-"] }, { not: "prefix:Quarantine-" }],
    };
    expect(formatFilter(ast)).toBe("(Payments OR prefix:Checkout-) AND NOT prefix:Quarantine-");
    expect(formatFilter({ not: { op: "OR", items: ["Smoke", "Regression"] } })).toBe("NOT (Smoke OR Regression)");
    expect(formatFilter(null)).toBe("");
  });

  it("quotes what would not read back as one pattern", () => {
    expect(quotePattern("Payments")).toBe("Payments");
    expect(quotePattern("regex:^Cart(V2)?$")).toBe('"regex:^Cart(V2)?$"');
    expect(quotePattern("Sprint 12")).toBe('"Sprint 12"');
    expect(quotePattern("AND")).toBe('"AND"');
    expect(quotePattern('say "hi"')).toBe('"say \\"hi\\""');
  });
});

describe("parseFilter", () => {
  it("reads operators, parentheses and NOT with AND binding tighter than OR", () => {
    expect(parseFilter("Payments")).toBe("Payments");
    expect(parseFilter("Payments AND Smoke AND Sprint-12")).toEqual({ op: "AND", items: ["Payments", "Smoke", "Sprint-12"] });
    expect(parseFilter("Payments AND Smoke OR Checkout")).toEqual({
      op: "OR",
      items: [{ op: "AND", items: ["Payments", "Smoke"] }, "Checkout"],
    });
    expect(parseFilter("Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-")).toEqual({
      op: "AND",
      items: ["Payments", { op: "OR", items: ["Smoke", "Regression"] }, { not: "prefix:Quarantine-" }],
    });
    expect(parseFilter("NOT NOT Smoke")).toEqual({ not: { not: "Smoke" } });
    expect(parseFilter("   ")).toBeNull();
  });

  it("keeps a lower-case and/or/not as an ordinary tag", () => {
    expect(parseFilter("and OR not")).toEqual({ op: "OR", items: ["and", "not"] });
  });

  it("reads quoted patterns with spaces, parentheses and escapes", () => {
    expect(parseFilter('"regex:^Cart(V2)?$" OR "Sprint 12"')).toEqual({ op: "OR", items: ["regex:^Cart(V2)?$", "Sprint 12"] });
    expect(parseFilter('"say \\"hi\\""')).toBe('say "hi"');
  });

  it("round-trips every expression it can format", () => {
    const cases: FilterAst[] = [
      "Payments",
      { not: "Payments" },
      { op: "AND", items: ["Payments", { not: "prefix:Quarantine-" }] },
      { op: "OR", items: [{ op: "AND", items: ["Auth", "Smoke"] }, { not: { op: "OR", items: ["Cart", "Catalog"] } }] },
      { op: "AND", items: ["regex:^Cart(V2)?$", "Sprint 12", "OR"] },
      MATCH_ALL,
    ];
    for (const ast of cases) expect(parseFilter(formatFilter(ast))).toEqual(ast);
  });

  it("writes and reads everything as *, never as its wire encoding", () => {
    expect(formatFilter(MATCH_ALL)).toBe("*");
    expect(parseFilter("*")).toEqual(MATCH_ALL);
    expect(isEverything(MATCH_ALL)).toBe(true);
    expect(isEverything({ not: "Smoke" })).toBe(false);
    const narrowed: FilterAst = { op: "AND", items: [MATCH_ALL, { not: "prefix:Quarantine-" }] };
    expect(formatFilter(narrowed)).toBe("* AND NOT prefix:Quarantine-");
    expect(parseFilter("* AND NOT prefix:Quarantine-")).toEqual(narrowed);
    // A tag that is literally an asterisk is quoted, and stays a tag.
    expect(quotePattern("*")).toBe('"*"');
    expect(parseFilter('"*"')).toBe("*");
    expect(problemOf("Payments *")).toBe("missing-operator");
  });

  it("says what is wrong and where", () => {
    expect(problemOf("Payments Smoke")).toBe("missing-operator");
    expect(problemOf("Payments AND")).toBe("unexpected-end");
    expect(problemOf("AND Payments")).toBe("unexpected-operator");
    expect(problemOf("(Payments OR Smoke")).toBe("unbalanced-open");
    expect(problemOf("Payments)")).toBe("unbalanced-close");
    expect(problemOf('"Payments')).toBe("unterminated-quote");
    expect(problemOf('Payments AND ""')).toBe("empty");
    expect(problemOf("Payments AND regex:(")).toBe("needs-quotes");
    expect(problemOf("regex:^Cart(V2)?$")).toBe("needs-quotes");
    expect(problemOf("Payments (Smoke)")).toBe("missing-operator");
    expect(parseFilter("(regex:^Cart)")).toBe("regex:^Cart");
    try {
      parseFilter("Payments Smoke");
    } catch (error) {
      expect((error as FilterParseError).position).toBe(9);
      expect((error as FilterParseError).token).toBe("Smoke");
    }
  });
});

describe("a group of one item", () => {
  it("is written as its operator applied to it, and read back to the same AST", () => {
    const cases: [FilterAst, string][] = [
      [{ op: "AND", items: ["Auth"] }, "AND (Auth)"],
      [{ not: { op: "OR", items: ["Cart"] } }, "NOT OR (Cart)"],
      [{ op: "OR", items: ["Auth", { op: "AND", items: ["Cart"] }] }, "Auth OR AND (Cart)"],
      [{ op: "AND", items: [{ op: "OR", items: [{ op: "AND", items: ["Auth", "Cart"] }] }] }, "AND (OR (Auth AND Cart))"],
    ];
    for (const [ast, text] of cases) {
      expect(formatFilter(ast)).toBe(text);
      expect(parseFilter(text)).toEqual(ast);
    }
  });

  it("left open is unbalanced at its parenthesis", () => {
    try {
      parseFilter("AND (Auth");
      expect.unreachable();
    } catch (error) {
      expect([(error as FilterParseError).problem, (error as FilterParseError).position]).toEqual(["unbalanced-open", 4]);
    }
    expect(problemOf("AND Auth")).toBe("unexpected-operator");
  });
});

describe("positions", () => {
  it("are counted in code points, and a caret offset is in UTF-16 units", () => {
    const text = 'Auth OR 🛒 "Cart';
    try {
      parseFilter(text);
      expect.unreachable();
    } catch (error) {
      const problem = error as FilterParseError;
      expect(problem.problem).toBe("unterminated-quote");
      expect(problem.position).toBe(10); // 🛒 is one character…
      expect(problem.offset).toBe(11); // …and two UTF-16 units
      expect(text.slice(problem.offset, problem.offset + 1)).toBe('"');
    }
    expect(codePointPosition("🛒🛒x", 4)).toBe(2);
    expect(utf16Offset("🛒🛒x", 2)).toBe(4);
    expect(utf16Offset("abc", 2)).toBe(2);
    expect(utf16Offset("ab", 9)).toBe(2);
  });
});

describe("leaves", () => {
  it("lists every pattern with its polarity", () => {
    expect(leaves({ op: "AND", items: ["Payments", { not: { op: "OR", items: ["Smoke", { not: "Auth" }] } }] })).toEqual([
      { pattern: "Payments", negated: false },
      { pattern: "Smoke", negated: true },
      { pattern: "Auth", negated: false },
    ]);
  });
});

describe("errorPath", () => {
  it("reads the node path the server puts in front of a refused filter", () => {
    expect(errorPath("tag_filter: invalid regular expression 'regex:('")).toBe("");
    expect(errorPath("tag_filter.items[1].items[0]: invalid regular expression 'regex:('")).toBe("items[1].items[0]");
    expect(errorPath("tag_filter.items[0].not: empty pattern in 'prefix:'")).toBe("items[0].not");
    expect(errorPath("tag_filter.op: unknown operator 'XOR'")).toBe("");
    expect(errorPath("tag_filter.items[2].items: must be a non-empty list")).toBe("items[2]");
  });

  it("marks nothing when the detail names no node", () => {
    expect(errorPath("selection is empty: provide tag_filter and/or item_ids")).toBeNull();
    expect(errorPath("unknown item ids")).toBeNull();
  });
});

describe("withoutPath", () => {
  it("leaves what is wrong and drops where in the data it is", () => {
    expect(withoutPath("tag_filter.items[1]: invalid regular expression 'regex:(': missing )")).toBe("invalid regular expression 'regex:(': missing )");
    expect(withoutPath("tag_filter.items[0].not: empty pattern in 'prefix:'")).toBe("empty pattern in 'prefix:'");
    expect(withoutPath("tag_filter: pattern must be a non-empty string")).toBe("pattern must be a non-empty string");
    expect(withoutPath("unknown item ids")).toBe("unknown item ids");
  });
});

// A table of expressions as people type them — the plain ones, the ones with
// surprising precedence, quoting, `*`, and the mistakes. The text editor, the
// add field, the command line and the API read each of them alike; this is
// the editor's side of that table.
describe("expressions as people type them", () => {
  const AND = (...items: FilterAst[]): FilterAst => ({ op: "AND", items });
  const OR = (...items: FilterAst[]): FilterAst => ({ op: "OR", items });
  const NOT = (not: FilterAst): FilterAst => ({ not });
  const ALL = NOT("regex:^$");

  const read: [text: string, ast: FilterAst | null][] = [
    ["Payments", "Payments"],
    ["Payments AND Smoke", AND("Payments", "Smoke")],
    ["Payments OR Cart AND Smoke", OR("Payments", AND("Cart", "Smoke"))],
    ["(Payments OR Cart) AND Smoke", AND(OR("Payments", "Cart"), "Smoke")],
    ["NOT Payments", NOT("Payments")],
    ["NOT Payments AND Smoke", AND(NOT("Payments"), "Smoke")],
    ["NOT (Payments OR Cart)", NOT(OR("Payments", "Cart"))],
    ["prefix:Sprint-", "prefix:Sprint-"],
    ["regex:^Sprint-1[23]$", "regex:^Sprint-1[23]$"],
    ['"regex:^Cart(V2)?$"', "regex:^Cart(V2)?$"],
    ["*", ALL],
    ["* AND NOT Smoke", AND(ALL, NOT("Smoke"))],
    ["NOT *", NOT(ALL)],
    ['"Payments"', "Payments"],
    // Single quotes are no quotes: they are part of the tag.
    ["'Payments'", "'Payments'"],
    // A tag is compared without regard to case by the server; the text keeps what was typed.
    ["payments", "payments"],
    ["NOT NOT Payments", NOT(NOT("Payments"))],
    ["((Payments))", "Payments"],
    ["Payments AND NOT prefix:Quarantine- OR Smoke", OR(AND("Payments", NOT("prefix:Quarantine-")), "Smoke")],
    // In quotes, operator words and the star are tags spelled so.
    ['"Auth OR Cart"', "Auth OR Cart"],
    ['"AND"', "AND"],
    ['"*"', "*"],
    ['"a \\"quoted\\" tag"', 'a "quoted" tag'],
    ["  Payments   OR\tCart  ", OR("Payments", "Cart")],
    ["prefix:Sprint- AND regex:3$", AND("prefix:Sprint-", "regex:3$")],
    ["Sprint-12 OR Sprint-13 AND NOT Payments", OR("Sprint-12", AND("Sprint-13", NOT("Payments")))],
    // No text is no filter — "nothing selected", not an error.
    ["", null],
  ];
  it.each(read)("%j is read", (text, ast) => {
    expect(parseFilter(text)).toEqual(ast);
  });

  const refused: [text: string, problem: string, position: number][] = [
    ["Payments AND", "unexpected-end", 12],
    ["Payments OR OR Cart", "unexpected-operator", 12],
    ['"unterminated', "unterminated-quote", 0],
    ["NOT", "unexpected-end", 3],
    ["AND Payments", "unexpected-operator", 0],
    ["(Payments", "unbalanced-open", 0],
    ["Payments)", "unbalanced-close", 8],
    // Two words are two patterns with nothing between them — not one tag with a space.
    ["Payments Smoke", "missing-operator", 9],
    // Operators are capitals: `and` is a word like any other.
    ["Payments and Smoke", "missing-operator", 9],
    ["regex:(", "needs-quotes", 0],
    // A pattern without a pattern: refused here as the server refuses it one step later.
    ["prefix:", "empty", 0],
    ["regex:", "empty", 0],
    ["Payments AND prefix:", "empty", 13],
    ['Smoke OR "regex:"', "empty", 9],
  ];
  it.each(refused)("%j is refused: %s at %i", (text, problem, position) => {
    let caught: unknown;
    try {
      parseFilter(text);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(FilterParseError);
    expect({ problem: (caught as FilterParseError).problem, position: (caught as FilterParseError).position }).toEqual({ problem, position });
  });
});
