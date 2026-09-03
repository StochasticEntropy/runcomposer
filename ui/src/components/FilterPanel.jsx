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

import { useMemo, useRef } from "react";
import { Locale } from "@svar-ui/react-core";
import { FilterBuilder, Willow, WillowDark } from "@svar-ui/react-filter";
import "@svar-ui/react-filter/all.css";
import { de as coreDe } from "@svar-ui/core-locales";
import { de as filterDe } from "@svar-ui/filter-locales";

import { useI18n } from "../i18n.jsx";

const SVAR_WORDS = { de: { ...coreDe, ...filterDe } }; // en is SVAR's default
const SVAR_OPTIONS = {};

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
}) {
  const { t, locale } = useI18n();
  const fields = useMemo(() => [{ id: "tag", label: t("filter.fieldTag"), type: "text" }], [locale]);
  const SvarTheme = theme === "dark" ? WillowDark : Willow;

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
      options={SVAR_OPTIONS}
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
        <button className="link" onClick={onClear}>
          {t("filter.clear")}
        </button>
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
      <SvarTheme fonts={false}>
        {SVAR_WORDS[locale] ? <Locale words={SVAR_WORDS[locale]}>{builder}</Locale> : builder}
      </SvarTheme>
    </section>
  );
}
