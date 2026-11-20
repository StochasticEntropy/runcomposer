import { describe, expect, it } from "vitest";

import {
  ROOT_ID,
  addGroup,
  addPatterns,
  addTag,
  emptyRoot,
  fromAst,
  negatedPatterns,
  positivePatterns,
  removeNode,
  removePatterns,
  setOp,
  setPattern,
  toAst,
  toAstWithPaths,
  toggleNot,
  ungroup,
} from "./editor.ts";
import type { GroupNode } from "./editor.ts";
import { formatFilter } from "./filter.ts";
import type { FilterAst } from "./types.ts";

const text = (root: GroupNode) => formatFilter(toAst(root));
const childId = (root: GroupNode, index: number) => root.children[index]!.id;

describe("fromAst / toAst", () => {
  it("round-trips the expressions a run can carry", () => {
    const cases: FilterAst[] = [
      "Payments",
      { not: "Payments" },
      { op: "AND", items: ["Payments", "Smoke"] },
      { op: "AND", items: [{ op: "OR", items: ["Payments", "prefix:Checkout-", "regex:^Cart(V2)?$"] }, { not: "prefix:Quarantine-" }] },
      { not: { op: "OR", items: ["Cart", "Catalog"] } },
      { op: "OR", items: [{ not: { op: "AND", items: ["Auth", "Smoke"] } }, "Regression"] },
    ];
    for (const ast of cases) expect(toAst(fromAst(ast))).toEqual(ast);
  });

  it("an empty builder is no filter at all", () => {
    expect(toAst(emptyRoot())).toBeNull();
    expect(toAst(fromAst(null))).toBeNull();
  });

  it("keeps the meaning of a double negation", () => {
    expect(text(fromAst({ not: { not: "Smoke" } }))).toBe("NOT NOT Smoke");
  });

  it("sends no scaffolding: empty groups vanish, a group of one is that one", () => {
    let root = addTag(emptyRoot(), ROOT_ID, "Payments");
    const withGroup = addGroup(root, ROOT_ID, "OR");
    expect(toAst(withGroup.root)).toBe("Payments");
    root = addTag(withGroup.root, withGroup.id, "Smoke");
    expect(toAst(root)).toEqual({ op: "AND", items: ["Payments", "Smoke"] });
    root = addTag(root, withGroup.id, "Regression");
    expect(text(root)).toBe("Payments AND (Smoke OR Regression)");
    expect(toAst(addTag(emptyRoot(), ROOT_ID, "   "))).toBeNull();
  });
});

describe("operations", () => {
  const base = fromAst({ op: "AND", items: ["Payments", { op: "OR", items: ["Smoke", "Regression"] }] });

  it("negates a rule and a group", () => {
    expect(text(toggleNot(base, childId(base, 0)))).toBe("NOT Payments AND (Smoke OR Regression)");
    expect(text(toggleNot(base, childId(base, 1)))).toBe("Payments AND NOT (Smoke OR Regression)");
    expect(text(toggleNot(base, ROOT_ID))).toBe("NOT (Payments AND (Smoke OR Regression))");
  });

  it("changes an operator, a pattern, and removes things", () => {
    expect(text(setOp(base, ROOT_ID, "OR"))).toBe("Payments OR (Smoke OR Regression)");
    expect(text(setPattern(base, childId(base, 0), "Auth-SSO"))).toBe("Auth-SSO AND (Smoke OR Regression)");
    expect(text(removeNode(base, childId(base, 0)))).toBe("Smoke OR Regression");
    expect(text(removeNode(base, childId(base, 1)))).toBe("Payments");
    expect(toAst(removeNode(base, ROOT_ID))).toBeNull();
  });

  it("dissolves a group into its parent — unless it is negated", () => {
    expect(text(ungroup(base, childId(base, 1)))).toBe("Payments AND Smoke AND Regression");
    const negated = toggleNot(base, childId(base, 1));
    expect(text(ungroup(negated, childId(negated, 1)))).toBe("Payments AND NOT (Smoke OR Regression)");
  });

  it("never changes the tree it was given", () => {
    const before = JSON.stringify(base);
    toggleNot(base, childId(base, 0));
    removeNode(base, childId(base, 1));
    addTag(base, ROOT_ID, "Auth");
    expect(JSON.stringify(base)).toBe(before);
  });
});

