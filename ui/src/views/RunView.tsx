// One run, at its own URL: where it stands, what it found, what it was
// composed from, and everything that can be done with it — cancel, run again,
// rerun its failures, load its selection back into the builder, export it,
// close it as it stands, delete it.

import { useEffect, useMemo, useRef, useState } from "react";

import { ExportDialog } from "../components/ExportDialog.tsx";
import { RerunDialog } from "../components/RerunDialog.tsx";
import { RunResults, runProgress } from "../components/RunResults.tsx";
import {
  ConfirmDialog,
  CopyButton,
  Dialog,
  EmptyState,
  ErrorState,
  ErrorText,
  Foreign,
  LabelChips,
  Loading,
  Notice,
  ResultBadge,
  StateBadge,
  TimeAgo,
  VerdictBar,
  VerdictCounts,
} from "../components/ui.tsx";
import { useApp, useI18n } from "../hooks/context.tsx";
import { usePageVisible } from "../hooks/usePage.ts";
import { navigate } from "../hooks/useRoute.ts";
import { useRunFollow } from "../hooks/useRunFollow.ts";
import { dispatchSpecUrl, resultsExportUrl, specUrl } from "../lib/api.ts";
import { formatFilter } from "../lib/filter.ts";
import { useFlash } from "../hooks/useFlash.ts";
import { setFlash } from "../lib/flash.ts";
import { absoluteTime, elapsedMs, formatDuration } from "../lib/format.ts";
import type { Translator } from "../lib/i18n.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { composeHref, runHref, runsHref } from "../lib/router.ts";
import type { Route } from "../lib/router.ts";
import { isActive } from "../lib/types.ts";
import type { Dispatch, RunDetail, RunEvent } from "../lib/types.ts";
import { ModeName } from "./RunsView.tsx";

type Tab = "results" | "selection" | "activity";
const TABS: Tab[] = ["results", "selection", "activity"];

type Pending =
  | { kind: "cancel" }
  | { kind: "delete" }
  | { kind: "finalize" }
  | { kind: "rotate"; dispatch: Dispatch }
  | { kind: "rerun"; scope: "all" | "failed" }
  | { kind: "export" }
  | null;

/** How long the latest dispatch took, in ms. Times are known to the second. */
export function runDuration(run: Pick<RunDetail, "dispatches">): number | null {
  const latest = run.dispatches[run.dispatches.length - 1];
  return latest ? elapsedMs(latest.created_at, latest.completed_at) : null;
}

/** The link that loads this run's selection into the builder. */
export function loadHref(run: RunDetail): string {
  const { tag_filter: filter, item_ids: ids, derived_from: derived } = run.selection;
  const filterText = filter == null ? "" : formatFilter(filter);
  // A filter alone is the whole selection. Anything else — fixed ids, a
  // history query — is pinned as the list of items the run actually had.
  const needsItems = filter == null || ids.length > 0 || derived.length > 0;
  // `like` asks the builder to also take over how this run was run: its
  // runner, the runner's options, its labels.
  return composeHref({ f: filterText || null, from: needsItems ? run.id : null, like: run.id });
}

const FAILING = ["FAIL", "ERROR", "MISSING"];

/** Something in the run's latest dispatch did not pass. */
export const hasFailures = (run: RunDetail): boolean => FAILING.some((status) => (run.verdict_summary[status] ?? 0) > 0);

/** How many ITEMS did not pass — an item that failed on two partitions is one
 * item to rerun. Known when the latest dispatch is the one on screen. */
export function failedItems(run: RunDetail): number | null {
  const latest = run.dispatches[run.dispatches.length - 1];
  if (!latest || latest.dispatch_id !== run.dispatch_id) return null;
  const newest = new Map<string, { attempt: number; status: string }>();
  for (const row of run.verdicts) {
    if (row.live) continue;
    const slot = `${row.shard}\u0000${row.item_id}`;
    const held = newest.get(slot);
    if (!held || row.attempt >= held.attempt) newest.set(slot, { attempt: row.attempt, status: row.status });
  }
  const failed = new Set<string>();
  for (const [slot, verdict] of newest) if (FAILING.includes(verdict.status)) failed.add(slot.split("\u0000")[1]!);
  return failed.size;
}

