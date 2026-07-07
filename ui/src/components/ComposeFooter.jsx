// Runner-aware compose footer (DESIGN.md §10): "Run with: [runner ▾] | Export
// spec". Runners come from the API registry, never hardcoded.

import { useState } from "react";

import { useI18n } from "../i18n.jsx";

export default function ComposeFooter({ runners, disabled, busy, onCompose }) {
  const { t } = useI18n();
  const [title, setTitle] = useState("");
  const [runnerId, setRunnerId] = useState(runners[0]?.id ?? "");
  const activeRunner = runnerId || runners[0]?.id || "";

  return (
    <footer className="compose-footer">
      <label>
        <span className="muted small">{t("compose.titleLabel")}</span>
        <input
          type="text"
          value={title}
          placeholder={t("compose.titlePlaceholder")}
          onChange={(ev) => setTitle(ev.target.value)}
        />
      </label>
      <div className="compose-actions">
        <span className="muted">{t("compose.runWith")}</span>
        <select value={activeRunner} onChange={(ev) => setRunnerId(ev.target.value)}>
          {runners.map((runner) => (
            <option key={runner.id} value={runner.id}>
              {runner.id}
            </option>
          ))}
        </select>
        <button
          className="primary"
          disabled={disabled || busy || !activeRunner}
          onClick={() => onCompose({ runner: activeRunner }, title)}
        >
          {busy ? t("compose.composing") : "▶"}
        </button>
        <button
          disabled={disabled || busy}
          onClick={() => onCompose({ mode: "export" }, title)}
        >
          {t("compose.exportSpec")}
        </button>
      </div>
    </footer>
  );
}
