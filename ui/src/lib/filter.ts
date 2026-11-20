// The tag-filter AST of the run spec (DESIGN.md §3.1) and its one-line text
// form. The AST is what the server compiles; the text form is what a person
// reads, types and shares in a URL:
//
//     Payments AND (Smoke OR Regression) AND NOT prefix:Quarantine-
//
// Grammar of the text form:
//
//     expr    := and ("OR" and)*
//     and     := unary ("AND" unary)*
//     unary   := "NOT" unary | "(" expr ")" | pattern
//     pattern := bare | "quoted"
//
// AND binds tighter than OR. A pattern is quoted when it contains a space, a
// parenthesis or a quote, or would read as an operator — `"regex:^Cart(V2)?$"`.
// Operators are upper case only, so a tag called `and` needs no quotes.

import type { FilterAst } from "./types.ts";

export type { FilterAst };

/** A filter that matches every item of the catalog: no tag is the empty string. */
export const MATCH_ALL: FilterAst = { not: "regex:^$" };

export const isGroup = (node: FilterAst): node is { op: "AND" | "OR"; items: FilterAst[] } =>
  typeof node === "object" && node !== null && "op" in node;
export const isNot = (node: FilterAst): node is { not: FilterAst } =>
  typeof node === "object" && node !== null && "not" in node;

const OPERATORS = new Set(["AND", "OR", "NOT"]);
const NEEDS_QUOTES = /[\s()"\\]/;

export function quotePattern(pattern: string): string {
  if (pattern !== "" && !NEEDS_QUOTES.test(pattern) && !OPERATORS.has(pattern)) return pattern;
  return `"${pattern.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

export function formatFilter(node: FilterAst | null): string {
  if (node == null) return "";
  if (typeof node === "string") return quotePattern(node);
  if (isNot(node)) {
    const inner = formatFilter(node.not);
    return isGroup(node.not) ? `NOT (${inner})` : `NOT ${inner}`;
  }
  return node.items.map((child) => (isGroup(child) ? `(${formatFilter(child)})` : formatFilter(child))).join(` ${node.op} `);
}

export type ParseProblem =
  | "empty"
  | "unexpected-end"
  | "unexpected-operator"
  | "missing-operator"
  | "unbalanced-open"
  | "unbalanced-close"
  | "unterminated-quote"
  | "needs-quotes";

export class FilterParseError extends Error {
  readonly problem: ParseProblem;
  /** Offset into the text where reading stopped. */
  readonly position: number;
  readonly token: string;

  constructor(problem: ParseProblem, position: number, token = "") {
    super(`${problem} at ${position}`);
    this.name = "FilterParseError";
    this.problem = problem;
    this.position = position;
    this.token = token;
  }
}

type Token =
  | { type: "op"; value: "AND" | "OR" | "NOT"; at: number }
  | { type: "open" | "close"; at: number }
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
      if (!closed) throw new FilterParseError("unterminated-quote", start);
      tokens.push({ type: "pattern", value, at: start });
    } else {
      const start = index;
      while (index < text.length && !/[\s()"]/.test(text[index]!)) index += 1;
      const word = text.slice(start, index);
      // `regex:(a|b)` typed bare: the pattern runs into a parenthesis or a quote.
      if (/^(regex|prefix):/.test(word) && index < text.length && /[("]/.test(text[index]!)) {
        throw new FilterParseError("needs-quotes", start, word);
      }
      if (OPERATORS.has(word)) tokens.push({ type: "op", value: word as "AND" | "OR" | "NOT", at: start });
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
  const end = text.length;

  const unary = (): FilterAst => {
    const token = peek();
    if (!token) throw new FilterParseError("unexpected-end", end);
    cursor += 1;
    if (token.type === "pattern") {
      if (token.value === "") throw new FilterParseError("empty", token.at);
      return token.value;
    }
    if (token.type === "op") {
      if (token.value === "NOT") return { not: unary() };
      throw new FilterParseError("unexpected-operator", token.at, token.value);
    }
    if (token.type === "open") {
      const inner = or();
      const closing = peek();
      if (!closing || closing.type !== "close") throw new FilterParseError("unbalanced-open", token.at);
      cursor += 1;
      return inner;
    }
    throw new FilterParseError("unbalanced-close", token.at);
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
  const or = () => chain("OR", and);

  const ast = or();
  const rest = peek();
  if (rest) {
    if (rest.type === "close") throw new FilterParseError("unbalanced-close", rest.at);
    throw new FilterParseError("missing-operator", rest.at, rest.type === "pattern" || rest.type === "op" ? rest.value : "(");
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
