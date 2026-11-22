// Compose: build a selection, see what it matches, choose how to run it.
//
// The builder's state is three things — the filter expression, the items
// unticked in the preview, and optionally a fixed list of items loaded from
// an earlier run. The shareable part of it lives in the URL (`f`, `x`,
// `from`); all of it lives in session storage, so a reload or a trip to
// another view brings back exactly what was there.

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ExportDialog } from "../components/ExportDialog.tsx";
import { ExpressionEditor } from "../components/ExpressionEditor.tsx";
import { PreviewTable } from "../components/PreviewTable.tsx";
import { PlanView } from "../components/PlanView.tsx";
import { SavedSelections } from "../components/SavedSelections.tsx";
import { LabelsInput, ParameterForm, RunnerSelect, problemTexts, refusedValue, runnerUsable } from "../components/RunnerFields.tsx";
import type { FieldProblem } from "../components/RunnerFields.tsx";
import { TagPicker } from "../components/TagPicker.tsx";
import { CopyButton, Dialog, ErrorState, ErrorText, Foreign, Loading, Notice, TimeAgo, describeError } from "../components/ui.tsx";
import { useApp, useI18n } from "../hooks/context.tsx";
import { usePlan } from "../hooks/usePlan.ts";
import { usePageVisible } from "../hooks/usePage.ts";
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
import { ROOT_ID, addNode, addTag, emptyRoot, fromAst, nodeFromAst, toAstWithPaths } from "../lib/editor.ts";
import type { GroupNode } from "../lib/editor.ts";
import { MATCH_ALL, errorNodeKey, formatFilter, isEverything, parseFilter, quotePattern } from "../lib/filter.ts";
import { setFlash } from "../lib/flash.ts";
import { parseLabel } from "../lib/format.ts";
import { fromPayload, mergeValues, toPayload, validateValues } from "../lib/params.ts";
import type { ParamValues } from "../lib/params.ts";
import { readSession, writeSession } from "../lib/prefs.ts";
import { loadSaved } from "../lib/saved.ts";
import type { SavedSelection } from "../lib/saved.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { composeHref, parseHash, runHref } from "../lib/router.ts";
import type { Route } from "../lib/router.ts";
import { indexByPattern, isSelectable, orderTree } from "../lib/tagTree.ts";
import type { Preset, QuickFilter, TaxonomyNode } from "../lib/types.ts";

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

const RETRY_FIRST_MS = 1000;
const RETRY_MAX_MS = 15_000;

/** A selection as the URL would say it, as a comparable key — "" for no selection, which is what a bare URL says. */
function keyOfLink(link: ComposeLink | null): string {
  if (!link) return "";
  const query = linkQuery(link);
  return query.f === null && query.from === null && query.x === null ? "" : JSON.stringify(query);
}

/** The selection a URL carries, as that key. */
const urlKeyOf = (query: Record<string, string>): string => keyOfLink(linkFromQuery(query));

function rootFromText(text: string): { root: GroupNode; unreadable: boolean } {
  try {
    return { root: fromAst(parseFilter(text)), unreadable: false };
  } catch {
    return { root: emptyRoot(), unreadable: true };
  }
}

function initialDraft(query: Record<string, string>, defaultFilter: string): { draft: StoredDraft; root: GroupNode; unreadable: boolean } {
  const stored = readSession<StoredDraft>(DRAFT_KEY);
  const link = linkFromQuery(query);
  // A link to another selection than the one in the draft: the title typed
  // for the old selection does not belong to this one.
  const otherSelection = link !== null && (link.filter !== (stored?.filter ?? "") || link.from !== (stored?.from ?? null));
  const draft: StoredDraft = {
    // The deployment's default filter is where a NEW selection starts: no
    // selection in the URL, and no draft of this session to come back to.
    filter: link?.filter ?? stored?.filter ?? defaultFilter,
    excluded: link ? link.excluded : (stored?.excluded ?? []),
    from: link ? link.from : (stored?.from ?? null),
    runner: stored?.runner ?? null,
    params: stored?.params ?? {},
    title: otherSelection ? "" : (stored?.title ?? ""),
    labels: stored?.labels ?? {},
  };
  return { draft, ...rootFromText(draft.filter) };
}

