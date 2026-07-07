// The filter-builder adapter boundary (DESIGN.md §10): the SVAR widget's
// JSON shape stays inside this module + FilterPanel, so the widget remains
// swappable. Everything else in the app speaks the runspec tag-filter AST.
//
// SVAR shape:  {glue: "and"|"or", rules: [{field, filter, value} | group]}
// runspec AST: "pattern" | {op: "AND"|"OR", items: [...]} | {not: node}
//
// Text operators map onto the three pattern kinds; an `equal` value that
// already spells `prefix:`/`regex:` passes through raw, so power users can
// type spec-grammar patterns directly.

const escapeRegex = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

export const EMPTY_FILTER = { glue: "and", rules: [] };

export function svarToAst(filterSet) {
  if (!filterSet || !Array.isArray(filterSet.rules)) return null;
  const items = filterSet.rules.map(ruleToNode).filter((node) => node !== null);
  if (items.length === 0) return null;
  if (items.length === 1) return items[0];
  return { op: (filterSet.glue || "and").toUpperCase(), items };
}

function ruleToNode(rule) {
  if (rule.rules) return svarToAst(rule); // nested group
  const value = rule.value == null ? "" : String(rule.value).trim();
  if (!value) return null;
  switch (rule.filter || "equal") {
    case "equal":
      return value; // literal — or raw prefix:/regex: passthrough
    case "notEqual":
      return { not: value };
    case "beginsWith":
      return "prefix:" + value;
    case "notBeginsWith":
      return { not: "prefix:" + value };
    case "contains":
      return "regex:" + escapeRegex(value);
    case "notContains":
      return { not: "regex:" + escapeRegex(value) };
    case "endsWith":
      return "regex:" + escapeRegex(value) + "$";
    case "notEndsWith":
      return { not: "regex:" + escapeRegex(value) + "$" };
    default:
      return null; // number/date operators don't apply to tags
  }
}

// Taxonomy nodes and quick filters carry spec-grammar pattern strings;
// translate the common ones into editable SVAR rules.
export function patternToSvarRule(pattern) {
  if (pattern.startsWith("prefix:")) {
    return { field: "tag", type: "text", filter: "beginsWith", value: pattern.slice("prefix:".length) };
  }
  return { field: "tag", type: "text", filter: "equal", value: pattern };
}

export function appendPattern(filterSet, pattern) {
  const base = filterSet && Array.isArray(filterSet.rules) ? filterSet : EMPTY_FILTER;
  return { ...base, rules: [...base.rules, patternToSvarRule(pattern)] };
}
