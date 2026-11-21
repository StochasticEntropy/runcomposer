// The tag-filter AST of the run spec (DESIGN.md §3.1) and its one-line text
// form. The AST is what the server compiles; the text form is what a person
// reads, types and shares in a URL:
//
//     Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-
//
// Grammar of the text form — one grammar, the run spec's (DESIGN.md §3.1),
// read alike by the server (`runcomposer.core.filtertext`) and here; the
// cases in tests/fixtures/filter-text.json hold both readers to it:
//
//     expr    := and ("OR" and)*
//     and     := unary ("AND" unary)*
//     unary   := "NOT" unary | "(" expr ")" | ("AND" | "OR") "(" expr ")" | "*" | pattern
//     pattern := bare | "quoted"
//
// AND binds tighter than OR. A pattern is quoted when it contains a space, a
// parenthesis, a quote or a backslash, or would read as an operator or as
// `*` — `"regex:^Cart(V2)?$"`. Operators are upper case only, so a tag
// called `and` needs no quotes. `AND (x)` / `OR (x)` is a group of exactly
// one item, the one shape infix notation cannot spell otherwise.
//
// `*` stands for every item of the catalog. The run spec's grammar has no
// node that says "everything" (an empty AND is refused, and the closed
// runspec/1.0 schema takes no new node shape), so on the wire it is the one
// expression that is true for every item — NOT of a pattern no tag can
// match. That encoding is a detail of the wire: the text form, the URL and
// the editor say `*`, and read `*`; the server's text form does the same.

import type { FilterAst } from "./types.ts";

export type { FilterAst };

/** A filter that matches every item of the catalog: no tag is the empty string. */
export const MATCH_ALL: FilterAst = { not: "regex:^$" };
/** How "everything" is written in the text form. */
export const EVERYTHING = "*";
const NO_TAG = "regex:^$";

/** This node is the wire encoding of "everything". */
export const isEverything = (node: FilterAst | null): boolean =>
  node !== null && typeof node === "object" && "not" in node && node.not === NO_TAG;

export const isGroup = (node: FilterAst): node is { op: "AND" | "OR"; items: FilterAst[] } =>
  typeof node === "object" && node !== null && "op" in node;
export const isNot = (node: FilterAst): node is { not: FilterAst } =>
  typeof node === "object" && node !== null && "not" in node;

