// Viewer preferences that live in the browser, never on the server: the UI
// language and the colour theme. Storage access throws outright in some
// private modes, so every read and write is guarded — a preference that
// cannot be persisted simply applies to this session.

export const AVAILABLE_THEMES = ["system", "light", "dark"] as const;
export type Theme = (typeof AVAILABLE_THEMES)[number];

export const LOCALE_KEY = "runcomposer.locale";
export const THEME_KEY = "runcomposer.theme";

export function readPref<T extends string>(key: string, allowed: readonly T[]): T | null {
  try {
    const value = window.localStorage.getItem(key);
    return allowed.includes(value as T) ? (value as T) : null;
  } catch {
    return null;
  }
}

export function writePref(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage unavailable: the choice applies to this session only.
  }
}

export function readSession<T>(key: string): T | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

export function writeSession(key: string, value: unknown): void {
  try {
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Not persisted: the state lives as long as the page does.
  }
}

// "system" leaves the attribute off, so the stylesheet's prefers-color-scheme
// block decides; an explicit choice sets it and wins in both directions.
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}
