// The small pieces every view is built from: dialogs, the three states a
// view can be in besides "showing data", badges, and how an error is said.

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

import { useI18n } from "../hooks/context.tsx";
import { useNow } from "../hooks/usePage.ts";
import { absoluteTime, relativeTime } from "../lib/format.ts";
import type { Translator } from "../lib/i18n.ts";
import type { ApiError } from "../lib/request.ts";

// -- errors -----------------------------------------------------------------------

/** An error in the viewer's language: what happened (by the API's stable
 * code), and the server's own words as the detail. */
export function describeError(t: Translator, error: ApiError): { title: string; detail: string } {
  const title = t.tOr(`error.code.${error.code}`, t.t("error.generic"));
  if (error.code === "network") return { title, detail: t.t("error.networkDetail") };
  return { title, detail: error.detail };
}

export function ErrorText({ error }: { error: ApiError }) {
  const i18n = useI18n();
  const { title, detail } = describeError(i18n, error);
  return (
    <>
      <strong>{title}</strong>
      {detail && detail !== title && <span className="error-detail"> {detail}</span>}
    </>
  );
}

export function ErrorState({ error, onRetry, compact = false }: { error: ApiError; onRetry?: () => void; compact?: boolean }) {
  const { t } = useI18n();
  return (
    <div className={`state state-error${compact ? " compact" : ""}`} role="alert">
      <p>
        <ErrorText error={error} />
      </p>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          {t("action.retry")}
        </button>
      )}
    </div>
  );
}

export function Loading({ label, compact = false }: { label?: string; compact?: boolean }) {
  const { t } = useI18n();
  return (
    <div className={`state state-loading${compact ? " compact" : ""}`} role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label ?? t("state.loading")}</span>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <p className="state-title">{title}</p>
      {children}
    </div>
  );
}

export type NoticeTone = "info" | "ok" | "warn" | "error";

export function Notice({ tone = "info", children, onDismiss }: { tone?: NoticeTone; children: ReactNode; onDismiss?: () => void }) {
  const { t } = useI18n();
  return (
    <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}>
      <div className="notice-body">{children}</div>
      {onDismiss && (
        <button type="button" className="icon-btn" onClick={onDismiss} aria-label={t("action.dismiss")}>
          ×
        </button>
      )}
    </div>
  );
}

// -- dialogs ----------------------------------------------------------------------

interface DialogProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: "small" | "medium" | "large";
  /** Extra class on the <dialog>, for a dialog with a layout of its own. */
  className?: string;
}

// The platform's modal dialog: focus is trapped inside, Escape closes, the
// page behind is inert, and focus returns to where it came from. A click on
// the backdrop does NOT close it — a dialog holds unsaved choices, and losing
// them to a stray click is the kind of thing that happens daily.
export function Dialog({ title, onClose, children, footer, size = "medium", className = "" }: DialogProps) {
  const { t } = useI18n();
  const ref = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    // Where the keyboard starts: the element the dialog marks for it, else
    // its first field — never the Close button, which is merely first in
    // the markup. A dialog without either takes the focus itself, so its
    // title is read and Tab goes to its first control.
    const start =
      dialog.querySelector<HTMLElement>("[data-autofocus]") ??
      dialog.querySelector<HTMLElement>(".dialog-body input, .dialog-body select, .dialog-body textarea") ??
      dialog;
    start.focus();
    const onCancel = (event: Event) => {
      event.preventDefault();
      closeRef.current();
    };
    dialog.addEventListener("cancel", onCancel);
    return () => {
      dialog.removeEventListener("cancel", onCancel);
      if (dialog.open) dialog.close();
    };
  }, []);

  return (
    <dialog ref={ref} className={`dialog dialog-${size} ${className}`} aria-labelledby={titleId} tabIndex={-1}>
      <header className="dialog-head">
        <h2 id={titleId}>{title}</h2>
        <button type="button" className="icon-btn" onClick={onClose} aria-label={t("action.close")}>
          ×
        </button>
      </header>
      <div className="dialog-body">{children}</div>
      {footer && <footer className="dialog-foot">{footer}</footer>}
    </dialog>
  );
}

interface ConfirmProps {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  error?: ApiError | null;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmDialog({ title, children, confirmLabel, danger, busy, error, onConfirm, onClose }: ConfirmProps) {
  const { t } = useI18n();
  return (
    <Dialog
      title={title}
      onClose={onClose}
      size="small"
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} data-autofocus>
            {t("action.cancel")}
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </>
      }
    >
      {children}
      {error && (
        <Notice tone="error">
          <ErrorText error={error} />
        </Notice>
      )}
    </Dialog>
  );
}

// -- badges and small displays ----------------------------------------------------

const STATE_TONE: Record<string, string> = {
  COMPOSED: "neutral",
  DISPATCHED: "running",
  RUNNING: "running",
  AWAITING_RESULTS: "info",
  COMPLETE: "neutral",
  FAILED: "danger",
  CANCELLED: "warn",
  STALE: "warn",
};