const OPERATORS = new Set(["AND", "OR", "NOT"]);
const NEEDS_QUOTES = /[\s()"\\]/;

export function quotePattern(pattern: string): string {
  if (pattern !== "" && pattern !== EVERYTHING && !NEEDS_QUOTES.test(pattern) && !OPERATORS.has(pattern)) return pattern;
  return `"${pattern.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/** AST → text: the canonical spelling, the server's own (`format_filter`). */
export function formatFilter(node: FilterAst | null): string {
  if (node == null) return "";
  if (typeof node === "string") return quotePattern(node);
  if (isEverything(node)) return EVERYTHING;
  if (isNot(node)) return `NOT ${operand(node.not)}`;
  // A group of one item has no infix spelling: its operator applied to it.
  if (node.items.length === 1) return `${node.op} (${formatFilter(node.items[0]!)})`;
  return node.items.map(operand).join(` ${node.op} `);
}

/** A node as an operand of NOT, AND or OR: a group of several is parenthesized. */
const operand = (node: FilterAst): string => (isGroup(node) && node.items.length > 1 ? `(${formatFilter(node)})` : formatFilter(node));

export type ParseProblem =
  | "empty"
  | "unexpected-end"
  | "unexpected-operator"
  | "missing-operator"
  | "unbalanced-open"
  | "unbalanced-close"
  | "unterminated-quote"
  | "needs-quotes";

// Positions. The text form counts where reading stopped in Unicode code
// points — the unit of the server's `filter-syntax` errors and of the shared
// conformance cases, and what a person counts as characters. A JavaScript
// string, a text field's selection and this reader index UTF-16 code units,
// which differ after an emoji or any other character that takes two
// UTF-16 units. These two functions are the one place that converts.

/** The code-point position of a UTF-16 offset into `text`. */
export function codePointPosition(text: string, utf16Offset: number): number {
  let position = 0;
  for (let index = 0; index < utf16Offset && index < text.length; index += 1) {
    const unit = text.charCodeAt(index);
    // A surrogate pair is one code point: count it at its first half.
    if (unit >= 0xdc00 && unit <= 0xdfff && index > 0 && isHighSurrogate(text.charCodeAt(index - 1))) continue;
    position += 1;
  }
  return position;
}

/** The UTF-16 offset of a code-point position in `text` — to place the caret where the server says. */
export function utf16Offset(text: string, codePoints: number): number {
  let offset = 0;
  for (let counted = 0; counted < codePoints && offset < text.length; counted += 1) {
    offset += isHighSurrogate(text.charCodeAt(offset)) && offset + 1 < text.length ? 2 : 1;
  }
  return offset;
}

const isHighSurrogate = (unit: number) => unit >= 0xd800 && unit <= 0xdbff;

export class FilterParseError extends Error {
  readonly problem: ParseProblem;
  /** Where reading stopped, in code points from 0 — as the server and the shared cases count. */
  readonly position: number;
  /** The same place as a UTF-16 offset into the text — for a text field's caret. */
  readonly offset: number;
  readonly token: string;

  constructor(problem: ParseProblem, text: string, offset: number, token = "") {
    const position = codePointPosition(text, offset);
    super(`${problem} at ${position}`);
    this.name = "FilterParseError";
    this.problem = problem;
    this.position = position;
    this.offset = offset;
    this.token = token;
  }
}

type Token =
  | { type: "op"; value: "AND" | "OR" | "NOT"; at: number }
  | { type: "open" | "close" | "all"; at: number }
  | { type: "pattern"; value: string; at: number };

function tokenize(text: string): Token[] {
  const tokens: Token[] = [];
  let index = 0;
  while (index < text.length) {
    const char = text[index]!;
    if (/\s/.test(char)) {
      index += 1;
    } else if (char === "(") {
      tokens.push({ type: "open", at: index++ });
    } else if (char === ")") {
      tokens.push({ type: "close", at: index++ });
    } else if (char === '"') {
      const start = index++;
      let value = "";
      let closed = false;
      while (index < text.length) {
        const next = text[index]!;
        if (next === "\\" && index + 1 < text.length) {
          value += text[index + 1];
          index += 2;
        } else if (next === '"') {
          closed = true;
          index += 1;
          break;
        } else {
          value += next;
          index += 1;
        }
      }
      if (!closed) throw new FilterParseError("unterminated-quote", text, start);
      tokens.push({ type: "pattern", value, at: start });
    } else {
      const start = index;
      while (index < text.length && !/[\s()"]/.test(text[index]!)) index += 1;
      const word = text.slice(start, index);
      // `regex:(a|b)` typed bare: the pattern runs into a parenthesis or a quote.
      if (/^(regex|prefix):/.test(word) && index < text.length && /[("]/.test(text[index]!)) {
        throw new FilterParseError("needs-quotes", text, start, word);
      }
      if (word === EVERYTHING) tokens.push({ type: "all", at: start });
      else if (OPERATORS.has(word)) tokens.push({ type: "op", value: word as "AND" | "OR" | "NOT", at: start });
      else tokens.push({ type: "pattern", value: word, at: start });
    }
  }
  return tokens;
}

/** Text → AST. An empty text is no filter (`null`). Throws `FilterParseError`. */
export function parseFilter(text: string): FilterAst | null {
  const tokens = tokenize(text);
  if (tokens.length === 0) return null;
  let cursor = 0;
  const peek = () => tokens[cursor];
  const fail = (problem: ParseProblem, at: number, token = "") => new FilterParseError(problem, text, at, token);

  const group = (opening: Token): FilterAst => {
    const inner = or();
    const closing = peek();
    if (!closing || closing.type !== "close") throw fail("unbalanced-open", opening.at);
    cursor += 1;
    return inner;
  };

  const unary = (): FilterAst => {
    const token = peek();
    if (!token) throw fail("unexpected-end", text.length);
    cursor += 1;
    if (token.type === "all") return { not: NO_TAG };
    if (token.type === "pattern") {
      if (token.value === "") throw fail("empty", token.at);
      return token.value;
    }
    if (token.type === "op") {
      if (token.value === "NOT") return { not: unary() };
      const following = peek();
      if (following?.type === "open") {
        // `AND (x)`: a group of one item.
        cursor += 1;
        return { op: token.value, items: [group(following)] };
      }
      throw fail("unexpected-operator", token.at, token.value);
    }
    if (token.type === "open") return group(token);
    throw fail("unbalanced-close", token.at);
  };

  const chain = (op: "AND" | "OR", operand: () => FilterAst): FilterAst => {
    const items = [operand()];
    for (let token = peek(); token?.type === "op" && token.value === op; token = peek()) {
      cursor += 1;
      items.push(operand());
    }
    return items.length === 1 ? items[0]! : { op, items };
  };
  const and = () => chain("AND", unary);
  const or = (): FilterAst => chain("OR", and);

  const ast = or();
  const rest = peek();
  if (rest) {
    if (rest.type === "close") throw fail("unbalanced-close", rest.at);
    throw fail("missing-operator", rest.at, rest.type === "pattern" || rest.type === "op" ? rest.value : rest.type === "all" ? EVERYTHING : "(");
  }
  return ast;
}

/** Every leaf pattern with whether it sits under an odd number of NOTs. */
export function leaves(node: FilterAst | null, negated = false, into: { pattern: string; negated: boolean }[] = []) {
  if (node == null) return into;
  if (typeof node === "string") into.push({ pattern: node, negated });
  else if (isNot(node)) leaves(node.not, !negated, into);
  else for (const child of node.items) leaves(child, negated, into);
  return into;
}

// The server names the offending node of a refused filter by its path —
// `tag_filter.items[1].not: invalid regular expression …`. Reading that
// prefix lets the editor mark the rule; a detail without one marks nothing.
export function errorPath(detail: string): string | null {
  const match = /^tag_filter((?:\.items\[\d+\]|\.not|\.op|\.items)*):/.exec(detail);
  if (!match) return null;
  const segments = (match[1] ?? "").split(".").filter(Boolean);
  while (segments.length > 0 && ["op", "items"].includes(segments[segments.length - 1]!)) segments.pop();
  return segments.join(".");
}
