// Runs: every stored run, newest first, with what a tester scans for — is it
// done, how did it go, how big was it, who ran it. Search, state and label
// filters and the page live in the URL. Rows of runs in flight update on
// their own; nothing is asked of the server while the tab is hidden.

import { useCallback, useEffect, useRef, useState } from "react";

import { EmptyState, ErrorState, LabelChips, Loading, Notice, ResultBadge, StateBadge, TimeAgo, VerdictBar, VerdictCounts, ErrorText } from "../components/ui.tsx";
import { useApp, useI18n } from "../hooks/context.tsx";
import { sleep, usePageVisible } from "../hooks/usePage.ts";
import { useResource } from "../hooks/useResource.ts";
import { navigate } from "../hooks/useRoute.ts";
import { runListPath } from "../lib/api.ts";
import { takeFlash } from "../lib/flash.ts";
import type { Flash } from "../lib/flash.ts";
import { formatDuration, parseLabel } from "../lib/format.ts";
import { composeHref, runHref, runsHref } from "../lib/router.ts";
import type { Route } from "../lib/router.ts";
import { isActive } from "../lib/types.ts";
import type { RunRow } from "../lib/types.ts";

const PAGE_SIZE = 25;
const ACTIVE_POLL_MS = 3000;
const IDLE_POLL_MS = 20_000;

/** The runner a run went to, or how else it came to have results. */
export function ModeName({ mode }: { mode: string | null }) {
  const { t } = useI18n();
  if (mode === null) return <span className="muted">{t("runs.notDispatched")}</span>;
  if (mode === "export") return <>{t("mode.export")}</>;
  if (mode === "ingest") return <>{t("mode.ingest")}</>;
  return <>{mode}</>;
}

export function plannedVerdicts(run: RunRow): number {
  const latest = run.dispatches[run.dispatches.length - 1];
  return latest?.planned_verdicts ?? run.planned_count;
}

