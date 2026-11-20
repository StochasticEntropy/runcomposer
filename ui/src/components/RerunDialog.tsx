// Running a stored run again — all of it, or only what did not pass in THIS
// run (DESIGN.md §7). The runner and its parameters start out as they were
// for the run's last dispatch and can be changed before anything starts.

import { useMemo, useRef, useState } from "react";

import { useApp, useI18n } from "../hooks/context.tsx";
import { fromPayload, initialValues, toPayload, validateValues } from "../lib/params.ts";
import type { ParamValues } from "../lib/params.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import type { DispatchResponse, RunDetail } from "../lib/types.ts";
import { ParameterForm, RunnerSelect, runnerUsable } from "./RunnerFields.tsx";
import { Dialog, ErrorText, Notice, describeError } from "./ui.tsx";

interface Props {
  run: RunDetail;
  scope: "all" | "failed";
  /** How many items a rerun of the failures would carry. */
  failedCount: number;
  onDone: (response: DispatchResponse) => void;
  onClose: () => void;
}

export function RerunDialog({ run, scope, failedCount, onDone, onClose }: Props) {
  const i18n = useI18n();
  const { t } = i18n;
  const { api, runners } = useApp();
  const latest = run.dispatches[run.dispatches.length - 1];
  const previous = latest && runners.some((runner) => runner.id === latest.mode && runnerUsable(runner)) ? latest.mode : null;
  const [runnerId, setRunnerId] = useState<string | null>(previous ?? runners.find(runnerUsable)?.id ?? null);
  const runner = runners.find((candidate) => candidate.id === runnerId);
  const specs = useMemo(() => (runnerUsable(runner) ? (runner.parameters ?? []) : []), [runner]);
  const [edited, setEdited] = useState<Record<string, ParamValues>>({});
  const values = useMemo(() => {
    if (runnerId && edited[runnerId]) return edited[runnerId];
    // Same runner as last time: start from what it was given then.
    return latest && runnerId === latest.mode ? fromPayload(specs, latest.parameters) : initialValues(specs);
  }, [edited, runnerId, latest, specs]);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const submit = async () => {
    if (inFlight.current || !runnerUsable(runner)) return;
    const found = validateValues(specs, values);
    if (Object.keys(found).length > 0) {
      setProblems(Object.fromEntries(Object.entries(found).map(([name, problem]) => [name, t(`runner.problem.${problem}`)])));
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setProblems({});
    try {
      onDone(await api.rerun(run.id, { scope, runner: runner.id, parameters: toPayload(specs, values) }));
    } catch (caught) {
      const failure = asApiError(caught);
      const parameter = failure.extra.parameter;
      if (failure.code === "invalid-parameter" && typeof parameter === "string" && specs.some((spec) => spec.name === parameter)) {
        const { title, detail } = describeError(i18n, failure);
        setProblems({ [parameter]: `${title} ${detail}` });
      } else {
        setError(failure);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const count = scope === "failed" ? failedCount : run.planned_count;
  return (
    <Dialog
      title={scope === "failed" ? t("rerun.titleFailed") : latest ? t("rerun.titleAll") : t("rerun.titleFirst")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t("action.cancel")}
          </button>
          <button type="submit" form="rerun-form" className="btn btn-primary" disabled={busy || !runnerUsable(runner)}>
            {busy ? t("compose.starting") : t("compose.run", { count })}
          </button>
        </>
      }
    >
      <form
        id="rerun-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <p>{scope === "failed" ? t("rerun.explainFailed", { count, title: run.title }) : t("rerun.explainAll", { count, title: run.title })}</p>
        <RunnerSelect
          value={runnerId}
          onChange={(id) => {
            setRunnerId(id);
            setProblems({});
            setError(null);
          }}
        />
        <ParameterForm
          specs={specs}
          values={values}
          onChange={(next) => {
            if (runnerId) setEdited((current) => ({ ...current, [runnerId]: next }));
            setProblems({});
          }}
          problems={problems}
        />
        {error && (
          <Notice tone="error">
            <ErrorText error={error} />
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
