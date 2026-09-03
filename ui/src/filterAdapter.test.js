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

test("the nested filter the picker exists for: Regression AND (Checkout OR Cart)", () => {
  const outer = appendSelection(EMPTY_FILTER, ["Regression"], { within: "or", join: "and" });
  const both = appendSelection(outer, ["Checkout", "Cart"], { within: "or", join: "and" });
  assert.deepEqual(svarToAst(both), {
    op: "AND",
    items: ["Regression", { op: "OR", items: ["Checkout", "Cart"] }],
  });
  // Exactly the AST the raw path builds by hand — the contract is the grammar.
  assert.deepEqual(svarToAst(both), svarToAst(nested));
});

test("a group joins under the glue that was asked for, and pushes down when they differ", () => {
  const base = build("Payments", "Auth"); // an AND of two
  const joinedAnd = appendSelection(base, ["Checkout", "Cart"], { within: "or", join: "and" });
  assert.deepEqual(svarToAst(joinedAnd), {
    op: "AND",
    items: ["Payments", "Auth", { op: "OR", items: ["Checkout", "Cart"] }],
  });
  const joinedOr = appendSelection(base, ["Checkout", "Cart"], { within: "or", join: "or" });
  assert.deepEqual(svarToAst(joinedOr), {
    op: "OR",
    items: [{ op: "AND", items: ["Payments", "Auth"] }, { op: "OR", items: ["Checkout", "Cart"] }],
  });
});

test("a single existing condition is re-glued, not wrapped in a pointless level", () => {
  const one = build("Payments");
  const joined = appendSelection(one, ["Auth"], { join: "or" });
  assert.deepEqual(joined.rules.length, 2);
  assert.deepEqual(svarToAst(joined), { op: "OR", items: ["Payments", "Auth"] });
});

test("within=AND makes the picked tags all required", () => {
  const value = appendSelection(EMPTY_FILTER, ["Regression", "Checkout"], { within: "and", join: "and" });
  assert.deepEqual(svarToAst(value), { op: "AND", items: ["Regression", "Checkout"] });
});

test("exclude wraps each pattern in {not}, and de Morgan flips the glue", () => {
  // "none of these" — the AND of the negations, which is NOT (A OR B).
  const any = appendSelection(EMPTY_FILTER, ["Payments", "Cart"], { within: "or", exclude: true });
  assert.deepEqual(svarToAst(any), { op: "AND", items: [{ not: "Payments" }, { not: "Cart" }] });
  // "not both of these" — the OR of the negations, which is NOT (A AND B).
  const all = appendSelection(EMPTY_FILTER, ["Payments", "Cart"], { within: "and", exclude: true });
  assert.deepEqual(svarToAst(all), { op: "OR", items: [{ not: "Payments" }, { not: "Cart" }] });
});

test("a single excluded pattern is one negated rule", () => {
  const value = appendSelection(EMPTY_FILTER, ["prefix:Checkout"], { exclude: true });
  assert.deepEqual(svarToAst(value), { not: "prefix:Checkout" });
  // and it is an ordinary editable condition, listed like any other
  assert.deepEqual(listConditions(value), [
    { index: 0, kind: "condition", operator: "notStartsWith", value: "Checkout", pattern: null },
  ]);
});

test("an excluded pattern is not an active pattern — nothing may claim it is selected", () => {
  const value = appendSelection(EMPTY_FILTER, ["Payments"], { exclude: true });
  assert.equal(activePatterns(value).has("Payments"), false);
});

test("the same pattern picked twice lands once", () => {
  const value = appendSelection(EMPTY_FILTER, ["Payments", "Payments", " Payments ", "Cart"], {
    within: "or",
  });
  assert.deepEqual(svarToAst(value), { op: "OR", items: ["Payments", "Cart"] });
});

test("an empty selection changes nothing", () => {
  const base = build("Payments");
  assert.equal(appendSelection(base, []), base);
  assert.equal(appendSelection(base, ["", "  "]), base);
  assert.equal(buildSelectionNode([]), null);
  assert.equal(selectionAst([]), null);
});

// The operator words are lowercase here only because the guard in
// tests/test_self_containment.py reads quoted words in this file as candidate
// tag names; the UI passes the translated, capitalised ones.
const words = { and: "and", or: "or", not: "not" };

test("the picker can show what it is about to add, before it adds it", () => {
  assert.equal(
    formatAst(selectionAst(["Checkout", "Cart"], { within: "or" }), words),
    "Checkout or Cart"
  );
  assert.equal(
    formatAst(selectionAst(["Payments", "Cart"], { within: "or", exclude: true }), words),
    "not Payments and not Cart"
  );
  assert.equal(formatAst(selectionAst(["prefix:Smoke"], {}), words), "prefix:Smoke");
  assert.equal(
    formatAst({ op: "AND", items: ["Regression", { op: "OR", items: ["Checkout", "Cart"] }] }, words),
    "Regression and (Checkout or Cart)"
  );
  assert.equal(formatAst(null, words), "");
});

