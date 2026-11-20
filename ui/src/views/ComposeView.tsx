// Compose: build a selection, see what it matches, choose how to run it.
//
// The builder's state is three things — the filter expression, the items
// unticked in the preview, and optionally a fixed list of items loaded from
// an earlier run. The shareable part of it lives in the URL (`f`, `x`,
// `from`); all of it lives in session storage, so a reload or a trip to
// another view brings back exactly what was there.

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { ExportDialog } from "../components/ExportDialog.tsx";
import { ExpressionEditor } from "../components/ExpressionEditor.tsx";
import { PreviewTable } from "../components/PreviewTable.tsx";
import { LabelsInput, ParameterForm, RunnerSelect, runnerUsable } from "../components/RunnerFields.tsx";
import { TagPicker } from "../components/TagPicker.tsx";
import { CopyButton, Dialog, ErrorState, ErrorText, Loading, Notice, TimeAgo, describeError } from "../components/ui.tsx";
import { useApp, useI18n } from "../hooks/context.tsx";
import { useResource } from "../hooks/useResource.ts";
import { navigate } from "../hooks/useRoute.ts";
import {
  IDLE_PREVIEW,
  MAX_LINKED_EXCLUSIONS,
  PreviewController,
  dispatchSelection,
  isFresh,
  linkFromQuery,
  linkQuery,
  previewSelection,
} from "../lib/compose.ts";
import type { ComposeLink, PreviewState } from "../lib/compose.ts";
import { ROOT_ID, addTag, emptyRoot, fromAst, toAstWithPaths } from "../lib/editor.ts";
import type { GroupNode } from "../lib/editor.ts";
import { MATCH_ALL, errorPath, formatFilter, parseFilter } from "../lib/filter.ts";
import { setFlash } from "../lib/flash.ts";
import { parseLabel } from "../lib/format.ts";
import { fromPayload, mergeValues, toPayload, validateValues } from "../lib/params.ts";
import type { ParamValues } from "../lib/params.ts";
import { readSession, writeSession } from "../lib/prefs.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { composeHref, runHref } from "../lib/router.ts";
import type { Route } from "../lib/router.ts";
import { indexByPattern, isSelectable, orderTree } from "../lib/tagTree.ts";
import type { QuickFilter, TaxonomyNode } from "../lib/types.ts";

const DRAFT_KEY = "runcomposer.compose.v2";

interface StoredDraft {
  filter: string;
  excluded: string[];
  from: string | null;
  runner: string | null;
  params: Record<string, ParamValues>;
  title: string;
  labels: Record<string, string>;
}

function rootFromText(text: string): { root: GroupNode; unreadable: boolean } {
  try {
    return { root: fromAst(parseFilter(text)), unreadable: false };
  } catch {
    return { root: emptyRoot(), unreadable: true };
  }
}

function initialDraft(query: Record<string, string>): { draft: StoredDraft; root: GroupNode; unreadable: boolean } {
  const stored = readSession<StoredDraft>(DRAFT_KEY);
  const link = linkFromQuery(query);
  // A link to another selection than the one in the draft: the title typed
  // for the old selection does not belong to this one.
  const otherSelection = link !== null && (link.filter !== (stored?.filter ?? "") || link.from !== (stored?.from ?? null));
  const draft: StoredDraft = {
    filter: link?.filter ?? stored?.filter ?? "",
    excluded: link ? link.excluded : (stored?.excluded ?? []),
    from: link ? link.from : (stored?.from ?? null),
    runner: stored?.runner ?? null,
    params: stored?.params ?? {},
    title: otherSelection ? "" : (stored?.title ?? ""),
    labels: stored?.labels ?? {},
  };
  return { draft, ...rootFromText(draft.filter) };
}

function quickStarts(taxonomy: TaxonomyNode[], configured: (string | QuickFilter)[]): QuickFilter[] {
  const starts: QuickFilter[] = configured.map((entry) =>
    typeof entry === "string" ? { label: entry, filter: entry } : entry,
  );
  const seen = new Set(starts.map((start) => start.filter));
  const walk = (nodes: TaxonomyNode[]) => {
    for (const node of nodes) {
      if (starts.length >= 12) return;
      if (node.origin === "file" && isSelectable(node) && !seen.has(node.filter!)) {
        seen.add(node.filter!);
        starts.push({ label: node.label, filter: node.filter! });
      }
      if (node.origin === "file" && node.children) walk(node.children);
    }
  };
  walk(taxonomy);
  return starts;
}

