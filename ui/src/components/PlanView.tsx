// A plan preview: what a dispatch would do, read before anything runs —
// which shard gets which items, how the shards group, how long the runner
// expects each to take, and what it wants said about it. A dispatch the
// runner would refuse is shown as exactly that.

import { useI18n } from "../hooks/context.tsx";
import { formatDuration } from "../lib/format.ts";
import { isCurrent } from "../lib/keyed.ts";
import type { KeyedState } from "../lib/keyed.ts";
import type { PlanResult } from "../lib/types.ts";
import { ErrorState, Foreign, Loading, Notice } from "./ui.tsx";

interface Props {
  state: KeyedState<PlanResult>;
  onRetry: () => void;
}

export function PlanView({ state, onRetry }: Props) {
  const { t, locale } = useI18n();
  const estimate = (ms: number | null) => (ms === null || ms <= 0 ? "–" : formatDuration(ms, locale));

  if (state.status === "error" && state.error) return <ErrorState error={state.error} onRetry={onRetry} compact />;
  if (!isCurrent(state) || !state.result) return <Loading compact label={t("plan.loading")} />;

  const result = state.result;
  if (result.refused) {
    return (
      <div className="plan-view" data-testid="plan" data-refused="true">
        <Notice tone="error">
          <strong>{t("plan.refused")}</strong> <Foreign className="pre-line">{result.refusal}</Foreign>
        </Notice>
        <p className="hint">{t("plan.refusedHint")}</p>
      </div>
    );
  }
  const plan = result.plan;
  if (!plan) return <p className="state state-empty">{t("plan.none")}</p>;

  const total = plan.shards.reduce((sum, shard) => sum + (shard.estimate_ms ?? 0), 0);
  const grouped = plan.groups.length > 0;
  return (
    <div className="plan-view" data-testid="plan" data-refused="false">
      <p className="plan-summary">
        <strong>
          {t("plan.summary", { count: result.item_count })} {t("plan.inShards", { count: plan.shards.length })}
          {grouped && <> {t("plan.inGroups", { count: plan.groups.length })}</>}
        </strong>
        {total > 0 && <span className="muted"> · {t("plan.estimated", { duration: formatDuration(total, locale) })}</span>}
      </p>

      {result.warnings.map((warning) => (
        <Notice tone="warn" key={warning}>
          <Foreign>{warning}</Foreign>
        </Notice>
      ))}
      {plan.notes.length > 0 && (
        <ul className="plan-notes" aria-label={t("plan.notes")}>
          {plan.notes.map((note) => (
            <li key={note}>
              <Foreign>{note}</Foreign>
            </li>
          ))}
        </ul>
      )}

      {grouped && (
        <div className="table-scroll">
          <table className="table plan-groups">
            <caption className="sr-only">{t("plan.groups")}</caption>
            <thead>
              <tr>
                <th scope="col">{t("run.group")}</th>
                <th scope="col" className="num">
                  {t("plan.column.shards")}
                </th>
                <th scope="col" className="num">
                  {t("plan.column.items")}
                </th>
                <th scope="col" className="num">
                  {t("plan.column.estimate")}
                </th>
              </tr>
            </thead>
            <tbody>
              {plan.groups.map((group) => (
                <tr key={group.group} data-plan-group={group.group}>
                  <th scope="row">{group.group}</th>
                  <td className="num">{group.shards.length}</td>
                  <td className="num">{group.count}</td>
                  <td className="num">{estimate(group.estimate_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="table-scroll plan-shards-scroll">
        <table className="table plan-shards">
          <caption className="sr-only">{t("plan.shards")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("run.shard")}</th>
              {grouped && <th scope="col">{t("run.group")}</th>}
              <th scope="col" className="num">
                {t("plan.column.items")}
              </th>
              <th scope="col" className="num">
                {t("plan.column.estimate")}
              </th>
            </tr>
          </thead>
          <tbody>
            {plan.shards.map((shard) => (
              <tr key={shard.shard} data-plan-shard={shard.shard}>
                <th scope="row">
                  <details>
                    <summary>{shard.shard || t("run.unnamedShard")}</summary>
                    <ul className="plan-items mono">
                      {shard.item_ids.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </details>
                </th>
                {grouped && <td>{shard.group ?? "–"}</td>}
                <td className="num">{shard.count}</td>
                <td className="num">{estimate(shard.estimate_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {plan.text && (
        <details className="plan-text">
          <summary>{t("plan.asText")}</summary>
          <Foreign block className="plan">
            {plan.text}
          </Foreign>
        </details>
      )}
    </div>
  );
}
