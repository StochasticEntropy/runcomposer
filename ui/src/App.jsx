import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { compileSelection, createRun, getRunners, getTaxonomy } from "./api.js";
import {
  EMPTY_FILTER,
  activePatterns,
  appendSelection,
  listConditions,
  removeConditionAt,
  removePattern,
  svarToAst,
  togglePattern,
} from "./filterAdapter.js";
import { AVAILABLE_LOCALES, useI18n } from "./i18n.jsx";
import { AVAILABLE_THEMES } from "./prefs.js";
import ComposeFooter from "./components/ComposeFooter.jsx";
import FilterPanel from "./components/FilterPanel.jsx";
import PreviewTable from "./components/PreviewTable.jsx";
import QuarantineView from "./components/QuarantineView.jsx";
import RunsView from "./components/RunsView.jsx";
import TagPicker from "./components/TagPicker.jsx";

export default function App({
  uiConfig,
  locale,
  onLocaleChange,
  theme,
  resolvedTheme,
  onThemeChange,
}) {
  const { t } = useI18n();
  const [tab, setTab] = useState("compose");
  const [taxonomy, setTaxonomy] = useState([]);
  const [tags, setTags] = useState([]);
  const [runners, setRunners] = useState([]);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [picking, setPicking] = useState(false);

  const [filterValue, setFilterValue] = useState(EMPTY_FILTER);
  const [historyQuery, setHistoryQuery] = useState(null); // §7 compose-time provider
  const [items, setItems] = useState([]);
  const [checked, setChecked] = useState(new Set());
  const [warnings, setWarnings] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const debounceRef = useRef(null);
  // Closing the picker has to put focus back where it came from. The dialog
  // restores focus to whatever was focused when it opened — which on macOS is
  // nothing, because clicking a button there does not focus it, so a keyboard
  // user was dropped on <body> and had to tab back through the whole page.
  // This runs as a passive effect on the way down, i.e. AFTER the dialog's own
  // layout-effect restore, which is the only ordering that wins.
  const pickerTrigger = useRef(null);
  const wasPicking = useRef(false);

  const ast = useMemo(() => svarToAst(filterValue), [filterValue]);
  // What the filter currently holds, in the app's own words: the removable
  // conditions the panel lists, and the patterns the picker marks as already
  // held.
  const conditions = useMemo(() => listConditions(filterValue), [filterValue]);
  const active = useMemo(() => activePatterns(filterValue), [filterValue]);
  const quickFilters = uiConfig.quick_filters ?? [];

  useEffect(() => {
    getTaxonomy()
      .then((body) => {
        setTaxonomy(body.taxonomy ?? []);
        setTags(body.tags ?? []);
      })
      .catch((err) => setError(err.message));
    getRunners()
      .then((all) => setRunners(all.filter((runner) => !runner.error)))
      .catch((err) => setError(err.message));
  }, []);

  const selectionPayload = () => {
    const selection = {};
    if (ast) selection.tag_filter = ast;
    if (historyQuery) selection.history = historyQuery;
    return selection;
  };

  // Auto-compiled preview (DESIGN.md §10): every filter edit recompiles.
  useEffect(() => {
    if (!ast && !historyQuery) {
      setItems([]);
      setChecked(new Set());
      setWarnings([]);
      return undefined;
    }
    setLoading(true);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      compileSelection(selectionPayload())
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
  }, [ast, historyQuery]);

  // A quick filter is a switch: clicking one that is already in the filter
  // takes it back out instead of adding a duplicate.
  const pickPattern = (pattern) => {
    setFilterValue((value) => togglePattern(value, pattern));
    setTab("compose");
  };

  // The picker's whole selection arrives as one edit: the patterns it staged
  // for removal come out, and everything it picked goes back in as ONE group
  // (filterAdapter.js). Removal runs first, so a pattern both removed and
  // picked ends up in the new group rather than in its old place — which is
  // how a condition is moved into a group.
  const applySelection = (patterns, options, removals = []) => {
    setFilterValue((value) =>
      appendSelection(removals.reduce(removePattern, value), patterns, options)
    );
  };

  const closePicker = useCallback(() => setPicking(false), []);

  useEffect(() => {
    if (wasPicking.current && !picking) pickerTrigger.current?.focus();
    wasPicking.current = picking;
  }, [picking]);

  const removeCondition = (index) => {
    setFilterValue((value) => removeConditionAt(value, index));
  };

  const clearFilter = () => {
    setFilterValue(EMPTY_FILTER);
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
    const selection = selectionPayload();
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
        <label className="header-switch">
          <span className="muted small">{t("language.label")}</span>
          <select value={locale} onChange={(ev) => onLocaleChange(ev.target.value)}>
            {AVAILABLE_LOCALES.map((code) => (
              <option key={code} value={code}>
                {code.toUpperCase()}
              </option>
            ))}
          </select>
        </label>
        <label className="header-switch">
          <span className="muted small">{t("theme.label")}</span>
          <select value={theme} onChange={(ev) => onThemeChange(ev.target.value)}>
            {AVAILABLE_THEMES.map((name) => (
              <option key={name} value={name}>
                {t(`theme.${name}`)}
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
            <div className="compose-main">
              <FilterPanel
                value={filterValue}
                theme={resolvedTheme}
                onChange={setFilterValue}
                conditions={conditions}
                onRemoveCondition={removeCondition}
                activePatterns={active}
                quickFilters={quickFilters}
                onPickPattern={pickPattern}
                onClear={clearFilter}
                historyQuery={historyQuery}
                onHistory={setHistoryQuery}
                onPickTags={() => setPicking(true)}
                pickTagsRef={pickerTrigger}
                tags={tags}
                ast={ast}
              />
              <PreviewTable
                items={items}
                checked={checked}
                onToggle={toggleItem}
                onToggleAll={(all) => setChecked(all ? new Set(items.map((i) => i.id)) : new Set())}
                loading={loading}
                warnings={warnings}
                hasFilter={Boolean(ast) || Boolean(historyQuery)}
              />
            </div>
          </main>
          <TagPicker
            open={picking}
            taxonomy={taxonomy}
            activePatterns={active}
            onClose={closePicker}
            onApply={applySelection}
          />
          <ComposeFooter
            runners={runners}
            disabled={(!ast && !historyQuery) || checked.size === 0}
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
