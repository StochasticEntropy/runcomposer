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
// The server compiles Python's dialect of regular expressions; the browser
// speaks JavaScript's. An expression is translated where the translation
// means exactly what Python means (`\A`, `\Z`, `.`, `$`, `\w` and its kin
// over Unicode, a literal `{`); where it cannot be — inline flags,
// possessive quantifiers, atomic groups, named groups `(?P<…>)`, comments,
// conditionals — nothing is claimed: a wrong hint is worse than none.

export type PatternKind = "tag" | "prefix" | "regex";

export function patternKind(pattern: string): PatternKind {
  return pattern.startsWith("regex:") ? "regex" : pattern.startsWith("prefix:") ? "prefix" : "tag";
}

// Python's classes over Unicode, written for a JavaScript `u` expression.
const WORD = "\\p{L}\\p{N}_";
const DIGIT = "\\p{Nd}";
const SPACE = "\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const IN_CLASS: Record<string, string> = { w: WORD, d: DIGIT, s: SPACE };
const OUTSIDE: Record<string, string> = {
  w: `[${WORD}]`, W: `[^${WORD}]`, d: DIGIT, D: `\\P{Nd}`, s: `[${SPACE}]`, S: `[^${SPACE}]`,
  b: `(?:(?<=[${WORD}])(?![${WORD}])|(?<![${WORD}])(?=[${WORD}]))`,
  B: `(?:(?<=[${WORD}])(?=[${WORD}])|(?<![${WORD}])(?![${WORD}]))`,
  A: "^", Z: "$",
};
const CONTROL: Record<string, string> = { a: "\\x07", f: "\\f", n: "\\n", r: "\\r", t: "\\t", v: "\\v" };

const codePoint = (char: string) => `\\u{${char.codePointAt(0)!.toString(16)}}`;

/**
 * A Python expression as a JavaScript one (for the `u` flag) that matches
 * exactly the same strings — or null when no such translation is known
 * here.
 */
export function fromPython(expression: string): string | null {
  let out = "";
  let index = 0;
  let inClass = false;
  let classStart = false;
  // Whether the token just written may take a quantifier.
  let quantifiable = false;
  const at = (offset = 0) => expression[index + offset] ?? "";
  while (index < expression.length) {
    const char = at();
    if (char === "\\") {
      const next = at(1);
      if (next === "") return null;
      index += 2;
      if (/[0-9]/.test(next)) {
        if (inClass || next === "0") {
          // An octal escape: `\0`, `\012`.
          let digits = next;
          while (digits.length < 3 && /[0-7]/.test(at())) digits += expression[index++];
          if (!/^[0-7]+$/.test(digits)) return null;
          out += codePoint(String.fromCodePoint(parseInt(digits, 8)));
        } else {
          let digits = next;
          if (/[0-9]/.test(at())) digits += expression[index++];
          out += `\\${digits}`; // a back-reference, read alike
        }
      } else if (next === "x") {
        const hex = expression.slice(index, index + 2);
        if (!/^[0-9a-fA-F]{2}$/.test(hex)) return null;
        out += `\\x${hex}`;
        index += 2;
      } else if (next === "u" || next === "U") {
        const width = next === "u" ? 4 : 8;
        const hex = expression.slice(index, index + width);
        if (!new RegExp(`^[0-9a-fA-F]{${width}}$`).test(hex)) return null;
        out += `\\u{${parseInt(hex, 16).toString(16)}}`;
        index += width;
      } else if (next in CONTROL) {
        out += CONTROL[next];
      } else if (inClass && next in IN_CLASS) {
        out += IN_CLASS[next];
      } else if (!inClass && next in OUTSIDE) {
        out += OUTSIDE[next];
        quantifiable = !"AZbB".includes(next);
        classStart = false;
        continue;
      } else if (/[A-Za-z]/.test(next)) {
        return null; // \N{…}, a negated class inside a set, \z, or not Python's
      } else {
        out += codePoint(next); // an escaped punctuation character is itself
      }
      quantifiable = !inClass;
      classStart = false;
      continue;
    }
    if (inClass) {
      if (char === "]" && !classStart) {
        inClass = false;
        out += "]";
        quantifiable = true;
      } else if (char === "^" && out.endsWith("[") && classStart) {
        out += "^";
        index += 1;
        continue; // a `]` right after `[^` is still a member
      } else if (char === "]" || char === "[") {
        out += codePoint(char);
      } else {
        out += char;
      }
      classStart = false;
      index += 1;
      continue;
    }
    if (char === "[") {
      inClass = true;
      classStart = true;
      out += "[";
      index += 1;
      continue;
    }
    if (char === "(") {
      if (at(1) === "?") {
        const rest = expression.slice(index + 2);
        const kind = ["<=", "<!", ":", "=", "!"].find((opening) => rest.startsWith(opening));
        if (kind === undefined) return null; // flags, (?P…), (?#…), (?>…), (?(…)…)
        out += `(?${kind}`;
        index += 2 + kind.length;
      } else {
        out += "(";
        index += 1;
      }
      quantifiable = false;
      continue;
    }
    if (char === "{") {
      const quantifier = /^\{(\d*)(,?)(\d*)\}/.exec(expression.slice(index));
      // `{,}` and `{,n}` count from zero in Python; only `{}` is literal text.
      if (quantifier && (quantifier[1] !== "" || quantifier[2] === ",")) {
        if (!quantifiable) return null;
        out += `{${quantifier[1] || "0"}${quantifier[2]}${quantifier[3]}}`;
        index += quantifier[0].length;
        if (at() === "+") return null; // possessive
        if (at() === "?") out += expression[index++];
        quantifiable = false;
        continue;
      }
      out += codePoint("{"); // not a quantifier: Python reads it as itself
      quantifiable = true;
      index += 1;
      continue;
    }
    if (char === "*" || char === "+" || char === "?") {
      if (!quantifiable) return null;
      out += char;
      index += 1;
      if (at() === "+") return null; // possessive
      if (at() === "?") out += expression[index++];
      quantifiable = false;
      continue;
    }
    if (char === ".") out += "[^\\n]";
    else if (char === "$") out += "(?=\\n?$)"; // Python's `$` also matches before a final newline
    else if (char === "}" || char === "]") out += codePoint(char); // literal in Python, an error in a `u` expression
    else out += char;
    quantifiable = char !== "^" && char !== "|" && char !== "$";
    if (char === ")") quantifiable = true;
    index += 1;
  }
  return inClass ? null : out;
}

function searcher(expression: string): RegExp | null {
  const translated = fromPython(expression);
  if (translated === null) return null;
  try {
    return new RegExp(translated, "u");
  } catch {
    return null;
  }
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
