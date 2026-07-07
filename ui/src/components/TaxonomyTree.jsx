// Taxonomy navigation (DESIGN.md §2: a curated tree over tag patterns —
// data served by the API, never hardcoded).

import { useI18n } from "../i18n.jsx";

function Node({ node, onPick, depth }) {
  return (
    <li>
      {node.filter ? (
        <button
          className="taxonomy-leaf"
          style={{ paddingLeft: `${depth * 14}px` }}
          title={node.filter}
          onClick={() => onPick(node.filter)}
        >
          {node.label}
        </button>
      ) : (
        <span className="taxonomy-group" style={{ paddingLeft: `${depth * 14}px` }}>
          {node.label}
        </span>
      )}
      {(node.children || []).length > 0 && (
        <ul>
          {node.children.map((child) => (
            <Node key={child.label} node={child} onPick={onPick} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function TaxonomyTree({ taxonomy, onPick }) {
  const { t } = useI18n();
  return (
    <section className="panel taxonomy-panel">
      <header className="panel-header">
        <h2>{t("taxonomy.title")}</h2>
      </header>
      <p className="muted small">{t("taxonomy.hint")}</p>
      <ul className="taxonomy-tree">
        {taxonomy.map((node) => (
          <Node key={node.label} node={node} onPick={onPick} depth={0} />
        ))}
      </ul>
    </section>
  );
}
