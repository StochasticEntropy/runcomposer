// The one component that talks to the SVAR filter widget (DESIGN.md §10:
// SVAR stays behind the adapter so it remains swappable). SVAR ships its own
// label localization (@svar-ui/*-locales, de included) — the P1 spike
// outcome: built-in localization exists, no override needed.

import { Locale } from "@svar-ui/react-core";
import { FilterBuilder, Willow } from "@svar-ui/react-filter";
import "@svar-ui/react-filter/all.css";
import { de as coreDe } from "@svar-ui/core-locales";
import { de as filterDe } from "@svar-ui/filter-locales";

import { useI18n } from "../i18n.jsx";

const SVAR_WORDS = { de: { ...coreDe, ...filterDe } }; // en is SVAR's default

export default function FilterPanel({ value, revision, onChange, quickFilters, onPickPattern, onClear }) {
  const { t, locale } = useI18n();
  const fields = [{ id: "tag", label: t("filter.fieldTag"), type: "text" }];

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
      <Willow fonts={false}>
        {SVAR_WORDS[locale] ? <Locale words={SVAR_WORDS[locale]}>{builder}</Locale> : builder}
      </Willow>
    </section>
  );
}
