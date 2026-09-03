// The one component that talks to the SVAR filter widget (DESIGN.md §10:
// SVAR stays behind the adapter so it remains swappable). SVAR ships its own
// label localization (@svar-ui/*-locales, de included) — the P1 spike
// outcome: built-in localization exists, no override needed. It also ships its
// own themes, which do NOT follow the page: the widget renders light inside a
// dark app unless the dark variant is selected explicitly, so the app hands it
// the resolved theme.
//
// Two things the widget does not give us, both handled here:
//
//  * Its per-rule controls hang off an icon-font glyph the package does not
//    ship (no @font-face for `wxi` anywhere in @svar-ui) — the menu that holds
//    Edit/Delete/Add group renders as a blank 20px box, so removing a single
//    condition was a hidden click. styles.css draws those glyphs; the chip row
//    below adds a one-click ✕ per condition on top, which is the affordance a
//    first-time user actually finds.
//  * A changing `value` prop re-seeds the widget's store. The `shown` ref below
//    keeps the object identity stable for changes the widget itself just made,
//    so only genuinely external edits (taxonomy, quick filter, ✕, Clear) reach
//    it — where a remount used to throw the whole widget away on every click.

import { useCallback, useMemo, useRef, useState } from "react";
import { Locale } from "@svar-ui/react-core";
import { FilterBuilder, Willow, WillowDark } from "@svar-ui/react-filter";
import "@svar-ui/react-filter/all.css";
import { de as coreDe } from "@svar-ui/core-locales";
import { de as filterDe } from "@svar-ui/filter-locales";

import { formatAst } from "../filterAdapter.js";
import { useI18n } from "../i18n.jsx";

const SVAR_WORDS = { de: { ...coreDe, ...filterDe } }; // en is SVAR's default
const NO_OPTIONS = {};

