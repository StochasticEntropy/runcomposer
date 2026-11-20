// The preview: the items the selection matches, searchable, sortable,
// groupable by suite — and the place to untick the ones that should not run.
// However many items match, only the rows in view are in the DOM.
//
// When the rows on screen are not the answer to the selection in the builder
// (a request is under way, or the last one failed) they are marked stale:
// dimmed, not editable, and said to be so.

import { useMemo, useState } from "react";
import type { ReactNode } from "react";

import { useI18n } from "../hooks/context.tsx";
import { useMediaQuery } from "../hooks/usePage.ts";
import { useVirtual } from "../hooks/useVirtual.ts";
import { isFresh, previewRows, tristate } from "../lib/compose.ts";
import type { PreviewSort, PreviewState } from "../lib/compose.ts";
import { naturalCompare } from "../lib/format.ts";
import type { CatalogItem } from "../lib/types.ts";
import { ErrorState, Loading, TriCheckbox } from "./ui.tsx";

interface Props {
  state: PreviewState;
  excluded: ReadonlySet<string>;
  onExcludedChange: (next: Set<string>) => void;
  onRetry: () => void;
  /** Shown instead of the table while there is no selection at all. */
  landing: ReactNode;
  /** The error is about the filter and is shown next to it; here it is only said that the rows are old. */
  filterError: boolean;
}

