// The endpoints the UI uses (DESIGN.md §9), typed. Same-origin: the UI is
// served by `runcomposer serve` itself (or the dev server's proxy).

import { createClient } from "./request.ts";
import type { Client } from "./request.ts";
import type {
  CompileResult,
  CreateRunResponse,
  DispatchResponse,
  IngestReport,
  Meta,
  QuarantineEntry,
  ResolvedTaxonomy,
  RunDetail,
  RunEvent,
  RunList,
  RunRow,
  RunnerInfo,
  Selection,
  TagCount,
} from "./types.ts";

const API = "/api/v1";
const id = encodeURIComponent;

export interface RunListQuery {
  q?: string;
  state?: string;
  labels?: string[];
  limit?: number;
  offset?: number;
}

export interface DispatchBody {
  runner?: string;
  parameters?: Record<string, unknown>;
  mode?: "export";
  format?: "json" | "yaml";
}

export interface ComposeBody {
  selection: Selection;
  title?: string;
  labels?: Record<string, string>;
  dispatch?: DispatchBody;
}

export function runListPath(query: RunListQuery = {}): string {
  const params = new URLSearchParams();
  if (query.q) params.set("q", query.q);
  if (query.state) params.set("state", query.state);
  for (const label of query.labels ?? []) params.append("label", label);
  if (query.limit != null) params.set("limit", String(query.limit));
  if (query.offset) params.set("offset", String(query.offset));
  const text = params.toString();
  return `${API}/runs${text ? `?${text}` : ""}`;
}

export function createApi(client: Client = createClient()) {
  const { request } = client;
  const get = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal });
  const post = <T>(path: string, body?: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: "POST", body: body ?? {}, signal });

  return {
    meta: (signal?: AbortSignal) => get<Meta>(`${API}/meta`, signal),
    locale: (locale: string, signal?: AbortSignal) =>
      get<Record<string, unknown>>(`/locales/${id(locale)}.json`, signal),
    runners: (refresh = false, signal?: AbortSignal) =>
      get<RunnerInfo[]>(`${API}/runners${refresh ? "?refresh=true" : ""}`, signal),
    taxonomy: (signal?: AbortSignal) => get<ResolvedTaxonomy>(`${API}/taxonomy?resolve=true`, signal),
    tags: (signal?: AbortSignal) =>
      get<{ tags: TagCount[]; items: number; snapshot: string }>(`${API}/catalog/tags`, signal),
    reloadCatalog: () => post<{ reloaded: boolean; items: number }>(`${API}/catalog/reload`),

    compile: (selection: Selection, signal?: AbortSignal) =>
      post<CompileResult>(`${API}/selection/compile`, selection, signal),
    count: (selection: Selection, signal?: AbortSignal) =>
      post<CompileResult>(`${API}/selection/compile`, { ...selection, limit: 0 }, signal),

    createRun: (body: ComposeBody) => post<CreateRunResponse>(`${API}/runs`, body),
    listRuns: (query: RunListQuery, signal?: AbortSignal) => get<RunList>(runListPath(query), signal),
    run: (runId: string, dispatch?: string | null, signal?: AbortSignal) =>
      get<RunDetail>(`${API}/runs/${id(runId)}${dispatch ? `?dispatch=${id(dispatch)}` : ""}`, signal),
    runItems: (runId: string, signal?: AbortSignal) =>
      get<{ item_ids: string[]; count: number }>(`${API}/runs/${id(runId)}/items`, signal),
    events: (runId: string, after: number, wait: number, signal?: AbortSignal) =>
      get<{ events: RunEvent[]; last_seq: number }>(
        `${API}/runs/${id(runId)}/events?after=${after}&wait=${wait}&limit=200`,
        signal,
      ),
    dispatch: (runId: string, body: DispatchBody) =>
      post<DispatchResponse>(`${API}/runs/${id(runId)}/dispatch`, body),
    rerun: (runId: string, body: { scope: "all" | "failed"; runner?: string; parameters?: Record<string, unknown> }) =>
      post<DispatchResponse>(`${API}/runs/${id(runId)}/rerun`, body),
    cancel: (runId: string, force = false) =>
      post<DispatchResponse>(`${API}/runs/${id(runId)}/cancel${force ? "?force=true" : ""}`),
    finalize: (runId: string) => post<RunRow>(`${API}/runs/${id(runId)}/finalize`),
    deleteRun: (runId: string) =>
      request<void>(`${API}/runs/${id(runId)}`, { method: "DELETE", expect: "none" }),
    rotateToken: (runId: string, dispatchId: string) =>
      post<{ dispatch_id: string; ingest_token: string | null }>(
        `${API}/runs/${id(runId)}/dispatches/${id(dispatchId)}/token`,
      ),

    quarantine: (signal?: AbortSignal) => get<{ entries: QuarantineEntry[] }>(`${API}/quarantine`, signal),
    attach: (entryId: string, runId: string, format?: string) =>
      post<IngestReport>(`${API}/quarantine/${id(entryId)}/attach`, { run_id: runId, ...(format ? { format } : {}) }),
    promote: (entryId: string, format?: string) =>
      post<IngestReport>(`${API}/quarantine/${id(entryId)}/promote`, format ? { format } : {}),
    discard: (entryId: string) =>
      request<void>(`${API}/quarantine/${id(entryId)}`, { method: "DELETE", expect: "none" }),
  };
}

export type Api = ReturnType<typeof createApi>;

// Plain links: the browser downloads the server's bytes itself, so nothing
// in between can re-serialise a document whose hash matters (§5).
export const specUrl = (runId: string, format: "json" | "yaml") => `${API}/runs/${id(runId)}/spec?format=${format}`;
export const dispatchSpecUrl = (runId: string, dispatchId: string) =>
  `${API}/runs/${id(runId)}/dispatches/${id(dispatchId)}/spec`;
export const resultsUrl = (runId: string) => `${API}/runs/${id(runId)}/results`;
