// The tag picker (DESIGN.md §2 + §3.1): a modal over the resolved taxonomy
// that commits a whole selection as ONE filter group.
//
// It exists because the tree stopped being navigable. Resolving the taxonomy
// against the catalog (0.1.4) took a real corpus from ~150 written nodes to
// 2452 rendered ones over 1609 tags — complete, and unusable as an
// always-visible list with no search. That list is gone; this is what
// replaced it.
//
// Four things it does that a column of clickable rows cannot:
//
//  1. **Search that narrows without flattening.** `filterTree` prunes branches
//     that match nothing and keeps a matching node's children, so a hit stays
//     under the heading that explains what it is (tagTree.js). A short query
//     matches nearly everything, and searching opens every branch it kept, so
//     the tree is virtualized: without that, typing one letter on the real
//     corpus put all 2452 rows in the DOM and took 17 seconds to settle
//     (measured). Collapsed branches are not rendered at all, so the ordinary
//     browsing case never builds them either.
//  2. **Multi-select.** Checkbox, Space, shift-range, ctrl/cmd — the tree is
//     `react-aria-components`' Tree, which is the reason that dependency is
//     here: full keyboard and screen-reader semantics for a multi-select tree
//     is not something worth hand-rolling.
//  3. **Three explicit controls** — include/exclude, how the picked tags
//     combine with each other, how the group joins the filter already built —
//     shown next to the expression they produce, so the consequence of each is
//     visible before it is applied.
//  4. **One apply.** Which is also the usable road to a nested filter:
//     `TZR AND (Krankenkasse OR Drittrecht)` is two tags with within=OR and
//     join=AND, not a filter assembled row by row.
//
// What survives from the sidebar it replaces: a node already in the filter is
// marked, and a closed branch says how many are hidden underneath it. The mark
// follows the **pattern**, not the position — the only reading that holds on a
// resolved tree, where the same tag hangs under every node whose pattern
// covers it, so one condition legitimately marks several rows.
//
// A row therefore has THREE states, and they are deliberately not three shades
// of one thing:
//
//  * **selected** — a checkbox, an accent-filled row: what this round is about
//    to add. Empty every time the dialog opens.
//  * **in the filter** — a green ✓ button: what the user already has. It is
//    NOT pre-selected, because Apply would then re-add it.
//  * **staged for removal** — the same ✓ pressed, turning red with the label
//    struck through: on Apply it comes back out.
//
// Which settles the question of what "untick a row that is already active"
// does: nothing, because such a row is never ticked. Taking something out has
// its own control, so the two directions cannot be confused — and it is staged
// rather than immediate, so Cancel cancels everything the dialog was about to
// do, not merely the additions. The picker is thus a full editor of the tag
// part of the filter without ever having to reproduce a structure it cannot
// represent (a nested group, a negation, a condition typed into the builder):
// it removes named patterns and appends one group, and leaves everything else
// exactly as it found it.

import { useDeferredValue, useEffect, useId, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  Dialog,
  Heading,
  Modal,
  ModalOverlay,
  ListLayout,
  Tree,
  TreeItem,
  TreeItemContent,
  Virtualizer,
} from "react-aria-components";

import { SELECTION_DEFAULTS, formatAst, selectionAst } from "../filterAdapter.js";
import {
  countActiveInside,
  countNodes,
  expandableIds,
  filterTree,
  isSelectable,
  selectedPatterns,
  topLevelIds,
} from "../tagTree.js";
import { useI18n } from "../i18n.jsx";

// The synthetic node the server appends for tags no written node claims. Its
// label is runcomposer's own string rather than the author's, so it is the one
// label in the tree the UI translates (DESIGN.md §10) — and therefore the one
// the search has to be told about, or it would match the English spelling of a
// row that is on screen in German.
const UNASSIGNED = "unassigned";

// Rows are one line each, so the virtualizer can be told their height instead
// of measuring 2452 of them. Kept in step with `.picker-row` in styles.css.
const ROW_HEIGHT = 26;
const TREE_LAYOUT = { rowHeight: ROW_HEIGHT };

// How many nodes a search may open at once. Searching opens every branch it
// kept, and a one-letter query keeps the whole tree: on the real corpus that
// is 2452 nodes, and building that many rows costs seconds of blocked main
// thread even though the virtualizer keeps the DOM small — the cost is the
// collection, not the pixels. Past this budget the matched branches stay
// CLOSED instead. Nothing is hidden and nothing is truncated; the tree is
// simply not thrown open, and the hint says so. A query specific enough to be
// worth reading is far below the budget.
const EXPAND_BUDGET = 400;

// Every id a selection may hold. Select-all (ctrl/cmd-A) reports "all", which
// in the widget's vocabulary includes the headings; here it means the rows
// that actually carry a pattern.
function selectableIds(nodes, into = new Set()) {
  for (const node of nodes) {
    if (isSelectable(node)) into.add(node.id);
    if (node.children?.length) selectableIds(node.children, into);
  }
  return into;
}