test("the excluding group can never be the shape that excludes nothing", () => {
  // An OR of negations is only false when every one of them holds, so it keeps
  // everything except the items carrying all the tags at once — an exclusion
  // that excludes nothing and reports no error. The correct shape and this one
  // differ only in the glue, which is why it is worth a test of its own.
  // tests/test_taxonomy_resolution.py pins the item counts on the demo corpus.
  for (const within of ["or", "and"]) {
    const ast = svarToAst(
      appendSelection(EMPTY_FILTER, ["Payments", "Cart"], { within, exclude: true })
    );
    assert.equal(
      ast.items.every((item) => item.not !== undefined),
      true
    );
    assert.notEqual(ast.op, within === "or" ? "OR" : "AND");
  }
});

test("what the picker offers and what it produces agree, for every combination", () => {
  // The dialog offers within ∈ {or, and} while including and only `or` while
  // excluding; these are the ASTs those states hand to the compiler.
  const of = (options) =>
    svarToAst(appendSelection(EMPTY_FILTER, ["Payments", "Cart"], options));
  assert.deepEqual(of({ within: "or" }), { op: "OR", items: ["Payments", "Cart"] });
  assert.deepEqual(of({ within: "and" }), { op: "AND", items: ["Payments", "Cart"] });
  assert.deepEqual(of({ within: "or", exclude: true }), {
    op: "AND",
    items: [{ not: "Payments" }, { not: "Cart" }],
  });
});

test("an exclusion joined under AND lists each negation as its own condition", () => {
  // The group's own glue is already AND (de Morgan), so it does not need a
  // level of its own — and each negation stays individually removable, which
  // is the affordance the panel's chips offer.
  const base = appendSelection(EMPTY_FILTER, ["Checkout", "Cart"], { within: "or" });
  const value = appendSelection(base, ["Quarantine-Flaky", "Quarantine-Blocked"], {
    within: "or",
    exclude: true,
    join: "and",
  });
  assert.deepEqual(svarToAst(value), {
    op: "AND",
    items: [
      { op: "OR", items: ["Checkout", "Cart"] },
      { not: "Quarantine-Flaky" },
      { not: "Quarantine-Blocked" },
    ],
  });
  assert.deepEqual(
    listConditions(value).map((condition) => condition.kind),
    ["group", "condition", "condition"]
  );
});

test("a group whose glue differs from the join keeps its level", () => {
  const base = appendSelection(EMPTY_FILTER, ["Regression"], {});
  const value = appendSelection(base, ["Checkout", "Cart"], { within: "or", join: "and" });
  assert.deepEqual(svarToAst(value), {
    op: "AND",
    items: ["Regression", { op: "OR", items: ["Checkout", "Cart"] }],
  });
});

// ---------------------------------------------------------------------------
// `includes`: the shape the widget's value editor writes once the catalog's
// tags are offered as options and several are ticked. Not an operator — a
// fourth rule shape, and one that used to translate to nothing at all, so a
// ticked list silently removed a condition from the filter.

test("a ticked list of tags is an OR of literals", () => {
  const value = {
    glue: "and",
    rules: [{ field: "tag", type: "text", filter: "equal", includes: ["Checkout", "Cart"] }],
  };
  assert.deepEqual(svarToAst(value), { op: "OR", items: ["Checkout", "Cart"] });
  assert.deepEqual(listConditions(value), [
    { index: 0, kind: "condition", operator: "isAnyOf", value: "Checkout, Cart", pattern: null },
  ]);
});

test("a negated ticked list is the AND of the negations, not the OR", () => {
  // The same trap as the picker's exclude side: an OR of negations excludes
  // only what carries every one of them, which is almost nothing.
  const value = {
    glue: "and",
    rules: [{ field: "tag", filter: "notEqual", includes: ["Payments", "Cart"] }],
  };
  assert.deepEqual(svarToAst(value), { op: "AND", items: [{ not: "Payments" }, { not: "Cart" }] });
  assert.deepEqual(listConditions(value)[0].operator, "isNoneOf");
});

test("a ticked list of one is that one value", () => {
  assert.equal(svarToAst({ glue: "and", rules: [{ field: "tag", includes: ["Smoke"] }] }), "Smoke");
  assert.deepEqual(
    svarToAst({ glue: "and", rules: [{ field: "tag", filter: "notEqual", includes: ["Smoke"] }] }),
    { not: "Smoke" }
  );
});

test("a ticked list does not make its values switchable patterns", () => {
  // The picker's ✓ follows a single pattern; a list is not one, and claiming
  // otherwise would let a click switch off something it cannot address.
  const value = { glue: "and", rules: [{ field: "tag", includes: ["Payments", "Cart"] }] };
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
      { field: "tag", type: "text", filter: "equal", includes: ["Checkout", "Cart"] },
      { field: "tag", type: "text", filter: "notEqual", value: "Quarantine-Flaky" },
    ],
  };
  assert.deepEqual(svarToAst(value), {
    op: "AND",
    items: [{ op: "OR", items: ["Checkout", "Cart"] }, { not: "Quarantine-Flaky" }],
  });
});
