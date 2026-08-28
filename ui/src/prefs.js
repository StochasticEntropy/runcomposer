// Viewer preferences that live in the browser, never on the server: the UI
// language and the colour theme. localStorage access throws outright in
// private-mode browsers, so every read and write is guarded — a preference
// that cannot be persisted simply stays session-local.

export const AVAILABLE_THEMES = ["system", "light", "dark"];

export const LOCALE_KEY = "runcomposer.locale";
export const THEME_KEY = "runcomposer.theme";

export function readPref(key, allowed) {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value) ? value : null;
  } catch {
    return null;
  }
}

export function writePref(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage unavailable: the choice applies to this session only.
  }
}

export function prefersDarkOs() {
  try {
    return Boolean(window.matchMedia?.("(prefers-color-scheme: dark)")?.matches);
  } catch {
    return false;
  }
}

// "system" leaves the attribute off, so the stylesheet's prefers-color-scheme
// block decides; an explicit choice sets it and wins in both directions.
export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function watchOsTheme(onChange) {
  let query = null;
  try {
    query = window.matchMedia?.("(prefers-color-scheme: dark)");
  } catch {
    query = null;
  }
  if (!query?.addEventListener) return () => {};
  const handler = (event) => onChange(event.matches);
  query.addEventListener("change", handler);
  return () => query.removeEventListener("change", handler);
}
