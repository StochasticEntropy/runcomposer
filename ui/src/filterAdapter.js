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

// ---------------------------------------------------------------------------
// Removal side of the boundary. The app addresses a condition by its position
// (SVAR's serialized value carries no ids — its internal rule ids are dropped
// by the widget's own serializer), and reads it back as app vocabulary: an
// operator name of ours, never SVAR's `filter` id.

const OPERATOR_NAMES = {
  equal: "is",
  notEqual: "isNot",
  beginsWith: "startsWith",
  notBeginsWith: "notStartsWith",
  contains: "contains",
  notContains: "notContains",
  endsWith: "endsWith",
  notEndsWith: "notEndsWith",
};

// The conditions of the outermost level, in the order they are shown.
// A nested group stays one condition here: removing it removes the group.
export function listConditions(filterSet) {
  const rules = filterSet && Array.isArray(filterSet.rules) ? filterSet.rules : [];
  return rules.map((rule, index) => {
    if (rule.rules) {
      return { index, kind: "group", count: countRules(rule) };
    }
    return {
      index,
      kind: "condition",
      operator: OPERATOR_NAMES[rule.filter || "equal"] ?? "is",
      value: rule.value == null ? "" : String(rule.value),
      pattern: rulePattern(rule),
    };
  });
}

function countRules(filterSet) {
  return (filterSet.rules || []).reduce(
    (total, rule) => total + (rule.rules ? countRules(rule) : 1),
    0
  );
}

export function removeConditionAt(filterSet, index) {
  const base = filterSet && Array.isArray(filterSet.rules) ? filterSet : EMPTY_FILTER;
  return { ...base, rules: base.rules.filter((_, position) => position !== index) };
}

// The inverse of patternToSvarRule: the pattern a rule stands for, or null
// when the rule says something no taxonomy node or quick filter can say.
function rulePattern(rule) {
  const value = rule.value == null ? "" : String(rule.value).trim();
  if (!value) return null;
  if ((rule.filter || "equal") === "equal") return value;
  if (rule.filter === "beginsWith") return "prefix:" + value;
  return null;
}

// Which patterns the filter currently carries — at any depth, so a node the
// tree marks active is one the tree can also switch off. The same pattern can
// sit at two places in the taxonomy; both read as active, because what is
// active is the pattern, not the tree position.
export function activePatterns(filterSet) {
  const found = new Set();
  const walk = (set) => {
    (set.rules || []).forEach((rule) => {
      if (rule.rules) return walk(rule);
      const pattern = rulePattern(rule);
      if (pattern) found.add(pattern);
    });
  };
  if (filterSet && Array.isArray(filterSet.rules)) walk(filterSet);
  return found;
}

// One click adds, the next takes it back: a taxonomy node or quick filter is
// a switch, not an append-only button. Removal reaches into groups and prunes
// a group left empty behind it.
export function togglePattern(filterSet, pattern) {
  return activePatterns(filterSet).has(pattern)
    ? removePattern(filterSet, pattern)
    : appendPattern(filterSet, pattern);
}

export function removePattern(filterSet, pattern) {
  const base = filterSet && Array.isArray(filterSet.rules) ? filterSet : EMPTY_FILTER;
  const strip = (set) => ({
    ...set,
    rules: set.rules
      .map((rule) => (rule.rules ? strip(rule) : rule))
      .filter((rule) => (rule.rules ? rule.rules.length > 0 : rulePattern(rule) !== pattern)),
  });
  return strip(base);
}
