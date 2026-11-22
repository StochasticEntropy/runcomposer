// The app shell: boot, the providers every view relies on, the header with
// its navigation, and the router. Views are chosen by the URL's hash route
// and nothing else — a link, a reload and the Back button all land in the
// same place.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ErrorBoundary } from "./components/ErrorBoundary.tsx";
import { EmptyState, ErrorState, Loading } from "./components/ui.tsx";
import { AppContext, I18nContext, useI18n } from "./hooks/context.tsx";
import type { AppContextValue, I18nValue } from "./hooks/context.tsx";
import { sleep, usePageVisible } from "./hooks/usePage.ts";
import { useResource } from "./hooks/useResource.ts";
import { useRoute } from "./hooks/useRoute.ts";
import { createApi } from "./lib/api.ts";
import { AVAILABLE_LOCALES, createTranslator } from "./lib/i18n.ts";
import type { Messages } from "./lib/i18n.ts";
import { AVAILABLE_THEMES, LOCALE_KEY, THEME_KEY, applyTheme, browserLanguages, chooseLocale, readPref, writePref } from "./lib/prefs.ts";
import type { Theme } from "./lib/prefs.ts";
import { composeHref, inboxHref, runsHref } from "./lib/router.ts";
import type { Route } from "./lib/router.ts";
import type { RunnerInfo } from "./lib/types.ts";
import de from "./locales/de.json";
import en from "./locales/en.json";
import { ComposeView } from "./views/ComposeView.tsx";
import { InboxView } from "./views/InboxView.tsx";
import { RunView } from "./views/RunView.tsx";
import { RunsView } from "./views/RunsView.tsx";

// The messages travel inside the bundle: the texts always belong to the code
// that uses them, and switching language needs no request.
const MESSAGES: Record<string, Messages> = { en, de };
const INBOX_POLL_MS = 60_000;

const api = createApi();

export function App() {
  const [chosenLocale, setChosenLocale] = useState<string | null>(() => readPref(LOCALE_KEY, AVAILABLE_LOCALES));
  const [theme, setThemeState] = useState<Theme>(() => readPref(THEME_KEY, AVAILABLE_THEMES) ?? "system");
  const meta = useResource(useCallback((signal: AbortSignal) => api.meta(signal), []), "meta");

  // The viewer's own choice, else the language their browser asks for, else
  // the deployment's default — so the page speaks a language from its first
  // paint, and the document says which one.
  const locale = chooseLocale(AVAILABLE_LOCALES, chosenLocale, browserLanguages(), meta.data?.locale_default);
  const translator = useMemo(() => createTranslator(MESSAGES[locale] ?? en, locale), [locale]);

  useEffect(() => applyTheme(theme), [theme]);
  useLayoutEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  // The language the server's own free text is in — what it is marked with.
  const textLanguage = meta.data?.text_language ?? "en";
  const i18n: I18nValue = useMemo(
    () => ({
      ...translator,
      setLocale: (next) => {
        writePref(LOCALE_KEY, next);
        setChosenLocale(next);
      },
      textLanguage,
      theme,
      setTheme: (next) => {
        writePref(THEME_KEY, next);
        setThemeState(next);
      },
    }),
    [translator, textLanguage, theme],
  );

  return (
    <I18nContext.Provider value={i18n}>
      {meta.data ? (
        <Shell meta={meta.data} />
      ) : (
        <div className="boot">
          <p className="brand">runcomposer</p>
          {meta.error ? <ErrorState error={meta.error} onRetry={() => void meta.reload()} /> : <Loading />}
        </div>
      )}
    </I18nContext.Provider>
  );
}

