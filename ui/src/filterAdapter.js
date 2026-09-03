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
//
// A rule can also carry `includes` — a LIST of values, which is what the
// widget's value editor writes when the catalog's tags are offered as options
// and several are ticked. It is a fourth shape, not an operator, and it has to
// be translated here: while it was not, ticking two tags produced a rule the
// translation returned nothing for, and the condition vanished from the filter
// silently — a preview that went from 158 items to 69 with nothing to say why.

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
  if (Array.isArray(rule.includes) && rule.includes.length > 0) return includesToNode(rule);
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

// A ticked list of concrete values is an OR of literals — and its negation is
// the AND of the negations, the same de Morgan step the picker's exclude side
// takes. (A one-value list is just that value: a group of one says nothing.)
const NEGATING = new Set(["notEqual", "notBeginsWith", "notContains", "notEndsWith"]);

function includesToNode(rule) {
  const values = [];
  for (const raw of rule.includes) {
    const value = raw == null ? "" : String(raw).trim();
    if (value && !values.includes(value)) values.push(value);
  }
  if (values.length === 0) return null;
  const negated = NEGATING.has(rule.filter);
  const items = values.map((value) => (negated ? { not: value } : value));
  if (items.length === 1) return items[0];
  return { op: negated ? "AND" : "OR", items };
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
    if (Array.isArray(rule.includes) && rule.includes.length > 0) {
      return {
        index,
        kind: "condition",
        operator: NEGATING.has(rule.filter) ? "isNoneOf" : "isAnyOf",
        value: rule.includes.join(", "),
        pattern: null, // a list is not a single pattern any node could switch
      };
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
  if (Array.isArray(rule.includes) && rule.includes.length > 0) return null;
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

// ---------------------------------------------------------------------------
// A whole picked selection, committed as ONE group.
//
// This is the other half of the picker (DESIGN.md §3.1): three explicit
// choices — include or exclude, how the picked patterns combine with each
// other, how that group joins the filter already built — and one action that
// applies them together. It is also the usable way to reach a nested filter:
// `TZR AND (Krankenkasse OR Drittrecht)` is picking two tags with within=OR
// and join=AND, not assembling rows one at a time.
//
// **Exclusion is de Morgan, not a second field.** The runspec grammar has
// `{not: node}` and SVAR has negating operators per rule — but SVAR has no way
// to spell a negated *group*, and the SVAR value is this app's state of record
// for the filter (§10). So excluding "any of A, B" is written as the AND of
// the two negations, which is the same proposition:
//
//     NOT (A OR B)  ==  (NOT A) AND (NOT B)
//     NOT (A AND B) ==  (NOT A) OR  (NOT B)
//
// The glue therefore FLIPS under exclusion, which is the one non-obvious line
// in this file. Two things fall out of doing it this way rather than inventing
// a shape SVAR cannot hold: every negation stays an ordinary editable rule in
// the widget, and each one is its own removable chip.

export const SELECTION_DEFAULTS = { within: "or", join: "and", exclude: false };

const NEGATED = { equal: "notEqual", beginsWith: "notBeginsWith" };

function selectionRule(pattern, exclude) {
  const rule = patternToSvarRule(pattern);
  if (!exclude) return rule;
  return { ...rule, filter: NEGATED[rule.filter] ?? "notEqual" };
}

// The one SVAR node a selection becomes: a bare rule for a single pattern
// (a group of one is noise), a group for several.
export function buildSelectionNode(patterns, options = {}) {
  const { within = SELECTION_DEFAULTS.within, exclude = SELECTION_DEFAULTS.exclude } = options;
  const unique = [];
  for (const pattern of patterns ?? []) {
    const text = String(pattern ?? "").trim();
    if (text && !unique.includes(text)) unique.push(text);
  }
  if (unique.length === 0) return null;
  if (unique.length === 1) return selectionRule(unique[0], exclude);
  const glue = exclude ? (within === "or" ? "and" : "or") : within;
  return { glue, rules: unique.map((pattern) => selectionRule(pattern, exclude)) };
}

// The group joins what is already there under `join`. Three cases, and only
// the third builds nesting: an empty filter simply takes the glue; a filter
// whose glue already IS the requested one grows by one rule; anything else is
// pushed down a level so the two glues do not have to be the same one.
export function appendSelection(filterSet, patterns, options = {}) {
  const { join = SELECTION_DEFAULTS.join } = options;
  const base = filterSet && Array.isArray(filterSet.rules) ? filterSet : EMPTY_FILTER;
  const node = buildSelectionNode(patterns, options);
  if (!node) return base;
  // A group whose own glue IS the join adds nothing by being a level of its
  // own: its rules belong beside the existing ones, where each is a condition
  // the panel can list and remove on its own. (This is the common shape of an
  // exclusion — de Morgan turns "none of these" into an AND of negations, and
  // AND is also the default join.)
  const rules = node.rules && (node.glue || "and") === join ? node.rules : [node];
  // A single existing rule has no glue to disagree about, so it is re-glued
  // rather than wrapped — nesting that says nothing is nesting to undo.
  if (base.rules.length <= 1) return { glue: join, rules: [...base.rules, ...rules] };
  if ((base.glue || "and") === join) return { ...base, rules: [...base.rules, ...rules] };
  return { glue: join, rules: [base, node] };
}

// What the selection adds, as a runspec AST — the picker shows this back
// before it is applied, so the three controls have a visible consequence.
export function selectionAst(patterns, options = {}) {
  const node = buildSelectionNode(patterns, options);
  if (!node) return null;
  return svarToAst(node.rules ? node : { glue: "and", rules: [node] });
}

// An AST read aloud. `words` carries the operator names, so the expression is
// translated where every other literal is (i18n, §10.1) and this stays pure.
export function formatAst(node, words, depth = 0) {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (node.not !== undefined) return `${words.not} ${formatAst(node.not, words, depth + 1)}`;
  const glue = node.op === "OR" ? words.or : words.and;
  const inner = node.items.map((item) => formatAst(item, words, depth + 1)).join(` ${glue} `);
  return depth > 0 ? `(${inner})` : inner;
}
