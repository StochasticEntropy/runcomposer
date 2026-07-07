// Quarantine inbox (DESIGN.md §4/§5, §10): unmatched deliveries stay visible
// until a human attaches them to a run, promotes them to their own run, or
// discards them. Nothing here ever auto-attaches.

import { useEffect, useState } from "react";

import { attachQuarantined, discardQuarantined, listQuarantine, promoteQuarantined } from "../api.js";
import { useI18n } from "../i18n.jsx";

export default function QuarantineView({ onError, onNotice }) {
  const { t } = useI18n();
  const [entries, setEntries] = useState([]);
  const [attachTarget, setAttachTarget] = useState({}); // entry_id -> run id input

  const refresh = () => {
    listQuarantine()
      .then((body) => setEntries(body.entries))
      .catch((error) => onError(error.message));
  };
  useEffect(refresh, []);

  const act = (promise, notice) => {
    promise
      .then((report) => {
        onNotice(notice(report));
        refresh();
      })
      .catch((error) => onError(t("errors.request", { message: error.message })));
  };

  return (
    <section className="panel quarantine-panel">
      <header className="panel-header">
        <h2>{t("quarantine.title")}</h2>
        <button className="link" onClick={refresh}>
          {t("quarantine.refresh")}
        </button>
      </header>
      <p className="muted small">{t("quarantine.hint")}</p>
      {entries.length === 0 ? (
        <p className="muted">{t("quarantine.empty")}</p>
      ) : (
        <table className="runs-table">
          <thead>
            <tr>
              <th>{t("quarantine.columns.entry")}</th>
              <th>{t("quarantine.columns.received")}</th>
              <th>{t("quarantine.columns.reason")}</th>
              <th>{t("quarantine.columns.transport")}</th>
              <th>{t("quarantine.columns.claimedRun")}</th>
              <th>{t("quarantine.columns.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.entry_id}>
                <td className="mono">{entry.entry_id}</td>
                <td className="mono small">{entry.received_at}</td>
                <td>
                  <span className="state state-AWAITING_RESULTS">{entry.reason}</span>
                </td>
                <td>{entry.transport}</td>
                <td className="mono small">{entry.claimed_run_id ?? "—"}</td>
                <td className="quarantine-actions">
                  <input
                    type="text"
                    placeholder={t("quarantine.runIdPlaceholder")}
                    value={attachTarget[entry.entry_id] ?? entry.claimed_run_id ?? ""}
                    onChange={(ev) =>
                      setAttachTarget((old) => ({ ...old, [entry.entry_id]: ev.target.value }))
                    }
                  />
                  <button
                    disabled={!(attachTarget[entry.entry_id] ?? entry.claimed_run_id)}
                    onClick={() =>
                      act(
                        attachQuarantined(
                          entry.entry_id,
                          attachTarget[entry.entry_id] ?? entry.claimed_run_id
                        ),
                        (report) => t("quarantine.attached", { id: report.run_id })
                      )
                    }
                  >
                    {t("quarantine.attach")}
                  </button>
                  <button
                    onClick={() =>
                      act(promoteQuarantined(entry.entry_id), (report) =>
                        t("quarantine.promoted", { id: report.run_id })
                      )
                    }
                  >
                    {t("quarantine.promote")}
                  </button>
                  <button
                    className="link"
                    onClick={() =>
                      act(discardQuarantined(entry.entry_id), () => t("quarantine.discarded"))
                    }
                  >
                    {t("quarantine.discard")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
