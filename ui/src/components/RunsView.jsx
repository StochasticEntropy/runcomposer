// Runs list + detail (DESIGN.md §10): dispatch mode and full lifecycle state
// (incl. AWAITING_RESULTS) are first-class columns.

import { useEffect, useState } from "react";

import { getRun, listRuns } from "../api.js";
import { useI18n } from "../i18n.jsx";

function RunDetail({ run }) {
  const { t } = useI18n();
  return (
    <div className="run-detail">
      <h3>{t("runs.detail.dispatches")}</h3>
      {run.dispatches.length === 0 && <p className="muted">{t("runs.detail.none")}</p>}
      {run.dispatches.map((dispatch) => (
        <p key={dispatch.dispatch_id} className="mono small">
          {dispatch.dispatch_id} — {t("runs.detail.mode")}: {dispatch.mode},{" "}
          {t("runs.detail.shards")}: {dispatch.declared_shards ?? "?"}
        </p>
      ))}
      <h3>{t("runs.detail.deliveries")}</h3>
      {run.deliveries.length === 0 && <p className="muted">{t("runs.detail.none")}</p>}
      {run.deliveries.map((delivery) => (
        <p key={delivery.delivery_id} className="mono small">
          {t("runs.detail.shard")} {delivery.shard} — {t("runs.detail.format")}: {delivery.format}
        </p>
      ))}
      <h3>{t("runs.detail.verdicts")}</h3>
      <p className="mono small">
        {Object.entries(run.verdict_summary ?? {})
          .map(([status, count]) => `${count} ${status}`)
          .join(", ") || t("runs.detail.none")}
      </p>
    </div>
  );
}

export default function RunsView({ onError }) {
  const { t } = useI18n();
  const [runs, setRuns] = useState([]);
  const [openRun, setOpenRun] = useState(null);

  const refresh = () => {
    listRuns()
      .then((body) => setRuns(body.runs))
      .catch((error) => onError(error.message));
  };
  useEffect(refresh, []);

  const toggle = (runId) => {
    if (openRun?.id === runId) {
      setOpenRun(null);
      return;
    }
    getRun(runId)
      .then(setOpenRun)
      .catch((error) => onError(error.message));
  };

  return (
    <section className="panel runs-panel">
      <header className="panel-header">
        <h2>{t("runs.title")}</h2>
        <button className="link" onClick={refresh}>
          {t("runs.refresh")}
        </button>
      </header>
      {runs.length === 0 ? (
        <p className="muted">{t("runs.empty")}</p>
      ) : (
        <table className="runs-table">
          <thead>
            <tr>
              <th>{t("runs.columns.id")}</th>
              <th>{t("runs.columns.state")}</th>
              <th>{t("runs.columns.result")}</th>
              <th>{t("runs.columns.created")}</th>
              <th>{t("runs.columns.title")}</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <>
                <tr key={run.id} className="run-row" onClick={() => toggle(run.id)}>
                  <td className="mono">{run.id}</td>
                  <td>
                    <span className={`state state-${run.state}`}>{run.state}</span>
                  </td>
                  <td>{run.completion ?? "—"}</td>
                  <td className="mono small">{run.created_at}</td>
                  <td>{run.title}</td>
                </tr>
                {openRun?.id === run.id && (
                  <tr key={`${run.id}-detail`}>
                    <td colSpan={5}>
                      <RunDetail run={openRun} />
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
