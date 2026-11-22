// What every view needs and none of them should build: the API client, what
// the server said about itself at boot, the translator, and a way to say
// something to a screen reader.

import { createContext, useContext } from "react";

import type { Api } from "../lib/api.ts";
import type { Translator } from "../lib/i18n.ts";
import type { Theme } from "../lib/prefs.ts";
import type { Meta, RunnerInfo } from "../lib/types.ts";

export interface AppContextValue {
  api: Api;
  meta: Meta;
  /** The runners with their health; `meta.runners` until the first probe answered. */
  runners: RunnerInfo[];
  runnersChecking: boolean;
  refreshRunners: (probe?: boolean) => void;
  /**
   * Speak a status message through the page's live region. The region is
   * emptied when the view changes; `forNextView` keeps the message through
   * the one change it is said together with (a run started, and its page
   * opened).
   */
  announce: (message: string, options?: { forNextView?: boolean }) => void;
  inboxCount: number | null;
  refreshInbox: () => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const value = useContext(AppContext);
  if (!value) throw new Error("useApp outside of the app shell");
  return value;
}

export interface I18nValue extends Translator {
  setLocale: (locale: string) => void;
  /** The language the server and its runners write free text in (`text_language`); `en` until the server said. */
  textLanguage: string;
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

export const I18nContext = createContext<I18nValue | null>(null);

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n outside of the i18n provider");
  return value;
}