function Shell({ meta }: { meta: AppContextValue["meta"] }) {
  const i18n = useI18n();
  const { t } = i18n;
  const route = useRoute();
  const visible = usePageVisible();
  const mainRef = useRef<HTMLElement | null>(null);

  // -- runners, with their health ---------------------------------------------------
  const [runners, setRunners] = useState<RunnerInfo[]>(meta.runners);
  const [runnersChecking, setRunnersChecking] = useState(false);
  const refreshRunners = useCallback((probe = false) => {
    setRunnersChecking(true);
    api
      .runners(probe)
      .then(setRunners)
      .catch(() => undefined) // the list from boot stays; a run that cannot start says why
      .finally(() => setRunnersChecking(false));
  }, []);
  useEffect(() => refreshRunners(false), [refreshRunners]);

  // -- the inbox badge -----------------------------------------------------------------
  const [inboxCount, setInboxCount] = useState<number | null>(null);
  const refreshInbox = useCallback(() => {
    api
      .quarantine()
      .then((body) => setInboxCount(body.entries.length))
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    void (async () => {
      try {
        while (!controller.signal.aborted) {
          refreshInbox();
          await sleep(INBOX_POLL_MS, controller.signal);
        }
      } catch {
        /* hidden or unmounted */
      }
    })();
    return () => controller.abort();
  }, [visible, refreshInbox]);

  // -- the live region --------------------------------------------------------------------
  const [spoken, setSpoken] = useState("");
  // A message said for the view being opened survives that one change.
  const keepThroughNextView = useRef(false);
  const announce = useCallback((message: string, options: { forNextView?: boolean } = {}) => {
    keepThroughNextView.current = options.forNextView === true;
    // The same text twice in a row would not be read again; a trailing
    // no-break space makes it a new text without changing what is heard.
    setSpoken((current) => (current === message ? `${message}\u00a0` : message));
  }, []);

  // A new view: focus moves to it, so the keyboard starts where the content is.
  const firstRoute = useRef(true);
  useEffect(() => {
    if (firstRoute.current) {
      firstRoute.current = false;
      return;
    }
    // What was said belongs to the view it was said on: a hidden live region
    // keeps no message of another view for a screen reader to come across.
    if (keepThroughNextView.current) keepThroughNextView.current = false;
    else setSpoken("");
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [route.name, route.id]);

  const app: AppContextValue = useMemo(
    () => ({ api, meta, runners, runnersChecking, refreshRunners, announce, inboxCount, refreshInbox }),
    [meta, runners, runnersChecking, refreshRunners, announce, inboxCount, refreshInbox],
  );

  const section = route.name === "run" ? "runs" : route.name;
  const nav: { name: string; href: string; label: string; badge?: number | null }[] = [
    { name: "compose", href: composeHref(), label: t("nav.compose") },
    { name: "runs", href: runsHref(), label: t("nav.runs") },
    { name: "inbox", href: inboxHref(), label: t("nav.inbox"), badge: inboxCount },
  ];

  return (
    <AppContext.Provider value={app}>
      <a className="skip-link" href="#main" onClick={(event) => { event.preventDefault(); mainRef.current?.focus(); }}>
        {t("nav.skip")}
      </a>
      <header className="app-header">
        <a className="brand" href={composeHref()}>
          runcomposer
        </a>
        <nav className="app-nav" aria-label={t("nav.label")}>
          {nav.map((entry) => (
            <a key={entry.name} href={entry.href} aria-current={section === entry.name ? "page" : undefined} data-nav={entry.name}>
              {entry.label}
              {entry.badge ? (
                <span className="nav-badge" data-testid="inbox-badge">
                  {entry.badge}
                  <span className="sr-only"> {t("nav.inboxWaiting", { count: entry.badge })}</span>
                </span>
              ) : null}
            </a>
          ))}
        </nav>
        <div className="app-prefs">
          <label>
            <span className="sr-only">{t("prefs.language")}</span>
            <select className="input input-small" value={i18n.locale} onChange={(event) => i18n.setLocale(event.target.value)} aria-label={t("prefs.language")}>
              {AVAILABLE_LOCALES.map((code) => (
                <option key={code} value={code}>
                  {t(`prefs.locale.${code}`)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">{t("prefs.theme")}</span>
            <select className="input input-small" value={i18n.theme} onChange={(event) => i18n.setTheme(event.target.value as Theme)} aria-label={t("prefs.theme")}>
              {AVAILABLE_THEMES.map((name) => (
                <option key={name} value={name}>
                  {t(`prefs.themes.${name}`)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </header>

      <main id="main" ref={mainRef} tabIndex={-1}>
        <ErrorBoundary
          resetKey={`${route.name}:${route.id}`}
          title={t("error.boundaryTitle")}
          hint={t("error.boundaryHint")}
          reloadLabel={t("action.reload")}
        >
          <View route={route} />
        </ErrorBoundary>
      </main>

      <footer className="app-footer">
        <span>runcomposer {meta.version}</span>
        <ApiLink docs={meta.api?.docs === true} />
      </footer>
      <div className="sr-only" role="status" aria-live="polite" data-testid="announcer">
        {spoken}
      </div>
    </AppContext.Provider>
  );
}

/** The API reference: the documentation viewer where the server serves it
 * (`core.api.docs`), else the OpenAPI description it always serves. */
export function ApiLink({ docs }: { docs: boolean }) {
  const { t } = useI18n();
  return (
    <a href={docs ? "/docs" : "/openapi.json"} target="_blank" rel="noreferrer noopener" data-testid="api-link">
      {docs ? t("nav.apiDocs") : t("nav.apiSchema")}
    </a>
  );
}

function View({ route }: { route: Route }) {
  const { t } = useI18n();
  useEffect(() => {
    if (route.name === "compose") document.title = `${t("nav.compose")} · runcomposer`;
    if (route.name === "notfound") document.title = `${t("error.notFoundTitle")} · runcomposer`;
  }, [route.name, t]);
  switch (route.name) {
    case "compose":
      return <ComposeView route={route} />;
    case "runs":
      return <RunsView route={route} />;
    case "run":
      return <RunView key={route.id} route={route} />;
    case "inbox":
      return <InboxView />;
    default:
      return (
        <div className="page">
          <EmptyState title={t("error.notFoundTitle")}>
            <a className="btn btn-primary" href={composeHref()}>
              {t("error.notFoundBack")}
            </a>
          </EmptyState>
        </div>
      );
  }
}
