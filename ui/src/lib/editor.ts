// The expression editor's model: the filter AST with identity.
//
// The AST (filter.ts) is a value — two equal patterns are indistinguishable,
// and `{not: x}` is a wrapper rather than a property. An editor needs the
// opposite: every rule has an id a component can key on and an action can
// address, and "negated" is a switch on the rule. This module is that view
// and the pure operations on it; `toAst` is the way back, and the only thing
// the rest of the app sends anywhere.

import { isGroup, isNot } from "./filter.ts";
import type { FilterAst } from "./types.ts";

export interface TagNode {
  kind: "tag";
  id: string;
  pattern: string;
  negated: boolean;
}

export interface GroupNode {
  kind: "group";
  id: string;
  op: "AND" | "OR";
  negated: boolean;
  children: EditorNode[];
}

export type EditorNode = TagNode | GroupNode;

export const ROOT_ID = "root";

let counter = 0;
const nextId = () => `n${++counter}`;

export const tag = (pattern: string, negated = false): TagNode => ({ kind: "tag", id: nextId(), pattern, negated });
export const group = (op: "AND" | "OR", children: EditorNode[] = [], negated = false): GroupNode => ({
  kind: "group",
  id: nextId(),
  op,
  negated,
  children,
});

export const emptyRoot = (): GroupNode => ({ kind: "group", id: ROOT_ID, op: "AND", negated: false, children: [] });

function convert(ast: FilterAst): EditorNode {
  if (typeof ast === "string") return tag(ast);
  if (isGroup(ast)) return group(ast.op, ast.items.map(convert));
  if (isNot(ast)) {
    const inner = convert(ast.not);
    if (!inner.negated) return { ...inner, negated: true };
    // NOT NOT x: the switch is already on, so the outer NOT gets a group of its own.
    return group("AND", [inner], true);
  }
  throw new TypeError("not a filter node");
}

/** One AST node as an editor node with ids of its own — to add a whole expression as a rule. */
export const nodeFromAst = (ast: FilterAst): EditorNode => convert(ast);

/** AST → editor tree. The root is always a group, so there is always somewhere to add to. */
export function fromAst(ast: FilterAst | null): GroupNode {
  if (ast == null) return emptyRoot();
  const node = convert(ast);
  if (node.kind === "group") return { ...node, id: ROOT_ID };
  return { ...emptyRoot(), children: [node] };
}

export interface Emitted {
  ast: FilterAst | null;
  /** AST path (`items[1].not`, `""` for the root) → id of the editor node it came from. */
  paths: Map<string, string>;
}

/** Editor tree → AST. Empty groups and blank patterns contribute nothing; a
 * group of one is that one — the server never sees scaffolding. */
export function toAstWithPaths(root: GroupNode): Emitted {
  const paths = new Map<string, string>();
  const emit = (node: EditorNode): { build: (path: string) => FilterAst } | null => {
    if (node.kind === "tag") {
      const pattern = node.pattern.trim();
      if (!pattern) return null;
      return {
        build: (path) => {
          paths.set(path, node.id);
          if (!node.negated) return pattern;
          paths.set(join(path, "not"), node.id);
          return { not: pattern };
        },
      };
    }
    const children = node.children.map(emit).filter((child) => child !== null);
    if (children.length === 0) return null;
    return {
      build: (path) => {
        paths.set(path, node.id);
        const innerPath = node.negated ? join(path, "not") : path;
        if (node.negated) paths.set(innerPath, node.id);
        const inner: FilterAst =
          children.length === 1
            ? children[0]!.build(innerPath)
            : { op: node.op, items: children.map((child, index) => child.build(join(innerPath, `items[${index}]`))) };
        return node.negated ? { not: inner } : inner;
      },
    };
  };
  const emitted = emit(root);
  return { ast: emitted ? emitted.build("") : null, paths };
}

const join = (path: string, segment: string) => (path ? `${path}.${segment}` : segment);

export const toAst = (root: GroupNode): FilterAst | null => toAstWithPaths(root).ast;

// -- reading --------------------------------------------------------------------

export function findNode(root: GroupNode, id: string): EditorNode | null {
  if (root.id === id) return root;
  for (const child of root.children) {
    if (child.id === id) return child;
    if (child.kind === "group") {
      const found = findNode(child, id);
      if (found) return found;
    }
  }
  return null;
}

// Whether a tag ends up included or excluded is decided by every NOT above
// it, not only by its own switch: a tag inside a negated group is excluded.
function walkTags(node: GroupNode, visit: (tag: TagNode, excluded: boolean) => void, excluded = node.negated): void {
  for (const child of node.children) {
    if (child.kind === "group") walkTags(child, visit, excluded !== child.negated);
    else if (child.pattern.trim()) visit(child, excluded !== child.negated);
  }
}

/** Patterns the filter *includes* — under an even number of NOTs. */
export function positivePatterns(root: GroupNode): Set<string> {
  const found = new Set<string>();
  walkTags(root, (node, excluded) => void (!excluded && found.add(node.pattern)));
  return found;
}

/** Patterns the filter *excludes* — under an odd number of NOTs. */
export function negatedPatterns(root: GroupNode): Set<string> {
  const found = new Set<string>();
  walkTags(root, (node, excluded) => void (excluded && found.add(node.pattern)));
  return found;
}

