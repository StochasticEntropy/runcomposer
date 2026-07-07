// Auto-compiled preview with fine selection (DESIGN.md §10): checkboxes
// narrow the filter result — explicit picks intersect (fixed AND, §3.1).

import { useI18n } from "../i18n.jsx";

export default function PreviewTable({ items, checked, onToggle, onToggleAll, loading, warnings, hasFilter }) {
  const { t } = useI18n();
  const allChecked = items.length > 0 && checked.size === items.length;

  return (
    <section className="panel preview-panel">
      <header className="panel-header">
        <h2>{t("preview.title")}</h2>
        <span className="muted">
          {loading
            ? t("preview.loading")
            : hasFilter
              ? t("preview.selected", { selected: checked.size, count: items.length })
              : ""}
        </span>
      </header>
      {warnings.map((warning) => (
        <p key={warning} className="warning">
          {warning}
        </p>
      ))}
      {!hasFilter ? (
        <p className="muted">{t("filter.empty")}</p>
      ) : items.length === 0 && !loading ? (
        <p className="muted">{t("preview.empty")}</p>
      ) : (
        <table className="preview-table">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  checked={allChecked}
                  onChange={(ev) => onToggleAll(ev.target.checked)}
                />
              </th>
              <th>{t("preview.columns.id")}</th>
              <th>{t("preview.columns.name")}</th>
              <th>{t("preview.columns.tags")}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} className={checked.has(item.id) ? "" : "unchecked"}>
                <td>
                  <input
                    type="checkbox"
                    checked={checked.has(item.id)}
                    onChange={() => onToggle(item.id)}
                  />
                </td>
                <td className="mono">{item.id}</td>
                <td>{item.name}</td>
                <td>
                  {item.tags.map((tag) => (
                    <span key={tag} className="tag">
                      {tag}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