export function RunView({ route }: { route: Route }) {
  const { api } = useApp();
  const { t, tOr, locale } = useI18n();
  const runId = route.id;
  const dispatchParam = route.query.d ?? null;
  const tab: Tab = TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : "results";
  const follow = useRunFollow(api, runId, dispatchParam);
  const { run, refresh } = follow;

  const { flash, show: showFlash, dismiss: dismissFlash } = useFlash();
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [newToken, setNewToken] = useState<string | null>(null);
  const [force, setForce] = useState(false);
  const acting = useRef(false);

  // Coming back to the tab: look again, whatever state the run was in.
  const visible = usePageVisible();
  const wasHidden = useRef(false);
  useEffect(() => {
    if (!visible) wasHidden.current = true;
    else if (wasHidden.current) {
      wasHidden.current = false;
      refresh();
    }
  }, [visible, refresh]);

  useEffect(() => {
    document.title = `${run?.title ?? t("run.title")} · runcomposer`;
  }, [run?.title, t]);

  const close = () => {
    setPending(null);
    setActionError(null);
    setForce(false);
  };

  /** Run one action with the double-submit guard; close the dialog when it worked. */
  const act = async (action: () => Promise<void>) => {
    if (acting.current) return;
    acting.current = true;
    setBusy(true);
    setActionError(null);
    try {
      await action();
      close();
    } catch (caught) {
      setActionError(asApiError(caught));
    } finally {
      acting.current = false;
      setBusy(false);
    }
  };

  const progress = useMemo(() => (run ? runProgress(run) : null), [run]);

  if (!run) {
    return (
      <div className="page">
        <p className="crumbs">
          <a href={runsHref()}>← {t("nav.runs")}</a>
        </p>
        {follow.error ? (
          follow.error.status === 404 ? (
            <EmptyState title={t("run.notFound")}>
              <p className="mono">{runId}</p>
              <a className="btn" href={runsHref()}>
                {t("run.backToRuns")}
              </a>
            </EmptyState>
          ) : (
            <ErrorState error={follow.error} onRetry={refresh} />
          )
        ) : (
          <Loading />
        )}
      </div>
    );
  }

  const latest = run.dispatches[run.dispatches.length - 1] ?? null;
  // What this run is a re-run of: the relation the server records (`rerun_of`),
  // named by the title the selection's provenance carries for that run.
  const provenance = run.selection.derived_from.find((entry) => entry.resolved_run_id);
  const rerunId = run.rerun_of?.run_id ?? (run.origin === "rerun" ? provenance?.resolved_run_id : undefined);
  const rerunSource = rerunId
    ? { id: rerunId, title: (provenance?.resolved_run_id === rerunId && provenance.resolved_run_title) || rerunId }
    : null;
  const handedOut = latest !== null && latest.spec_sha256 !== null && isActive(latest.state) ? latest : null;
  const shown = run.dispatches.find((entry) => entry.dispatch_id === run.dispatch_id) ?? null;
  const active = isActive(run.state);
  const failures = failedItems(run);
  const canFinalize = latest !== null && isActive(latest.state) && !latest.executing;
  const tabHref = (name: Tab) => runHref(run.id, { tab: name === "results" ? null : name, d: dispatchParam });

  return (
    <div className="page run-page" data-run-state={run.state}>
      <p className="crumbs">
        <a href={runsHref()}>← {t("nav.runs")}</a>
      </p>

      <header className="run-head">
        <div className="run-title">
          <h1>{run.title || run.id}</h1>
          <span className="badges" data-testid="run-status">
            <StateBadge state={run.state} />
            <ResultBadge result={run.completion} />
          </span>
        </div>
        <dl className="run-facts">
          <div>
            <dt>{t("run.id")}</dt>
            <dd>
              <code>{run.id}</code>
            </dd>
          </div>
          <div>
            <dt>{t("run.started")}</dt>
            <dd>
              <TimeAgo iso={run.created_at} />
            </dd>
          </div>
          {runDuration(run) !== null && (
            <div>
              <dt>{t("run.duration")}</dt>
              <dd>{formatDuration(runDuration(run), locale, { coarse: true })}</dd>
            </div>
          )}
          <div>
            <dt>{t("run.runner")}</dt>
            <dd>
              <ModeName mode={run.mode} />
            </dd>
          </div>
          <div>
            <dt>{t("run.items")}</dt>
            <dd>{run.planned_count}</dd>
          </div>
          <div>
            <dt>{t("run.origin")}</dt>
            <dd>
              {tOr(`origin.${run.origin}`, run.origin)}
            </dd>
          </div>
          {rerunSource && (
            <div data-testid="rerun-of">
              <dt>{t("run.rerunOf")}</dt>
              <dd>
                <a href={runHref(rerunSource.id)}>{rerunSource.title}</a>
              </dd>
            </div>
          )}
          {Object.keys(run.labels).length > 0 && (
            <div>
              <dt>{t("labels.label")}</dt>
              <dd>
                <LabelChips labels={run.labels} />
              </dd>
            </div>
          )}
        </dl>

        <div className="run-actions" role="group" aria-label={t("run.actions")}>
          {active && (
            <button type="button" className="btn btn-danger" onClick={() => setPending({ kind: "cancel" })}>
              {t("run.cancel")}
            </button>
          )}
          {!active && (
            <button type="button" className="btn btn-primary" onClick={() => setPending({ kind: "rerun", scope: "all" })}>
              {latest ? t("run.rerunAll") : t("run.dispatch")}
            </button>
          )}
          {!active && hasFailures(run) && (
            <button type="button" className="btn" onClick={() => setPending({ kind: "rerun", scope: "failed" })}>
              {failures !== null ? t("run.rerunFailed", { count: failures }) : t("run.rerunFailedAny")}
            </button>
          )}
          <a className="btn" href={loadHref(run)}>
            {t("run.load")}
          </a>
          {canFinalize && (
            <button type="button" className="btn" onClick={() => setPending({ kind: "finalize" })}>
              {t("run.finalize")}
            </button>
          )}
          {!active && (
            <button type="button" className="btn" onClick={() => setPending({ kind: "export" })}>
              {t("run.export")}
            </button>
          )}
          <button type="button" className="btn btn-quiet" onClick={refresh}>
            {t("action.refresh")}
          </button>
          {!active && (
            <button type="button" className="btn btn-quiet btn-danger-quiet" onClick={() => setPending({ kind: "delete" })}>
              {t("run.delete")}
            </button>
          )}
        </div>
      </header>

      {flash && (
        <Notice tone={flash.tone} onDismiss={dismissFlash}>
          {flash.text}
        </Notice>
      )}
      {follow.error && (
        <Notice tone="error">
          {t("run.refreshFailed")} <ErrorText error={follow.error} />
        </Notice>
      )}
      {/* A cancel somebody asked for is not an error of the runner. */}
      {latest?.error && run.state !== "CANCELLED" && (
        <Notice tone="error">
          <strong>{t("run.runnerError")}</strong> <Foreign className="pre-line">{latest.error}</Foreign>
        </Notice>
      )}
      {run.state === "CANCELLED" && <Notice tone="info">{t("run.cancelledHint")}</Notice>}
      {latest?.cancel_requested && active && <Notice tone="warn">{t("run.cancelRequested")}</Notice>}
      {run.state === "STALE" && <Notice tone="warn">{t("run.staleHint")}</Notice>}
      {run.state === "AWAITING_RESULTS" && (
        <Notice tone="info">
          <p>{t("run.awaitingHint")}</p>
          {handedOut && (
            <p className="row awaiting-tools">
              <a className="btn btn-small" href={dispatchSpecUrl(run.id, handedOut.dispatch_id)} download>
                {t("run.downloadDocument")}
              </a>
              <button type="button" className="btn btn-small" onClick={() => setPending({ kind: "rotate", dispatch: handedOut })}>
                {t("run.activity.newToken")}
              </button>
            </p>
          )}
        </Notice>
      )}
      {run.state === "COMPOSED" && <Notice tone="info">{t("run.composedHint")}</Notice>}

      {progress && latest && (
        <section className="panel run-progress" aria-label={t("run.progress")}>
          <div className="progress-line">
            <strong data-testid="run-progress" role="status">
              {t("progress.label", { done: progress.done, total: progress.planned })}
            </strong>
            <VerdictCounts counts={progress.counts} />
            {progress.running > 0 && (
              <span className="count-running" data-testid="run-running">
                {t("run.nowRunning", { count: progress.running })}
              </span>
            )}
          </div>
          <VerdictBar counts={progress.counts} planned={progress.planned} active={shown ? isActive(shown.state) : false} />
        </section>
      )}

      {run.dispatches.length > 1 && (
        <label className="field field-inline dispatch-select">
          <span>{t("run.showDispatch")}</span>
          <select
            className="input"
            value={run.dispatch_id ?? ""}
            onChange={(event) => {
              const chosen = event.target.value;
              navigate(runHref(run.id, { tab: tab === "results" ? null : tab, d: chosen === latest?.dispatch_id ? null : chosen }));
            }}
          >
            {run.dispatches.map((dispatch, index) => (
              <option key={dispatch.dispatch_id} value={dispatch.dispatch_id}>
                {t("run.dispatchOption", { number: index + 1, total: run.dispatches.length })} · {dispatch.mode} ·{" "}
                {tOr(`runState.${dispatch.state}`, dispatch.state)}
                {dispatch.completion ? ` · ${tOr(`completion.${dispatch.completion}`, dispatch.completion)}` : ""}
              </option>
            ))}
          </select>
        </label>
      )}

      <nav className="tabs" aria-label={t("run.sections")}>
        {TABS.map((name) => (
          <a key={name} href={tabHref(name)} aria-current={tab === name ? "page" : undefined}>
            {t(`run.tab.${name}`)}
          </a>
        ))}
      </nav>

      {tab === "results" && (
        <RunResults
          run={run}
          query={{ status: route.query.status, group: route.query.group, shard: route.query.shard, q: route.query.q }}
          onQuery={(changes) => navigate(runHref(run.id, { d: dispatchParam, ...route.query, tab: null, ...changes }), { replace: true })}
        />
      )}
      {tab === "selection" && <SelectionTab run={run} shown={shown} />}
      {tab === "activity" && <ActivityTab run={run} onRotate={(dispatch) => setPending({ kind: "rotate", dispatch })} />}

      {pending?.kind === "cancel" && (
        <ConfirmDialog
          title={t("run.cancelTitle")}
          confirmLabel={t("run.cancelConfirm")}
          cancelLabel={t("run.cancelKeep")}
          danger
          busy={busy}
          error={actionError}
          onClose={close}
          onConfirm={() =>
            void act(async () => {
              await api.cancel(run.id, force);
              refresh();
            })
          }
        >
          <p>{t("run.cancelExplain")}</p>
          {latest?.cancel_requested && (
            <label className="check">
              <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
              {t("run.cancelForce")}
            </label>
          )}
        </ConfirmDialog>
      )}
      {pending?.kind === "delete" && (
        <ConfirmDialog
          title={t("run.deleteTitle")}
          confirmLabel={t("run.deleteConfirm")}
          danger
          busy={busy}
          error={actionError}
          onClose={close}
          onConfirm={() =>
            void act(async () => {
              await api.deleteRun(run.id);
              setFlash({ tone: "ok", text: t("run.deleted", { title: run.title }) });
              navigate(runsHref());
            })
          }
        >
          <p>{t("run.deleteExplain", { title: run.title })}</p>
        </ConfirmDialog>
      )}
      {pending?.kind === "finalize" && (
        <ConfirmDialog
          title={t("run.finalizeTitle")}
          confirmLabel={t("run.finalize")}
          busy={busy}
          error={actionError}
          onClose={close}
          onConfirm={() =>
            void act(async () => {
              await api.finalize(run.id);
              refresh();
            })
          }
        >
          <p>{t("run.finalizeExplain")}</p>
        </ConfirmDialog>
      )}
      {pending?.kind === "rotate" && (
        <ConfirmDialog
          title={t("run.rotateTitle")}
          confirmLabel={t("run.rotateConfirm")}
          busy={busy}
          error={actionError}
          onClose={close}
          onConfirm={() =>
            void act(async () => {
              const rotated = await api.rotateToken(run.id, pending.dispatch.dispatch_id);
              setNewToken(rotated.ingest_token ?? "");
            })
          }
        >
          <p>{t("run.rotateExplain")}</p>
        </ConfirmDialog>
      )}
      {newToken !== null && (
        <Dialog title={t("run.rotateDone")} onClose={() => setNewToken(null)} size="small">
          {newToken ? (
            <>
              <Notice tone="warn">{t("export.tokenOnce")}</Notice>
              <div className="secret">
                <code data-testid="rotated-token">{newToken}</code>
                <CopyButton text={newToken} label={t("export.copyToken")} />
              </div>
            </>
          ) : (
            <p>{t("export.noToken")}</p>
          )}
        </Dialog>
      )}
      {pending?.kind === "rerun" && (
        <RerunDialog
          run={run}
          scope={pending.scope}
          onClose={close}
          onDone={(response) => {
            close();
            if (response.run.id !== run.id) {
              setFlash({ tone: "ok", text: t("rerun.startedNew", { count: response.run.planned_count, title: run.title }) });
              navigate(runHref(response.run.id));
            } else {
              showFlash({ tone: "ok", text: t("rerun.started") });
              if (dispatchParam) navigate(runHref(run.id));
              refresh();
            }
          }}
        />
      )}
      {pending?.kind === "export" && (
        <ExportDialog
          intro={t("export.introRun", { title: run.title, count: run.planned_count })}
          perform={(format) => api.dispatch(run.id, { mode: "export", format })}
          onExported={refresh}
          onClose={close}
          linkToRun={false}
        />
      )}
    </div>
  );
}