/** Ways into a selection on the empty builder: the curated taxonomy's own nodes. */
function quickStarts(taxonomy: TaxonomyNode[], configured: QuickFilter[]): QuickFilter[] {
  const starts: QuickFilter[] = [];
  // What the deployment configured is offered above the filter at all times; no need to repeat it here.
  // Compared as canonical text: a configured entry's `text` is the server's own.
  const seen = new Set(configured.map((start) => start.text));
  const walk = (nodes: TaxonomyNode[]) => {
    for (const node of nodes) {
      if (starts.length >= 12) return;
      // A resolved leaf's filter is one pattern, as the AST holds it.
      const text = node.filter === undefined ? "" : quotePattern(node.filter);
      if (node.origin === "file" && isSelectable(node) && !seen.has(text)) {
        seen.add(text);
        starts.push({ label: node.label, tag_filter: node.filter!, text, kind: "pattern" });
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

  // The deployment's default filter, from its AST: exact, whatever the text says.
  const defaultFilter = meta.ui.default_filter ? formatFilter(meta.ui.default_filter.tag_filter) : "";
  const [initial] = useState(() => initialDraft(route.query, defaultFilter));
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
  const [planning, setPlanning] = useState(false);
  // The filter as it is being typed in the text editor; null while it is not open.
  const [textDraft, setTextDraft] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError | null>(null);
  const [paramErrors, setParamErrors] = useState<Record<string, FieldProblem>>({});
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
  const knownTags = useMemo(() => tags.data?.tags.map((entry) => entry.tag) ?? null, [tags.data]);
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

  // A server that cannot be reached is nothing the viewer did wrong and
  // nothing they should have to watch: while the page is looked at, the
  // preview asks again by itself — after 1 s, 2 s, 4 s … at most every 15 s —
  // and is current again as soon as the server is back.
  const pageVisible = usePageVisible();
  const retries = useRef(0);
  useEffect(() => {
    if (preview.status !== "error" || preview.error?.code !== "network") {
      if (preview.status === "ready") retries.current = 0;
      return;
    }
    if (!pageVisible) return;
    const delay = Math.min(RETRY_FIRST_MS * 2 ** retries.current, RETRY_MAX_MS);
    const timer = window.setTimeout(() => {
      retries.current += 1;
      controller.retryQuietly();
    }, delay);
    return () => window.clearTimeout(timer);
  }, [preview, pageVisible, controller]);

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
    preview.status === "error" && preview.error && ["invalid-filter", "filter-syntax", "invalid-selection"].includes(preview.error.code)
      ? preview.error
      : null;
  const errorNodeId = useMemo(() => {
    if (!filterError || filterError.code !== "invalid-filter") return null;
    const path = errorNodeKey(filterError.extra.path);
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
  const linkKey = keyOfLink(link);
  const linkKeyRef = useRef(linkKey);
  useEffect(() => {
    linkKeyRef.current = linkKey;
  });

  // The builder and the URL follow each other, and two rules keep them from
  // fighting:
  //
  // - `synced` is the URL state the builder's state was derived from, or was
  //   last written as. The builder writes to the URL only while the URL still
  //   IS that state. If the URL moved on meanwhile — Back pressed twice before
  //   the first step was rendered — what the builder holds is derived from an
  //   older URL, and writing it would overwrite the newer one.
  // - An edit of the filter is a step in the browser's history (Back takes it
  //   back); ticking items is not, and neither is writing a state that came
  //   FROM the URL.
  const [urlTick, setUrlTick] = useState(0);
  const synced = useRef(urlKeyOf(route.query));
  const written = useRef<{ filter: string; from: string | null } | null>(null);
  const adopted = useRef(false);
  useEffect(() => {
    if (urlKeyOf(parseHash(window.location.hash).query) !== synced.current) return;
    const previous = written.current;
    const edited = previous !== null && (previous.filter !== link.filter || previous.from !== link.from);
    written.current = { filter: link.filter, from: link.from };
    synced.current = keyOfLink(link);
    navigate(composeHref(linkQuery(link)), { replace: !edited || adopted.current });
    adopted.current = false;
  }, [link, urlTick]);

  // Somebody else changed the URL — Back, a pasted link, "load into Compose".
  useEffect(() => {
    const incomingKey = urlKeyOf(route.query);
    if (incomingKey === synced.current) return;
    synced.current = incomingKey;
    const incoming = linkFromQuery(route.query);
    if (!incoming) {
      // A bare #/compose: the builder keeps what it holds and says so in the URL.
      setUrlTick((tick) => tick + 1);
      return;
    }
    // Whatever was typed as text, or open in a dialog, belonged to the selection that was there before.
    setTextDraft(null);
    setPickerGroup(null);
    setExporting(false);
    setPlanning(false);
    if (incomingKey === linkKeyRef.current) return;
    adopted.current = true;
    written.current = { filter: incoming.filter, from: incoming.from };
    const parsed = rootFromText(incoming.filter);
    setRoot(parsed.root);
    setUnreadableLink(parsed.unreadable);
    setExcluded(new Set(incoming.excluded));
    setFrom(incoming.from);
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

  const presets = useResource(useCallback((signal: AbortSignal) => api.presets(signal), [api]), "presets");
  const presetList = presets.data?.presets ?? [];
  /** A preset is a complete starting point: selection, runner, its options, title, labels. */
  const applyPreset = (preset: Preset) => {
    setRoot(fromAst(preset.tag_filter));
    setTextDraft(null);
    setUnreadableLink(false);
    setExcluded(new Set());
    setFrom(null);
    setTitle(preset.title);
    setLabels(preset.labels);
    setLabelDraft("");
    setSubmitError(null);
    setParamErrors({});
    const wanted = runners.find((candidate) => candidate.id === preset.runner);
    if (runnerUsable(wanted)) {
      setRunnerId(wanted.id);
      setParamsByRunner((current) => ({ ...current, [wanted.id]: fromPayload(wanted.parameters ?? [], preset.parameters) }));
      setNotice(t("compose.presetApplied", { label: preset.label }));
    } else {
      setNotice(preset.runner ? t("compose.presetWithoutRunner", { label: preset.label, runner: preset.runner }) : t("compose.presetApplied", { label: preset.label }));
    }
    announce(t("compose.presetApplied", { label: preset.label }));
  };

  // -- selections saved in this browser ---------------------------------------------------------
  const [saved, setSaved] = useState<SavedSelection[]>(() => loadSaved());
  const [savedOpen, setSavedOpen] = useState(false);
  const applySaved = (entry: SavedSelection) => {
    const parsed = rootFromText(entry.filter);
    setRoot(parsed.root);
    setUnreadableLink(parsed.unreadable);
    setTextDraft(null);
    setExcluded(new Set(entry.excluded));
    setFrom(null);
    setTitle(entry.title);
    setLabels(entry.labels);
    setLabelDraft("");
    setSubmitError(null);
    setParamErrors({});
    const wanted = runners.find((candidate) => candidate.id === entry.runner);
    if (runnerUsable(wanted)) {
      setRunnerId(wanted.id);
      setParamsByRunner((current) => ({ ...current, [wanted.id]: mergeValues(wanted.parameters ?? [], entry.params) }));
    }
    setNotice(t("saved.applied", { name: entry.name }));
    announce(t("saved.applied", { name: entry.name }));
  };

  /** A quick filter: one pattern is one more rule; a whole expression is the filter, or one more group of it. */
  const applyQuick = (quick: QuickFilter) => {
    // Text that was being typed belonged to the filter before this one.
    setTextDraft(null);
    if (quick.kind === "pattern" && typeof quick.tag_filter === "string") changeRoot(addTag(root, ROOT_ID, quick.tag_filter));
    else if (emitted.ast === null) changeRoot(fromAst(quick.tag_filter));
    else changeRoot(addNode(root, ROOT_ID, nodeFromAst(quick.tag_filter)));
  };
  const quickFilters = meta.ui.quick_filters ?? [];

  const changeRoot = (next: GroupNode) => {
    setRoot(next);
    setUnreadableLink(false);
    setSubmitError(null);
  };

  const reset = () => {
    setTextDraft(null);
    setRoot(rootFromText(defaultFilter).root);
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
  // A title nobody typed says what the selection is, in words — never in the wire encoding of anything.
  const defaultTitle = filterError
    ? ""
    : isEverything(emitted.ast)
      ? t("filter.everything")
      : filterText
        ? filterText.slice(0, 80)
        : pinned
          ? t("compose.titleFromRun", { title: pinned.title })
          : "";

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

  /** The options are acceptable as far as the browser can tell; if not, the
   * problems are put on their fields and the keyboard goes to the first. */
  const optionsValid = (): boolean => {
    const problems = validateValues(specs, values);
    if (Object.keys(problems).length === 0) return true;
    setParamErrors(problemTexts(t, problems));
    const first = specs.find((spec) => spec.name in problems)?.name;
    formRef.current?.querySelector<HTMLElement>(`[data-param="${first}"] :is(input, select, textarea)`)?.focus();
    return false;
  };

  // -- the plan preview ---------------------------------------------------------------------
  // Asked for (selection, runner, options) as they are when "Plan" is pressed,
  // and keyed by them: should any of them change, the answer on screen is
  // dropped and asked for again — it is never the plan of something else.
  const planQuestion = useMemo(() => {
    if (!planning || !fresh || !runnerUsable(runner)) return null;
    const planned = dispatchSelection(emitted.ast, pinned?.ids ?? null, matched, excluded);
    if (planned === null) return null;
    const body = { selection: planned, runner: runner.id, parameters: toPayload(specs, values) };
    return { key: JSON.stringify(body), ask: (signal: AbortSignal) => api.plan(body, signal) };
  }, [planning, fresh, runner, emitted.ast, pinned, matched, excluded, specs, values, api]);
  const plan = usePlan(planQuestion);
  const canPlan = runnerUsable(runner) && (runner.capabilities ?? []).includes("plan");

  const run = async () => {
    // Enter in a field asks for a run as well — never for one the bar would refuse.
    if (submitting.current || !runnerUsable(runner) || blocked !== null) return;
    if (!optionsValid()) return;
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
        setParamErrors({ [parameter]: refusedValue(i18n, specs.find((spec) => spec.name === parameter), error) });
      } else {
        setSubmitError(error);
      }
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };

  // What is typed in the text editor is not the filter until it is applied.
  // While the two differ, the screen shows one thing and a run would start
  // another — so nothing can be run, planned or exported.
  const textPending = textDraft !== null && textDraft.trim() !== filterText;
  const blocked = textPending
    ? t("compose.blocked.textDraft")
    : pinnedPending || preview.status === "loading"
      ? t("compose.blocked.updating")
      : preview.status === "error"
        ? filterError
          ? t("compose.blocked.error")
          : preview.error?.code === "network"
            ? t("compose.blocked.network")
            : t("compose.blocked.failed")
        : preview.status === "idle"
          ? t("compose.blocked.empty")
          : selectedCount === 0
            ? t("compose.blocked.nothing")
            : null;
  // Why Run is off beyond the selection itself: there is nothing to run it with.
  const runnerBlocked = usable.length === 0 || runnerUsable(runner) ? null : t("compose.blocked.runner");
  const runBlocked = blocked !== null || usable.length === 0 || runnerBlocked !== null;
  const summaryId = useId();
  const formId = useId();

  // The preview reaches down to the run bar, so it has to know how tall the
  // bar is — it grows when it wraps or shows an error.
  const composeRef = useRef<HTMLDivElement | null>(null);
  const barRef = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const [compose, bar] = [composeRef.current, barRef.current];
    if (!compose || !bar || typeof ResizeObserver === "undefined") return;
    const measure = () => compose.style.setProperty("--run-bar-height", `${Math.ceil(bar.getBoundingClientRect().height)}px`);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);
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
              // One pattern is added as a rule; a whole expression becomes the filter.
              const pattern = start.kind === "pattern" && typeof start.tag_filter === "string" ? start.tag_filter : null;
              const node = pattern === null ? undefined : byPattern.get(pattern);
              return (
                <li key={start.text}>
                  <button
                    type="button"
                    className="btn"
                    title={start.kind === "expression" ? start.text : undefined}
                    onClick={() => changeRoot(pattern === null ? fromAst(start.tag_filter) : addTag(root, ROOT_ID, pattern))}
                  >
                    {start.label}
                    {node && <span className="muted"> · {node.item_count}</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}
      {presetList.length > 0 && (
        <>
          <p>{t("compose.landing.presets")}</p>
          <ul className="preset-cards">
            {presetList.map((preset) => (
              <li key={preset.name} data-preset={preset.name}>
                <div>
                  <strong>{preset.label}</strong>
                  {preset.description && <span className="hint"> {preset.description}</span>}
                  <span className="cell-sub mono">{preset.text}</span>
                </div>
                <button type="button" className="btn btn-small" onClick={() => applyPreset(preset)} aria-label={t("compose.usePresetNamed", { label: preset.label })}>
                  {t("compose.usePreset")}
                </button>
              </li>
            ))}
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
    <div className="compose" ref={composeRef}>
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

        <div className="starts">
          {(presetList.length > 0 || saved.length > 0) && (
            <label className="field field-inline preset-select">
              <span>{t("compose.preset")}</span>
              <select
                className="input"
                value=""
                onChange={(event) => {
                  const [kind, name] = [event.target.value.slice(0, 1), event.target.value.slice(2)];
                  const preset = kind === "p" ? presetList.find((entry) => entry.name === name) : undefined;
                  const own = kind === "s" ? saved.find((entry) => entry.name === name) : undefined;
                  if (preset) applyPreset(preset);
                  if (own) applySaved(own);
                }}
              >
                <option value="">{t("compose.presetChoose")}</option>
                {presetList.length > 0 && (
                  <optgroup label={t("compose.presetsOfDeployment")}>
                    {presetList.map((preset) => (
                      <option key={preset.name} value={`p:${preset.name}`}>
                        {preset.label}
                      </option>
                    ))}
                  </optgroup>
                )}
                {saved.length > 0 && (
                  <optgroup label={t("saved.inThisBrowser")}>
                    {saved.map((entry) => (
                      <option key={entry.name} value={`s:${entry.name}`}>
                        {entry.name}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </label>
          )}
          <button type="button" className="btn btn-small" onClick={() => setSavedOpen(true)}>
            {t("saved.open")}
          </button>
        </div>
        {quickFilters.length > 0 && (
          <div className="quick-filters" role="group" aria-label={t("compose.quickFilters")}>
            <span className="muted" aria-hidden="true">
              {t("compose.quickFilters")}
            </span>
            {quickFilters.map((quick) => (
              <button key={quick.text} type="button" className="btn btn-small" title={quick.kind === "expression" ? quick.text : undefined} onClick={() => applyQuick(quick)}>
                {quick.label}
              </button>
            ))}
          </div>
        )}

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
          textDraft={textDraft}
          onTextDraft={setTextDraft}
          knownTags={knownTags}
        />
        <datalist id={tagListId}>
          {(tags.data?.tags ?? []).map((entry) => (
            <option key={entry.tag} value={entry.tag}>
              {t("picker.items", { count: entry.count })}
            </option>
          ))}
        </datalist>

        {/* One tag is its own sentence; anything more is also shown as text. */}
        {/* While the text editor is open it says itself what is in effect. */}
        {textDraft === null && filterText && typeof emitted.ast !== "string" && (
          <p className="expr-sentence">
            <span className="sr-only">{t("filter.asText")}: </span>
            <code data-testid="filter-text">{filterText}</code>
          </p>
        )}
        {filterError && (
          <p className="field-error" role="alert" data-testid="filter-error">
            {errorNodeId === null ? (
              <ErrorText error={filterError} />
            ) : (
              // The server said which rule it is, as a path into the filter. The
              // editor marks that rule; the path itself is no reading for a person.
              <>
                <strong>{describeError(i18n, filterError).title}</strong> {t("filter.errorAtRule")} <Foreign>{typeof filterError.extra.reason === "string" ? filterError.extra.reason : filterError.detail}</Foreign>
              </>
            )}
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
          id={formId}
          onSubmit={(event) => {
            event.preventDefault();
            void run();
          }}
          noValidate
        >
          <RunnerSelect
            value={runnerId}
            onChange={(id) => {
              setRunnerId(id);
              setParamErrors({});
              setSubmitError(null);
            }}
          />
          <ParameterForm
            specs={specs}
            values={values}
            onChange={setValues}
            problems={paramErrors}
            plugin={runner?.plugin}
            variant={typeof runner?.health?.data.partition_mode === "string" ? runner.health.data.partition_mode : undefined}
          />

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

        </form>
      </section>

      {/* The run bar: always in reach, whatever is scrolled — what would run, with what, and the actions. */}
      <div className="run-bar" ref={barRef} role="group" aria-label={t("compose.runBar")}>
        {submitError && (
          <div className="run-bar-error">
            <Notice tone="error" onDismiss={() => setSubmitError(null)}>
              <ErrorText error={submitError} />
            </Notice>
          </div>
        )}
        <div className="run-bar-info">
          <p className="run-summary" data-testid="run-summary" id={summaryId}>
            {fresh && preview.result && !textPending
              ? t("compose.summary", { selected: selectedCount, matched: preview.result.count, total: preview.result.total })
              : (blocked ?? "")}
          </p>
          <p className="hint" id={blockedId}>
            {runnerBlocked ?? (runnerUsable(runner) ? t("compose.withRunner", { runner: runner.id }) : "")}
          </p>
        </div>
        <div className="run-bar-actions">
          {canPlan && (
            <button
              type="button"
              className="btn"
              disabled={busy || runBlocked}
              onClick={() => {
                if (optionsValid()) setPlanning(true);
              }}
            >
              {t("compose.plan")}
            </button>
          )}
          <button type="button" className="btn" disabled={busy || blocked !== null} onClick={() => setExporting(true)}>
            {t("compose.export")}
          </button>
          <button
            type="submit"
            form={formId}
            className="btn btn-primary btn-large"
            disabled={runBlocked}
            aria-disabled={busy || undefined}
            aria-describedby={`${summaryId} ${blockedId}`}
          >
            {/* A number only where there is one: while the preview is not the answer to this filter, nothing is counted. */}
            {busy ? t("compose.starting") : fresh || preview.status === "idle" ? t("compose.run", { count: selectedCount }) : t("compose.runUncounted")}
          </button>
        </div>
      </div>

      {planning && (
        <Dialog
          title={t("plan.title")}
          onClose={() => setPlanning(false)}
          size="large"
          footer={
            <>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || runBlocked || plan.state.result?.refused === true}
                onClick={() => {
                  setPlanning(false);
                  void run();
                }}
              >
                {t("compose.run", { count: selectedCount })}
              </button>
            </>
          }
        >
          <p className="hint">
            {runnerUsable(runner) && t("plan.intro", { count: selectedCount, runner: runner.id })}
          </p>
          <PlanView state={plan.state} onRetry={plan.retry} />
        </Dialog>
      )}

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

      {savedOpen && (
        <SavedSelections
          saved={saved}
          onSaved={setSaved}
          currentFilter={filterText}
          current={() => ({
            filter: filterText,
            excluded: [...excluded],
            runner: runnerId,
            params: runnerId ? values : {},
            title,
            labels,
          })}
          onUse={(entry) => {
            applySaved(entry);
            setSavedOpen(false);
          }}
          onClose={() => setSavedOpen(false)}
        />
      )}

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
