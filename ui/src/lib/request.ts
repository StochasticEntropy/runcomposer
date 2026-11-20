// The request layer: every call to the server goes through `request`, so
// there is one place that knows what an error answer looks like, how a call
// is cancelled, and that two identical reads in flight are one request.
//
// The API answers every failure with `{detail, code}` (DESIGN.md §9) plus,
// for a validation failure, `fields`. A failure that never reached the server
// — offline, refused, DNS — gets the code `network`, so the UI can tell "the
// server said no" from "nobody answered" without reading message texts.

export interface FieldError {
  field: string;
  message: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly fields: FieldError[];
  /** Whatever else the error body carried (`run_kept`, `parameter`, …). */
  readonly extra: Record<string, unknown>;

  constructor(status: number, code: string, detail: string, body: Record<string, unknown> = {}) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    const { fields, detail: _detail, code: _code, ...extra } = body;
    void _detail;
    void _code;
    this.fields = Array.isArray(fields) ? (fields as FieldError[]) : [];
    this.extra = extra;
  }
}

export const isAbort = (error: unknown): boolean =>
  error instanceof DOMException ? error.name === "AbortError" : (error as { name?: string })?.name === "AbortError";

export const asApiError = (error: unknown): ApiError =>
  error instanceof ApiError
    ? error
    : new ApiError(0, "client-error", error instanceof Error ? error.message : String(error));

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** How to read a successful answer. `none` is for 204. */
  expect?: "json" | "none";
}

type Fetch = typeof fetch;

// A detail is always a string on the wire. Should a proxy in between answer
// with something else, it is still shown as text and never as "[object Object]".
function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail != null) return JSON.stringify(detail);
  }
  return fallback;
}

export function createClient(fetchImpl: Fetch = (...args) => fetch(...args)) {
  // Identical GETs in flight share one request. Each caller still has its own
  // signal: the shared fetch is aborted only when every caller has left.
  const inflight = new Map<string, { promise: Promise<unknown>; controller: AbortController; waiting: number }>();

  async function send<T>(path: string, options: RequestOptions, signal?: AbortSignal): Promise<T> {
    const init: RequestInit = { method: options.method ?? "GET", signal };
    if (options.body !== undefined) {
      init.headers = { "content-type": "application/json" };
      init.body = JSON.stringify(options.body);
    }
    let response: Response;
    try {
      response = await fetchImpl(path, init);
    } catch (error) {
      if (isAbort(error)) throw error;
      throw new ApiError(0, "network", error instanceof Error ? error.message : String(error));
    }
    if (!response.ok) {
      let body: unknown = null;
      try {
        body = await response.json();
      } catch {
        /* not JSON: a proxy's error page */
      }
      const record = body && typeof body === "object" ? (body as Record<string, unknown>) : {};
      const code = typeof record.code === "string" ? record.code : `http-${response.status}`;
      throw new ApiError(response.status, code, detailOf(body, response.statusText || `HTTP ${response.status}`), record);
    }
    if (options.expect === "none" || response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = options.method ?? "GET";
    if (method !== "GET") return send<T>(path, options, options.signal);

    let entry = inflight.get(path);
    if (!entry) {
      const controller = new AbortController();
      const promise = send<T>(path, options, controller.signal).finally(() => inflight.delete(path));
      entry = { promise, controller, waiting: 0 };
      inflight.set(path, entry);
    }
    const shared = entry;
    shared.waiting += 1;
    return new Promise<T>((resolve, reject) => {
      const leave = () => {
        shared.waiting -= 1;
        if (shared.waiting === 0 && inflight.get(path) === shared) {
          inflight.delete(path);
          shared.controller.abort();
        }
      };
      const onAbort = () => {
        leave();
        reject(new DOMException("aborted", "AbortError"));
      };
      if (options.signal?.aborted) return onAbort();
      options.signal?.addEventListener("abort", onAbort, { once: true });
      shared.promise.then(
        (value) => {
          options.signal?.removeEventListener("abort", onAbort);
          resolve(value as T);
        },
        (error: unknown) => {
          options.signal?.removeEventListener("abort", onAbort);
          if (!options.signal?.aborted) reject(error);
        },
      );
    });
  }

  return { request };
}

export type Client = ReturnType<typeof createClient>;

// "Only the latest answer counts": each call to `run` aborts the one before
// it, and an answer that arrives for a superseded call is dropped instead of
// delivered. This is what keeps a slow early response from overwriting a fast
// late one.
export class Latest {
  private controller: AbortController | null = null;
  private generation = 0;

  async run<T>(task: (signal: AbortSignal) => Promise<T>): Promise<{ current: true; value: T } | { current: false }> {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const generation = ++this.generation;
    try {
      const value = await task(controller.signal);
      if (generation !== this.generation) return { current: false };
      return { current: true, value };
    } catch (error) {
      if (generation !== this.generation || isAbort(error)) return { current: false };
      throw error;
    }
  }

  cancel(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
  }
}
