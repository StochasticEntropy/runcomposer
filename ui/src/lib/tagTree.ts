// Tree helpers over the *resolved* taxonomy (`GET /taxonomy?resolve=true`):
// narrow it, order it, and flatten the open part of it into rows a
// virtualised list can render. Nothing here touches React.
//
// Node shape: {id, label, filter?, origin, tag_count, item_count, children?}

import { naturalCompare } from "./format.ts";
import type { TaxonomyNode } from "./types.ts";

/** A node stands for a pattern the filter can carry, or it is only a heading. */
export const isSelectable = (node: TaxonomyNode): boolean => typeof node.filter === "string" && node.filter !== "";

// Curated nodes keep the order of the file — somebody chose it. Nodes that
// were generated from the catalog's tags have no chosen order, so they get a
// natural one: Sprint-2 before Sprint-10.
export function orderTree(nodes: TaxonomyNode[]): TaxonomyNode[] {
  const curated = nodes.filter((node) => node.origin !== "tag");
  const generated = nodes.filter((node) => node.origin === "tag").sort((a, b) => naturalCompare(a.label, b.label));
  return [...curated, ...generated].map((node) =>
    node.children?.length ? { ...node, children: orderTree(node.children) } : node,
  );
}

// Narrow the tree to what matches, WITHOUT flattening it. A node that matches
// keeps all of its children, so a hit stays in the context that explains it;
// a node that does not match survives only as the road to a descendant that
// does. Matching reads the label and the pattern — the word a person
// remembers, or the tag they were told to use.
export function filterTree(nodes: TaxonomyNode[], query: string): TaxonomyNode[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return nodes;
  const walk = (level: TaxonomyNode[]): TaxonomyNode[] => {
    const kept: TaxonomyNode[] = [];
    for (const node of level) {
      const hit = `${node.label} ${node.filter ?? ""}`.toLowerCase().includes(needle);
      const children = walk(node.children ?? []);
      if (!hit && children.length === 0) continue;
      kept.push({ ...node, children: hit ? (node.children ?? []) : children });
    }
    return kept;
  };
  return walk(nodes);
}

export function expandableIds(nodes: TaxonomyNode[], into = new Set<string>()): Set<string> {
  for (const node of nodes) {
    if (node.children?.length) {
      into.add(node.id);
      expandableIds(node.children, into);
    }
  }
  return into;
}

export interface TreeRow {
  node: TaxonomyNode;
  level: number;
  /** Position among its siblings, 1-based, and how many siblings there are. */
  position: number;
  setSize: number;
  expandable: boolean;
  expanded: boolean;
  parentId: string | null;
}

/** The rows currently on show: every node whose ancestors are all open. */
export function flattenTree(nodes: TaxonomyNode[], expanded: ReadonlySet<string>): TreeRow[] {
  const rows: TreeRow[] = [];
  const walk = (level: TaxonomyNode[], depth: number, parentId: string | null) => {
    level.forEach((node, index) => {
      const expandable = Boolean(node.children?.length);
      const open = expandable && expanded.has(node.id);
      rows.push({ node, level: depth, position: index + 1, setSize: level.length, expandable, expanded: open, parentId });
      if (open) walk(node.children!, depth + 1, node.id);
    });
  };
  walk(nodes, 1, null);
  return rows;
}

/** pattern → the first node that carries it, so a pattern can be shown by its label. */
export function indexByPattern(nodes: TaxonomyNode[], into = new Map<string, TaxonomyNode>()): Map<string, TaxonomyNode> {
  for (const node of nodes) {
    if (node.filter && !into.has(node.filter)) into.set(node.filter, node);
    if (node.children?.length) indexByPattern(node.children, into);
  }
  return into;
}

/** How many distinct patterns of `active` sit strictly below each node. */
export function countActiveInside(nodes: TaxonomyNode[], active: ReadonlySet<string>): Map<string, number> {
  const hidden = new Map<string, number>();
  const walk = (node: TaxonomyNode): Set<string> => {
    const found = new Set<string>();
    for (const child of node.children ?? []) {
      if (child.filter && active.has(child.filter)) found.add(child.filter);
      for (const pattern of walk(child)) found.add(pattern);
    }
    if (found.size > 0) hidden.set(node.id, found.size);
    return found;
  };
  nodes.forEach(walk);
  return hidden;
}
