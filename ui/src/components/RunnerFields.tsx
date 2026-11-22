// Choosing a runner and filling in what it asks for. The form is rendered
// from the runner's own declaration (DESIGN.md §6.2): the UI knows the five
// kinds of parameter, not the parameters of any runner.

import { useId, useState } from "react";

import { useApp, useI18n } from "../hooks/context.tsx";
import { parseLabel } from "../lib/format.ts";
import { Foreign, describeError } from "./ui.tsx";
import type { Translator } from "../lib/i18n.ts";
import { wholeMatch } from "../lib/params.ts";
import type { MappingRow, ParamProblem, ParamValues } from "../lib/params.ts";
import type { ApiError } from "../lib/request.ts";
import type { HealthCheck, ParameterSpec, RunnerInfo } from "../lib/types.ts";

/** What is wrong with a field: the UI's own sentence, and — when the server
 * refused the value — the server's words as supporting detail. */
export interface FieldProblem {
  text: string;
  foreign?: string;
}

/** What kind of refusal an `invalid-parameter` answer is (its `problem`) — the server's closed list. */
export const PARAMETER_PROBLEMS = ["unknown", "required", "type", "choice", "pattern", "key-pattern", "runner"] as const;
type ParameterProblem = (typeof PARAMETER_PROBLEMS)[number];

/**
 * A value the SERVER refused (`invalid-parameter`), put on its field. The
 * answer says what kind of refusal it is (`problem`) and, for a mapping,
 * which name (`key`): every kind the declaration decides is worded by the
 * UI itself. Only a refusal by the runner (`runner`) carries a reason the
 * UI cannot know — its sentence is shown, as the runner's.
 */
export function refusedValue(i18n: Translator, _spec: ParameterSpec | undefined, error: ApiError): FieldProblem {
  const problem = error.extra.problem as ParameterProblem | undefined;
  const key = typeof error.extra.key === "string" ? error.extra.key : null;
  if (problem && problem !== "runner" && PARAMETER_PROBLEMS.includes(problem)) {
    const text = i18n.t(`runner.refused.${problem}`);
    return { text: key ? `${text} (${key})` : text };
  }
  const { title, detail } = describeError(i18n, error);
  return { text: problem === "runner" ? i18n.t("runner.refused.runner") : title, foreign: detail };
}

/** What is wrong with a value, in the viewer's words. */
export function problemTexts(t: Translator["t"], problems: Record<string, ParamProblem>): Record<string, FieldProblem> {
  return Object.fromEntries(Object.entries(problems).map(([name, problem]) => [name, { text: t(`runner.problem.${problem}`) }]));
}

// The reference runners are part of this product, so the UI has their words
// in its locale files, by plugin id — with what the server sent as the
// fallback. Anything else a runner says about itself is the runner's text.

/** A built-in runner's title in the viewer's language; null for any other runner. */
export function builtinText(i18n: Pick<Translator, "tOr">, plugin: string | undefined, part: string, vars: Record<string, string | number> = {}): string | null {
  if (!plugin) return null;
  return i18n.tOr(`builtin.runner.${plugin}.${part}`, "", vars) || null;
}

/** What a built-in runner does, worded for the way it is configured to get its results back. */
function builtinDescription(i18n: Pick<Translator, "tOr">, runner: RunnerInfo): string | null {
  const completion = (runner.capabilities ?? []).find((capability) => capability === "callback" || capability === "poll");
  return (completion && builtinText(i18n, runner.plugin, `description_${completion}`)) || builtinText(i18n, runner.plugin, "description");
}

/** A built-in parameter's label or help; null when the runner (or the deployment) declared its own. */
function builtinParam(i18n: Pick<Translator, "tOr">, plugin: string | undefined, name: string, part: string): string | null {
  if (!plugin) return null;
  return i18n.tOr(`builtin.param.${plugin}.${name}.${part}`, "") || null;
}

/** A built-in runner's health summary, worded from its status and the data it reports. */
function builtinHealth(i18n: Pick<Translator, "tOr">, runner: RunnerInfo): string | null {
  const health = runner.health;
  if (!runner.plugin || !health) return null;
  const version = health.data.robot_version ?? health.data.version;
  if (typeof version !== "string" && i18n.tOr(`builtin.health.${runner.plugin}.${health.status}`, "").includes("{version}")) return null;
  return i18n.tOr(`builtin.health.${runner.plugin}.${health.status}`, "", { version: typeof version === "string" ? version : "" }) || null;
}

/** The checks a runner reports about itself (`health.data.checks`), if it does. */
export function healthChecks(runner: RunnerInfo | undefined): HealthCheck[] {
  const checks = runner?.health?.data?.checks;
  if (!Array.isArray(checks)) return [];
  return checks.filter(
    (entry): entry is HealthCheck => entry !== null && typeof entry === "object" && typeof (entry as HealthCheck).check === "string",
  );
}

