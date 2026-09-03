// Node's own test runner, no dependency: `npm test` in ui/.
// The picker's tree logic is pure and lives here, so the behaviour that makes
// a 2452-node tree usable — narrowing without flattening — is pinned down by
// something other than a click-through.

import assert from "node:assert/strict";
import test from "node:test";

import {
  countActiveInside,
  expandableIds,
  filterTree,
  indexTree,
  isSelectable,
  selectedPatterns,
  topLevelIds,
} from "./tagTree.js";

// A miniature of what `?resolve=true` serves: written nodes with children,
// synthesized one-tag nodes, and the synthetic unassigned bucket.
const tree = [
  {
    id: "taxonomy[0]",
    label: "Areas",
    origin: "file",
    children: [
      {
        id: "taxonomy[0].children[0]",
        label: "Checkout",
        filter: "prefix:Checkout-",
        origin: "file",
        item_count: 12,
        children: [
          { id: "taxonomy[0].children[0].tags[0]", label: "Checkout-Cart", filter: "Checkout-Cart", origin: "tag", item_count: 7 },
          { id: "taxonomy[0].children[0].tags[1]", label: "Checkout-Pay", filter: "Checkout-Pay", origin: "tag", item_count: 5 },
        ],
      },
      { id: "taxonomy[0].children[1]", label: "Search", filter: "Search", origin: "file", item_count: 3 },
    ],
  },
  {
    id: "taxonomy[unassigned]",
    label: "Unassigned tags",
    origin: "unassigned",
    children: [{ id: "taxonomy[unassigned].tags[0]", label: "Cart", filter: "Cart", origin: "tag", item_count: 2 }],
  },
];

test("a heading is not selectable; a node carrying a pattern is", () => {
  assert.equal(isSelectable(tree[0]), false);
  assert.equal(isSelectable(tree[0].children[1]), true);
});

test("search keeps a hit inside its parent context, and drops branches with nothing in them", () => {
  const narrowed = filterTree(tree, "search");
  assert.deepEqual(
    narrowed.map((node) => node.label),
    ["Areas"] // the unassigned branch matches nothing and is gone
  );
  // The road to the hit survives; the sibling that does not match does not.
  assert.deepEqual(
    narrowed[0].children.map((node) => node.label),
    ["Search"]
  );
});

test("a node that matches keeps ALL of its children — a hit is not flattened", () => {
  const narrowed = filterTree(tree, "checkout-");
  const checkout = narrowed[0].children[0];
  assert.equal(checkout.label, "Checkout");
  assert.deepEqual(
    checkout.children.map((node) => node.label),
    ["Checkout-Cart", "Checkout-Pay"]
  );
});

test("search reads the pattern too, not only the label", () => {
  // "Checkout" as a *label* is not in the unassigned branch; the pattern is
  // what carries the tag spelling, and a user searches for the tag.
  const narrowed = filterTree(tree, "prefix:");
  assert.deepEqual(narrowed[0].children.map((node) => node.label), ["Checkout"]);
});

test("search can be told how a label is spelled on screen", () => {
  // The synthetic bucket's label is the server's English string; the UI
  // translates it, and a user searches for what they can read.
  const german = (node) => (node.origin === "unassigned" ? "Nicht zugeordnete Tags" : node.label);
  assert.deepEqual(filterTree(tree, "zugeordnete", german).map((node) => node.label), ["Unassigned tags"]);
  assert.deepEqual(filterTree(tree, "zugeordnete").length, 0);
});

test("an empty query is the whole tree, unchanged and uncopied", () => {
  assert.equal(filterTree(tree, "   "), tree);
});

test("no match at all is an empty tree, not a partial one", () => {
  assert.deepEqual(filterTree(tree, "nothing-here"), []);
});

test("the openable nodes are the ones with children", () => {
  assert.deepEqual(
    [...expandableIds(tree)].sort(),
    ["taxonomy[0]", "taxonomy[0].children[0]", "taxonomy[unassigned]"]
  );
  assert.deepEqual(topLevelIds(tree), ["taxonomy[0]", "taxonomy[unassigned]"]);
});

test("every node is reachable by id", () => {
  const index = indexTree(tree);
  assert.equal(index.size, 7);
  assert.equal(index.get("taxonomy[0].children[0].tags[1]").filter, "Checkout-Pay");
});

test("a selection reads back as patterns in tree order, headings ignored", () => {
  // Clicked bottom-up and including a heading, which carries no pattern.
  const clicked = new Set([
    "taxonomy[unassigned].tags[0]",
    "taxonomy[0].children[1]",
    "taxonomy[0]",
    "taxonomy[0].children[0].tags[0]",
  ]);
  assert.deepEqual(selectedPatterns(tree, clicked), ["Checkout-Cart", "Search", "Cart"]);
});

test("one pattern under two nodes is one pattern, not two", () => {
  // The resolved tree hangs a tag under every node whose pattern covers it.
  const twice = [
    { id: "a", label: "A", filter: "Cart", origin: "tag" },
    { id: "b", label: "B", filter: "Cart", origin: "tag" },
  ];
  assert.deepEqual(selectedPatterns(twice, new Set(["a", "b"])), ["Cart"]);
});

test("a closed branch says how many distinct filter patterns hide below it", () => {
  const active = new Set(["Checkout-Cart", "Checkout-Pay", "Search"]);
  const inside = countActiveInside(tree, active);
  assert.equal(inside.get("taxonomy[0]"), 3);
  assert.equal(inside.get("taxonomy[0].children[0]"), 2);
  assert.equal(inside.has("taxonomy[unassigned]"), false);
});