function SelectionTab({ run, shown }: { run: RunDetail; shown: Dispatch | null }) {
  const { t, tOr, locale } = useI18n();
  const { runners } = useApp();
  // A parameter is shown under the label its runner declares, when that runner is still configured.
  const declared = runners.find((runner) => runner.id === shown?.mode)?.parameters ?? [];
  const labelOf = (name: string) => declared.find((spec) => spec.name === name)?.label ?? name;
  const { tag_filter: filter, item_ids: ids, derived_from: derived } = run.selection;
  const parameters = Object.entries(shown?.parameters ?? {});
  return (
    <div className="selection-tab">
      <section className="panel">
        <header className="panel-head">
          <h2>{t("run.selection.title")}</h2>
          <a className="btn btn-small" href={loadHref(run)}>
            {t("run.load")}
          </a>
        </header>
        <dl className="facts">
          <dt>{t("run.selection.filter")}</dt>
          <dd>{filter == null ? <span className="muted">{t("run.selection.noFilter")}</span> : <code data-testid="run-filter">{formatFilter(filter)}</code>}</dd>
          {ids.length > 0 && (
            <>
              <dt>{t("run.selection.fixed")}</dt>
              <dd>{t("run.selection.fixedCount", { count: ids.length })}</dd>
            </>
          )}
          {derived.map((provenance, index) => (
            <FragmentRow key={index} term={t("run.selection.derived")}>
              {provenance.resolved_run_id ? (
                <a href={runHref(provenance.resolved_run_id)}>{provenance.resolved_run_title || provenance.resolved_run_id}</a>
              ) : null}{" "}
              —{" "}
              {t("run.selection.derivedFrom", {
                verdicts: (provenance.query?.verdicts ?? []).map((status) => tOr(`verdict.${status}`, status)).join(", "),
              })}
              {provenance.resolved_run_completed_at && (
                <span className="muted"> ({absoluteTime(provenance.resolved_run_completed_at, locale)})</span>
              )}
            </FragmentRow>
          ))}
          <dt>{t("run.items")}</dt>
          <dd>{run.planned_count}</dd>
          <dt>{t("run.selection.source")}</dt>
          <dd>
            {run.source.provider ?? "–"}
            {run.source.snapshot && <code className="snapshot"> {run.source.snapshot.slice(0, 19)}…</code>}
          </dd>
          <dt>{t("run.selection.expect")}</dt>
          <dd>{run.expect_format}</dd>
        </dl>
      </section>

      {shown && (
        <section className="panel">
          <header className="panel-head">
            <h2>{t("run.selection.dispatch")}</h2>
          </header>
          <dl className="facts">
            <dt>{t("run.runner")}</dt>
            <dd>
              <ModeName mode={shown.mode} />
            </dd>
            {parameters.map(([name, value]) => (
              <FragmentRow key={name} term={labelOf(name)}>
                <code>{Array.isArray(value) ? value.join(", ") : String(value)}</code>
              </FragmentRow>
            ))}
          </dl>
          {shown.plan && (
            <Foreign block className="plan">
              {shown.plan}
            </Foreign>
          )}
        </section>
      )}

      <section className="panel">
        <header className="panel-head">
          <h2>{t("run.selection.downloads")}</h2>
        </header>
        <dl className="facts">
          <dt>{t("run.selection.spec")}</dt>
          <dd>
            <span className="row">
              <a className="btn btn-small" href={specUrl(run.id, "json")} download={`runspec-${run.id}.json`}>
                JSON
              </a>
              <a className="btn btn-small" href={specUrl(run.id, "yaml")} download={`runspec-${run.id}.yaml`}>
                YAML
              </a>
              <span className="hint">{t("run.selection.specHint")}</span>
            </span>
          </dd>
          {run.dispatches.length > 0 && (
            <>
              <dt>{t("run.selection.results")}</dt>
              <dd>
                <span className="row">
                  <a className="btn btn-small" href={resultsExportUrl(run.id)} download={`results-${run.id}.ctrf.json`} data-testid="results-export">
                    CTRF
                  </a>
                  <span className="hint">{t("run.selection.resultsHint")}</span>
                </span>
              </dd>
            </>
          )}
        </dl>
      </section>
    </div>
  );
}