export default function TagPicker({ open, taxonomy, activePatterns, onClose, onApply }) {
  const { t } = useI18n();
  const ids = useId();

  const [query, setQuery] = useState("");
  const [exclude, setExclude] = useState(SELECTION_DEFAULTS.exclude);
  const [within, setWithin] = useState(SELECTION_DEFAULTS.within);
  const [join, setJoin] = useState(SELECTION_DEFAULTS.join);
  const [selected, setSelected] = useState(() => new Set());
  const [removing, setRemoving] = useState(() => new Set());
  const [expanded, setExpanded] = useState(() => new Set());

  const labelOf = (node) => (node.origin === UNASSIGNED ? t("taxonomy.unassigned") : node.label);

  // Typing stays instant while the tree re-narrows behind it: the input owns
  // `query`, the tree reads the deferred copy.
  const search = useDeferredValue(query);
  const narrowed = useMemo(() => filterTree(taxonomy, search, labelOf), [taxonomy, search]);
  const openable = useMemo(() => expandableIds(narrowed), [narrowed]);
  const roots = useMemo(() => new Set(topLevelIds(taxonomy)), [taxonomy]);
  const matches = useMemo(() => countNodes(narrowed), [narrowed]);
  const searching = search.trim().length > 0;
  const tooBroad = searching && matches > EXPAND_BUDGET;
  const inside = useMemo(
    () => countActiveInside(narrowed, activePatterns),
    [narrowed, activePatterns]
  );

  // Opening is a fresh selection every time: the dialog adds a group, it does
  // not edit one, so carrying the last one over would be a trap.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setExclude(SELECTION_DEFAULTS.exclude);
    setWithin(SELECTION_DEFAULTS.within);
    setJoin(SELECTION_DEFAULTS.join);
    setSelected(new Set());
    setRemoving(new Set());
  }, [open]);

  // While searching, everything the search kept is open — a match behind a
  // closed twisty is a match the user still has to hunt for. With the box
  // empty the tree is back to its first level, which is the only readable
  // state at this size.
  useEffect(() => {
    if (!open) return;
    if (!searching) return setExpanded(roots);
    setExpanded(tooBroad ? new Set(topLevelIds(narrowed)) : openable);
  }, [open, searching, tooBroad, narrowed, openable, roots]);

  const patterns = useMemo(() => selectedPatterns(narrowed, selected), [narrowed, selected]);
  const words = { and: t("picker.and"), or: t("picker.or"), not: t("picker.not") };
  const expression = formatAst(selectionAst(patterns, { within, exclude }), words);

  const stageRemoval = (pattern) =>
    setRemoving((current) => {
      const next = new Set(current);
      if (!next.delete(pattern)) next.add(pattern);
      return next;
    });

  const nothingToDo = patterns.length === 0 && removing.size === 0;

  const apply = () => {
    if (nothingToDo) return;
    onApply(patterns, { within, join, exclude }, [...removing]);
    onClose();
  };

  const renderNodes = (nodes) =>
    nodes.map((node) => {
      const children = node.children ?? [];
      const label = labelOf(node);
      const selectable = isSelectable(node);
      const held = selectable && activePatterns.has(node.filter);
      const staged = held && removing.has(node.filter);
      const isOpen = expanded.has(node.id);
      // Only while the branch is closed: open, the ✓ on the rows themselves
      // says it, and repeating it on every ancestor is noise.
      const hiddenInside = isOpen ? 0 : inside.get(node.id) ?? 0;
      return (
        <TreeItem
          key={node.id}
          id={node.id}
          textValue={label}
          hasChildItems={children.length > 0}
          isDisabled={!selectable}
          className="picker-item"
        >
          <TreeItemContent>
            {({ level, hasChildItems }) => (
              <div className="picker-row" style={{ paddingLeft: `${(level - 1) * 14}px` }}>
                {hasChildItems ? (
                  <Button
                    slot="chevron"
                    className="picker-twisty"
                    aria-label={t(isOpen ? "taxonomy.collapse" : "taxonomy.expand", { label })}
                  >
                    {isOpen ? "▾" : "▸"}
                  </Button>
                ) : (
                  <span className="picker-twisty" aria-hidden="true" />
                )}
                {selectable ? (
                  <Checkbox slot="selection" className="picker-check" excludeFromTabOrder>
                    {({ isSelected }) => <span aria-hidden="true">{isSelected ? "☑" : "☐"}</span>}
                  </Checkbox>
                ) : (
                  <span className="picker-check" aria-hidden="true" />
                )}
                {/* The pattern goes in the tooltip, not the accessible name: a
                    composed alternation regex runs to hundreds of characters
                    and would replace the one word a screen reader needs. */}
                <span
                  className={[
                    "picker-label",
                    selectable ? "" : "picker-group",
                    staged ? "picker-struck" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  title={node.filter ?? label}
                >
                  {label}
                </span>
                {held && (
                  <Button
                    className={staged ? "picker-held staged" : "picker-held"}
                    aria-pressed={staged}
                    aria-label={t(staged ? "picker.keepInFilter" : "picker.removeFromFilter", { label })}
                    onPress={() => stageRemoval(node.filter)}
                  >
                    {staged ? "✕" : "✓"}
                  </Button>
                )}
                {hiddenInside > 0 && (
                  <span
                    className="picker-inside"
                    title={t("picker.inFilterInside", { count: hiddenInside })}
                  >
                    {hiddenInside}
                  </span>
                )}
                {typeof node.item_count === "number" && (
                  <span className="picker-count" title={t("taxonomy.items")}>
                    {node.item_count}
                  </span>
                )}
              </div>
            )}
          </TreeItemContent>
          {isOpen && children.length > 0 ? renderNodes(children) : null}
        </TreeItem>
      );
    });

  return (
    <ModalOverlay
      isOpen={open}
      onOpenChange={(next) => !next && onClose()}
      isDismissable
      className="picker-overlay"
    >
      <Modal className="picker-modal">
        <Dialog className="picker-dialog">
          <header className="picker-header">
            <Heading slot="title">{t("picker.title")}</Heading>
            <Button onPress={onClose} className="picker-button">
              {t("picker.close")}
            </Button>
          </header>

          <div className="picker-controls">
            <label htmlFor={`${ids}-search`}>
              {t("picker.search")}
              <input
                id={`${ids}-search`}
                type="search"
                autoFocus
                value={query}
                placeholder={t("picker.searchPlaceholder")}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <label htmlFor={`${ids}-mode`}>
              {t("picker.mode")}
              <select
                id={`${ids}-mode`}
                value={exclude ? "exclude" : "include"}
                onChange={(event) => {
                  const next = event.target.value === "exclude";
                  setExclude(next);
                  // "all of them" has no honest excluded reading (see below),
                  // so switching to exclude lands on the one that has.
                  if (next) setWithin("or");
                }}
              >
                <option value="include">{t("picker.modeInclude")}</option>
                <option value="exclude">{t("picker.modeExclude")}</option>
              </select>
            </label>
            {/* Under "exclude" this control offers ONE reading, and that is
                deliberate. Negating a whole selection has two arithmetically
                available readings and only one of them is ever meant:
                measured on a 2652-item corpus, "carries neither Krankenkasse
                nor Drittrecht" is 2423 items, while "does not carry both" is
                2652 — the entire catalog, an exclusion that excludes nothing
                and reports no error. Offering the second as a peer of the
                first would be offering a footgun with a plausible number on
                it. The raw builder underneath can still express it. */}
            <label htmlFor={`${ids}-within`}>
              {t("picker.within")}
              <select
                id={`${ids}-within`}
                value={within}
                onChange={(event) => setWithin(event.target.value)}
              >
                {exclude ? (
                  <option value="or">{t("picker.withinNone")}</option>
                ) : (
                  <>
                    <option value="or">{t("picker.withinAny")}</option>
                    <option value="and">{t("picker.withinAll")}</option>
                  </>
                )}
              </select>
            </label>
            <label htmlFor={`${ids}-join`}>
              {t("picker.join")}
              <select id={`${ids}-join`} value={join} onChange={(event) => setJoin(event.target.value)}>
                <option value="and">{t("picker.joinAnd")}</option>
                <option value="or">{t("picker.joinOr")}</option>
              </select>
            </label>
          </div>

          <p className="muted small picker-hint">
            {tooBroad ? t("picker.tooBroad", { count: matches }) : t("picker.hint")}
          </p>

          <div className="picker-body">
            {narrowed.length === 0 ? (
              <p className="picker-empty">{t("picker.noMatches")}</p>
            ) : (
              <Virtualizer layout={ListLayout} layoutOptions={TREE_LAYOUT}>
                <Tree
                  aria-label={t("picker.tree")}
                  className="picker-tree"
                  selectionMode="multiple"
                  selectionBehavior="toggle"
                  disabledBehavior="selection"
                  selectedKeys={selected}
                  onSelectionChange={(keys) =>
                    setSelected(keys === "all" ? selectableIds(narrowed) : new Set(keys))
                  }
                  expandedKeys={expanded}
                  onExpandedChange={(keys) =>
                    setExpanded(keys === "all" ? new Set(openable) : new Set(keys))
                  }
                >
                  {renderNodes(narrowed)}
                </Tree>
              </Virtualizer>
            )}
          </div>

          <footer className="picker-footer">
            <div className="picker-summary">
              <strong>
                {patterns.length === 1
                  ? t("picker.selectedOne")
                  : t("picker.selected", { count: patterns.length })}
              </strong>
              {expression && (
                <span className="mono picker-expression" title={expression}>
                  {t("picker.adds", { expression })}
                </span>
              )}
              {removing.size > 0 && (
                <span className="picker-removing" title={[...removing].join(", ")}>
                  {t("picker.removes", { count: removing.size })}
                </span>
              )}
            </div>
            <div className="picker-actions">
              <Button onPress={onClose} className="picker-button">
                {t("picker.cancel")}
              </Button>
              <Button
                onPress={apply}
                isDisabled={nothingToDo}
                className="picker-button primary"
              >
                {t("picker.apply")}
              </Button>
            </div>
          </footer>
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}
