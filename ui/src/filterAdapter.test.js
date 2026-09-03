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
  const once = togglePattern(EMPTY_FILTER, "KKMeldung");
  assert.equal(once.rules.length, 1);
  assert.deepEqual(togglePattern(once, "KKMeldung"), EMPTY_FILTER);
});

test("a prefix pattern round-trips through the rule shape", () => {
  const value = togglePattern(EMPTY_FILTER, "prefix:Smoke");
  assert.deepEqual([...activePatterns(value)], ["prefix:Smoke"]);
  assert.equal(svarToAst(value), "prefix:Smoke");
  assert.deepEqual(togglePattern(value, "prefix:Smoke"), EMPTY_FILTER);
});

test("the tree's active set holds every pattern the filter carries", () => {
  const value = build("KKMeldung", "GD3", "prefix:Smoke");
  assert.deepEqual([...activePatterns(value)].sort(), ["GD3", "KKMeldung", "prefix:Smoke"]);
});

test("removing the middle condition keeps the other two, in order", () => {
  const value = build("KKMeldung", "Privatinsolvenz", "Pfaendung");
  const left = removeConditionAt(value, 1);
  assert.deepEqual(
    listConditions(left).map((condition) => condition.value),
    ["KKMeldung", "Pfaendung"]
  );
  assert.deepEqual(svarToAst(left), { op: "AND", items: ["KKMeldung", "Pfaendung"] });
});

test("conditions are described in the app's words, not SVAR's", () => {
  const value = {
    glue: "and",
    rules: [
      patternToSvarRule("KKMeldung"),
      patternToSvarRule("prefix:Smoke"),
      { field: "tag", type: "text", filter: "notContains", value: "wip" },
    ],
  };
  assert.deepEqual(
    listConditions(value).map((condition) => [condition.index, condition.operator, condition.value]),
    [
      [0, "is", "KKMeldung"],
      [1, "startsWith", "Smoke"],
      [2, "notContains", "wip"],
    ]
  );
});

test("a negated rule is not an active pattern — the tree must not claim it", () => {
  const value = { glue: "and", rules: [{ field: "tag", type: "text", filter: "notEqual", value: "GD3" }] };
  assert.equal(activePatterns(value).has("GD3"), false);
  assert.deepEqual(svarToAst(value), { not: "GD3" });
});

// Nested groups: the engine and the runspec grammar take arbitrary nesting,
// and the widget builds it, so the adapter has to carry it through unchanged.
const nested = {
  glue: "and",
  rules: [
    patternToSvarRule("TZR"),
    { glue: "or", rules: [patternToSvarRule("Krankenkasse"), patternToSvarRule("Drittrecht")] },
  ],
};

test("a nested group becomes a nested AST, not a flattened one", () => {
  assert.deepEqual(svarToAst(nested), {
    op: "AND",
    items: ["TZR", { op: "OR", items: ["Krankenkasse", "Drittrecht"] }],
  });
});

test("a group is one removable condition and counts what is inside it", () => {
  const [outer, group] = listConditions(nested);
  assert.deepEqual(outer, { index: 0, kind: "condition", operator: "is", value: "TZR", pattern: "TZR" });
  assert.deepEqual(group, { index: 1, kind: "group", count: 2 });
  assert.deepEqual(svarToAst(removeConditionAt(nested, 1)), "TZR");
});

test("the tree sees patterns inside a group, and switching one off prunes it", () => {
  assert.equal(activePatterns(nested).has("Drittrecht"), true);
  const left = togglePattern(nested, "Drittrecht");
  assert.deepEqual(svarToAst(left), { op: "AND", items: ["TZR", "Krankenkasse"] });
  const bare = removePattern(left, "Krankenkasse");
  assert.deepEqual(svarToAst(bare), "TZR"); // the emptied group is gone, not left behind
});

test("an empty filter compiles to no filter at all", () => {
  assert.equal(svarToAst(EMPTY_FILTER), null);
  assert.deepEqual(listConditions(EMPTY_FILTER), []);
  assert.deepEqual([...activePatterns(EMPTY_FILTER)], []);
});
