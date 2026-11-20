// The inbox: results that arrived and could not be matched to a run on their
// own (DESIGN.md §5). Nothing here is attached automatically — each entry
// waits for a person to attach it to a run, promote it to a run of its own,
// or discard it. Discarding deletes the delivery, so it asks first.

import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ConfirmDialog, Dialog, EmptyState, ErrorState, ErrorText, Loading, Notice, TimeAgo } from "../components/ui.tsx";
import { useApp, useI18n } from "../hooks/context.tsx";
import { useResource } from "../hooks/useResource.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { runHref } from "../lib/router.ts";
import type { QuarantineEntry } from "../lib/types.ts";

type Pending = { kind: "attach" | "promote" | "discard"; entry: QuarantineEntry } | null;

export function InboxView() {
  const { api, meta, refreshInbox } = useApp();
  const { t, tOr } = useI18n();
  const list = useResource(useCallback((signal: AbortSignal) => api.quarantine(signal), [api]), "quarantine");
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [done, setDone] = useState<{ text: string; runId: string | null } | null>(null);
  const [runId, setRunId] = useState("");
  const [format, setFormat] = useState("");
  const acting = useRef(false);
  const runFieldId = useId();
  const formatFieldId = useId();
  const formatListId = useId();
  const formats = meta.configured.filter((plugin) => plugin.section === "parsers").map((plugin) => plugin.id);

  useEffect(() => {
    document.title = `${t("nav.inbox")} · runcomposer`;
  }, [t]);

  const open = (kind: NonNullable<Pending>["kind"], entry: QuarantineEntry) => {
    setPending({ kind, entry });
    setError(null);
    setRunId(entry.claimed_run_id ?? "");
    setFormat(entry.format ?? "");
  };
  const close = () => {
    setPending(null);
    setError(null);
  };

  const act = async (action: () => Promise<{ text: string; runId: string | null }>) => {
    if (acting.current) return;
    acting.current = true;
    setBusy(true);
    setError(null);
    try {
      setDone(await action());
      close();
      await list.reload();
      refreshInbox();
    } catch (caught) {
      setError(asApiError(caught));
    } finally {
      acting.current = false;
      setBusy(false);
    }
  };

  const entries = list.data?.entries;
  const formatField = (
    <div className="field">
      <label className="field-label" htmlFor={formatFieldId}>
        {t("inbox.format")}
      </label>
      <input
        id={formatFieldId}
        className="input"
        type="text"
        list={formatListId}
        value={format}
        placeholder={t("inbox.formatPlaceholder")}
        onChange={(event) => setFormat(event.target.value)}
      />
      <datalist id={formatListId}>
        {formats.map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <p className="hint">{t("inbox.formatHint")}</p>
    </div>
  );

  return (
    <div className="page">
      <header className="page-head">
        <h1>{t("inbox.title")}</h1>
        <button type="button" className="btn btn-small" onClick={() => void list.reload().then(refreshInbox)} disabled={list.loading}>
          {t("action.refresh")}
        </button>
      </header>
      <p className="hint">{t("inbox.hint")}</p>

      {done && (
        <Notice tone="ok" onDismiss={() => setDone(null)}>
          {done.text} {done.runId && <a href={runHref(done.runId)}>{t("inbox.openRun")}</a>}
        </Notice>
      )}
      {list.error && entries && (
        <Notice tone="error">
          <ErrorText error={list.error} />
        </Notice>
      )}

      {!entries ? (
        list.error ? (
          <ErrorState error={list.error} onRetry={() => void list.reload()} />
        ) : (
          <Loading />
        )
      ) : entries.length === 0 ? (
        <EmptyState title={t("inbox.empty")} />
      ) : (
        <div className="table-scroll">
          <table className="table cards-on-narrow inbox-table">
            <thead>
              <tr>
                <th scope="col">{t("inbox.column.received")}</th>
                <th scope="col">{t("inbox.column.reason")}</th>
                <th scope="col">{t("inbox.column.via")}</th>
                <th scope="col">{t("inbox.column.claimed")}</th>
                <th scope="col">{t("inbox.column.format")}</th>
                <th scope="col">{t("inbox.column.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.entry_id} data-entry-id={entry.entry_id}>
                  <td>
                    <TimeAgo iso={entry.received_at} />
                    <span className="cell-sub mono">{entry.entry_id}</span>
                  </td>
                  <td data-label={t("inbox.column.reason")}>
                    <strong>{tOr(`inbox.reason.${entry.reason}`, entry.reason)}</strong>
                    {entry.detail && <span className="cell-sub">{entry.detail}</span>}
                  </td>
                  <td data-label={t("inbox.column.via")}>{tOr(`inbox.transport.${entry.transport}`, entry.transport)}</td>
                  <td data-label={t("inbox.column.claimed")} className="mono">
                    {entry.claimed_run_id ?? "–"}
                  </td>
                  <td data-label={t("inbox.column.format")}>{entry.format ?? "–"}</td>
                  <td className="col-actions">
                    <span className="row">
                      <button type="button" className="btn btn-small" onClick={() => open("attach", entry)}>
                        {t("inbox.attach")}
                      </button>
                      <button type="button" className="btn btn-small" onClick={() => open("promote", entry)}>
                        {t("inbox.promote")}
                      </button>
                      <button type="button" className="btn btn-small btn-danger-quiet" onClick={() => open("discard", entry)}>
                        {t("inbox.discard")}
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pending?.kind === "discard" && (
        <ConfirmDialog
          title={t("inbox.discardTitle")}
          confirmLabel={t("inbox.discardConfirm")}
          danger
          busy={busy}
          error={error}
          onClose={close}
          onConfirm={() =>
            void act(async () => {
              await api.discard(pending.entry.entry_id);
              return { text: t("inbox.discarded"), runId: null };
            })
          }
        >
          <p>{t("inbox.discardExplain")}</p>
          <p className="mono">{pending.entry.entry_id}</p>
        </ConfirmDialog>
      )}

      {pending?.kind === "attach" && (
        <Dialog
          title={t("inbox.attachTitle")}
          onClose={close}
          size="small"
          footer={
            <>
              <button type="button" className="btn" onClick={close}>
                {t("action.cancel")}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || !runId.trim()}
                onClick={() =>
                  void act(async () => {
                    const report = await api.attach(pending.entry.entry_id, runId.trim(), format.trim() || undefined);
                    return { text: t("inbox.attached", { count: report.verdict_count }), runId: report.run_id };
                  })
                }
              >
                {t("inbox.attach")}
              </button>
            </>
          }
        >
          <p>{t("inbox.attachExplain")}</p>
          <div className="field">
            <label className="field-label" htmlFor={runFieldId}>
              {t("inbox.runId")}
            </label>
            <input id={runFieldId} className="input mono" type="text" value={runId} data-autofocus onChange={(event) => setRunId(event.target.value)} />
          </div>
          {formatField}
          {error && (
            <Notice tone="error">
              <ErrorText error={error} />
            </Notice>
          )}
        </Dialog>
      )}

      {pending?.kind === "promote" && (
        <Dialog
          title={t("inbox.promoteTitle")}
          onClose={close}
          size="small"
          footer={
            <>
              <button type="button" className="btn" onClick={close}>
                {t("action.cancel")}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy}
                onClick={() =>
                  void act(async () => {
                    const report = await api.promote(pending.entry.entry_id, format.trim() || undefined);
                    return { text: t("inbox.promoted", { count: report.verdict_count }), runId: report.run_id };
                  })
                }
              >
                {t("inbox.promote")}
              </button>
            </>
          }
        >
          <p>{t("inbox.promoteExplain")}</p>
          {formatField}
          {error && (
            <Notice tone="error">
              <ErrorText error={error} />
            </Notice>
          )}
        </Dialog>
      )}
    </div>
  );
}
