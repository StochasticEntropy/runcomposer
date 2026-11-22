// Does a pattern of the filter match any tag of the catalog — and if a tag
// was meant and none is spelled so, which tags come close?
//
// The server decides what a filter selects. This is the editor's early word
// on one thing only: a rule whose pattern no tag of the catalog matches
// selects nothing (or, negated, excludes nothing), and that is almost always
// a typing mistake. The matching follows the server's (core/filter.py):
//
// - a plain tag is compared without regard to case;
// - `regex:<e>` is searched for anywhere in a tag, case-sensitive;
// - `prefix:<t>` is `regex:^<t>`.
//
// Where the browser cannot read an expression the server can (the two
// regular-expression dialects differ at the edges), nothing is claimed.

export type PatternKind = "tag" | "prefix" | "regex";

export function patternKind(pattern: string): PatternKind {
  return pattern.startsWith("regex:") ? "regex" : pattern.startsWith("prefix:") ? "prefix" : "tag";
}

function searcher(expression: string): RegExp | null {
  for (const flags of ["u", ""]) {
    try {
      return new RegExp(expression, flags);
    } catch {
      // try the next way to read it
    }
  }
  return null;
}

/** The tags of a catalog, prepared once for many questions. */
export interface TagIndex {
  tags: readonly string[];
  folded: ReadonlySet<string>;
  /** `tags` in lower case, index by index. */
  lower: readonly string[];
}

export function tagIndex(tags: readonly string[]): TagIndex {
  const lower = tags.map((tag) => tag.toLowerCase());
  return { tags, folded: new Set(lower), lower };
}

/** Whether any tag matches the pattern; null when that cannot be told here. */
export function matchesAnyTag(pattern: string, index: TagIndex): boolean | null {
  const kind = patternKind(pattern);
  if (kind === "tag") return index.folded.has(pattern.toLowerCase());
  const body = kind === "regex" ? pattern.slice("regex:".length) : `^${pattern.slice("prefix:".length)}`;
  const expression = searcher(body);
  if (expression === null) return null;
  return index.tags.some((tag) => expression.test(tag));
}

/**
 * The edit distance between two words where swapping two neighbouring
 * letters is one step (`Paymnets` → `Payments`), given up beyond `limit`.
 */
export function editDistance(a: string, b: string, limit: number): number {
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  let twoBack: number[] = [];
  let previous = Array.from({ length: b.length + 1 }, (_, column) => column);
  for (let row = 1; row <= a.length; row += 1) {
    const current = [row];
    let best = row;
    for (let column = 1; column <= b.length; column += 1) {
      const cost = a[row - 1] === b[column - 1] ? 0 : 1;
      let value = Math.min(previous[column]! + 1, current[column - 1]! + 1, previous[column - 1]! + cost);
      if (row > 1 && column > 1 && a[row - 1] === b[column - 2] && a[row - 2] === b[column - 1]) value = Math.min(value, twoBack[column - 2]! + 1);
      current.push(value);
      if (value < best) best = value;
    }
    if (best > limit) return limit + 1;
    twoBack = previous;
    previous = current;
  }
  return previous[b.length]!;
}

/**
 * The tags nearest to a word that is no tag: those a slip of the fingers
 * away first (closest first), then those the word is the beginning of.
 */
export function nearestTags(word: string, index: TagIndex, limit = 3): string[] {
  const needle = word.trim().toLowerCase();
  if (needle === "") return [];
  // A third of the word may be wrong, and at least one letter.
  const allowed = Math.max(1, Math.floor(needle.length / 3));
  const close: { tag: string; distance: number }[] = [];
  const longer: string[] = [];
  for (let position = 0; position < index.tags.length; position += 1) {
    const candidate = index.lower[position]!;
    const distance = editDistance(needle, candidate, allowed);
    if (distance <= allowed) close.push({ tag: index.tags[position]!, distance });
    else if (needle.length >= 3 && candidate.startsWith(needle)) longer.push(index.tags[position]!);
  }
  close.sort((a, b) => a.distance - b.distance || a.tag.localeCompare(b.tag));
  longer.sort((a, b) => a.length - b.length || a.localeCompare(b));
  return [...close.map((entry) => entry.tag), ...longer].slice(0, limit);
}

/** What the editor says about one pattern. */
export interface PatternCheck {
  /** False: no tag of the catalog matches. Null: not known. */
  matches: boolean | null;
  /** For a plain tag nothing matches: the tags that were probably meant. */
  nearest: string[];
}

/**
 * Checks patterns against a catalog, each pattern once: the editor asks for
 * every rule on every render, and a catalog has thousands of tags.
 */
export function patternChecker(tags: readonly string[]): (pattern: string) => PatternCheck {
  const index = tagIndex(tags);
  const known = new Map<string, PatternCheck>();
  return (pattern) => {
    let check = known.get(pattern);
    if (!check) {
      const matches = matchesAnyTag(pattern, index);
      check = { matches, nearest: matches === false && patternKind(pattern) === "tag" ? nearestTags(pattern, index) : [] };
      known.set(pattern, check);
    }
    return check;
  };
}