function FragmentRow({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </>
  );
}

/**
 * What an event says, in a line: the fields people look for, each with its
 * name — and, apart from them, what the runner wrote in its own words.
 */
function eventSummary(event: RunEvent, i18n: { t: Translator["t"]; tOr: Translator["tOr"] }): { known: string; written: string } {
  const { t, tOr } = i18n;
  const data = event.data;
  const parts: string[] = [];
  const text = (value: unknown) => (typeof value === "string" || typeof value === "number" ? String(value) : "");
  if (text(data.shard)) parts.push(`${t("run.shard")} ${text(data.shard)}`);
  if (typeof data.verdicts === "number") parts.push(t("run.resultCount", { count: data.verdicts }));
  if (text(data.outcome)) parts.push(tOr(`outcome.${text(data.outcome)}`, text(data.outcome)));
  if (text(data.completion)) parts.push(tOr(`completion.${text(data.completion)}`, text(data.completion)));
  if (typeof data.planned === "number") parts.push(t("picker.items", { count: data.planned }));
  if (text(data.mode)) parts.push(text(data.mode) === "export" ? t("mode.export") : text(data.mode) === "ingest" ? t("mode.ingest") : text(data.mode));
  const written: string[] = [];
  for (const field of ["reason", "message", "error"] as const) if (text(data[field])) written.push(text(data[field]));
  return { known: parts.join(" · "), written: written.join(" · ") };
}

