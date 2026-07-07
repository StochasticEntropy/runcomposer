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
export const getTaxonomy = () => request("/api/v1/taxonomy");
export const getRunners = () => request("/api/v1/runners");
export const getLocaleMessages = (locale) => request(`/locales/${locale}.json`);
export const compileSelection = (selection) => post("/api/v1/selection/compile", selection);
export const createRun = (payload) => post("/api/v1/runs", payload);
export const listRuns = () => request("/api/v1/runs");
export const getRun = (runId) => request(`/api/v1/runs/${encodeURIComponent(runId)}`);
