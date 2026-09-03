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
  appendSelection,
  buildSelectionNode,
  formatAst,
  listConditions,
  patternToSvarRule,
  removeConditionAt,
  removePattern,
  selectionAst,
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

// ---------------------------------------------------------------------------
// The picker's whole-selection path: three controls, one group.

test("a selection of one pattern is one plain rule, not a group of one", () => {
  const value = appendSelection(EMPTY_FILTER, ["Payments"], { within: "or", join: "and" });
  assert.deepEqual(value, { glue: "and", rules: [patternToSvarRule("Payments")] });
  assert.equal(svarToAst(value), "Payments");
});

test("the nested filter the picker exists for: TZR AND (Krankenkasse OR Drittrecht)", () => {
  const withTzr = appendSelection(EMPTY_FILTER, ["TZR"], { within: "or", join: "and" });
  const both = appendSelection(withTzr, ["Krankenkasse", "Drittrecht"], { within: "or", join: "and" });
  assert.deepEqual(svarToAst(both), {
    op: "AND",
    items: ["TZR", { op: "OR", items: ["Krankenkasse", "Drittrecht"] }],
  });
  // Exactly the AST the raw path builds by hand — the contract is the grammar.
  assert.deepEqual(svarToAst(both), svarToAst(nested));
});

test("a group joins under the glue that was asked for, and pushes down when they differ", () => {
  const base = build("A", "B"); // an AND of two
  const joinedAnd = appendSelection(base, ["C", "D"], { within: "or", join: "and" });
  assert.deepEqual(svarToAst(joinedAnd), {
    op: "AND",
    items: ["A", "B", { op: "OR", items: ["C", "D"] }],
  });
  const joinedOr = appendSelection(base, ["C", "D"], { within: "or", join: "or" });
  assert.deepEqual(svarToAst(joinedOr), {
    op: "OR",
    items: [{ op: "AND", items: ["A", "B"] }, { op: "OR", items: ["C", "D"] }],
  });
});

test("a single existing condition is re-glued, not wrapped in a pointless level", () => {
  const one = build("A");
  const joined = appendSelection(one, ["B"], { join: "or" });
  assert.deepEqual(joined.rules.length, 2);
  assert.deepEqual(svarToAst(joined), { op: "OR", items: ["A", "B"] });
});

test("within=AND makes the picked tags all required", () => {
  const value = appendSelection(EMPTY_FILTER, ["TZR", "Krankenkasse"], { within: "and", join: "and" });
  assert.deepEqual(svarToAst(value), { op: "AND", items: ["TZR", "Krankenkasse"] });
});

test("exclude wraps each pattern in {not}, and de Morgan flips the glue", () => {
  // "none of these" — the AND of the negations, which is NOT (A OR B).
  const any = appendSelection(EMPTY_FILTER, ["Quarantine-Flaky", "Slow"], { within: "or", exclude: true });
  assert.deepEqual(svarToAst(any), { op: "AND", items: [{ not: "Quarantine-Flaky" }, { not: "Slow" }] });
  // "not both of these" — the OR of the negations, which is NOT (A AND B).
  const all = appendSelection(EMPTY_FILTER, ["Quarantine-Flaky", "Slow"], { within: "and", exclude: true });
  assert.deepEqual(svarToAst(all), { op: "OR", items: [{ not: "Quarantine-Flaky" }, { not: "Slow" }] });
});

test("a single excluded pattern is one negated rule", () => {
  const value = appendSelection(EMPTY_FILTER, ["prefix:Quarantine-"], { exclude: true });
  assert.deepEqual(svarToAst(value), { not: "prefix:Quarantine-" });
  // and it is an ordinary editable condition, listed like any other
  assert.deepEqual(listConditions(value), [
    { index: 0, kind: "condition", operator: "notStartsWith", value: "Quarantine-", pattern: null },
  ]);
});

test("an excluded pattern is not an active pattern — nothing may claim it is selected", () => {
  const value = appendSelection(EMPTY_FILTER, ["Quarantine"], { exclude: true });
  assert.equal(activePatterns(value).has("Quarantine"), false);
});

test("the same pattern picked twice lands once", () => {
  const value = appendSelection(EMPTY_FILTER, ["A", "A", " A ", "B"], { within: "or" });
  assert.deepEqual(svarToAst(value), { op: "OR", items: ["A", "B"] });
});

test("an empty selection changes nothing", () => {
  const base = build("A");
  assert.equal(appendSelection(base, []), base);
  assert.equal(appendSelection(base, ["", "  "]), base);
  assert.equal(buildSelectionNode([]), null);
  assert.equal(selectionAst([]), null);
});

test("the picker can show what it is about to add, before it adds it", () => {
  const words = { and: "UND", or: "ODER", not: "NICHT" };
  assert.equal(
    formatAst(selectionAst(["Krankenkasse", "Drittrecht"], { within: "or" }), words),
    "Krankenkasse ODER Drittrecht"
  );
  assert.equal(
    formatAst(selectionAst(["Quarantine-Flaky", "Slow"], { within: "or", exclude: true }), words),
    "NICHT Quarantine-Flaky UND NICHT Slow"
  );
  assert.equal(formatAst(selectionAst(["prefix:Smoke"], {}), words), "prefix:Smoke");
  assert.equal(
    formatAst({ op: "AND", items: ["TZR", { op: "OR", items: ["A", "B"] }] }, words),
    "TZR UND (A ODER B)"
  );
  assert.equal(formatAst(null, words), "");
});