export default function FilterPanel({
  value,
  theme,
  onChange,
  conditions,
  onRemoveCondition,
  activePatterns,
  quickFilters,
  onPickPattern,
  onClear,
  historyQuery,
  onHistory,
  onPickTags,
  pickTagsRef,
  tags,
  ast,
}) {
  const { t, locale } = useI18n();
  const [showSummary, setShowSummary] = useState(false);
  const fields = useMemo(() => [{ id: "tag", label: t("filter.fieldTag"), type: "text" }], [locale]);
  const SvarTheme = theme === "dark" ? WillowDark : Willow;

  // The widget is deliberately given NO options, and that is the opposite of
  // an oversight. Handed the catalog's tags it renders them as its own value
  // editor: a flat, unsorted, unsearchable scrolling list of all 1609 with a
  // checkbox each, four rows visible at a time. It is not a typeahead — it
  // narrows only on an exact match — so it is the very list the picker exists
  // to replace, reached from the panel's most obvious button. Choosing tags
  // happens in the dialog; this widget stays what it is good at, which is
  // showing and editing the resulting expression.
  //
  // `tags` still arrives as a prop: the picker is what consumes it (App.jsx).

  // Adding a filter opens the PICKER. The widget's own add puts up a rule
  // editor whose value field, once it is given the catalog's tags, is a flat
  // scrolling list of every tag in the corpus with a checkbox each — 1609 of
  // them, unsorted into anything, no search. That is precisely the problem the
  // picker exists to solve, so reaching it by the most obvious button in the
  // panel is not acceptable.
  //
  // Every `add-rule` is intercepted, not just the toolbar's. The two cannot be
  // told apart anyway: in @svar-ui/react-filter 2.6.0 the toolbar's add and a
  // row menu's add both send `{rule, edit}` where `rule` is the click's own
  // React event object (probed live), so neither names the group it was aimed
  // at. Where the new group lands is therefore the picker's own "join with the
  // current filter" choice — visible and stated, rather than an invisible
  // click position.
  //
  // The raw path is not lost with it: the row menu's `Bearbeiten` still opens
  // that editor on an existing rule, and its value field takes any pattern the
  // grammar accepts, `regex:` and `prefix:` included. So a technical user picks
  // anything and edits it into the expression they want.
  //
  // `init` runs once per mount with a stable callback, so this does not
  // reintroduce the remount 0.1.5 removed — `key` still depends on locale and
  // theme alone.
  const wantPicker = useRef(onPickTags);
  wantPicker.current = onPickTags;
  const init = useCallback((api) => {
    api.detach("runcomposer-tag-picker");
    api.intercept(
      "add-rule",
      () => {
        wantPicker.current();
        return false;
      },
      { tag: "runcomposer-tag-picker" }
    );
  }, []);

  // Identity, not equality: the widget re-reads its value whenever the prop is
  // a different object. For an edit it made itself it already holds the state,
  // so we keep handing it the object it knows — and it keeps its own (an open
  // rule editor, a menu) instead of being re-seeded on every keystroke.
  //
  // A remount is the exception: a fresh widget holds nothing, so it has to be
  // handed the current value. Both the locale (SVAR reads its labels once, at
  // mount) and the theme (Willow and WillowDark are different components, so
  // switching swaps the subtree) remount it.
  const mountKey = `${locale}-${theme}`;
  const emitted = useRef(null);
  const shown = useRef(value);
  const mounted = useRef(mountKey);
  if (value !== emitted.current || mountKey !== mounted.current) {
    shown.current = value;
    mounted.current = mountKey;
  }

  const conditionLabel = (condition) => {
    if (condition.kind !== "group") {
      return `${t("filter.fieldTag")} ${t(`filter.operator.${condition.operator}`)} ${condition.value}`;
    }
    return condition.count === 1
      ? t("filter.conditionGroupOne")
      : t("filter.conditionGroup", { count: condition.count });
  };

  const builder = (
    <FilterBuilder
      key={mountKey}
      init={init}
      fields={fields}
      options={NO_OPTIONS}
      value={shown.current}
      type="list"
      onChange={(ev) => {
        emitted.current = ev.value;
        onChange(ev.value);
      }}
    />
  );

  return (
    <section className="panel filter-panel">
      <header className="panel-header">
        <h2>{t("filter.title")}</h2>
        <div className="panel-actions">
          {/* The way into the taxonomy. It was an always-visible sidebar until
              the tree was resolved against the catalog and grew to thousands of
              rows; the picker behind this button is what a tree that size needs
              (TagPicker.jsx), and it is the primary action of this panel. */}
          <button className="primary" ref={pickTagsRef} onClick={onPickTags}>
            {t("picker.open")}
          </button>
          <button className="link" onClick={onClear}>
            {t("filter.clear")}
          </button>
        </div>
      </header>
      {quickFilters.length > 0 && (
        <div className="quick-filters">
          <span className="muted">{t("filter.quickFilters")}:</span>
          {quickFilters.map((quick) => {
            const on = activePatterns.has(quick.filter);
            return (
              <button
                key={quick.label}
                className={on ? "chip chip-active" : "chip"}
                aria-pressed={on}
                onClick={() => onPickPattern(quick.filter)}
              >
                {quick.label}
                {on ? " ✕" : ""}
              </button>
            );
          })}
        </div>
      )}
      <div className="quick-filters">
        <span className="muted">{t("filter.historyLabel")}:</span>
        {historyQuery ? (
          <button className="chip chip-active" onClick={() => onHistory(null)} title={historyQuery}>
            {t("filter.historyActive", { query: historyQuery })} ✕
          </button>
        ) : (
          <button className="chip" onClick={() => onHistory("failed@latest")}>
            {t("filter.historyRerunFailed")}
          </button>
        )}
      </div>
      {conditions.length > 0 && (
        <div className="quick-filters filter-conditions">
          <span className="muted">{t("filter.conditions")}:</span>
          {conditions.map((condition) => {
            const label = conditionLabel(condition);
            return (
              <button
                key={condition.index}
                className="chip chip-active"
                title={t("filter.removeCondition", { condition: label })}
                aria-label={t("filter.removeCondition", { condition: label })}
                onClick={() => onRemoveCondition(condition.index)}
              >
                {label} ✕
              </button>
            );
          })}
        </div>
      )}
      {ast && (
        // The builder shows widgets; this shows the sentence they add up to.
        // It is the only place the filter's *structure* is legible — a chip
        // row flattens it, and a nested group is exactly what the picker
        // makes easy to build. Wording is the runspec grammar's own, so what
        // is read here is what the run spec will carry (DESIGN.md §3.1).
        <details
          className="filter-summary"
          open={showSummary}
          onToggle={(ev) => setShowSummary(ev.currentTarget.open)}
        >
          <summary>{t("filter.summary")}</summary>
          <p className="mono">
            {formatAst(ast, { and: t("picker.and"), or: t("picker.or"), not: t("picker.not") })}
          </p>
        </details>
      )}
      <SvarTheme fonts={false}>
        {SVAR_WORDS[locale] ? <Locale words={SVAR_WORDS[locale]}>{builder}</Locale> : builder}
      </SvarTheme>
    </section>
  );
}
