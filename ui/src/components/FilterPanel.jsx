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

import { useMemo, useRef, useState } from "react";
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

  // The value field completes against the real catalog tags. Until this was
  // wired the widget was handed an empty options map, so typing a tag was
  // blind and you had to know its spelling by heart — which on a corpus of
  // 1609 tags is not a thing anyone knows. The list comes back from the same
  // resolution the picker's tree does (api.js), so there is one source for
  // "which tags exist" and it is the server's.
  //
  // Identity matters here for the same reason it matters for `value`: a fresh
  // object every render re-seeds the widget's store.
  const options = useMemo(() => (tags?.length ? { tag: tags } : NO_OPTIONS), [tags]);

  // NOT DONE, and the reason is worth keeping: the picker cannot take over the
  // widget's own "add a filter" action. Intercepting `add-rule` works, but the
  // payload does not say WHERE the rule was going: in @svar-ui/react-filter
  // 2.6.0 both the toolbar's add and a row menu's add send `{rule, edit}` where
  // `rule` is the click's own React event object (probed live: the keys are
  // ["rule","edit"] and `rule.nativeEvent` is set for both). So the target
  // group is not knowable, the two adds are indistinguishable, and an
  // interception that always appended at the top level would silently break
  // the "add a group, then add a rule inside it" path 0.1.5 deliberately
  // opened up. The picker therefore sits BESIDE the widget's own add rather
  // than replacing it — as the panel's primary action, one line above it — and
  // the raw row is no longer blind either, now that the value field completes
  // against the catalog's real tags.

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
      fields={fields}
      options={options}
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
