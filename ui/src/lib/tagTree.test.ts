import { describe, expect, it } from "vitest";

import { countActiveInside, expandableIds, filterTree, flattenTree, indexByPattern, isSelectable, orderTree } from "./tagTree.ts";
import type { TaxonomyNode } from "./types.ts";

const node = (id: string, label: string, extra: Partial<TaxonomyNode> = {}): TaxonomyNode => ({
  id,
  label,
  origin: "file",
  tag_count: 1,
  item_count: 1,
  ...extra,
});
const tagNode = (id: string, name: string) => node(id, name, { filter: name, origin: "tag" });

const TREE: TaxonomyNode[] = [
  node("a", "Areas", {
    children: [
      node("a.0", "Payments", { filter: "Payments" }),
      node("a.1", "Cart", { filter: "regex:^Cart(V2)?$", children: [tagNode("a.1.t0", "Cart"), tagNode("a.1.t1", "CartV2")] }),
    ],
  }),
  node("s", "Sprints", {
    filter: "prefix:Sprint-",
    children: [tagNode("s.t0", "Sprint-12"), tagNode("s.t1", "Sprint-2"), tagNode("s.t2", "Sprint-13")],
  }),
];

describe("orderTree", () => {
  it("keeps curated order and sorts generated tag nodes naturally", () => {
    const ordered = orderTree(TREE);
    expect(ordered.map((entry) => entry.label)).toEqual(["Areas", "Sprints"]);
    expect(ordered[1]!.children!.map((entry) => entry.label)).toEqual(["Sprint-2", "Sprint-12", "Sprint-13"]);
    expect(ordered[0]!.children!.map((entry) => entry.label)).toEqual(["Payments", "Cart"]);
  });
});

describe("filterTree", () => {
  it("keeps a hit in its context and a miss only as the road to a hit", () => {
    const hit = filterTree(TREE, "cartv2");
    expect(hit.map((entry) => entry.label)).toEqual(["Areas"]);
    expect(hit[0]!.children!.map((entry) => entry.label)).toEqual(["Cart"]);
    expect(hit[0]!.children![0]!.children!.map((entry) => entry.label)).toEqual(["CartV2"]);
  });

  it("matches the pattern as well as the label, and keeps a matching node's children", () => {
    const hit = filterTree(TREE, "sprint-");
    expect(hit.map((entry) => entry.label)).toEqual(["Sprints"]);
    expect(hit[0]!.children).toHaveLength(3);
    expect(filterTree(TREE, "  ")).toBe(TREE);
    expect(filterTree(TREE, "no such thing")).toEqual([]);
  });
});

describe("flattenTree", () => {
  it("lists only what is open, with level and position", () => {
    expect(flattenTree(TREE, new Set()).map((row) => row.node.label)).toEqual(["Areas", "Sprints"]);
    const rows = flattenTree(TREE, new Set(["a", "a.1"]));
    expect(rows.map((row) => `${row.level}:${row.node.label}`)).toEqual(["1:Areas", "2:Payments", "2:Cart", "3:Cart", "3:CartV2", "1:Sprints"]);
    expect(rows[2]).toMatchObject({ position: 2, setSize: 2, expandable: true, expanded: true, parentId: "a" });
    expect(rows[5]).toMatchObject({ expandable: true, expanded: false, parentId: null });
  });

  it("everything expandable can be opened at once", () => {
    expect([...expandableIds(TREE)].sort()).toEqual(["a", "a.1", "s"]);
  });
});

describe("reading the tree", () => {
  it("tells headings from nodes that carry a pattern", () => {
    expect(isSelectable(TREE[0]!)).toBe(false);
    expect(isSelectable(TREE[1]!)).toBe(true);
  });

  it("finds the node for a pattern and counts ticks hidden in a closed branch", () => {
    expect(indexByPattern(TREE).get("regex:^Cart(V2)?$")?.label).toBe("Cart");
    const inside = countActiveInside(TREE, new Set(["CartV2", "Payments", "Sprint-12"]));
    expect(inside.get("a")).toBe(2);
    expect(inside.get("a.1")).toBe(1);
    expect(inside.get("s")).toBe(1);
  });
});
