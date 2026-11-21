// Translation without a framework: every literal of the UI lives in
// /locales/<locale>.json (DESIGN.md §10), addressed by a dotted key.
//
//   t("picker.items", {count: 3})    → messages.picker.items.{one|other}
//   t("runState.RUNNING")            → messages.runState.RUNNING
//
// A message that is an object of plural categories is resolved with the
// locale's own plural rules. A key that does not exist comes back as the key:
// loud enough to notice, and `ui/src/locales.test.ts` fails on every key the
// source uses that a locale file lacks.

export type Messages = Record<string, unknown>;
export type Vars = Record<string, string | number>;

export interface Translator {
  locale: string;
  t: (key: string, vars?: Vars) => string;
  /** `t`, but with a fallback instead of the key — for values that come from the server. */
  tOr: (key: string, fallback: string, vars?: Vars) => string;
  has: (key: string) => boolean;
}

export const AVAILABLE_LOCALES = ["en", "de"] as const;

/** The primary language subtag of a BCP 47 tag, lower case: `de` for `de-CH`. */
export const primaryLanguage = (tag: string): string => (tag.split("-")[0] ?? "").toLowerCase();

/**
 * Whether free text the server or a runner wrote — in the deployment's
 * `text_language` (`core.text_language`, served by `/ui-config` and
 * `/meta`) — is marked as not translated on a page in `uiLocale`: when the
 * two are different languages. Regions and scripts do not count: text in
 * `de-CH` on a page in `de` is in the page's language.
 */
export const marksUntranslated = (uiLocale: string, textLanguage: string): boolean =>
  primaryLanguage(uiLocale) !== primaryLanguage(textLanguage);

export function lookup(messages: Messages, key: string): unknown {
  let node: unknown = messages;
  for (const part of key.split(".")) {
    if (node === null || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

export function createTranslator(messages: Messages, locale: string): Translator {
  const plural = new Intl.PluralRules(locale);
  const resolve = (key: string, vars: Vars): string | undefined => {
    let raw = lookup(messages, key);
    if (raw !== null && typeof raw === "object" && typeof vars.count === "number") {
      const forms = raw as Record<string, unknown>;
      raw = forms[plural.select(vars.count)] ?? forms.other;
    }
    if (typeof raw !== "string") return undefined;
    return raw.replace(/\{(\w+)\}/g, (match, name: string) => {
      const value = vars[name];
      if (value === undefined) return match;
      return typeof value === "number" ? new Intl.NumberFormat(locale).format(value) : value;
    });
  };
  return {
    locale,
    t: (key, vars = {}) => resolve(key, vars) ?? key,
    tOr: (key, fallback, vars = {}) => resolve(key, vars) ?? fallback,
    has: (key) => lookup(messages, key) !== undefined,
  };
}
