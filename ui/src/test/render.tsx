// Rendering a component under test with what every component expects around
// it: the English messages, and — where asked for — an app context.

import { render } from "@testing-library/react";
import type { ReactElement } from "react";

import { AppContext, I18nContext } from "../hooks/context.tsx";
import type { AppContextValue, I18nValue } from "../hooks/context.tsx";
import { createTranslator } from "../lib/i18n.ts";
import en from "../locales/en.json";

export const translator = createTranslator(en, "en");

const i18n: I18nValue = { ...translator, setLocale: () => undefined, theme: "system", setTheme: () => undefined };

export function renderWithI18n(ui: ReactElement, app?: Partial<AppContextValue>) {
  const tree = <I18nContext.Provider value={i18n}>{ui}</I18nContext.Provider>;
  if (!app) return render(tree);
  return render(<AppContext.Provider value={app as AppContextValue}>{tree}</AppContext.Provider>);
}