function ActivityTab({ run, onRotate }: { run: RunDetail; onRotate: (dispatch: Dispatch) => void }) {
  const i18n = useI18n();
  const { t, tOr, locale } = i18n;
  const events = [...run.events].reverse();
  return (
    <div className="activity-tab">
      <section className="panel">
        <header className="panel-head">
          <h2>{t("run.activity.dispatches")}</h2>
        </header>
        {run.dispatches.length === 0 ? (
          <p className="state state-empty">{t("run.neverDispatched")}</p>
        ) : (
          <div className="table-scroll">
            <table className="table cards-on-narrow">
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">{t("run.runner")}</th>
                  <th scope="col">{t("runs.column.status")}</th>
                  <th scope="col">{t("run.started")}</th>
                  <th scope="col">{t("run.activity.shards")}</th>
                  <th scope="col">{t("run.activity.document")}</th>
                </tr>
              </thead>
              <tbody>
                {run.dispatches.map((dispatch, index) => (
                  <tr key={dispatch.dispatch_id} data-dispatch-mode={dispatch.mode}>
                    <td>
                      <a href={runHref(run.id, { d: index === run.dispatches.length - 1 ? null : dispatch.dispatch_id })}>{index + 1}</a>
                    </td>
                    <td data-label={t("run.runner")}>
                      <ModeName mode={dispatch.mode} />
                    </td>
                    <td data-label={t("runs.column.status")}>
                      <span className="badges">
                        <StateBadge state={dispatch.state} />
                        <ResultBadge result={dispatch.completion} />
                      </span>
                    </td>
                    <td data-label={t("run.started")}>{absoluteTime(dispatch.created_at, locale)}</td>
                    <td data-label={t("run.activity.shards")}>{dispatch.shards.join(", ") || "–"}</td>
                    <td data-label={t("run.activity.document")}>
                      {dispatch.spec_sha256 ? (
                        <span className="row">
                          <a
                            className="btn btn-small"
                            href={dispatchSpecUrl(run.id, dispatch.dispatch_id)}
                            download
                            data-testid="dispatch-spec"
                            aria-label={t("run.downloadDocument")}
                          >
                            {t("run.activity.download")}
                          </a>
                          {isActive(dispatch.state) && (
                            <button type="button" className="btn btn-small" onClick={() => onRotate(dispatch)} data-testid="rotate-token">
                              {t("run.activity.newToken")}
                            </button>
                          )}
                        </span>
                      ) : (
                        "–"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <header className="panel-head">
          <h2>{t("run.activity.events")}</h2>
        </header>
        <ol className="events">
          {events.map((event) => {
            const summary = eventSummary(event, i18n);
            return (
              <li key={event.seq} data-kind={event.kind}>
                <time dateTime={event.at}>{absoluteTime(event.at, locale)}</time>
                <strong>{tOr(`event.${event.kind}`, event.kind)}</strong>
                <span className="event-data">
                  {summary.known}
                  {summary.known && summary.written ? " · " : ""}
                  {summary.written && <Foreign>{summary.written}</Foreign>}
                </span>
              </li>
            );
          })}
        </ol>
      </section>

      {run.deliveries.length > 0 && (
        <section className="panel">
          <header className="panel-head">
            <h2>{t("run.activity.deliveries")}</h2>
          </header>
          <div className="table-scroll">
            <table className="table cards-on-narrow">
              <thead>
                <tr>
                  <th scope="col">{t("run.column.shard")}</th>
                  <th scope="col">{t("run.activity.format")}</th>
                  <th scope="col">{t("run.activity.received")}</th>
                </tr>
              </thead>
              <tbody>
                {run.deliveries.map((delivery) => (
                  <tr key={delivery.delivery_id}>
                    <td>{delivery.shard || "–"}</td>
                    <td data-label={t("run.activity.format")}>{delivery.format}</td>
                    <td data-label={t("run.activity.received")}>{absoluteTime(delivery.created_at, locale)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
