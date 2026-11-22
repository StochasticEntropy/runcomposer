// What Compose sends, and when a preview may be trusted.
//
// A selection has three parts in the builder:
//
//   filter    the tag-filter expression                       (tag_filter)
//   pinned    a fixed list of item ids, loaded from a run     (item_ids)
//   excluded  items the person unticked in the preview
//
// The preview asks the server for `filter ∩ pinned`. Exclusions are applied
// on top by naming the ids that remain — and they are a set of their own, so
// editing the filter does not wipe them: an excluded item that matches again
// is still excluded.
//
// The rule that keeps the preview and the dispatched run from disagreeing:
// a run can only be started from a preview that is *fresh* — the answer on
// screen belongs to the selection in the builder right now. `PreviewController`
// is the one place that decides that.

import { quotePattern } from "./filter.ts";
import { Latest, asApiError } from "./request.ts";
import type { ApiError } from "./request.ts";
import type { CatalogItem, CompileResult, FilterAst, Selection } from "./types.ts";

/**
 * A filter as `selection.tag_filter` takes it. The API takes the AST — with
 * one exception: a lone string is read as filter TEXT (`Auth OR Cart` is a
 * union), so a filter that is a single pattern travels as its quoted text.
 * `quotePattern` leaves an ordinary tag as it is (`Payments`) and quotes
 * what would otherwise read as something else: a tag spelled `Auth OR Cart`,
 * a tag named `AND` or `*`, a pattern with a space, a quote or parentheses
 * (`"regex:^Cart(V2)?$"`). The server reads it back to that one pattern, and
 * the run spec stores the pattern — never the quotes.
 */
export const wireFilter = (filter: FilterAst): FilterAst => (typeof filter === "string" ? quotePattern(filter) : filter);

/** What the preview is compiled from: the filter and the pinned ids. */
export function previewSelection(filter: FilterAst | null, pinned: string[] | null): Selection | null {
  const selection: Selection = {};
  if (filter != null) selection.tag_filter = wireFilter(filter);
  if (pinned && pinned.length > 0) selection.item_ids = pinned;
  return selection.tag_filter === undefined && selection.item_ids === undefined ? null : selection;
}

/** What a run is composed from: the preview's selection, minus the exclusions
 * that actually hit something. Without one, the filter travels as it is — a
 * run composed from "Smoke" says "Smoke", not a list of ids. */
export function dispatchSelection(
  filter: FilterAst | null,
  pinned: string[] | null,
  matched: CatalogItem[],
  excluded: ReadonlySet<string>,
): Selection | null {
  const base = previewSelection(filter, pinned);
  if (base === null) return null;
  const kept = matched.filter((item) => !excluded.has(item.id));
  if (kept.length === matched.length) return base;
  return { ...base, item_ids: kept.map((item) => item.id) };
}

export const selectionKey = (selection: Selection | null): string => (selection ? JSON.stringify(selection) : "");

export interface PreviewState {
  /** The selection the builder holds right now ("" when there is none). */
  key: string;
  status: "idle" | "loading" | "ready" | "error";
  /** The last answer received — possibly for an older selection. */
  result: CompileResult | null;
  resultKey: string;
  error: ApiError | null;
}

export const IDLE_PREVIEW: PreviewState = { key: "", status: "idle", result: null, resultKey: "", error: null };

/** The answer on screen is the answer to the selection in the builder. */
export const isFresh = (state: PreviewState): boolean =>
  state.status === "ready" && state.result !== null && state.resultKey === state.key;

type Compile = (selection: Selection, signal: AbortSignal) => Promise<CompileResult>;

export class PreviewController {
  private state: PreviewState = IDLE_PREVIEW;
  private readonly latest = new Latest();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private selection: Selection | null = null;

  constructor(
    private readonly compile: Compile,
    private readonly onChange: (state: PreviewState) => void,
    private readonly debounceMs = 180,
  ) {}

  get current(): PreviewState {
    return this.state;
  }

  private set(next: PreviewState): void {
    this.state = next;
    this.onChange(next);
  }

  /** The builder now holds `selection`. Compiles it after a short pause;
   * whatever was on its way for an earlier selection no longer counts. */
  request(selection: Selection | null, options: { immediate?: boolean } = {}): void {
    const key = selectionKey(selection);
    if (key === this.state.key && this.state.status !== "error") return;
    this.selection = selection;
    this.clearTimer();
    this.latest.cancel();
    if (selection === null) {
      // Nothing to compile: no request, and nothing left "loading".
      this.set(IDLE_PREVIEW);
      return;
    }
    this.set({ ...this.state, key, status: "loading", error: null });
    if (options.immediate || this.debounceMs === 0) void this.send(selection, key);
    else this.timer = setTimeout(() => void this.send(selection, key), this.debounceMs);
  }

