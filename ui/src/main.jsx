import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.jsx";
import { getLocaleMessages, getUiConfig } from "./api.js";
import { AVAILABLE_LOCALES, I18nProvider } from "./i18n.jsx";
import {
  AVAILABLE_THEMES,
  LOCALE_KEY,
  THEME_KEY,
  applyTheme,
  prefersDarkOs,
  readPref,
  watchOsTheme,
  writePref,
} from "./prefs.js";
import "./styles.css";

function Root() {
  const [uiConfig, setUiConfig] = useState(null);
  const [locale, setLocale] = useState(null);
  const [messages, setMessages] = useState(null);
  const [theme, setTheme] = useState(() => readPref(THEME_KEY, AVAILABLE_THEMES) ?? "system");
  const [osDark, setOsDark] = useState(prefersDarkOs);

  useEffect(() => {
    getUiConfig()
      .then((config) => {
        setUiConfig(config);
        const stored = readPref(LOCALE_KEY, AVAILABLE_LOCALES);
        const initial = stored ?? config.locale_default;
        setLocale(AVAILABLE_LOCALES.includes(initial) ? initial : "en");
      })
      .catch(() => {
        setUiConfig({});
        setLocale("en");
      });
  }, []);

  useEffect(() => {
    if (!locale) return;
    getLocaleMessages(locale)
      .then(setMessages)
      .catch(() => setMessages({}));
  }, [locale]);

  useEffect(() => applyTheme(theme), [theme]);
  useEffect(() => watchOsTheme(setOsDark), []);

  if (!uiConfig || !locale || !messages) return null;

  // What the page actually renders as — what widgets shipping their own themes
  // (the SVAR filter builder) have to be told explicitly.
  const resolvedTheme = theme === "system" ? (osDark ? "dark" : "light") : theme;

  return (
    <I18nProvider locale={locale} messages={messages}>
      <App
        uiConfig={uiConfig}
        locale={locale}
        onLocaleChange={(code) => {
          writePref(LOCALE_KEY, code);
          setLocale(code);
        }}
        theme={theme}
        resolvedTheme={resolvedTheme}
        onThemeChange={(next) => {
          writePref(THEME_KEY, next);
          setTheme(next);
        }}
      />
    </I18nProvider>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Root />
  </StrictMode>
);