export function PreviewTable({ state, excluded, onExcludedChange, onRetry, landing, filterError }: Props) {
  const { t } = useI18n();
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<PreviewSort>("catalog");
  const [grouped, setGrouped] = useState(false);
  const narrow = useMediaQuery("(max-width: 700px)");
  const rowHeight = narrow ? 56 : 36;

  const fresh = isFresh(state);
  const items = useMemo(() => state.result?.items ?? [], [state.result]);
  const rows = useMemo(
    () => previewRows(items, { search, sort, grouped, compare: naturalCompare }),
    [items, search, sort, grouped],
  );
  const visibleItems = useMemo(() => rows.filter((row) => row.kind === "item").map((row) => row.item!), [rows]);
  const virtual = useVirtual<HTMLDivElement>(rows.length, rowHeight, 12, 37);

  const excludedHere = useMemo(() => items.filter((item) => excluded.has(item.id)).length, [items, excluded]);
  const excludedElsewhere = excluded.size - excludedHere;

  const setMany = (targets: CatalogItem[], selected: boolean) => {
    const next = new Set(excluded);
    for (const item of targets) {
      if (selected) next.delete(item.id);
      else next.add(item.id);
    }
    onExcludedChange(next);
  };

  if (state.status === "idle") return <section className="panel preview">{landing}</section>;

  const header = (
    <header className="panel-head">
      <h2>{t("preview.title")}</h2>
      {state.result && (
        <p className="preview-count" data-testid="preview-count">
          {t("preview.matched", { count: state.result.count, total: state.result.total })}
          {excludedHere > 0 && <> · {t("preview.excluded", { count: excludedHere })}</>}
        </p>
      )}
      {!fresh && state.status === "loading" && state.result && (
        <span className="stale-flag" role="status">
          <span className="spinner" aria-hidden="true" /> {t("preview.updating")}
        </span>
      )}
    </header>
  );

  if (!state.result) {
    return (
      <section className="panel preview" aria-busy={state.status === "loading"}>
        {header}
        {state.status === "error" && state.error ? (
          filterError ? (
            <p className="state state-empty">{t("preview.fixFilter")}</p>
          ) : (
            <ErrorState error={state.error} onRetry={onRetry} />
          )
        ) : (
          <Loading label={t("preview.loading")} />
        )}
      </section>
    );
  }

  return (
    <section className="panel preview" aria-busy={state.status === "loading"}>
      {header}
      {state.status === "error" && state.error && (
        <div className="preview-stale-error">
          {!filterError && <ErrorState error={state.error} onRetry={onRetry} compact />}
          <p className="hint" role="status">
            {t("preview.staleHint")}
          </p>
        </div>
      )}
      {state.result.warnings.length > 0 && fresh && state.result.count > 0 && (
        <p className="notice notice-warn" role="status">
          {state.result.warnings.join(" · ")}
        </p>
      )}

      <div className="toolbar">
        <label className="field field-grow">
          <span className="sr-only">{t("preview.search")}</span>
          <input
            type="search"
            className="input"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t("preview.searchPlaceholder")}
            aria-label={t("preview.search")}
          />
        </label>
        <label className="field field-inline">
          <span>{t("preview.sort")}</span>
          <select className="input" value={sort} onChange={(event) => setSort(event.target.value as PreviewSort)}>
            <option value="catalog">{t("preview.sortBy.catalog")}</option>
            <option value="id">{t("preview.sortBy.id")}</option>
            <option value="name">{t("preview.sortBy.name")}</option>
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={grouped} onChange={(event) => setGrouped(event.target.checked)} />
          {t("preview.group")}
        </label>
      </div>

      {(excluded.size > 0 || search.trim()) && (
        <p className="preview-note">
          {search.trim() && <span>{t("preview.shown", { count: visibleItems.length, total: items.length })}</span>}
          {excludedElsewhere > 0 && <span>{t("preview.excludedElsewhere", { count: excludedElsewhere })}</span>}
          {excluded.size > 0 && (
            <button type="button" className="btn btn-small btn-quiet" onClick={() => onExcludedChange(new Set())}>
              {t("preview.clearExclusions")}
            </button>
          )}
        </p>
      )}

      {items.length === 0 ? (
        <p className="state state-empty">{t("preview.empty")}</p>
      ) : rows.length === 0 ? (
        <p className="state state-empty">{t("preview.noSearchHits")}</p>
      ) : (
        <div
          className={`table-scroll preview-scroll${fresh ? "" : " is-stale"}`}
          ref={virtual.containerRef}
          onScroll={virtual.onScroll}
          tabIndex={0}
          role="group"
          aria-label={t("preview.tableLabel")}
        >
          <table className="table preview-table" aria-rowcount={rows.length + 1}>
            <thead>
              <tr aria-rowindex={1}>
                <th scope="col" className="col-check">
                  <TriCheckbox
                    state={tristate(visibleItems, excluded)}
                    onChange={(checked) => setMany(visibleItems, checked)}
                    label={t("preview.selectAll")}
                    disabled={!fresh}
                  />
                </th>
                <th scope="col">{t("preview.column.name")}</th>
                <th scope="col" className="col-id">
                  {t("preview.column.id")}
                </th>
                <th scope="col" className="col-tags">
                  {t("preview.column.tags")}
                </th>
              </tr>
            </thead>
            <tbody>
              {virtual.padTop > 0 && (
                <tr aria-hidden="true" className="spacer">
                  <td colSpan={4} style={{ height: virtual.padTop }} />
                </tr>
              )}
              {rows.slice(virtual.start, virtual.end).map((row, offset) => {
                const index = virtual.start + offset;
                if (row.kind === "group") {
                  const members = row.members!;
                  return (
                    <tr key={`g:${row.label}`} className="group-row" style={{ height: rowHeight }} aria-rowindex={index + 2}>
                      <td className="col-check">
                        <TriCheckbox
                          state={tristate(members, excluded)}
                          onChange={(checked) => setMany(members, checked)}
                          label={t("preview.selectGroup", { group: row.label })}
                          disabled={!fresh}
                        />
                      </td>
                      <th scope="rowgroup" colSpan={3}>
                        {row.label || t("preview.noSuite")} <span className="muted">· {t("preview.items", { count: members.length })}</span>
                      </th>
                    </tr>
                  );
                }
                const item = row.item!;
                const selected = !excluded.has(item.id);
                return (
                  <tr
                    key={item.id}
                    className={selected ? undefined : "is-excluded"}
                    style={{ height: rowHeight }}
                    aria-rowindex={index + 2}
                    data-item-id={item.id}
                  >
                    <td className="col-check">
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={!fresh}
                        aria-label={t("preview.selectItem", { item: item.name || item.id })}
                        onChange={(event) => setMany([item], event.target.checked)}
                      />
                    </td>
                    <td className="col-name">
                      <span className="cell-main">{item.name || item.id}</span>
                      <span className="cell-sub mono">{item.id}</span>
                    </td>
                    <td className="col-id mono">{item.id}</td>
                    <td className="col-tags">
                      <span className="tag-list">{item.tags.join(" · ")}</span>
                    </td>
                  </tr>
                );
              })}
              {virtual.padBottom > 0 && (
                <tr aria-hidden="true" className="spacer">
                  <td colSpan={4} style={{ height: virtual.padBottom }} />
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
