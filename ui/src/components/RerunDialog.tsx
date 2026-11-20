// Running a stored run again — all of it, or only what did not pass in THIS
// run (DESIGN.md §7). The runner and its parameters start out as they were
// for the run's last dispatch and can be changed before anything starts.

import { useCallback, useId, useMemo, useRef, useState } from "react";

import { useApp, useI18n } from "../hooks/context.tsx";
import { usePlan } from "../hooks/usePlan.ts";
import { useResource } from "../hooks/useResource.ts";
import { fromPayload, initialValues, toPayload, validateValues } from "../lib/params.ts";
import type { ParamValues } from "../lib/params.ts";
import { asApiError } from "../lib/request.ts";
import type { ApiError } from "../lib/request.ts";
import type { DispatchResponse, RunDetail } from "../lib/types.ts";
import { PlanView } from "./PlanView.tsx";
import { ParameterForm, RunnerSelect, problemTexts, runnerUsable } from "./RunnerFields.tsx";
import type { FieldProblem } from "./RunnerFields.tsx";
import { Dialog, ErrorText, Notice, describeError } from "./ui.tsx";

interface Props {
  run: RunDetail;
  scope: "all" | "failed";
  onDone: (response: DispatchResponse) => void;
  onClose: () => void;
}

export function RerunDialog({ run, scope, onDone, onClose }: Props) {
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
  const [problems, setProblems] = useState<Record<string, FieldProblem>>({});
  const [title, setTitle] = useState("");
  const [planning, setPlanning] = useState(false);
  const titleId = useId();
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);

  const submit = async () => {
    if (inFlight.current || !runnerUsable(runner)) return;
    const found = validateValues(specs, values);
    if (Object.keys(found).length > 0) {
      setProblems(problemTexts(t, found));
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setProblems({});
    try {
      onDone(
        await api.rerun(run.id, {
          scope,
          runner: runner.id,
          parameters: toPayload(specs, values),
          ...(title.trim() ? { title: title.trim() } : {}),
        }),
      );
    } catch (caught) {
      const failure = asApiError(caught);
      const parameter = failure.extra.parameter;
      if (failure.code === "invalid-parameter" && typeof parameter === "string" && specs.some((spec) => spec.name === parameter)) {
        const { title, detail } = describeError(i18n, failure);
        setProblems({ [parameter]: { text: title, foreign: detail } });
      } else {
        setError(failure);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  // What "the failures" are is the server's to say — the very query a rerun
  // composes from — so the dialog asks instead of counting rows itself.
  const failed = useResource(
    useCallback(
      async (signal: AbortSignal) => (scope === "failed" ? (await api.count({ history: `failed+error+missing@run:${run.id}` }, signal)).count : 0),
      [api, scope, run.id],
    ),
    `failed:${run.id}:${scope}`,
  );
  const count = scope === "failed" ? failed.data : run.planned_count;

  // The plan of exactly what this dialog would start: for "run again" the
  // stored run with these options, for the failures the selection the rerun
  // composes from. Keyed by runner and options — changing either drops the
  // plan on screen and asks again.
  const canPlan = runnerUsable(runner) && (runner.capabilities ?? []).includes("plan");
  const planQuestion = useMemo(() => {
    if (!planning || !runnerUsable(runner) || !canPlan) return null;
    if (Object.keys(validateValues(specs, values)).length > 0) return null;
    const parameters = toPayload(specs, values);
    const key = JSON.stringify({ run: run.id, scope, runner: runner.id, parameters });
    return {
      key,
      ask: (signal: AbortSignal) =>
        scope === "failed"
          ? api.plan({ selection: { history: `failed+error+missing@run:${run.id}` }, runner: runner.id, parameters }, signal)
          : api.planRun(run.id, { runner: runner.id, parameters }, signal),
    };
  }, [planning, runner, canPlan, specs, values, run.id, scope, api]);
  const plan = usePlan(planQuestion);
  return (
    <Dialog
      title={scope === "failed" ? t("rerun.titleFailed") : latest ? t("rerun.titleAll") : t("rerun.titleFirst")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t("action.cancel")}
          </button>
          <button type="submit" form="rerun-form" className="btn btn-primary" disabled={busy || !runnerUsable(runner) || count === undefined}>
            {busy ? t("compose.starting") : count === undefined ? t("state.loading") : t("compose.run", { count })}
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
        {count !== undefined && (
          <p>{scope === "failed" ? t("rerun.explainFailed", { count, title: run.title }) : t("rerun.explainAll", { count, title: run.title })}</p>
        )}
        {failed.error && (
          <Notice tone="error">
            <ErrorText error={failed.error} />
          </Notice>
        )}
        <RunnerSelect
          value={runnerId}
          onChange={(id) => {
            setRunnerId(id);
            setProblems({});
            setError(null);
          }}
        />
        {scope === "failed" && (
          <div className="field">
            <label className="field-label" htmlFor={titleId}>
              {t("rerun.title")}
            </label>
            <input
              id={titleId}
              className="input"
              type="text"
              value={title}
              maxLength={200}
              placeholder={run.title}
              onChange={(event) => setTitle(event.target.value)}
            />
            <p className="hint">{t("rerun.titleHint")}</p>
          </div>
        )}
        <ParameterForm
          specs={specs}
          values={values}
          onChange={(next) => {
            if (runnerId) setEdited((current) => ({ ...current, [runnerId]: next }));
            setProblems({});
          }}
          problems={problems}
          plugin={runner?.plugin}
          variant={typeof runner?.health?.data.partition_mode === "string" ? runner.health.data.partition_mode : undefined}
        />
        {canPlan && (
          <div className="rerun-plan">
            <button type="button" className="btn btn-small" aria-expanded={planning} onClick={() => setPlanning((open) => !open)}>
              {planning ? t("plan.hide") : t("plan.show")}
            </button>
            {planning && (planQuestion ? <PlanView state={plan.state} onRetry={plan.retry} /> : <p className="hint">{t("plan.fixOptions")}</p>)}
          </div>
        )}
        {error && (
          <Notice tone="error">
            <ErrorText error={error} />
          </Notice>
        )}
      </form>
    </Dialog>
  );
}
