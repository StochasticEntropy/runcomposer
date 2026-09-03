// Thin fetch layer over the runcomposer API (DESIGN.md §9). Same-origin:
// the UI is served by `runcomposer serve` itself (or the vite dev proxy).

async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response.json();
}

const post = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

export const getUiConfig = () => request("/api/v1/ui-config");
// Resolved against the catalog: every pattern's concrete tags as their own
// clickable nodes, plus `tags` — the catalog's whole tag universe, which the
// nodes do NOT carry (a leaf whose pattern resolves to a single tag is that
// tag's node and has no tag child, so a tag reached only through a regex is
// nowhere spelled out) and which the value field's completion needs
// (DESIGN.md §2). The unresolved shape is still the endpoint's default.
export const getTaxonomy = () => request("/api/v1/taxonomy?resolve=true");
export const getRunners = () => request("/api/v1/runners");
export const getLocaleMessages = (locale) => request(`/locales/${locale}.json`);
export const compileSelection = (selection) => post("/api/v1/selection/compile", selection);
export const createRun = (payload) => post("/api/v1/runs", payload);
export const listRuns = () => request("/api/v1/runs");
export const getRun = (runId) => request(`/api/v1/runs/${encodeURIComponent(runId)}`);
export const listQuarantine = () => request("/api/v1/quarantine");
export const attachQuarantined = (entryId, runId) =>
  post(`/api/v1/quarantine/${encodeURIComponent(entryId)}/attach`, { run_id: runId });
export const promoteQuarantined = (entryId) =>
  post(`/api/v1/quarantine/${encodeURIComponent(entryId)}/promote`, {});
export const discardQuarantined = (entryId) =>
  request(`/api/v1/quarantine/${encodeURIComponent(entryId)}`, { method: "DELETE" }).catch((error) => {
    // 204 has no JSON body; a parse error here means success
    if (error instanceof SyntaxError) return {};
    throw error;
  });
