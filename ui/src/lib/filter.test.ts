import { describe, expect, it } from "vitest";

import { FilterParseError, MATCH_ALL, errorPath, formatFilter, leaves, parseFilter, quotePattern } from "./filter.ts";
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

  it("says what is wrong and where", () => {
    expect(problemOf("Payments Smoke")).toBe("missing-operator");
    expect(problemOf("Payments AND")).toBe("unexpected-end");
    expect(problemOf("AND Payments")).toBe("unexpected-operator");
    expect(problemOf("(Payments OR Smoke")).toBe("unbalanced-open");
    expect(problemOf("Payments)")).toBe("unbalanced-close");
    expect(problemOf('"Payments')).toBe("unterminated-quote");
    expect(problemOf('Payments AND ""')).toBe("empty");
    try {
      parseFilter("Payments Smoke");
    } catch (error) {
      expect((error as FilterParseError).position).toBe(9);
      expect((error as FilterParseError).token).toBe("Smoke");
    }
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
