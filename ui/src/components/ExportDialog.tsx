// Exporting: handing a run's document out for somebody else to execute
// (DESIGN.md §6.2c). Two things matter and both are easy to get wrong:
//
// - the document is downloaded from the server as a file — a plain link, so
//   the bytes on disk are the bytes the server hashed, and a results bundle
//   marked with that hash verifies (§5);
// - the ingest token exists in the answer to this one request and nowhere
//   else. It is shown once, plainly labelled as such.
//
// An export makes a run that waits for results. The dialog says so before it
// does it — nothing is created by opening it.

import { useId, useRef, useState } from "react";

import { useI18n } from "../hooks/context.tsx";
import { resultsUrl } from "../lib/api.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import { runHref } from "../lib/router.ts";
import type { CreateRunResponse, DispatchResponse } from "../lib/types.ts";
import { CopyButton, Dialog, ErrorText, Notice } from "./ui.tsx";

type Exported = CreateRunResponse | DispatchResponse;
type Format = "json" | "yaml";

const FORMAT_NAME: Record<Format, string> = { json: "JSON", yaml: "YAML" };

interface Props {
  /** What this export is of, in a sentence. */
  intro: string;
  perform: (format: Format) => Promise<Exported>;
  onClose: () => void;
  /** Called once the export exists — the caller may want to refresh. */
  onExported?: (result: Exported) => void;
  /** Shown as "Open run" when the dialog is not already on that run's page. */
  linkToRun?: boolean;
  /** The document as it would be composed — offered before anything is created. */
  previewSpec?: () => Promise<Record<string, unknown>>;
}

export function ExportDialog({ intro, perform, onClose, onExported, linkToRun = true, previewSpec }: Props) {
  const { t } = useI18n();
  const [format, setFormat] = useState<Format>("json");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<Exported | null>(null);
  const inFlight = useRef(false);
  const formatName = useId();
  const [preview, setPreview] = useState<{ text: string | null; loading: boolean; error: ApiError | null }>({
    text: null,
    loading: false,
    error: null,
  });

  const showPreview = async () => {
    if (!previewSpec || preview.loading) return;
    setPreview({ text: null, loading: true, error: null });
    try {
      setPreview({ text: JSON.stringify(await previewSpec(), null, 2), loading: false, error: null });
    } catch (caught) {
      setPreview({ text: null, loading: false, error: asApiError(caught) });
    }
  };

  const submit = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const exported = await perform(format);
      setResult(exported);
      onExported?.(exported);
    } catch (caught) {
      setError(asApiError(caught));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  if (result) {
    const runId = result.run.id;
    const token = result.ingest_token ?? null;
    const extension: Format = result.spec_media_type?.includes("yaml") ? "yaml" : "json";
    const pushUrl = `${window.location.origin}${resultsUrl(runId)}`;
    const command = token
      ? `curl -H "Authorization: Bearer ${token}" -F files=@results.json ${pushUrl}`
      : `curl -F files=@results.json ${pushUrl}`;
    return (
      <Dialog
        title={t("export.doneTitle")}
        onClose={onClose}
        footer={
          <>
            {linkToRun && (
              <a className="btn" href={runHref(runId)} onClick={onClose}>
                {t("export.openRun")}
              </a>
            )}
            <button type="button" className="btn btn-primary" onClick={onClose}>
              {t("action.done")}
            </button>
          </>
        }
      >
        <p role="status">{t("export.doneIntro", { title: result.run.title })}</p>
        <ol className="steps">
          <li>
            <h3>{t("export.stepDownload")}</h3>
            <p>
              <a className="btn btn-primary" href={result.spec_url} download={`runspec-${runId}.${extension}`} data-testid="export-download">
                {t("export.download", { format: FORMAT_NAME[extension] })}
              </a>
            </p>
            <p className="hint">{t("export.downloadHint")}</p>
            {result.spec_sha256 && (
              <p className="hash">
                <span className="muted">SHA-256</span> <code data-testid="export-sha">{result.spec_sha256}</code>
              </p>
            )}
          </li>
          <li>
            <h3>{t("export.stepToken")}</h3>
            {token ? (
              <>
                <Notice tone="warn">{t("export.tokenOnce")}</Notice>
                <div className="secret">
                  <code data-testid="export-token">{token}</code>
                  <CopyButton text={token} label={t("export.copyToken")} />
                </div>
              </>
            ) : (
              <p className="hint">{t("export.noToken")}</p>
            )}
          </li>
          <li>
            <h3>{t("export.stepPush")}</h3>
            <p className="hint">{t("export.pushHint")}</p>
            <div className="secret">
              <code>{command}</code>
              <CopyButton text={command} label={t("action.copy")} />
            </div>
          </li>
        </ol>
      </Dialog>
    );
  }

  return (
    <Dialog
      title={t("export.title")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t("action.cancel")}
          </button>
          <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={busy}>
            {busy ? t("export.working") : t("export.confirm")}
          </button>
        </>
      }
    >
      <p>{intro}</p>
      <p className="hint">{t("export.explain")}</p>
      <fieldset className="segmented">
        <legend>{t("export.format")}</legend>
        {(Object.keys(FORMAT_NAME) as Format[]).map((option) => (
          <label key={option} className={format === option ? "is-on" : undefined}>
            <input type="radio" name={formatName} value={option} checked={format === option} onChange={() => setFormat(option)} />
            {FORMAT_NAME[option]}
          </label>
        ))}
      </fieldset>
      {previewSpec && preview.text === null && (
        <p>
          <button type="button" className="btn btn-small" onClick={() => void showPreview()} disabled={preview.loading}>
            {preview.loading ? t("state.loading") : t("export.preview")}
          </button>
        </p>
      )}
      {preview.error && (
        <Notice tone="error">
          <ErrorText error={preview.error} />
        </Notice>
      )}
      {preview.text !== null && (
        <>
          <p className="hint">{t("export.previewHint")}</p>
          <pre className="message-full" tabIndex={0} aria-label={t("export.preview")} data-testid="spec-preview">
            {preview.text}
          </pre>
        </>
      )}
      {error && (
        <Notice tone="error">
          <ErrorText error={error} />
        </Notice>
      )}
    </Dialog>
  );
}
