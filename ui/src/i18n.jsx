// Minimal i18n: every UI literal lives in /locales/<locale>.json (DESIGN.md §10).
// t("a.b.c", {var: x}) resolves nested keys and {var} placeholders.

import { createContext, useContext } from "react";

const I18nContext = createContext({ t: (key) => key, locale: "en" });

export function I18nProvider({ locale, messages, children }) {
  const t = (key, vars = {}) => {
    const raw = key
      .split(".")
      .reduce((node, part) => (node && typeof node === "object" ? node[part] : undefined), messages);
    if (typeof raw !== "string") return key;
    return raw.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
  };
  return <I18nContext.Provider value={{ t, locale }}>{children}</I18nContext.Provider>;
}

export const useI18n = () => useContext(I18nContext);

export const AVAILABLE_LOCALES = ["en", "de"];
