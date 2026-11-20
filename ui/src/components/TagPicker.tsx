// The taxonomy picker: the curated tree over the catalog's tags, searchable,
// with the number of items behind every node.
//
// A tick means "the filter includes this tag". Opening the picker shows the
// ticks the filter already has; applying makes the filter agree with the
// ticks — new ones are added to the group the picker was opened for, as ONE
// condition (any / all / none of them), and a tick that was taken away takes
// that tag out of the filter. The footer says what applying will do and how
// many items the result matches, before anything is applied.

import { useEffect, useId, useMemo, useRef, useState } from "react";

import { useApp, useI18n } from "../hooks/context.tsx";
import { useVirtual } from "../hooks/useVirtual.ts";
import { previewSelection } from "../lib/compose.ts";
import { ROOT_ID, addPatterns, findNode, negatedPatterns, positivePatterns, removePatterns, toAst } from "../lib/editor.ts";
import type { Combine, GroupNode } from "../lib/editor.ts";
import { Latest, asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { countActiveInside, expandableIds, filterTree, flattenTree, isSelectable } from "../lib/tagTree.ts";
import type { TaxonomyNode } from "../lib/types.ts";
import { Dialog, ErrorText } from "./ui.tsx";

const ROW_HEIGHT = 34;

interface Props {
  taxonomy: TaxonomyNode[];
  root: GroupNode;
  /** The group new ticks are added to. */
  groupId: string;
  pinned: string[] | null;
  /** Items the current filter matches, when known. */
  currentCount: number | null;
  onApply: (root: GroupNode) => void;
  onClose: () => void;
}

/** What applying the picker's ticks does to the filter. */
export function applyPicks(root: GroupNode, groupId: string, initial: ReadonlySet<string>, checked: ReadonlySet<string>, combine: Combine) {
  const added = [...checked].filter((pattern) => !initial.has(pattern));
  const removed = [...initial].filter((pattern) => !checked.has(pattern));
  let next = removePatterns(root, removed);
  // Removing the last rule of a group removes the group; the root is always there.
  const target = findNode(next, groupId) ? groupId : ROOT_ID;
  next = addPatterns(next, target, added, combine);
  return { root: next, added, removed };
}

export function TagPicker({ taxonomy, root, groupId, pinned, currentCount, onApply, onClose }: Props) {
  const { t } = useI18n();
  const { api } = useApp();
  const initial = useMemo(() => positivePatterns(root), [root]);
  const excluded = useMemo(() => negatedPatterns(root), [root]);
  const [checked, setChecked] = useState<Set<string>>(() => new Set(initial));
  const [combine, setCombine] = useState<Combine>("any");
  const [query, setQuery] = useState("");
  const [opened, setOpened] = useState<Set<string>>(
    // Curated branches start open while they are small; a long generated one
    // (every tag nobody claimed) starts closed.
    () => new Set(taxonomy.filter((node) => (node.children?.length ?? 0) <= 12).map((node) => node.id)),
  );
  const [activeId, setActiveId] = useState<string | null>(null);
  const treeId = useId();
  const combineName = useId();

  const shown = useMemo(() => filterTree(taxonomy, query), [taxonomy, query]);
  const expanded = useMemo(() => (query.trim() ? expandableIds(shown) : opened), [query, shown, opened]);
  const rows = useMemo(() => flattenTree(shown, expanded), [shown, expanded]);
  const inside = useMemo(() => countActiveInside(shown, checked), [shown, checked]);
  const activeIndex = Math.max(0, rows.findIndex((row) => row.node.id === activeId));
  const virtual = useVirtual<HTMLDivElement>(rows.length, ROW_HEIGHT);

  const outcome = useMemo(() => applyPicks(root, groupId, initial, checked, combine), [root, groupId, initial, checked, combine]);
  const changed = outcome.added.length > 0 || outcome.removed.length > 0;

  // How many items the filter would match with these ticks. Sequenced like
  // the main preview: only the answer to the latest ticks is shown.
  const [count, setCount] = useState<{ value: number | null; loading: boolean; error: ApiError | null }>({
    value: null,
    loading: false,
    error: null,
  });
  const latest = useRef(new Latest());
  useEffect(() => {
    const controller = latest.current;
    if (!changed) {
      controller.cancel();
      setCount({ value: null, loading: false, error: null });
      return;
    }
    const selection = previewSelection(toAst(outcome.root), pinned);
    if (selection === null) {
      controller.cancel();
      setCount({ value: 0, loading: false, error: null });
      return;
    }
    setCount((current) => ({ ...current, loading: true, error: null }));
    const timer = window.setTimeout(() => {
      controller
        .run((signal) => api.count(selection, signal))
        .then((result) => {
          if (result.current) setCount({ value: result.value.count, loading: false, error: null });
        })
        .catch((error: unknown) => setCount({ value: null, loading: false, error: asApiError(error) }));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [api, changed, outcome.root, pinned]);
  useEffect(() => {
    const controller = latest.current;
    return () => controller.cancel();
  }, []);

  const toggleCheck = (node: TaxonomyNode) => {
    if (!isSelectable(node)) return;
    const pattern = node.filter!;
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(pattern)) next.delete(pattern);
      else next.add(pattern);
      return next;
    });
  };
  const toggleOpen = (node: TaxonomyNode) => {
    if (query.trim()) return; // a search shows every match; there is nothing to close
    setOpened((current) => {
      const next = new Set(current);
      if (next.has(node.id)) next.delete(node.id);
      else next.add(node.id);
      return next;
    });
  };

  const moveTo = (index: number) => {
    const row = rows[Math.max(0, Math.min(rows.length - 1, index))];
    if (!row) return;
    setActiveId(row.node.id);
    virtual.scrollToIndex(rows.indexOf(row));
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    const row = rows[activeIndex];
    if (!row) return;
    switch (event.key) {
      case "ArrowDown":
        moveTo(activeIndex + 1);
        break;
      case "ArrowUp":
        moveTo(activeIndex - 1);
        break;
      case "Home":
        moveTo(0);
        break;
      case "End":
        moveTo(rows.length - 1);
        break;
      case "PageDown":
        moveTo(activeIndex + 10);
        break;
      case "PageUp":
        moveTo(activeIndex - 10);
        break;
      case "ArrowRight":
        if (row.expandable && !row.expanded) toggleOpen(row.node);
        else if (row.expandable) moveTo(activeIndex + 1);
        break;
      case "ArrowLeft":
        if (row.expandable && row.expanded && !query.trim()) toggleOpen(row.node);
        else if (row.parentId !== null) moveTo(rows.findIndex((candidate) => candidate.node.id === row.parentId));
        break;
      case " ":
      case "Enter":
        if (isSelectable(row.node)) toggleCheck(row.node);
        else toggleOpen(row.node);
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  const labelOf = (node: TaxonomyNode) => (node.origin === "unassigned" ? t("picker.unassigned") : node.label);
  const rowDomId = (index: number) => `${treeId}-row-${index}`;

  const summary = !changed
    ? t("picker.noChange")
    : [
        outcome.added.length > 0 ? t("picker.adds", { count: outcome.added.length }) : "",
        outcome.removed.length > 0 ? t("picker.removes", { count: outcome.removed.length }) : "",
      ]
        .filter(Boolean)
        .join(" · ");

  return (
    <Dialog
      title={t("picker.title")}
      onClose={onClose}
      size="large"
      className="picker"
      footer={
        <>
          <div className="picker-summary" role="status">
            <span>{summary}</span>
            {changed && (
              <span className="picker-result">
                {count.error ? (
                  <span className="field-error">
                    <ErrorText error={count.error} />
                  </span>
                ) : count.loading || count.value === null ? (
                  t("picker.counting")
                ) : currentCount !== null ? (
                  t("picker.resultWithNow", { count: count.value, now: currentCount })
                ) : (
                  t("picker.result", { count: count.value })
                )}
              </span>
            )}
          </div>
          <button type="button" className="btn" onClick={onClose}>
            {t("action.cancel")}
          </button>
          <button type="button" className="btn btn-primary" disabled={!changed} onClick={() => onApply(outcome.root)}>
            {t("picker.apply")}
          </button>
        </>
      }
    >
      <div className="picker-top">
        <label className="field">
          <span className="field-label">{t("picker.search")}</span>
          <input
            type="search"
            className="input"
            value={query}
            data-autofocus
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveId(null);
            }}
            placeholder={t("picker.searchPlaceholder")}
          />
        </label>
        <fieldset className="segmented" disabled={outcome.added.length === 0}>
          <legend>{t("picker.combine")}</legend>
          {(["any", "all", "none"] as const).map((mode) => (
            <label key={mode} className={combine === mode ? "is-on" : undefined}>
              <input type="radio" name={combineName} value={mode} checked={combine === mode} onChange={() => setCombine(mode)} />
              {t(`picker.mode.${mode}`)}
            </label>
          ))}
        </fieldset>
      </div>

      <p className="hint" id={`${treeId}-hint`}>
        {t("picker.hint")}
      </p>

      {rows.length === 0 ? (
        <p className="state state-empty">{t("picker.noMatches")}</p>
      ) : (
        <div
          className="tree"
          ref={virtual.containerRef}
          onScroll={virtual.onScroll}
          role="tree"
          tabIndex={0}
          aria-label={t("picker.tree")}
          aria-describedby={`${treeId}-hint`}
          aria-multiselectable="true"
          aria-activedescendant={rowDomId(activeIndex)}
          onKeyDown={onKeyDown}
        >
          <div style={{ height: virtual.padTop }} role="presentation" />
          {rows.slice(virtual.start, virtual.end).map((row, offset) => {
            const index = virtual.start + offset;
            const { node } = row;
            const selectable = isSelectable(node);
            const isChecked = selectable && checked.has(node.filter!);
            const isExcluded = selectable && excluded.has(node.filter!);
            const hiddenInside = !row.expanded ? (inside.get(node.id) ?? 0) : 0;
            const showPattern = selectable && node.filter !== node.label;
            return (
              <div
                key={node.id}
                id={rowDomId(index)}
                role="treeitem"
                aria-level={row.level}
                aria-setsize={row.setSize}
                aria-posinset={row.position}
                aria-expanded={row.expandable ? row.expanded : undefined}
                aria-checked={selectable ? isChecked : undefined}
                aria-selected={index === activeIndex}
                className={`tree-row${index === activeIndex ? " is-active" : ""}${isChecked ? " is-checked" : ""}`}
                style={{ height: ROW_HEIGHT, paddingLeft: 8 + (row.level - 1) * 20 }}
                onClick={() => {
                  setActiveId(node.id);
                  if (selectable) toggleCheck(node);
                  else toggleOpen(node);
                }}
              >
                <span
                  className={`twisty${row.expandable ? "" : " is-leaf"}`}
                  aria-hidden="true"
                  onClick={(event) => {
                    if (!row.expandable) return;
                    event.stopPropagation();
                    setActiveId(node.id);
                    toggleOpen(node);
                  }}
                >
                  {row.expandable ? (row.expanded ? "▾" : "▸") : ""}
                </span>
                {selectable ? <span className={`tick${isChecked ? " is-on" : ""}`} aria-hidden="true" /> : <span className="tick is-none" aria-hidden="true" />}
                <span className="tree-label">{labelOf(node)}</span>
                {showPattern && <span className="tree-pattern mono">{node.filter}</span>}
                {isExcluded && <span className="badge badge-warn">{t("picker.excluded")}</span>}
                {hiddenInside > 0 && <span className="badge badge-info">{t("picker.insideChecked", { count: hiddenInside })}</span>}
                <span className="tree-count">{t("picker.items", { count: node.item_count })}</span>
              </div>
            );
          })}
          <div style={{ height: virtual.padBottom }} role="presentation" />
        </div>
      )}
    </Dialog>
  );
}
