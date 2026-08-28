// The one component that talks to the SVAR filter widget (DESIGN.md §10:
// SVAR stays behind the adapter so it remains swappable). SVAR ships its own
// label localization (@svar-ui/*-locales, de included) — the P1 spike
// outcome: built-in localization exists, no override needed. It also ships its
// own themes, which do NOT follow the page: the widget renders light inside a
// dark app unless the dark variant is selected explicitly, so the app hands it
// the resolved theme.

import { Locale } from "@svar-ui/react-core";
import { FilterBuilder, Willow, WillowDark } from "@svar-ui/react-filter";
import "@svar-ui/react-filter/all.css";
import { de as coreDe } from "@svar-ui/core-locales";
import { de as filterDe } from "@svar-ui/filter-locales";

import { useI18n } from "../i18n.jsx";

const SVAR_WORDS = { de: { ...coreDe, ...filterDe } }; // en is SVAR's default

export default function FilterPanel({
  value,
  revision,
  theme,
  onChange,
  quickFilters,
  onPickPattern,
  onClear,
  historyQuery,
  onHistory,
}) {
  const { t, locale } = useI18n();
  const fields = [{ id: "tag", label: t("filter.fieldTag"), type: "text" }];
  const SvarTheme = theme === "dark" ? WillowDark : Willow;

  const builder = (
    <FilterBuilder
      key={`${locale}-${revision}`} /* remount on external edits + locale switch */
      fields={fields}
      options={{}}
      value={value}
      type="list"
      onChange={(ev) => onChange(ev.value)}
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
          {quickFilters.map((quick) => (
            <button key={quick.label} className="chip" onClick={() => onPickPattern(quick.filter)}>
              {quick.label}
            </button>
          ))}
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
      <SvarTheme fonts={false}>
        {SVAR_WORDS[locale] ? <Locale words={SVAR_WORDS[locale]}>{builder}</Locale> : builder}
      </SvarTheme>
    </section>
  );
}