describe("the picker's operations", () => {
  it("adds several picks as ONE condition", () => {
    const root = addTag(emptyRoot(), ROOT_ID, "Smoke");
    expect(text(addPatterns(root, ROOT_ID, ["Payments", "Checkout"], "any"))).toBe("Smoke AND (Payments OR Checkout)");
    expect(text(addPatterns(root, ROOT_ID, ["Payments", "Checkout"], "all"))).toBe("Smoke AND Payments AND Checkout");
    expect(text(addPatterns(root, ROOT_ID, ["Payments", "Checkout"], "none"))).toBe("Smoke AND NOT Payments AND NOT Checkout");
    expect(text(addPatterns(root, ROOT_ID, ["Payments"], "any"))).toBe("Smoke AND Payments");
    expect(text(addPatterns(root, ROOT_ID, ["Payments"], "none"))).toBe("Smoke AND NOT Payments");
  });

  it("joins a group that already has the wanted operator instead of nesting", () => {
    const root = fromAst({ op: "OR", items: ["Payments", "Checkout"] });
    expect(text(addPatterns(root, ROOT_ID, ["Cart", "Catalog"], "any"))).toBe("Payments OR Checkout OR Cart OR Catalog");
    expect(text(addPatterns(root, ROOT_ID, ["Cart", "Catalog"], "all"))).toBe("Payments OR Checkout OR (Cart AND Catalog)");
    expect(text(addPatterns(root, ROOT_ID, ["Cart", "Catalog"], "none"))).toBe("Payments OR Checkout OR NOT (Cart OR Catalog)");
  });

  it("removes an included tag wherever it is, and the group it leaves empty", () => {
    const root = fromAst({ op: "AND", items: [{ op: "OR", items: ["Payments", "Checkout"] }, "Payments", { not: "Payments" }] });
    expect(text(removePatterns(root, ["Payments"]))).toBe("Checkout AND NOT Payments");
    const nested = fromAst({ op: "AND", items: ["Smoke", { op: "OR", items: ["Payments"] }] });
    expect(text(removePatterns(nested, ["Payments"]))).toBe("Smoke");
  });

  it("knows which patterns end up included and which excluded", () => {
    const root = fromAst({ op: "AND", items: ["Payments", { not: "Smoke" }, { not: { op: "OR", items: ["Cart", { not: "Auth" }] } }] });
    expect([...positivePatterns(root)].sort()).toEqual(["Auth", "Payments"]);
    expect([...negatedPatterns(root)].sort()).toEqual(["Cart", "Smoke"]);
  });
});

describe("toAstWithPaths", () => {
  it("maps each AST path back to the rule it came from", () => {
    const root = fromAst({ op: "AND", items: ["Payments", { op: "OR", items: ["regex:(", "Smoke"] }, { not: "prefix:" }] });
    const { paths } = toAstWithPaths(root);
    const group = root.children[1] as GroupNode;
    expect(paths.get("")).toBe(ROOT_ID);
    expect(paths.get("items[0]")).toBe(root.children[0]!.id);
    expect(paths.get("items[1].items[0]")).toBe(group.children[0]!.id);
    expect(paths.get("items[2]")).toBe(root.children[2]!.id);
    expect(paths.get("items[2].not")).toBe(root.children[2]!.id);
  });

  it("follows the collapsing of single-rule groups", () => {
    const root = addTag(emptyRoot(), ROOT_ID, "regex:(");
    const { ast, paths } = toAstWithPaths(root);
    expect(ast).toBe("regex:(");
    expect(paths.get("")).toBe(root.children[0]!.id);
  });
});