export const runnerUsable = (runner: RunnerInfo | undefined): runner is RunnerInfo => Boolean(runner) && !runner!.error;

const HEALTH_TONE: Record<string, string> = { ok: "ok", degraded: "warn", down: "danger", unknown: "neutral" };

export function RunnerSelect({ value, onChange }: { value: string | null; onChange: (id: string) => void }) {
  const i18n = useI18n();
  const { t, tOr } = i18n;
  const { runners, refreshRunners, runnersChecking } = useApp();
  const selectId = useId();
  const statusId = useId();
  const selected = runners.find((runner) => runner.id === value);
  const broken = runners.filter((runner) => runner.error);

  if (runners.length === 0) return <p className="hint">{t("runner.none")}</p>;

  return (
    <div className="field">
      <label className="field-label" htmlFor={selectId}>
        {t("runner.label")}
      </label>
      <select
        id={selectId}
        className="input"
        value={value ?? ""}
        aria-describedby={statusId}
        onChange={(event) => onChange(event.target.value)}
      >
        {value === null && <option value="">{t("runner.choose")}</option>}
        {runners.map((runner) => {
          const status = runner.health?.status;
          const suffix = runner.error
            ? ` — ${t("runner.unavailable")}`
            : status && status !== "ok"
              ? ` — ${tOr(`health.${status}`, status)}`
              : "";
          return (
            <option key={runner.id} value={runner.id} disabled={Boolean(runner.error)}>
              {(() => {
                const title = builtinText(i18n, runner.plugin, "title") ?? runner.title;
                return title && title !== runner.id ? `${runner.id} — ${title}` : runner.id;
              })()}
              {suffix}
            </option>
          );
        })}
      </select>
      <div id={statusId} className="runner-status">
        {selected && !selected.error && (
          <>
            {selected.description && (
              <p className="hint">{builtinDescription(i18n, selected) ?? <Foreign>{selected.description}</Foreign>}</p>
            )}
            <p className="runner-health">
              {selected.health ? (
                <>
                  <span className={`badge badge-${HEALTH_TONE[selected.health.status] ?? "neutral"}`}>
                    {tOr(`health.${selected.health.status}`, selected.health.status)}
                  </span>{" "}
                  <span>{builtinHealth(i18n, selected) ?? <Foreign>{selected.health.detail}</Foreign>}</span>
                </>
              ) : (
                <span className="muted">{t("runner.checking")}</span>
              )}{" "}
              <button type="button" className="btn btn-small btn-quiet" onClick={() => refreshRunners(true)} disabled={runnersChecking}>
                {t("runner.recheck")}
              </button>
            </p>
          </>
        )}
        {healthChecks(selected).length > 0 && (
          <ul className="health-checks" aria-label={t("runner.checks")}>
            {healthChecks(selected).map((entry) => (
              <li key={entry.check} data-ok={entry.ok}>
                <span className={`badge badge-${entry.ok ? "ok" : "danger"}`}>{entry.ok ? t("runner.checkOk") : t("runner.checkFailed")}</span>{" "}
                <Foreign>
                  <strong>{entry.check}</strong>
                  {entry.detail && <span className="muted"> — {entry.detail}</span>}
                </Foreign>
              </li>
            ))}
          </ul>
        )}
        {selected?.health && ["down", "degraded"].includes(selected.health.status) && (
          <p className="field-error" role="status">
            {t("runner.unhealthyHint")}
          </p>
        )}
        {broken.length > 0 && (
          <ul className="runner-problems">
            {broken.map((runner) => (
              <li key={runner.id}>
                <span className="badge badge-danger">{t("runner.unavailable")}</span> <strong>{runner.id}</strong>: <Foreign>{runner.error}</Foreign>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

interface FormProps {
  specs: ParameterSpec[];
  values: ParamValues;
  onChange: (values: ParamValues) => void;
  /** name → message, from client-side checks or from the server's refusal. */
  problems: Record<string, FieldProblem>;
  /** The plugin behind the runner — a built-in one has its labels in the locale files. */
  plugin?: string;
  /** For a built-in parameter with two meanings: which one applies (robot-pool's partition mode). */
  variant?: string;
}

export function ParameterForm({ specs, values, onChange, problems, plugin, variant }: FormProps) {
  const i18n = useI18n();
  const { t } = i18n;
  const prefix = useId();
  if (specs.length === 0) return null;
  const set = (name: string, value: unknown) => onChange({ ...values, [name]: value });

  return (
    <fieldset className="params">
      <legend>{t("runner.parameters")}</legend>
      {specs.map((spec) => {
        const fieldId = `${prefix}-${spec.name}`;
        const helpId = `${fieldId}-help`;
        const errorId = `${fieldId}-error`;
        const problem = problems[spec.name];
        const patternId = `${fieldId}-pattern`;
        const keyPatternId = `${fieldId}-key-pattern`;
        const keyPattern = spec.kind === "mapping" ? spec.key_pattern : null;
        const described =
          [spec.help ? helpId : "", keyPattern ? keyPatternId : "", spec.pattern ? patternId : "", problem ? errorId : ""].filter(Boolean).join(" ") || undefined;
        const builtinLabel = builtinParam(i18n, plugin, spec.name, "label");
        const builtinHelp = (variant && builtinParam(i18n, plugin, spec.name, `help_${variant}`)) || builtinParam(i18n, plugin, spec.name, "help");
        const value = values[spec.name];
        const label = (
          <>
            {/* A declared label and its help are the deployment's own wording: shown as written. */}
            {builtinLabel ?? spec.label}
            {spec.required && (
              <>
                <span className="required" aria-hidden="true" title={t("runner.required")}>
                  {" "}
                  *
                </span>
                <span className="sr-only"> ({t("runner.required")})</span>
              </>
            )}
          </>
        );
        const tail = (
          <>
            {spec.help && (
              <p className="hint" id={helpId}>
                {builtinHelp ?? spec.help}
              </p>
            )}
            {keyPattern && (
              <p className="hint pattern-hint" id={keyPatternId} data-pattern-of="name">
                {t("runner.expectedNameForm")} <code>{keyPattern}</code>
              </p>
            )}
            {spec.pattern && (
              <p className="hint pattern-hint" id={patternId} data-pattern-of="value">
                {spec.kind === "mapping" ? t("runner.expectedValueForm") : t("runner.expectedForm")} <code>{spec.pattern}</code>
              </p>
            )}
            {problem && (
              <p className="field-error" id={errorId} role="alert">
                {problem.text}
                {problem.foreign && (
                  <>
                    {" "}
                    <Foreign>{problem.foreign}</Foreign>
                  </>
                )}
              </p>
            )}
          </>
        );

        if (spec.kind === "boolean") {
          return (
            <div className="field" key={spec.name} data-param={spec.name}>
              <label className="check">
                <input
                  id={fieldId}
                  type="checkbox"
                  checked={value === true}
                  aria-describedby={described}
                  aria-invalid={problem ? true : undefined}
                  onChange={(event) => set(spec.name, event.target.checked)}
                />
                {label}
              </label>
              {tail}
            </div>
          );
        }
        if (spec.kind === "multi_choice") {
          const chosen = Array.isArray(value) ? (value as string[]) : [];
          return (
            <fieldset className="field choices" key={spec.name} data-param={spec.name} aria-describedby={described}>
              <legend className="field-label">{label}</legend>
              <div className="choice-list">
                {spec.choices.map((choice) => (
                  <label className="check" key={choice}>
                    <input
                      type="checkbox"
                      checked={chosen.includes(choice)}
                      onChange={(event) =>
                        set(
                          spec.name,
                          // Keep the declared order, whatever order they were ticked in.
                          spec.choices.filter((entry) => (entry === choice ? event.target.checked : chosen.includes(entry))),
                        )
                      }
                    />
                    {choice}
                  </label>
                ))}
              </div>
              {tail}
            </fieldset>
          );
        }
        if (spec.kind === "mapping") {
          const rows = Array.isArray(value) ? (value as MappingRow[]) : [];
          // Which cell is wrong, once a problem is shown: the name or the value that does not have its form.
          const nameForm = wholeMatch(keyPattern);
          const valueForm = wholeMatch(spec.pattern);
          const used = (row: MappingRow) => row.key.trim() !== "" || row.value !== "";
          const badName = (row: MappingRow) => used(row) && nameForm !== null && !nameForm.test(row.key.trim());
          const badValue = (row: MappingRow) => used(row) && valueForm !== null && !valueForm.test(row.value);
          const anyBad = rows.some((row) => badName(row) || badValue(row));
          const setRow = (index: number, change: Partial<MappingRow>) =>
            set(spec.name, rows.map((row, position) => (position === index ? { ...row, ...change } : row)));
          return (
            <fieldset className="field mapping" key={spec.name} data-param={spec.name} aria-describedby={described}>
              <legend className="field-label">{label}</legend>
              {rows.map((row, index) => (
                <div className="mapping-row" key={index}>
                  <input
                    className="input"
                    type="text"
                    value={row.key}
                    placeholder={t("runner.mappingKey")}
                    aria-label={t("runner.mappingKeyOf", { label: spec.label, number: index + 1 })}
                    aria-invalid={problem && (badName(row) || !anyBad) ? true : undefined}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => setRow(index, { key: event.target.value })}
                  />
                  <input
                    className="input"
                    type="text"
                    value={row.value}
                    placeholder={t("runner.mappingValue")}
                    aria-label={t("runner.mappingValueOf", { label: spec.label, number: index + 1 })}
                    aria-invalid={problem && (badValue(row) || !anyBad) ? true : undefined}
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(event) => setRow(index, { value: event.target.value })}
                  />
                  <button
                    type="button"
                    className="btn btn-small btn-quiet"
                    aria-label={t("runner.mappingRemoveOf", { label: spec.label, number: index + 1 })}
                    onClick={() => set(spec.name, rows.filter((_, position) => position !== index))}
                  >
                    {t("action.remove")}
                  </button>
                </div>
              ))}
              <div>
                <button type="button" className="btn btn-small" data-add-row onClick={() => set(spec.name, [...rows, { key: "", value: "" }])}>
                  {t("runner.mappingAdd")}
                </button>
              </div>
              {tail}
            </fieldset>
          );
        }
        return (
          <div className="field" key={spec.name} data-param={spec.name}>
            <label className="field-label" htmlFor={fieldId}>
              {label}
            </label>
            {spec.kind === "text" ? (
              <textarea
                id={fieldId}
                className="input"
                rows={3}
                value={typeof value === "string" ? value : ""}
                aria-required={spec.required || undefined}
                aria-describedby={described}
                aria-invalid={problem ? true : undefined}
                onChange={(event) => set(spec.name, event.target.value)}
              />
            ) : spec.kind === "choice" ? (
              <select
                id={fieldId}
                className="input"
                value={typeof value === "string" ? value : ""}
                required={spec.required}
                aria-describedby={described}
                aria-invalid={problem ? true : undefined}
                onChange={(event) => set(spec.name, event.target.value)}
              >
                {(!spec.required || value === "") && <option value="">{t("runner.noChoice")}</option>}
                {spec.choices.map((choice) => (
                  <option key={choice} value={choice}>
                    {choice}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={fieldId}
                className="input"
                type="text"
                inputMode={spec.kind === "integer" ? "numeric" : undefined}
                value={typeof value === "string" ? value : ""}
                aria-required={spec.required || undefined}
                aria-describedby={described}
                aria-invalid={problem ? true : undefined}
                autoComplete="off"
                onChange={(event) => set(spec.name, event.target.value)}
              />
            )}
            {tail}
          </div>
        );
      })}
    </fieldset>
  );
}

interface LabelsProps {
  labels: Record<string, string>;
  onChange: (labels: Record<string, string>) => void;
  draft: string;
  onDraftChange: (draft: string) => void;
  /** Set when the draft was refused on submit. */
  problem: string | null;
}

export function LabelsInput({ labels, onChange, draft, onDraftChange, problem }: LabelsProps) {
  const { t } = useI18n();
  const fieldId = useId();
  const hintId = useId();
  const [localProblem, setLocalProblem] = useState<string | null>(null);
  const shownProblem = problem ?? localProblem;

  const add = () => {
    if (!draft.trim()) return;
    const parsed = parseLabel(draft);
    if (!parsed) {
      setLocalProblem(t("labels.invalid"));
      return;
    }
    onChange({ ...labels, [parsed[0]]: parsed[1] });
    onDraftChange("");
    setLocalProblem(null);
  };

  return (
    <div className="field">
      <label className="field-label" htmlFor={fieldId}>
        {t("labels.label")}
      </label>
      {Object.keys(labels).length > 0 && (
        <ul className="label-edit">
          {Object.entries(labels).map(([key, value]) => (
            <li key={key} className="label-chip">
              {key}={value}
              <button
                type="button"
                className="chip-remove"
                aria-label={t("labels.remove", { label: `${key}=${value}` })}
                onClick={() => {
                  const next = { ...labels };
                  delete next[key];
                  onChange(next);
                }}
              >
                {t("action.remove")}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="row">
        <input
          id={fieldId}
          className="input"
          type="text"
          value={draft}
          placeholder={t("labels.placeholder")}
          aria-describedby={hintId}
          aria-invalid={shownProblem ? true : undefined}
          autoComplete="off"
          onChange={(event) => {
            onDraftChange(event.target.value);
            setLocalProblem(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }}
        />
        <button type="button" className="btn btn-small" onClick={add} disabled={!draft.trim()}>
          {t("labels.add")}
        </button>
      </div>
      <p className={shownProblem ? "field-error" : "hint"} id={hintId} role={shownProblem ? "alert" : undefined}>
        {shownProblem ?? t("labels.hint")}
      </p>
    </div>
  );
}
