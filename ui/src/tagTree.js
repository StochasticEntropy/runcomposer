// Pure tree helpers over the *resolved* taxonomy (`GET /api/v1/taxonomy?resolve=true`).
//
// The endpoint already serves everything the picker needs — id / origin /
// label / filter / tag_count / item_count / children — so nothing here derives
// tree data from a second source. What this module does is the three things a
// picker has to do to a tree it did not build: narrow it, index it, and read a
// selection back out of it, all without touching React or the filter widget.
//
// Node shape (the file's own three keys plus the resolver's metadata):
//   {id, label, filter?, origin, tag_count, item_count, children?}

// A node stands for a pattern the filter can carry, or it is only a heading.
export const isSelectable = (node) => typeof node.filter === "string" && node.filter !== "";

const defaultLabel = (node) => node.label;

// Narrow the tree to what matches, WITHOUT flattening it. Two rules, and the
// second is the whole point: a node that matches keeps all of its children, so
// a hit stays in the context that explains it — a tag under the group it
// belongs to — instead of becoming an orphaned row in a result list. A node
// that does not match survives only as the road to a descendant that does.
//
// Matching reads the label and the pattern, because both are things a user
// searches for: the word they remember, or the tag they were told to use.
export function filterTree(nodes, query, labelOf = defaultLabel) {
  const needle = String(query ?? "").trim().toLowerCase();
  if (!needle) return nodes;
  const walk = (level) => {
    const kept = [];
    for (const node of level) {
      const text = `${labelOf(node)} ${node.filter ?? ""}`.toLowerCase();
      const hit = text.includes(needle);
      const children = walk(node.children ?? []);
      if (!hit && children.length === 0) continue;
      kept.push({ ...node, children: hit ? node.children ?? [] : children });
    }
    return kept;
  };
  return walk(nodes);
}

// id → node, for reading a selection of ids back as patterns.
export function indexTree(nodes, into = new Map()) {
  for (const node of nodes) {
    if (node.id != null) into.set(node.id, node);
    if (node.children?.length) indexTree(node.children, into);
  }
  return into;
}

// Every node that can be opened. Search expands them all, so a match is
// visible rather than hidden behind a twisty the user has to guess at.
export function expandableIds(nodes, into = new Set()) {
  for (const node of nodes) {
    if (node.children?.length) {
      into.add(node.id);
      expandableIds(node.children, into);
    }
  }
  return into;
}

export const topLevelIds = (nodes) => nodes.map((node) => node.id).filter((id) => id != null);

// The patterns a set of selected ids stands for, in **tree order** and without
// duplicates. Tree order, not click order, so the group that lands in the
// filter reads the way the tree reads; deduplicated, because the resolved tree
// hangs the same tag under every node whose pattern covers it and two rows can
// legitimately mean one pattern.
export function selectedPatterns(nodes, selectedIds) {
  const found = [];
  const seen = new Set();
  const walk = (level) => {
    for (const node of level) {
      if (selectedIds.has(node.id) && isSelectable(node) && !seen.has(node.filter)) {
        seen.add(node.filter);
        found.push(node.filter);
      }
      if (node.children?.length) walk(node.children);
    }
  };
  walk(nodes);
  return found;
}

// How many *distinct* patterns of `active` sit strictly below each node, keyed
// by node id. Distinct, because the resolved tree repeats a tag under every
// pattern that covers it and "3 in this branch" should mean three conditions,
// not three rows. Carried over from the sidebar the picker replaces: with the
// branches closed, something already in the filter can be nowhere on screen —
// that was true of the sidebar and is true of the dialog.
export function countActiveInside(nodes, active) {
  const hidden = new Map();
  const walk = (node) => {
    const found = new Set();
    for (const child of node.children ?? []) {
      if (child.filter && active.has(child.filter)) found.add(child.filter);
      for (const pattern of walk(child)) found.add(pattern);
    }
    if (node.id != null && found.size > 0) hidden.set(node.id, found.size);
    return found;
  };
  nodes.forEach(walk);
  return hidden;
}

// How many nodes a tree holds, all levels together — what it would cost to
// open everything at once.
export function countNodes(nodes) {
  let total = 0;
  for (const node of nodes) total += 1 + countNodes(node.children ?? []);
  return total;
}
