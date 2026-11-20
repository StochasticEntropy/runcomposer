// Hash routing: `#/runs/01ABC?tab=activity`.
//
// The server serves one document at `/`; everything after `#` never reaches
// it. So a deep link survives a reload without the server knowing a single UI
// route, and the UI works unchanged behind a reverse proxy that mounts it
// under some path prefix.

export type RouteName = "compose" | "runs" | "run" | "inbox" | "notfound";

export interface Route {
  name: RouteName;
  /** The run id of a `run` route. */
  id: string;
  query: Record<string, string>;
}

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#/, "");
  const mark = raw.indexOf("?");
  const path = (mark === -1 ? raw : raw.slice(0, mark)).replace(/^\/+|\/+$/g, "");
  const query: Record<string, string> = {};
  if (mark !== -1) for (const [key, value] of new URLSearchParams(raw.slice(mark + 1))) query[key] = value;
  const segments = path === "" ? [] : path.split("/").map(decodeSegment);
  const [first, second, ...rest] = segments;
  if (first === undefined || (first === "compose" && second === undefined)) return { name: "compose", id: "", query };
  if (first === "runs" && second === undefined) return { name: "runs", id: "", query };
  if (first === "runs" && second && rest.length === 0) return { name: "run", id: second, query };
  if (first === "inbox" && second === undefined) return { name: "inbox", id: "", query };
  return { name: "notfound", id: "", query };
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** Query values that are empty, null or undefined are left out. */
export function formatHash(path: string, query: Record<string, string | number | null | undefined> = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return `#${path}${text ? `?${text}` : ""}`;
}

export const composeHref = (query: Record<string, string | null | undefined> = {}) => formatHash("/compose", query);
export const runsHref = (query: Record<string, string | number | null | undefined> = {}) => formatHash("/runs", query);
export const runHref = (id: string, query: Record<string, string | null | undefined> = {}) =>
  formatHash(`/runs/${encodeURIComponent(id)}`, query);
export const inboxHref = () => formatHash("/inbox");