export const isEmpty = (root: GroupNode): boolean => toAst(root) === null;

// -- operations (each returns a new tree; the input is never touched) ------------

function mapGroups(node: GroupNode, change: (group: GroupNode) => GroupNode): GroupNode {
  const mapped = change(node);
  return {
    ...mapped,
    children: mapped.children.map((child) => (child.kind === "group" ? mapGroups(child, change) : child)),
  };
}

function mapNode(root: GroupNode, id: string, change: (node: EditorNode) => EditorNode): GroupNode {
  if (root.id === id) return change(root) as GroupNode;
  return mapGroups(root, (current) => ({
    ...current,
    children: current.children.map((child) => (child.id === id ? change(child) : child)),
  }));
}

const inGroup = (root: GroupNode, groupId: string, change: (group: GroupNode) => GroupNode): GroupNode =>
  mapNode(root, groupId, (node) => (node.kind === "group" ? change(node) : node));

export const addNode = (root: GroupNode, groupId: string, node: EditorNode): GroupNode =>
  inGroup(root, groupId, (current) => ({ ...current, children: [...current.children, node] }));

export function addTag(root: GroupNode, groupId: string, pattern: string, negated = false): GroupNode {
  const clean = pattern.trim();
  if (!clean) return root;
  return addNode(root, groupId, tag(clean, negated));
}

export const addGroup = (root: GroupNode, groupId: string, op: "AND" | "OR"): { root: GroupNode; id: string } => {
  const created = group(op);
  return { root: addNode(root, groupId, created), id: created.id };
};

export function removeNode(root: GroupNode, id: string): GroupNode {
  if (id === ROOT_ID) return emptyRoot();
  return mapGroups(root, (current) => ({ ...current, children: current.children.filter((child) => child.id !== id) }));
}

export const toggleNot = (root: GroupNode, id: string): GroupNode =>
  mapNode(root, id, (node) => ({ ...node, negated: !node.negated }));

export const setOp = (root: GroupNode, groupId: string, op: "AND" | "OR"): GroupNode =>
  inGroup(root, groupId, (current) => ({ ...current, op }));

export const setPattern = (root: GroupNode, id: string, pattern: string): GroupNode =>
  mapNode(root, id, (node) => (node.kind === "tag" ? { ...node, pattern } : node));

/** Dissolve a group into its parent: its rules move up one level. A negated
 * group cannot be dissolved without changing what the filter means. */
export function ungroup(root: GroupNode, id: string): GroupNode {
  if (id === ROOT_ID) return root;
  return mapGroups(root, (current) => ({
    ...current,
    children: current.children.flatMap((child) =>
      child.id === id && child.kind === "group" && !child.negated ? child.children : [child],
    ),
  }));
}

function pruneEmptyGroups(node: GroupNode): GroupNode {
  const children = node.children
    .map((child) => (child.kind === "group" ? pruneEmptyGroups(child) : child))
    .filter((child) => child.kind === "tag" || child.children.length > 0);
  return { ...node, children };
}

/** Take the *included* occurrences of these patterns out, wherever they are. */
export function removePatterns(root: GroupNode, patterns: Iterable<string>): GroupNode {
  const gone = new Set(patterns);
  if (gone.size === 0) return root;
  const strip = (node: GroupNode, excluded: boolean): GroupNode => ({
    ...node,
    children: node.children
      .filter((child) => !(child.kind === "tag" && excluded === child.negated && gone.has(child.pattern)))
      .map((child) => (child.kind === "group" ? strip(child, excluded !== child.negated) : child)),
  });
  return pruneEmptyGroups(strip(root, root.negated));
}

export type Combine = "any" | "all" | "none";

/** Add several picked patterns as ONE condition of the target group:
 *  any  → (a OR b)        all → a AND b        none → NOT (a OR b)
 * Where the target group already has the wanted operator, the patterns join
 * it directly instead of nesting a group that would say the same thing. */
export function addPatterns(root: GroupNode, groupId: string, patterns: string[], combine: Combine): GroupNode {
  const unique = [...new Set(patterns.map((pattern) => pattern.trim()).filter(Boolean))];
  if (unique.length === 0) return root;
  const target = findNode(root, groupId);
  if (!target || target.kind !== "group") return root;
  if (unique.length === 1) return addNode(root, groupId, tag(unique[0]!, combine === "none"));

  const wanted = combine === "all" ? "AND" : "OR";
  if (combine === "none") {
    if (target.op === "AND") {
      // NOT (a OR b) == NOT a AND NOT b: stays flat in an AND group.
      return inGroup(root, groupId, (current) => ({
        ...current,
        children: [...current.children, ...unique.map((pattern) => tag(pattern, true))],
      }));
    }
    return addNode(root, groupId, group("OR", unique.map((pattern) => tag(pattern)), true));
  }
  if (target.op === wanted) {
    return inGroup(root, groupId, (current) => ({
      ...current,
      children: [...current.children, ...unique.map((pattern) => tag(pattern))],
    }));
  }
  return addNode(root, groupId, group(wanted, unique.map((pattern) => tag(pattern))));
}