export function RunsView({ route }: { route: Route }) {
  const { api } = useApp();
  const { t, tOr, locale } = useI18n();
  const { meta } = useApp();
  const visible = usePageVisible();
  const [flash, setFlashState] = useState<Flash | null>(() => takeFlash());

  const q = route.query.q ?? "";
  const state = route.query.state ?? "";
  const label = route.query.label ?? "";
  const offset = Math.max(0, Number.parseInt(route.query.offset ?? "0", 10) || 0);
  const labelValid = label === "" || parseLabel(label) !== null;
  const query = { q, state, labels: label && labelValid ? [label] : [], limit: PAGE_SIZE, offset };
  const key = runListPath(query);

  const list = useResource(
    useCallback((signal: AbortSignal) => api.listRuns(query, signal), [api, key]), // eslint-disable-line react-hooks/exhaustive-deps
    key,
  );

  // Typing in the search box follows the URL with a short delay.
  const [search, setSearch] = useState(q);
  const [labelDraft, setLabelDraft] = useState(label);
  useEffect(() => setSearch(q), [q]);
  useEffect(() => setLabelDraft(label), [label]);
  const go = useCallback(
    (changes: Record<string, string | number | null>, replace = false) => {
      navigate(runsHref({ q, state, label, offset: offset || null, ...changes }), { replace });
    },
    [q, state, label, offset],
  );
  useEffect(() => {
    if (search === q) return;
    const timer = window.setTimeout(() => go({ q: search, offset: null }, true), 250);
    return () => window.clearTimeout(timer);
  }, [search, q, go]);

  // Live rows: ask again while something is in flight, slowly otherwise, and
  // not at all while the tab is hidden. One request at a time, by construction.
  const anyActive = (list.data?.runs ?? []).some((run) => isActive(run.state));
  const reloadRef = useRef(list.reload);
  useEffect(() => {
    reloadRef.current = list.reload;
  });
  const wasHidden = useRef(false);
  useEffect(() => {
    if (!visible) {
      wasHidden.current = true;
      return;
    }
    const controller = new AbortController();
    void (async () => {
      try {
        if (wasHidden.current) {
          wasHidden.current = false;
          await reloadRef.current();
        }
        while (!controller.signal.aborted) {
          await sleep(anyActive ? ACTIVE_POLL_MS : IDLE_POLL_MS, controller.signal);
          await reloadRef.current();
        }
      } catch {
        /* the view went away, the tab was hidden, or the pace changed */
      }
    })();
    return () => controller.abort();
    // `anyActive` restarts the loop at the pace the list now needs.
  }, [visible, key, anyActive]);

  useEffect(() => {
    document.title = `${t("nav.runs")} · runcomposer`;
  }, [t]);

  const filtered = Boolean(q || state || label);
  const data = list.data;
  const total = data?.total ?? 0;
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(total, offset + (data?.runs.length ?? 0));

  return (
    <div className="page">
      <header className="page-head">
        <h1>{t("runs.title")}</h1>
        <button type="button" className="btn btn-small" onClick={() => void list.reload()} disabled={list.loading}>
          {t("action.refresh")}
        </button>
      </header>

      {flash && (
        <Notice tone={flash.tone} onDismiss={() => setFlashState(null)}>
          {flash.text}
        </Notice>
      )}

      <form className="toolbar" role="search" onSubmit={(event) => event.preventDefault()}>
        <label className="field field-grow">
          <span className="field-label">{t("runs.search")}</span>
          <input
            type="search"
            className="input"
            value={search}
            placeholder={t("runs.searchPlaceholder")}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">{t("runs.state")}</span>
          <select className="input" value={state} onChange={(event) => go({ state: event.target.value, offset: null })}>
            <option value="">{t("runs.anyState")}</option>
            {meta.states.map((value) => (
              <option key={value} value={value}>
                {tOr(`runState.${value}`, value)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">{t("runs.label")}</span>
          <input
            type="text"
            className="input"
            value={labelDraft}
            placeholder={t("labels.placeholder")}
            aria-invalid={!labelValid || undefined}
            onChange={(event) => setLabelDraft(event.target.value)}
            onBlur={() => labelDraft !== label && go({ label: labelDraft.trim(), offset: null })}
            onKeyDown={(event) => {
              if (event.key === "Enter") go({ label: labelDraft.trim(), offset: null });
            }}
          />
        </label>
        {filtered && (
          <button type="button" className="btn btn-small btn-quiet" onClick={() => navigate(runsHref())}>
            {t("runs.clearFilters")}
          </button>
        )}
      </form>
      {!labelValid && (
        <p className="field-error" role="alert">
          {t("labels.invalid")}
        </p>
      )}

      {list.error && data && (
        <Notice tone="error">
          {t("runs.refreshFailed")} <ErrorText error={list.error} />{" "}
          <button type="button" className="btn btn-small" onClick={() => void list.reload()}>
            {t("action.retry")}
          </button>
        </Notice>
      )}

      {!data ? (
        list.error ? (
          <ErrorState error={list.error} onRetry={() => void list.reload()} />
        ) : (
          <Loading />
        )
      ) : data.runs.length === 0 ? (
        filtered ? (
          <EmptyState title={t("runs.noMatch")}>
            <button type="button" className="btn" onClick={() => navigate(runsHref())}>
              {t("runs.clearFilters")}
            </button>
          </EmptyState>
        ) : (
          <EmptyState title={t("runs.empty")}>
            <a className="btn btn-primary" href={composeHref()}>
              {t("runs.composeFirst")}
            </a>
          </EmptyState>
        )
      ) : (
        <>
          <div className="table-scroll">
            <table className="table runs-table cards-on-narrow">
              <thead>
                <tr>
                  <th scope="col">{t("runs.column.run")}</th>
                  <th scope="col">{t("runs.column.status")}</th>
                  <th scope="col">{t("runs.column.results")}</th>
                  <th scope="col" className="num">
                    {t("runs.column.items")}
                  </th>
                  <th scope="col" className="num">
                    {t("runs.column.duration")}
                  </th>
                  <th scope="col">{t("runs.column.runner")}</th>
                  <th scope="col">{t("runs.column.started")}</th>
                </tr>
              </thead>
              <tbody>
                {data.runs.map((run) => (
                  <tr key={run.id} data-run-id={run.id} data-state={run.state}>
                    <td className="col-run">
                      <a href={runHref(run.id)} className="run-link">
                        {run.title || run.id}
                      </a>
                      <span className="cell-sub">
                        <span className="mono">{run.id}</span>
                        {run.origin && <span> · {tOr(`origin.${run.origin}`, run.origin)}</span>}
                      </span>
                      <LabelChips labels={run.labels} />
                    </td>
                    <td data-label={t("runs.column.status")}>
                      <span className="badges">
                        <StateBadge state={run.state} />
                        <ResultBadge result={run.completion} />
                      </span>
                    </td>
                    <td data-label={t("runs.column.results")} className="col-results">
                      <VerdictBar counts={run.verdict_summary} planned={plannedVerdicts(run)} active={isActive(run.state)} />
                      <VerdictCounts counts={run.verdict_summary} />
                    </td>
                    <td data-label={t("runs.column.items")} className="num">
                      {run.planned_count}
                    </td>
                    <td data-label={t("runs.column.duration")} className="num">
                      {run.duration_s === null ? "–" : formatDuration(run.duration_s * 1000, locale)}
                    </td>
                    <td data-label={t("runs.column.runner")}>
                      <ModeName mode={run.mode} />
                      {run.dispatch_count > 1 && <span className="muted"> ×{run.dispatch_count}</span>}
                    </td>
                    <td data-label={t("runs.column.started")}>
                      <TimeAgo iso={run.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pager" aria-label={t("runs.pages")}>
            <span role="status">{t("runs.range", { first, last, total })}</span>
            <button type="button" className="btn btn-small" disabled={offset === 0} onClick={() => go({ offset: Math.max(0, offset - PAGE_SIZE) || null })}>
              {t("runs.previous")}
            </button>
            <button type="button" className="btn btn-small" disabled={last >= total} onClick={() => go({ offset: offset + PAGE_SIZE })}>
              {t("runs.next")}
            </button>
          </nav>
        </>
      )}
    </div>
  );
}
