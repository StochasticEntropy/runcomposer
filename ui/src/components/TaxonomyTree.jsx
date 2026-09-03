// Taxonomy navigation (DESIGN.md §2: a curated tree over tag patterns — data
// served by the API, never hardcoded).
//
// The API is asked for the tree *resolved* against the catalog
// (`?resolve=true`), so a node whose pattern stands for a family of tags
// carries one child per concrete tag and each of them can be picked on its
// own. That turns a small written tree into a large rendered one — the real
// corpora run to thousands of nodes — so a branch's children are mounted only
// while it is open, and everything below the first level starts collapsed.

import { useEffect, useMemo, useState } from "react";

import { useI18n } from "../i18n.jsx";

// The synthetic node the server appends for tags no written node claims. Its
// label is runcomposer's own string rather than the author's, so it is the one
// label in the tree the UI translates (DESIGN.md §10).
const UNASSIGNED = "unassigned";

function Node({ node, onPick, depth, open, onToggle }) {
  const { t } = useI18n();
  const children = node.children ?? [];
  const isOpen = open.has(node.id);
  const label = node.origin === UNASSIGNED ? t("taxonomy.unassigned") : node.label;
  const count =
    typeof node.item_count === "number" ? (
      <span className="taxonomy-count" title={t("taxonomy.items")}>
        {node.item_count}
      </span>
    ) : null;

  return (
    <li>
      <div className="taxonomy-row" style={{ paddingLeft: `${depth * 12}px` }}>
        {children.length > 0 ? (
          <button
            type="button"
            className="taxonomy-twisty"
            aria-expanded={isOpen}
            aria-label={t(isOpen ? "taxonomy.collapse" : "taxonomy.expand", { label })}
            onClick={() => onToggle(node.id)}
          >
            {isOpen ? "▾" : "▸"}
          </button>
        ) : (
          <span className="taxonomy-twisty" aria-hidden="true" />
        )}
        {node.filter ? (
          // aria-label pins the accessible name to the label. The pattern can
          // be long — a composed alternation regex runs to hundreds of
          // characters — and as the accessible name it would replace the one
          // word a screen reader needs. It stays in the tooltip, where it is
          // useful and where a long one does no harm.
          <button
            type="button"
            className="taxonomy-leaf"
            aria-label={label}
            title={node.filter}
            onClick={() => onPick(node.filter)}
          >
            {label}
          </button>
        ) : (
          <button
            type="button"
            className="taxonomy-group"
            title={label}
            aria-expanded={isOpen}
            onClick={() => onToggle(node.id)}
          >
            {label}
          </button>
        )}
        {count}
      </div>
      {isOpen && children.length > 0 && (
        <ul>
          {children.map((child) => (
            <Node
              key={child.id ?? child.label}
              node={child}
              onPick={onPick}
              depth={depth + 1}
              open={open}
              onToggle={onToggle}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function TaxonomyTree({ taxonomy, onPick }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(() => new Set());

  // The first level opens on arrival, so the panel reads as a tree rather
  // than as a list of closed headings; everything deeper waits to be asked
  // for, which is also what keeps the large branches out of the DOM.
  const topLevel = useMemo(() => taxonomy.map((node) => node.id).filter(Boolean), [taxonomy]);
  useEffect(() => setOpen(new Set(topLevel)), [topLevel]);

  const toggle = (id) =>
    setOpen((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  return (
    <section className="panel taxonomy-panel">
      <header className="panel-header">
        <h2>{t("taxonomy.title")}</h2>
      </header>
      <p className="muted small">{t("taxonomy.hint")}</p>
      <ul className="taxonomy-tree">
        {taxonomy.map((node) => (
          <Node
            key={node.id ?? node.label}
            node={node}
            onPick={onPick}
            depth={0}
            open={open}
            onToggle={toggle}
          />
        ))}
      </ul>
    </section>
  );
}