export function ComposeView({ route }: { route: Route }) {
  const { api, meta, runners, announce } = useApp();
  const i18n = useI18n();
  const { t } = i18n;

  const [initial] = useState(() => initialDraft(route.query));
  const [root, setRoot] = useState<GroupNode>(initial.root);
  const [unreadableLink, setUnreadableLink] = useState(initial.unreadable);
  const [excluded, setExcluded] = useState<Set<string>>(() => new Set(initial.draft.excluded));
  const [from, setFrom] = useState<string | null>(initial.draft.from);
  const [runnerId, setRunnerId] = useState<string | null>(initial.draft.runner);
  const [paramsByRunner, setParamsByRunner] = useState<Record<string, ParamValues>>(initial.draft.params);
  const [title, setTitle] = useState(initial.draft.title);
  const [labels, setLabels] = useState<Record<string, string>>(initial.draft.labels);
  const [labelDraft, setLabelDraft] = useState("");

  const [pickerGroup, setPickerGroup] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [paramErrors, setParamErrors] = useState<Record<string, string>>({});
  const [labelProblem, setLabelProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const submitting = useRef(false);
  const formRef = useRef<HTMLFormElement | null>(null);
  const tagListId = useId();
  const titleId = useId();

  // -- catalog data -----------------------------------------------------------------
  const taxonomy = useResource(useCallback((signal: AbortSignal) => api.taxonomy(signal), [api]), "taxonomy");
  const tags = useResource(useCallback((signal: AbortSignal) => api.tags(signal), [api]), "tags");
  const tree = useMemo(() => orderTree(taxonomy.data?.taxonomy ?? []), [taxonomy.data]);
  const byPattern = useMemo(() => indexByPattern(tree), [tree]);
  const labelOf = useCallback(
    (pattern: string) => {
      const node = byPattern.get(pattern);
      return node && node.origin === "file" ? node.label : null;
    },
    [byPattern],
  );
  const catalogItems = tags.data?.items ?? meta.catalog.items;
  const catalogTags = tags.data?.tags.length ?? meta.catalog.tags;

  // -- the pinned list ---------------------------------------------------------------
  const pinnedResource = useResource(
    useCallback(
      async (signal: AbortSignal) => {
        if (!from) return null;
        const [detail, items] = await Promise.all([api.run(from, null, signal), api.runItems(from, signal)]);
        return { runId: from, title: detail.title, ids: items.item_ids };
      },
      [api, from],
    ),
    `pinned:${from ?? ""}`,
  );
  const pinned = from ? (pinnedResource.data ?? null) : null;
  const pinnedPending = Boolean(from) && !pinned && !pinnedResource.error;

  // -- the selection and its preview ---------------------------------------------------
  const emitted = useMemo(() => toAstWithPaths(root), [root]);
  const filterText = useMemo(() => formatFilter(emitted.ast), [emitted.ast]);
  const [preview, setPreview] = useState<PreviewState>(IDLE_PREVIEW);
  const [controller] = useState(
    () => new PreviewController((selection, signal) => api.compile(selection, signal), setPreview),
  );
  useEffect(() => () => controller.dispose(), [controller]);

  const selection = useMemo(
    () => (from && !pinned ? null : previewSelection(emitted.ast, pinned?.ids ?? null)),
    [emitted.ast, pinned, from],
  );
  useEffect(() => {
    controller.request(selection);
  }, [controller, selection]);

  const fresh = isFresh(preview) && !pinnedPending;
  const matched = useMemo(() => (fresh ? (preview.result?.items ?? []) : []), [fresh, preview.result]);
  const selectedCount = useMemo(() => matched.filter((item) => !excluded.has(item.id)).length, [matched, excluded]);
  const filterError =
    preview.status === "error" && preview.error && ["invalid-filter", "invalid-selection"].includes(preview.error.code)
      ? preview.error
      : null;
  const errorNodeId = useMemo(() => {
    if (!filterError || filterError.code !== "invalid-filter") return null;
    const path = errorPath(filterError.detail);
    return path === null ? null : (emitted.paths.get(path) ?? null);
  }, [filterError, emitted.paths]);

  // Say the result of an edit to people who cannot see the preview change.
  const announced = useRef("");
  useEffect(() => {
    if (!fresh || !preview.result) return;
    const message = t("preview.matched", { count: preview.result.count, total: preview.result.total });
    if (message !== announced.current) {
      announced.current = message;
      announce(message);
    }
  }, [fresh, preview.result, t, announce]);

  // -- URL and session ------------------------------------------------------------------
  const link: ComposeLink = useMemo(() => ({ filter: filterText, excluded: [...excluded], from }), [filterText, excluded, from]);
  const linkKey = JSON.stringify(linkQuery(link));
  const linkKeyRef = useRef(linkKey);
  useEffect(() => {
    linkKeyRef.current = linkKey;
  });

  // An edit of the filter is a step in the browser's history — Back takes it
  // back. Ticking items is not (that would be one step per item), and neither
  // is writing the URL for a state that came FROM the URL.
  const written = useRef<{ filter: string; from: string | null } | null>(null);
  const adopted = useRef(false);
  useEffect(() => {
    const previous = written.current;
    const edited = previous !== null && (previous.filter !== link.filter || previous.from !== link.from);
    written.current = { filter: link.filter, from: link.from };
    navigate(composeHref(linkQuery(link)), { replace: !edited || adopted.current });
    adopted.current = false;
  }, [link]);

  // Somebody else changed the URL — Back, a pasted link, "load into Compose".
  useEffect(() => {
    const incoming = linkFromQuery(route.query);
    if (!incoming || JSON.stringify(linkQuery(incoming)) === linkKeyRef.current) return;
    adopted.current = true;
    const parsed = rootFromText(incoming.filter);
    setRoot(parsed.root);
    setUnreadableLink(parsed.unreadable);
    setExcluded(new Set(incoming.excluded));
    setFrom(incoming.from);
    // A dialog that was open belongs to the selection that was there before.
    setPickerGroup(null);
    setExporting(false);
  }, [route.query]);

  useEffect(() => {
    const draft: StoredDraft = { filter: filterText, excluded: [...excluded], from, runner: runnerId, params: paramsByRunner, title, labels };
    writeSession(DRAFT_KEY, draft);
  }, [filterText, excluded, from, runnerId, paramsByRunner, title, labels]);

  // -- runner and parameters --------------------------------------------------------------
  const usable = runners.filter(runnerUsable);
  const runner = runners.find((candidate) => candidate.id === runnerId);
  useEffect(() => {
    // A stored choice that is gone, or no choice yet: the first usable runner.
    if (runnerId !== null && runnerUsable(runner)) return;
    const first = runners.find(runnerUsable);
    if (first && first.id !== runnerId) setRunnerId(first.id);
  }, [runners, runner, runnerId]);
  const specs = useMemo(() => (runnerUsable(runner) ? (runner.parameters ?? []) : []), [runner]);
  const values = useMemo(() => mergeValues(specs, runnerId ? paramsByRunner[runnerId] : undefined), [specs, runnerId, paramsByRunner]);
  const setValues = (next: ParamValues) => {
    if (!runnerId) return;
    setParamsByRunner((current) => ({ ...current, [runnerId]: next }));
    setParamErrors({});
  };

  // Arriving from "Load into Compose": besides the selection (already in the
  // link), take over how that run was run — runner, its options, labels.
  const [like] = useState(() => route.query.like ?? null);
  useEffect(() => {
    if (!like) return;
    const controller = new AbortController();
    api
      .run(like, null, controller.signal)
      .then((source) => {
        const latest = source.dispatches[source.dispatches.length - 1];
        const sameRunner = runners.find((candidate) => candidate.id === latest?.mode);
        if (latest && runnerUsable(sameRunner)) {
          setRunnerId(sameRunner.id);
          setParamsByRunner((current) => ({ ...current, [sameRunner.id]: fromPayload(sameRunner.parameters ?? [], latest.parameters) }));
        }
        setLabels(source.labels);
        setTitle("");
        setNotice(t("compose.loadedFrom", { title: source.title }));
      })
      .catch(() => undefined); // the selection is loaded either way; this was a convenience
    return () => controller.abort();
    // Once, for the link this view was opened with.
  }, [like]); // eslint-disable-line react-hooks/exhaustive-deps

  const changeRoot = (next: GroupNode) => {
    setRoot(next);
    setUnreadableLink(false);
    setSubmitError(null);
  };

  const reset = () => {
    setRoot(emptyRoot());
    setExcluded(new Set());
    setFrom(null);
    setTitle("");
    setLabels({});
    setLabelDraft("");
    setSubmitError(null);
    setParamErrors({});
    setNotice(null);
  };

  const reloadCatalog = async () => {
    try {
      const result = await api.reloadCatalog();
      await Promise.all([taxonomy.reload(), tags.reload()]);
      controller.retry();
      setNotice(t("compose.catalogReloaded", { count: result.items }));
    } catch (error) {
      setSubmitError(asApiError(error));
    }
  };

  // -- composing ------------------------------------------------------------------------------
  const defaultTitle = filterError ? "" : filterText ? filterText.slice(0, 80) : pinned ? t("compose.titleFromRun", { title: pinned.title }) : "";

  /** Everything a run or an export is composed from, or null when the form says no. */
  const collect = (): { selection: NonNullable<ReturnType<typeof dispatchSelection>>; title?: string; labels: Record<string, string> } | null => {
    let allLabels = labels;
    if (labelDraft.trim()) {
      const parsed = parseLabel(labelDraft);
      if (!parsed) {
        setLabelProblem(t("labels.invalid"));
        return null;
      }
      allLabels = { ...labels, [parsed[0]]: parsed[1] };
      setLabels(allLabels);
      setLabelDraft("");
    }
    setLabelProblem(null);
    const composed = dispatchSelection(emitted.ast, pinned?.ids ?? null, matched, excluded);
    if (!fresh || composed === null || selectedCount === 0) return null;
    const chosenTitle = title.trim() || defaultTitle;
    return { selection: composed, labels: allLabels, ...(chosenTitle ? { title: chosenTitle } : {}) };
  };

  const run = async () => {
    if (submitting.current || !runnerUsable(runner)) return;
    const problems = validateValues(specs, values);
    if (Object.keys(problems).length > 0) {
      setParamErrors(Object.fromEntries(Object.entries(problems).map(([name, problem]) => [name, t(`runner.problem.${problem}`)])));
      // Take the keyboard to the first field that needs attention.
      const first = specs.find((spec) => spec.name in problems)?.name;
      formRef.current?.querySelector<HTMLElement>(`[data-param="${first}"] input, [data-param="${first}"] select`)?.focus();
      return;
    }
    const body = collect();
    if (!body) return;
    submitting.current = true;
    setBusy(true);
    setSubmitError(null);
    setParamErrors({});
    try {
      const response = await api.createRun({ ...body, dispatch: { runner: runner.id, parameters: toPayload(specs, values) } });
      const started = t("compose.started", { count: response.run.planned_count, title: response.run.title });
      setFlash({
        tone: response.run.planned_count === selectedCount ? "ok" : "warn",
        text:
          response.run.planned_count === selectedCount
            ? started
            : `${started} ${t("compose.countChanged", { expected: selectedCount, count: response.run.planned_count })}`,
      });
      announce(started);
      setTitle(""); // the title went with that run
      navigate(runHref(response.run.id));
    } catch (caught) {
      const error = asApiError(caught);
      const parameter = error.extra.parameter;
      if (error.code === "invalid-parameter" && typeof parameter === "string" && specs.some((spec) => spec.name === parameter)) {
        const { title: problemTitle, detail } = describeError(i18n, error);
        setParamErrors({ [parameter]: `${problemTitle} ${detail}` });
      } else {
        setSubmitError(error);
      }
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };

  const blocked = pinnedPending || preview.status === "loading"
    ? t("compose.blocked.updating")
    : preview.status === "error"
      ? t("compose.blocked.error")
      : preview.status === "idle"
        ? t("compose.blocked.empty")
        : selectedCount === 0
          ? t("compose.blocked.nothing")
          : null;
  // Why Run is off beyond the selection itself: there is nothing to run it with.
  const runnerBlocked = usable.length === 0 || runnerUsable(runner) ? null : t("compose.blocked.runner");
  const runBlocked = blocked !== null || usable.length === 0 || runnerBlocked !== null;
  const summaryId = useId();
  const blockedId = useId();

  const starts = useMemo(() => quickStarts(tree, meta.ui.quick_filters ?? []), [tree, meta.ui.quick_filters]);
  const tooManyExclusions = excluded.size > MAX_LINKED_EXCLUSIONS;

  const landing = (
    <div className="landing">
      <h2>{t("preview.title")}</h2>
      <p className="state-title">{t("compose.landing.title")}</p>
      <p>
        {t("compose.landing.catalog", { items: catalogItems, tags: catalogTags })}{" "}
        {meta.catalog.loaded_at && (
          <span className="muted">
            {t("compose.landing.loaded")} <TimeAgo iso={meta.catalog.loaded_at} />.
          </span>
        )}
      </p>
      {starts.length > 0 && (
        <>
          <p>{t("compose.landing.starts")}</p>
          <ul className="quick-starts">
            {starts.map((start) => {
              const node = byPattern.get(start.filter);
              return (
                <li key={start.filter}>
                  <button type="button" className="btn" onClick={() => changeRoot(addTag(root, ROOT_ID, start.filter))}>
                    {start.label}
                    {node && <span className="muted"> · {node.item_count}</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}
      <p>
        <button type="button" className="btn" onClick={() => changeRoot(fromAst(MATCH_ALL))}>
          {t("compose.landing.all", { count: catalogItems })}
        </button>
      </p>
    </div>
  );

  return (
    <div className="compose">
      <h1 className="sr-only">{t("nav.compose")}</h1>
      <section className="panel filter-panel" aria-labelledby={`${titleId}-filter`}>
        <header className="panel-head">
          <h2 id={`${titleId}-filter`}>{t("filter.title")}</h2>
          <span className="panel-tools">
            <button type="button" className="btn btn-small btn-quiet" onClick={() => void reloadCatalog()} title={t("compose.reloadCatalogHint")}>
              {t("compose.reloadCatalog")}
            </button>
            <button type="button" className="btn btn-small btn-quiet" onClick={reset}>
              {t("compose.reset")}
            </button>
          </span>
        </header>

        {unreadableLink && (
          <Notice tone="warn" onDismiss={() => setUnreadableLink(false)}>
            {t("compose.unreadableLink")}
          </Notice>
        )}
        {notice && (
          <Notice tone="ok" onDismiss={() => setNotice(null)}>
            {notice}
          </Notice>
        )}

        <ExpressionEditor
          root={root}
          onChange={changeRoot}
          onPick={setPickerGroup}
          tagListId={tagListId}
          errorNodeId={errorNodeId}
          labelOf={labelOf}
        />
        <datalist id={tagListId}>
          {(tags.data?.tags ?? []).map((entry) => (
            <option key={entry.tag} value={entry.tag}>
              {t("picker.items", { count: entry.count })}
            </option>
          ))}
        </datalist>

        {/* One tag is its own sentence; anything more is also shown as text. */}
        {filterText && typeof emitted.ast !== "string" && (
          <p className="expr-sentence">
            <span className="sr-only">{t("filter.asText")}: </span>
            <code data-testid="filter-text">{filterText}</code>
          </p>
        )}
        {filterError && (
          <p className="field-error" role="alert" data-testid="filter-error">
            <ErrorText error={filterError} />
          </p>
        )}

        {from && (
          <div className="pinned" data-testid="pinned">
            {pinnedResource.error ? (
              <ErrorState error={pinnedResource.error} onRetry={() => void pinnedResource.reload()} compact />
            ) : pinned ? (
              <p>
                {t("compose.pinned", { count: pinned.ids.length })}{" "}
                <a href={runHref(pinned.runId)}>{pinned.title}</a>
              </p>
            ) : (
              <Loading compact label={t("compose.pinnedLoading")} />
            )}
            <button type="button" className="btn btn-small" onClick={() => setFrom(null)}>
              {t("compose.unpin")}
            </button>
          </div>
        )}

        <p className="share">
          <CopyButton text={() => window.location.href} label={t("compose.copyLink")} quiet />
          {tooManyExclusions && <span className="hint">{t("compose.linkWithoutExclusions", { max: MAX_LINKED_EXCLUSIONS })}</span>}
        </p>
      </section>

      <PreviewTable
        state={pinnedPending ? { ...IDLE_PREVIEW, key: "pinned", status: "loading" } : preview}
        excluded={excluded}
        onExcludedChange={setExcluded}
        onRetry={() => controller.retry()}
        landing={landing}
        filterError={filterError !== null}
      />

      <section className="panel run-panel" aria-labelledby={`${titleId}-run`}>
        <header className="panel-head">
          <h2 id={`${titleId}-run`}>{t("compose.runTitle")}</h2>
        </header>
        <form
          ref={formRef}
          onSubmit={(event) => {
            event.preventDefault();
            void run();
          }}
          noValidate
        >
          <p className="run-summary" data-testid="run-summary" id={summaryId}>
            {fresh && preview.result
              ? t("compose.summary", { selected: selectedCount, matched: preview.result.count, total: preview.result.total })
              : (blocked ?? "")}
          </p>

          <RunnerSelect
            value={runnerId}
            onChange={(id) => {
              setRunnerId(id);
              setParamErrors({});
              setSubmitError(null);
            }}
          />
          <ParameterForm specs={specs} values={values} onChange={setValues} problems={paramErrors} />

          <div className="field">
            <label className="field-label" htmlFor={titleId}>
              {t("compose.title")}
            </label>
            <input
              id={titleId}
              className="input"
              type="text"
              value={title}
              maxLength={200}
              placeholder={defaultTitle || t("compose.titlePlaceholder")}
              onChange={(event) => setTitle(event.target.value)}
            />
          </div>
          <LabelsInput labels={labels} onChange={setLabels} draft={labelDraft} onDraftChange={setLabelDraft} problem={labelProblem} />

          {submitError && (
            <Notice tone="error" onDismiss={() => setSubmitError(null)}>
              <ErrorText error={submitError} />
            </Notice>
          )}

          <div className="run-actions">
            <button type="submit" className="btn btn-primary btn-large" disabled={runBlocked} aria-disabled={busy || undefined} aria-describedby={`${summaryId} ${blockedId}`}>
              {busy ? t("compose.starting") : t("compose.run", { count: selectedCount })}
            </button>
            <button type="button" className="btn" disabled={busy || blocked !== null} onClick={() => setExporting(true)}>
              {t("compose.export")}
            </button>
          </div>
          <p className="hint" id={blockedId}>
            {runnerBlocked ?? ""}
          </p>
        </form>
      </section>

      {pickerGroup !== null &&
        (taxonomy.data ? (
          <TagPicker
            taxonomy={tree}
            root={root}
            groupId={pickerGroup}
            pinned={pinned?.ids ?? null}
            currentCount={fresh && preview.result ? preview.result.count : null}
            onApply={(next) => {
              changeRoot(next);
              setPickerGroup(null);
            }}
            onClose={() => setPickerGroup(null)}
          />
        ) : (
          <PickerUnavailable error={taxonomy.error} onRetry={() => void taxonomy.reload()} onClose={() => setPickerGroup(null)} />
        ))}

      {exporting && (
        <ExportDialog
          intro={`${t("export.introCompose", { count: selectedCount })} ${t("export.runTitle", { title: title.trim() || defaultTitle || t("compose.titlePlaceholder") })}`}
          onExported={() => setTitle("")}
          perform={async (format) => {
            const body = collect();
            if (!body) throw new Error(t("compose.blocked.updating"));
            return api.createRun({ ...body, dispatch: { mode: "export", format } });
          }}
          previewSpec={async () => {
            const body = collect();
            if (!body) throw new Error(t("compose.blocked.updating"));
            return (await api.specPreview(body)).spec;
          }}
          onClose={() => setExporting(false)}
        />
      )}
    </div>
  );
}

function PickerUnavailable({ error, onRetry, onClose }: { error: ApiError | null; onRetry: () => void; onClose: () => void }) {
  const { t } = useI18n();
  return (
    <Dialog title={t("picker.title")} onClose={onClose}>
      {error ? <ErrorState error={error} onRetry={onRetry} /> : <Loading />}
    </Dialog>
  );
}
