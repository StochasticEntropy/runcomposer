import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import App from "./App.jsx";
import { getLocaleMessages, getUiConfig } from "./api.js";
import { AVAILABLE_LOCALES, I18nProvider } from "./i18n.jsx";
import "./styles.css";

function Root() {
  const [uiConfig, setUiConfig] = useState(null);
  const [locale, setLocale] = useState(null);
  const [messages, setMessages] = useState(null);

  useEffect(() => {
    getUiConfig()
      .then((config) => {
        setUiConfig(config);
        const stored = localStorage.getItem("runcomposer.locale");
        const initial = AVAILABLE_LOCALES.includes(stored) ? stored : config.locale_default;
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

  if (!uiConfig || !locale || !messages) return null;

  return (
    <I18nProvider locale={locale} messages={messages}>
      <App
        uiConfig={uiConfig}
        locale={locale}
        onLocaleChange={(code) => {
          localStorage.setItem("runcomposer.locale", code);
          setLocale(code);
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
