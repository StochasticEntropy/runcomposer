import { describe, expect, it } from "vitest";

import {
  codePointPosition,
  errorNodeKey,
  FilterParseError,
  formatFilter,
  isEverything,
  leaves,
  MATCH_ALL,
  parseFilter,
  quotePattern,
  utf16Offset,
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

describe("errorNodeKey", () => {
  it("writes the path a refused filter names the way the editor keys its nodes", () => {
    expect(errorNodeKey([])).toBe("");
    expect(errorNodeKey(["items", 1, "items", 0])).toBe("items[1].items[0]");
    expect(errorNodeKey(["items", 0, "not"])).toBe("items[0].not");
    expect(errorNodeKey(["op"])).toBe("");
    expect(errorNodeKey(["items", 2, "items"])).toBe("items[2]");
  });

  it("marks nothing when the answer names no node", () => {
    expect(errorNodeKey(undefined)).toBeNull();
    expect(errorNodeKey("tag_filter.items[1]")).toBeNull();
  });
});

// The expressions people type — precedence, quoting, `*`, and the mistakes —
// are shared cases now (tests/fixtures/filter-text.json, "strings"), read by
// this reader and the server's alike: filter.fixture.test.ts.
