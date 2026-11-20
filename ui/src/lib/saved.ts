// Selections a person saved under a name — kept in this browser.
//
// A saved selection is everything Compose holds: the filter, the unticked
// items, the runner with its options, title and labels. It lives in
// localStorage: per browser and per origin, not on the server and not shared
// with anybody (what a deployment offers everyone is `ui.presets`). Storage
// may be unavailable — private mode, a full quota, a policy — so every access
// is guarded: without storage nothing can be saved, and nothing else breaks.

export interface SavedSelection {
  name: string;
  /** The filter in its text form. */
  filter: string;
  excluded: string[];
  runner: string | null;
  /** The runner's option values, as the form holds them. */
  params: Record<string, unknown>;
  title: string;
  labels: Record<string, string>;
  savedAt: string;
}

export const SAVED_KEY = "runcomposer.saved-selections.v1";

type Storage = Pick<globalThis.Storage, "getItem" | "setItem">;

const usable = (entry: unknown): entry is SavedSelection =>
  entry !== null &&
  typeof entry === "object" &&
  typeof (entry as SavedSelection).name === "string" &&
  typeof (entry as SavedSelection).filter === "string";

function storageOf(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Every saved selection, by name. An unreadable store is an empty one. */
export function loadSaved(storage: Storage | null = storageOf()): SavedSelection[] {
  try {
    const parsed: unknown = JSON.parse(storage?.getItem(SAVED_KEY) ?? "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(usable)
      .map((entry) => ({ ...entry, excluded: entry.excluded ?? [], params: entry.params ?? {}, labels: entry.labels ?? {}, title: entry.title ?? "", runner: entry.runner ?? null }))
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
  } catch {
    return [];
  }
}

/** Write the list; false when the browser would not keep it. */
function store(list: SavedSelection[], storage: Storage | null): boolean {
  try {
    if (!storage) return false;
    storage.setItem(SAVED_KEY, JSON.stringify(list));
    return true;
  } catch {
    return false;
  }
}

export interface SavedResult {
  ok: boolean;
  list: SavedSelection[];
}

/** Save under a name; a selection of that name is replaced. */
export function saveSelection(entry: SavedSelection, storage: Storage | null = storageOf()): SavedResult {
  const name = entry.name.trim();
  const list = [...loadSaved(storage).filter((other) => other.name !== name), { ...entry, name }];
  const ok = name !== "" && store(list, storage);
  return { ok, list: loadSaved(storage) };
}

/** Rename; refused when the new name is empty or already taken. */
export function renameSelection(from: string, to: string, storage: Storage | null = storageOf()): SavedResult {
  const name = to.trim();
  const list = loadSaved(storage);
  if (name === "" || (name !== from && list.some((entry) => entry.name === name))) return { ok: false, list };
  const ok = store(list.map((entry) => (entry.name === from ? { ...entry, name } : entry)), storage);
  return { ok, list: loadSaved(storage) };
}

export function deleteSelection(name: string, storage: Storage | null = storageOf()): SavedResult {
  const ok = store(loadSaved(storage).filter((entry) => entry.name !== name), storage);
  return { ok, list: loadSaved(storage) };
}