  /** Ask again for the same selection — after an error, or to pick up a changed corpus. */
  retry(): void {
    if (this.selection === null) return;
    this.clearTimer();
    this.set({ ...this.state, status: "loading", error: null });
    void this.send(this.selection, this.state.key);
  }

  /**
   * Ask again without saying so: the error stays on screen until an answer
   * replaces it. For the retries the page makes by itself while the server
   * is out of reach — a spinner every few seconds would say "working" about
   * something that is waiting.
   */
  retryQuietly(): void {
    if (this.selection === null || this.state.status !== "error") return;
    this.clearTimer();
    void this.send(this.selection, this.state.key);
  }

  private async send(selection: Selection, key: string): Promise<void> {
    this.timer = null;
    try {
      const outcome = await this.latest.run((signal) => this.compile(selection, signal));
      if (!outcome.current || key !== this.state.key) return;
      this.set({ key, status: "ready", result: outcome.value, resultKey: key, error: null });
    } catch (error) {
      if (key !== this.state.key) return;
      // The old rows stay on screen, but they are no longer the answer to
      // anything: status "error" makes them stale and nothing can be run.
      this.set({ ...this.state, status: "error", error: asApiError(error) });
    }
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  /** Stop everything. The controller can be used again afterwards: it is
   * back to "nothing requested", so the next request is sent. */
  dispose(): void {
    this.clearTimer();
    this.latest.cancel();
    this.selection = null;
    this.state = IDLE_PREVIEW;
  }
}

// -- the part of the builder that lives in the URL ---------------------------------

export interface ComposeLink {
  /** The filter, in its text form. */
  filter: string;
  excluded: string[];
  /** A run whose items are pinned. */
  from: string | null;
}

/** Longest list of excluded ids a link carries; beyond it the link says so instead. */
export const MAX_LINKED_EXCLUSIONS = 60;

export function linkQuery(link: ComposeLink): Record<string, string | null> {
  const shareable = link.excluded.length <= MAX_LINKED_EXCLUSIONS;
  return {
    f: link.filter || null,
    from: link.from,
    x: link.excluded.length > 0 && shareable ? JSON.stringify(link.excluded) : null,
  };
}

export function linkFromQuery(query: Record<string, string>): ComposeLink | null {
  if (query.f === undefined && query.from === undefined && query.x === undefined) return null;
  let excluded: string[] = [];
  if (query.x) {
    try {
      const parsed: unknown = JSON.parse(query.x);
      if (Array.isArray(parsed)) excluded = parsed.filter((entry): entry is string => typeof entry === "string");
    } catch {
      excluded = [];
    }
  }
  return { filter: query.f ?? "", excluded, from: query.from || null };
}

// -- the preview's own view state -----------------------------------------------------

export type PreviewSort = "catalog" | "id" | "name";

export interface PreviewRow {
  kind: "item" | "group";
  /** For a group row: the suite path it stands for and the items below it. */
  label: string;
  item?: CatalogItem;
  members?: CatalogItem[];
}

const suiteOf = (item: CatalogItem): string => item.hierarchy.join(" › ");

/** Search, sort and group the matched items into the rows the table shows. */
export function previewRows(
  items: CatalogItem[],
  options: { search: string; sort: PreviewSort; grouped: boolean; compare: (a: string, b: string) => number },
): PreviewRow[] {
  const needle = options.search.trim().toLowerCase();
  let shown = needle
    ? items.filter(
        (item) =>
          item.id.toLowerCase().includes(needle) ||
          item.name.toLowerCase().includes(needle) ||
          item.tags.some((tagName) => tagName.toLowerCase().includes(needle)),
      )
    : items;
  if (options.sort === "id") shown = [...shown].sort((a, b) => options.compare(a.id, b.id));
  else if (options.sort === "name") shown = [...shown].sort((a, b) => options.compare(a.name, b.name));
  if (!options.grouped) return shown.map((item) => ({ kind: "item", label: item.id, item }));

  const groups = new Map<string, CatalogItem[]>();
  for (const item of shown) {
    const key = suiteOf(item);
    const members = groups.get(key);
    if (members) members.push(item);
    else groups.set(key, [item]);
  }
  const rows: PreviewRow[] = [];
  // A sort applies to the suites as well as to the items inside them.
  const ordered = options.sort === "catalog" ? [...groups] : [...groups].sort((a, b) => options.compare(a[0], b[0]));
  for (const [label, members] of ordered) {
    rows.push({ kind: "group", label, members });
    for (const item of members) rows.push({ kind: "item", label: item.id, item });
  }
  return rows;
}

/** all / some / none of these items are selected (not excluded). */
export function tristate(items: CatalogItem[], excluded: ReadonlySet<string>): "all" | "some" | "none" {
  let off = 0;
  for (const item of items) if (excluded.has(item.id)) off += 1;
  if (off === 0) return items.length === 0 ? "none" : "all";
  return off === items.length ? "none" : "some";
}