test("the excluding group can never be the shape that excludes nothing", () => {
  // Measured on a 2652-item corpus: `NOT(Krankenkasse OR Drittrecht)` is 2423
  // items and `NOT Krankenkasse OR NOT Drittrecht` is 2652 — the whole
  // catalog, an exclusion that removes nothing and reports no error. The two
  // differ only in the glue, so this is the one place a sign error is fatal
  // and silent.
  for (const within of ["or", "and"]) {
    const ast = svarToAst(appendSelection(EMPTY_FILTER, ["A", "B"], { within, exclude: true }));
    const negations = ast.items.every((item) => item.not !== undefined);
    assert.equal(negations, true);
    // an OR of negations is the shape that means nothing; it must never appear
    assert.notEqual(ast.op, within === "or" ? "OR" : "AND");
  }
});

test("what the picker offers and what it produces agree, for every combination", () => {
  // The dialog offers within ∈ {or, and} while including and only `or` while
  // excluding; these are the ASTs those four states hand to the compiler.
  const of = (options) => svarToAst(appendSelection(EMPTY_FILTER, ["A", "B"], options));
  assert.deepEqual(of({ within: "or" }), { op: "OR", items: ["A", "B"] });
  assert.deepEqual(of({ within: "and" }), { op: "AND", items: ["A", "B"] });
  assert.deepEqual(of({ within: "or", exclude: true }), {
    op: "AND",
    items: [{ not: "A" }, { not: "B" }],
  });
});

test("an exclusion joined under AND lists each negation as its own condition", () => {
  // The group's own glue is already AND (de Morgan), so it does not need a
  // level of its own — and each negation stays individually removable, which
  // is the affordance the panel's chips offer.
  const base = appendSelection(EMPTY_FILTER, ["Payments", "Cart"], { within: "or" });
  const value = appendSelection(base, ["Quarantine-Flaky", "Slow"], { within: "or", exclude: true, join: "and" });
  assert.deepEqual(svarToAst(value), {
    op: "AND",
    items: [{ op: "OR", items: ["Payments", "Cart"] }, { not: "Quarantine-Flaky" }, { not: "Slow" }],
  });
  assert.deepEqual(
    listConditions(value).map((condition) => condition.kind),
    ["group", "condition", "condition"]
  );
});

test("a group whose glue differs from the join keeps its level", () => {
  const base = appendSelection(EMPTY_FILTER, ["TZR"], {});
  const value = appendSelection(base, ["A", "B"], { within: "or", join: "and" });
  assert.deepEqual(svarToAst(value), { op: "AND", items: ["TZR", { op: "OR", items: ["A", "B"] }] });
});

// ---------------------------------------------------------------------------
// `includes`: the shape the widget's value editor writes once the catalog's
// tags are offered as options and several are ticked. Not an operator — a
// fourth rule shape, and one that used to translate to nothing at all, so a
// ticked list silently removed a condition from the filter.

test("a ticked list of tags is an OR of literals", () => {
  const value = { glue: "and", rules: [{ field: "tag", type: "text", filter: "equal", includes: ["Payments", "Cart"] }] };
  assert.deepEqual(svarToAst(value), { op: "OR", items: ["Payments", "Cart"] });
  assert.deepEqual(listConditions(value), [
    { index: 0, kind: "condition", operator: "isAnyOf", value: "Payments, Cart", pattern: null },
  ]);
});

test("a negated ticked list is the AND of the negations, not the OR", () => {
  // The same trap as the picker's exclude side: an OR of negations excludes
  // only what carries every one of them, which is almost nothing.
  const value = { glue: "and", rules: [{ field: "tag", filter: "notEqual", includes: ["A", "B"] }] };
  assert.deepEqual(svarToAst(value), { op: "AND", items: [{ not: "A" }, { not: "B" }] });
  assert.deepEqual(listConditions(value)[0].operator, "isNoneOf");
});

test("a ticked list of one is that one value", () => {
  assert.equal(svarToAst({ glue: "and", rules: [{ field: "tag", includes: ["Smoke"] }] }), "Smoke");
  assert.deepEqual(svarToAst({ glue: "and", rules: [{ field: "tag", filter: "notEqual", includes: ["Smoke"] }] }), {
    not: "Smoke",
  });
});

test("a ticked list does not make its values switchable patterns", () => {
  // The picker's ✓ follows a single pattern; a list is not one, and claiming
  // otherwise would let a click switch off something it cannot address.
  const value = { glue: "and", rules: [{ field: "tag", includes: ["A", "B"] }] };
  assert.deepEqual([...activePatterns(value)], []);
});

test("an empty ticked list is no condition, not a broken one", () => {
  assert.equal(svarToAst({ glue: "and", rules: [{ field: "tag", includes: [] }] }), null);
  assert.equal(svarToAst({ glue: "and", rules: [{ field: "tag", includes: ["", "  "] }] }), null);
});

test("a ticked list beside other conditions keeps all of them", () => {
  // The regression: this used to drop the list rule and quietly narrow the
  // filter to whatever else was in it.
  const value = {
    glue: "and",
    rules: [
      { field: "tag", filter: "equal", includes: ["Payments", "Cart"] },
      { field: "tag", type: "text", filter: "notEqual", value: "Quarantine-Flaky" },
    ],
  };
  assert.deepEqual(svarToAst(value), {
    op: "AND",
    items: [{ op: "OR", items: ["Payments", "Cart"] }, { not: "Quarantine-Flaky" }],
  });
});
