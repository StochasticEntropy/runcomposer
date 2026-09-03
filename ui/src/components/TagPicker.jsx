// The tag picker: a modal over the resolved taxonomy that applies a whole
// selection as ONE filter group.
//
// **This is the predecessor's dialog, ported — not a redesign.** Its layout is
// the specification and is followed as-is: a four-column control row in its
// order (include/exclude, how the picked tags combine, how the group joins
// what is there, search), the multi-select hint above a bordered tree box, and
// a footer of "selected: N" against Cancel and Apply. Two things are ours
// because they have to be: every label is runcomposer's own string in its own
// locale files (the original's German label strings and its two-field
// include/exclude vocabulary are another repository's and stay there), and the
// tree is virtualized, because resolving the taxonomy against a real catalog
// renders 2452 nodes where the original had a few hundred and a plain list
// took 17 seconds to settle on a one-letter search.
//
// The one addition on top of the original, and it was asked for: a row whose
// pattern the filter ALREADY carries is marked with a ✓. It is a mark, not a
// selection — the checkboxes say what this round will add, the ✓ says what you
// already have, so reopening the dialog does not make you remember. The mark
// follows the *pattern*, not the position, which is the only reading that
// holds once the tree is resolved and a tag hangs under every node whose
// pattern covers it. A closed branch carries the count of marked patterns
// hidden below it.

import { useDeferredValue, useEffect, useId, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  Dialog,
  Heading,
  ListLayout,
  Modal,
  ModalOverlay,
  Tree,
  TreeItem,
  TreeItemContent,
  Virtualizer,
} from "react-aria-components";

import { SELECTION_DEFAULTS } from "../filterAdapter.js";
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
// kept, and a one-letter query keeps the whole tree; building that many rows
// costs seconds of blocked main thread even with the DOM virtualized, because
// the cost is the collection, not the pixels. Past this budget the matched
// branches stay closed. Nothing is hidden and nothing is truncated — the tree
// is simply not thrown open, and a query specific enough to be worth reading
// is far below the budget.
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
  }, [open]);

  // While searching, everything the search kept is open — a match behind a
  // closed twisty is a match the user still has to hunt for. With the box
  // empty the tree is back to its first level.
  useEffect(() => {
    if (!open) return;
    if (!searching) return setExpanded(roots);
    setExpanded(tooBroad ? new Set(topLevelIds(narrowed)) : openable);
  }, [open, searching, tooBroad, narrowed, openable, roots]);

  const patterns = useMemo(() => selectedPatterns(narrowed, selected), [narrowed, selected]);

  const apply = () => {
    if (patterns.length === 0) return;
    onApply(patterns, { within, join, exclude });
    onClose();
  };

  const renderNodes = (nodes) =>
    nodes.map((node) => {
      const children = node.children ?? [];
      const label = labelOf(node);
      const selectable = isSelectable(node);
      const held = selectable && activePatterns.has(node.filter);
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
              <div className="picker-row" style={{ paddingLeft: `${(level - 1) * 13}px` }}>
                {hasChildItems ? (
                  <Button
                    slot="chevron"
                    className="picker-twisty"
                    aria-label={t(isOpen ? "taxonomy.collapse" : "taxonomy.expand", { label })}
                  >
                    {isOpen ? "▾" : "▸"}
                  </Button>
                ) : (
                  <span className="picker-twisty" aria-hidden="true">
                    •
                  </span>
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
                  className={selectable ? "picker-label" : "picker-label picker-group"}
                  title={node.filter ?? label}
                >
                  {label}
                </span>
                {held && (
                  <span className="picker-held" title={t("picker.inFilter")}>
                    ✓
                  </span>
                )}
                {hiddenInside > 0 && (
                  <span
                    className="picker-inside"
                    title={t("picker.inFilterInside", { count: hiddenInside })}
                  >
                    {hiddenInside}
                  </span>
                )}
                {node.origin === "tag" && <span className="picker-kind">{t("picker.tag")}</span>}
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
            <label htmlFor={`${ids}-mode`}>
              {t("picker.mode")}
              <select
                id={`${ids}-mode`}
                value={exclude ? "exclude" : "include"}
                onChange={(event) => {
                  const next = event.target.value === "exclude";
                  setExclude(next);
                  // "all of them" has no honest excluded reading, so switching
                  // to exclude lands on the one that has.
                  if (next) setWithin("or");
                }}
              >
                <option value="include">{t("picker.modeInclude")}</option>
                <option value="exclude">{t("picker.modeExclude")}</option>
              </select>
            </label>
            {/* Under "exclude" this offers ONE reading, and that is deliberate.
                Negating a selection has two arithmetically available readings
                and only one is ever meant: on the demo corpus "carries neither
                Payments nor Cart" is 38 items, while "does not carry both" is
                60 — the whole corpus, an exclusion that excludes nothing. */}
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
          </div>

          <div className="picker-body">
            <p className="picker-hint">
              {tooBroad ? t("picker.tooBroad", { count: matches }) : t("picker.hint")}
            </p>
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
            <div className="picker-summary">{t("picker.selected", { count: patterns.length })}</div>
            <div className="picker-actions">
              <Button onPress={onClose} className="picker-button">
                {t("picker.cancel")}
              </Button>
              <Button
                onPress={apply}
                isDisabled={patterns.length === 0}
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
