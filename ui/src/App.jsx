import { useEffect, useMemo, useRef, useState } from "react";

import { compileSelection, createRun, getRunners, getTaxonomy } from "./api.js";
import { EMPTY_FILTER, appendPattern, svarToAst } from "./filterAdapter.js";
import { AVAILABLE_LOCALES, useI18n } from "./i18n.jsx";
import ComposeFooter from "./components/ComposeFooter.jsx";
import FilterPanel from "./components/FilterPanel.jsx";
import PreviewTable from "./components/PreviewTable.jsx";
import QuarantineView from "./components/QuarantineView.jsx";
import RunsView from "./components/RunsView.jsx";
import TaxonomyTree from "./components/TaxonomyTree.jsx";

export default function App({ uiConfig, locale, onLocaleChange }) {
  const { t } = useI18n();
  const [tab, setTab] = useState("compose");
  const [taxonomy, setTaxonomy] = useState([]);
  const [runners, setRunners] = useState([]);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const [filterValue, setFilterValue] = useState(EMPTY_FILTER);
  const [revision, setRevision] = useState(0); // bumps remount the widget on external edits
  const [items, setItems] = useState([]);
  const [checked, setChecked] = useState(new Set());
  const [warnings, setWarnings] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const debounceRef = useRef(null);

  const ast = useMemo(() => svarToAst(filterValue), [filterValue]);
  const quickFilters = uiConfig.quick_filters ?? [];

  useEffect(() => {
    getTaxonomy()
      .then((body) => setTaxonomy(body.taxonomy ?? []))
      .catch((err) => setError(err.message));
    getRunners()
      .then((all) => setRunners(all.filter((runner) => !runner.error)))
      .catch((err) => setError(err.message));
  }, []);

  // Auto-compiled preview (DESIGN.md §10): every filter edit recompiles.
  useEffect(() => {
    if (!ast) {
      setItems([]);
      setChecked(new Set());
      setWarnings([]);
      return undefined;
    }
    setLoading(true);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      compileSelection({ tag_filter: ast })
        .then((body) => {
          setItems(body.items);
          setChecked(new Set(body.items.map((item) => item.id)));
          setWarnings(body.warnings);
          setError(null);
        })
        .catch((err) => setError(t("errors.request", { message: err.message })))
        .finally(() => setLoading(false));
    }, 250);
    return () => clearTimeout(debounceRef.current);
  }, [ast]);

  const pickPattern = (pattern) => {
    setFilterValue((value) => appendPattern(value, pattern));
    setRevision((n) => n + 1);
    setTab("compose");
  };

  const clearFilter = () => {
    setFilterValue(EMPTY_FILTER);
    setRevision((n) => n + 1);
  };

  const toggleItem = (itemId) => {
    setChecked((old) => {
      const next = new Set(old);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };

  const compose = (dispatch, title) => {
    if (checked.size === 0) {
      setError(t("compose.nothingSelected"));
      return;
    }
    const selection = { tag_filter: ast };
    if (checked.size < items.length) {
      selection.item_ids = items.filter((item) => checked.has(item.id)).map((item) => item.id);
    }
    setBusy(true);
    setError(null);
    createRun({ selection, title: title || t("compose.titlePlaceholder"), dispatch, labels: { origin: "ui" } })
      .then((body) => {
        if (body.spec_document) {
          const blob = new Blob([JSON.stringify(body.spec_document, null, 2)], {
            type: "application/json",
          });
          const link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.download = `${body.run.id}.runspec.json`;
          link.click();
          URL.revokeObjectURL(link.href);
          setNotice(
            t("compose.specExported", { id: body.run.id, dispatch: body.dispatch.dispatch_id })
          );
        } else {
          setNotice(t("compose.runCreated", { id: body.run.id, state: body.run.state }));
        }
      })
      .catch((err) => setError(t("errors.request", { message: err.message })))
      .finally(() => setBusy(false));
  };

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>{t("app.title")}</h1>
          <span className="muted">{t("app.tagline")}</span>
        </div>
        <nav>
          <button className={tab === "compose" ? "tab active" : "tab"} onClick={() => setTab("compose")}>
            {t("nav.compose")}
          </button>
          <button className={tab === "runs" ? "tab active" : "tab"} onClick={() => setTab("runs")}>
            {t("nav.runs")}
          </button>
          <button
            className={tab === "quarantine" ? "tab active" : "tab"}
            onClick={() => setTab("quarantine")}
          >
            {t("nav.quarantine")}
          </button>
        </nav>
        <label className="locale-switch">
          <span className="muted small">{t("language.label")}</span>
          <select value={locale} onChange={(ev) => onLocaleChange(ev.target.value)}>
            {AVAILABLE_LOCALES.map((code) => (
              <option key={code} value={code}>
                {code.toUpperCase()}
              </option>
            ))}
          </select>
        </label>
      </header>

      {error && (
        <p className="banner error" onClick={() => setError(null)}>
          {error}
        </p>
      )}
      {notice && (
        <p className="banner notice" onClick={() => setNotice(null)}>
          {notice}
        </p>
      )}

      {tab === "compose" ? (
        <>
          <main className="compose-grid">
            <TaxonomyTree taxonomy={taxonomy} onPick={pickPattern} />
            <div className="compose-main">
              <FilterPanel
                value={filterValue}
                revision={revision}
                onChange={setFilterValue}
                quickFilters={quickFilters}
                onPickPattern={pickPattern}
                onClear={clearFilter}
              />
              <PreviewTable
                items={items}
                checked={checked}
                onToggle={toggleItem}
                onToggleAll={(all) => setChecked(all ? new Set(items.map((i) => i.id)) : new Set())}
                loading={loading}
                warnings={warnings}
                hasFilter={Boolean(ast)}
              />
            </div>
          </main>
          <ComposeFooter
            runners={runners}
            disabled={!ast || checked.size === 0}
            busy={busy}
            onCompose={compose}
          />
        </>
      ) : tab === "runs" ? (
        <main className="runs-grid">
          <RunsView onError={(message) => setError(message)} />
        </main>
      ) : (
        <main className="runs-grid">
          <QuarantineView
            onError={(message) => setError(message)}
            onNotice={(message) => setNotice(message)}
          />
        </main>
      )}
    </div>
  );
}
