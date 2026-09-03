// Node's own test runner, no dependency: `npm test` in ui/.
// The adapter is the whole SVAR boundary (DESIGN.md §10), so this is where
// the filter's behaviour is pinned down — a click-through cannot prove that a
// nested expression survives the round trip.

import assert from "node:assert/strict";
import test from "node:test";

import {
  EMPTY_FILTER,
  activePatterns,
  appendPattern,
  listConditions,
  patternToSvarRule,
  removeConditionAt,
  removePattern,
  svarToAst,
  togglePattern,
} from "./filterAdapter.js";

const build = (...patterns) => patterns.reduce(appendPattern, EMPTY_FILTER);

test("a pattern clicked twice is added, then taken back out", () => {
  const once = togglePattern(EMPTY_FILTER, "Payments");
  assert.equal(once.rules.length, 1);
  assert.deepEqual(togglePattern(once, "Payments"), EMPTY_FILTER);
});

test("a prefix pattern round-trips through the rule shape", () => {
  const value = togglePattern(EMPTY_FILTER, "prefix:Smoke");
  assert.deepEqual([...activePatterns(value)], ["prefix:Smoke"]);
  assert.equal(svarToAst(value), "prefix:Smoke");
  assert.deepEqual(togglePattern(value, "prefix:Smoke"), EMPTY_FILTER);
});

test("the tree's active set holds every pattern the filter carries", () => {
  const value = build("Payments", "SHOP-1200", "prefix:Smoke");
  assert.deepEqual([...activePatterns(value)].sort(), ["Payments", "SHOP-1200", "prefix:Smoke"]);
});

test("removing the middle condition keeps the other two, in order", () => {
  const value = build("Payments", "Auth", "Catalog");
  const left = removeConditionAt(value, 1);
  assert.deepEqual(
    listConditions(left).map((condition) => condition.value),
    ["Payments", "Catalog"]
  );
  assert.deepEqual(svarToAst(left), { op: "AND", items: ["Payments", "Catalog"] });
});

test("conditions are described in the app's words, not SVAR's", () => {
  const value = {
    glue: "and",
    rules: [
      patternToSvarRule("Payments"),
      patternToSvarRule("prefix:Smoke"),
      { field: "tag", type: "text", filter: "notContains", value: "wip" },
    ],
  };
  assert.deepEqual(
    listConditions(value).map((condition) => [condition.index, condition.operator, condition.value]),
    [
      [0, "is", "Payments"],
      [1, "startsWith", "Smoke"],
      [2, "notContains", "wip"],
    ]
  );
});

test("a negated rule is not an active pattern — the tree must not claim it", () => {
  const value = { glue: "and", rules: [{ field: "tag", type: "text", filter: "notEqual", value: "SHOP-1200" }] };
  assert.equal(activePatterns(value).has("SHOP-1200"), false);
  assert.deepEqual(svarToAst(value), { not: "SHOP-1200" });
});

// Nested groups: the engine and the runspec grammar take arbitrary nesting,
// and the widget builds it, so the adapter has to carry it through unchanged.
const nested = {
  glue: "and",
  rules: [
    patternToSvarRule("Regression"),
    { glue: "or", rules: [patternToSvarRule("Checkout"), patternToSvarRule("Cart")] },
  ],
};

test("a nested group becomes a nested AST, not a flattened one", () => {
  assert.deepEqual(svarToAst(nested), {
    op: "AND",
    items: ["Regression", { op: "OR", items: ["Checkout", "Cart"] }],
  });
});

test("a group is one removable condition and counts what is inside it", () => {
  const [outer, group] = listConditions(nested);
  assert.deepEqual(outer, { index: 0, kind: "condition", operator: "is", value: "Regression", pattern: "Regression" });
  assert.deepEqual(group, { index: 1, kind: "group", count: 2 });
  assert.deepEqual(svarToAst(removeConditionAt(nested, 1)), "Regression");
});

test("the tree sees patterns inside a group, and switching one off prunes it", () => {
  assert.equal(activePatterns(nested).has("Cart"), true);
  const left = togglePattern(nested, "Cart");
  assert.deepEqual(svarToAst(left), { op: "AND", items: ["Regression", "Checkout"] });
  const bare = removePattern(left, "Checkout");
  assert.deepEqual(svarToAst(bare), "Regression"); // the emptied group is gone, not left behind
});

test("an empty filter compiles to no filter at all", () => {
  assert.equal(svarToAst(EMPTY_FILTER), null);
  assert.deepEqual(listConditions(EMPTY_FILTER), []);
  assert.deepEqual([...activePatterns(EMPTY_FILTER)], []);
});