const RESULT_TONE: Record<string, string> = {
  PASS: "ok",
  FAIL: "danger",
  ERROR: "danger",
  INCOMPLETE: "warn",
  SKIP: "neutral",
  MISSING: "warn",
};

export function StateBadge({ state }: { state: string }) {
  const { tOr } = useI18n();
  return <span className={`badge badge-${STATE_TONE[state] ?? "neutral"}`}>{tOr(`runState.${state}`, state)}</span>;
}

export function ResultBadge({ result }: { result: string | null }) {
  const { tOr } = useI18n();
  if (!result) return null;
  return (
    <span className={`badge badge-solid badge-${RESULT_TONE[result] ?? "neutral"}`}>{tOr(`completion.${result}`, result)}</span>
  );
}

export function VerdictBadge({ status, live = false }: { status: string; live?: boolean }) {
  const { t, tOr } = useI18n();
  return (
    <span className={`badge badge-${RESULT_TONE[status] ?? "neutral"}${live ? " badge-live" : ""}`}>
      {tOr(`verdict.${status}`, status)}
      {live && <span className="sr-only"> ({t("run.live")})</span>}
    </span>
  );
}

export const VERDICT_ORDER = ["FAIL", "ERROR", "MISSING", "SKIP", "PASS"] as const;

/** A stacked bar of verdict counts over what was planned. The numbers are in
 * the accessible name; the bar itself is decoration. */
export function VerdictBar({ counts, planned, active = false }: { counts: Record<string, number>; planned: number; active?: boolean }) {
  const { t, tOr } = useI18n();
  const known = VERDICT_ORDER.filter((status) => (counts[status] ?? 0) > 0);
  const other = Object.keys(counts).filter((status) => !(VERDICT_ORDER as readonly string[]).includes(status) && counts[status]! > 0);
  const done = Object.values(counts).reduce((total, value) => total + value, 0);
  const total = Math.max(planned, done, 1);
  const parts = [...known, ...other].map((status) => `${counts[status]} ${tOr(`verdict.${status}`, status)}`);
  const label = planned > 0 ? t("progress.label", { done, total: Math.max(planned, done) }) : t("progress.none");
  return (
    <div className="verdict-bar-wrap">
      <div
        className={`verdict-bar${active ? " is-active" : ""}`}
        role="img"
        aria-label={parts.length > 0 ? `${label}: ${parts.join(", ")}` : label}
      >
        {[...known, ...other].map((status) => (
          <span
            key={status}
            className={`seg seg-${RESULT_TONE[status] ?? "neutral"}`}
            style={{ width: `${((counts[status] ?? 0) / total) * 100}%` }}
          />
        ))}
      </div>
    </div>
  );
}

export function VerdictCounts({ counts }: { counts: Record<string, number> }) {
  const { tOr } = useI18n();
  const order = [...VERDICT_ORDER, ...Object.keys(counts).filter((status) => !(VERDICT_ORDER as readonly string[]).includes(status))];
  const shown = order.filter((status) => (counts[status] ?? 0) > 0);
  if (shown.length === 0) return null;
  return (
    <span className="verdict-counts">
      {shown.map((status) => (
        <span key={status} className={`count count-${RESULT_TONE[status] ?? "neutral"}`}>
          {counts[status]} {tOr(`verdict.${status}`, status)}
        </span>
      ))}
    </span>
  );
}

export function LabelChips({ labels }: { labels: Record<string, string> }) {
  const entries = Object.entries(labels);
  if (entries.length === 0) return null;
  return (
    <span className="label-chips">
      {entries.map(([key, value]) => (
        <span key={key} className="label-chip">
          {key}={value}
        </span>
      ))}
    </span>
  );
}

/** "3 minutes ago", with the exact local time one hover or focus away. */
export function TimeAgo({ iso }: { iso: string | null | undefined }) {
  const { locale } = useI18n();
  const now = useNow();
  if (!iso) return <>–</>;
  return (
    <time dateTime={iso} title={absoluteTime(iso, locale)}>
      {relativeTime(iso, now, locale)}
    </time>
  );
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // No clipboard access (an insecure origin): fall back to the selection API.
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    setCopied(true);
  };
  return (
    <button type="button" className="btn btn-small" onClick={() => void copy()}>
      {copied ? t("action.copied") : label}
      <span className="sr-only" role="status">
        {copied ? t("action.copied") : ""}
      </span>
    </button>
  );
}

/** A native checkbox that can show "some of them". */
export function TriCheckbox({
  state,
  onChange,
  label,
  disabled,
}: {
  state: "all" | "some" | "none";
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === "some";
  }, [state]);
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={state === "all"}
      aria-checked={state === "some" ? "mixed" : state === "all"}
      aria-label={label}
      disabled={disabled}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}
