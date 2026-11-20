// The filter, once: an editable expression over the run spec's tag-filter
// grammar (DESIGN.md §3.1). A rule is a chip, a group is a box with its
// operator, NOT is a switch on either — and the same expression can be typed
// as text. Everything is a native control, so the whole editor works from
// the keyboard and reads sensibly to a screen reader.

import { useEffect, useId, useRef, useState } from "react";

import { useI18n } from "../hooks/context.tsx";
import {
  ROOT_ID,
  addGroup,
  addTag,
  findNode,
  fromAst,
  removeNode,
  setOp,
  setPattern,
  toAst,
  toggleNot,
  ungroup,
} from "../lib/editor.ts";
import type { EditorNode, GroupNode, TagNode } from "../lib/editor.ts";
import { FilterParseError, formatFilter, parseFilter } from "../lib/filter.ts";

interface Props {
  root: GroupNode;
  onChange: (root: GroupNode) => void;
  /** Open the taxonomy picker for this group. */
  onPick: (groupId: string) => void;
  /** id of the <datalist> with the catalog's tags. */
  tagListId: string;
  /** The rule the server refused, if it named one. */
  errorNodeId: string | null;
  /** The label a curated taxonomy node gives a pattern, if any. */
  labelOf: (pattern: string) => string | null;
}

export function ExpressionEditor({ root, onChange, onPick, tagListId, errorNodeId, labelOf }: Props) {
  const { t } = useI18n();
  const [textMode, setTextMode] = useState(false);
  const [focusGroup, setFocusGroup] = useState<string | null>(null);
  const container = useRef<HTMLDivElement | null>(null);

  // After a rule is removed or a group is added, focus goes to the add field
  // of the group concerned — never to nowhere.
  useEffect(() => {
    if (focusGroup === null) return;
    const field = container.current?.querySelector<HTMLInputElement>(`[data-add-input="${focusGroup}"]`);
    field?.focus();
    setFocusGroup(null);
  }, [focusGroup]);

  const parentOf = (id: string): string => {
    const search = (node: GroupNode): string | null => {
      for (const child of node.children) {
        if (child.id === id) return node.id;
        if (child.kind === "group") {
          const found = search(child);
          if (found) return found;
        }
      }
      return null;
    };
    return search(root) ?? ROOT_ID;
  };

  const actions: Actions = {
    addTag: (groupId, pattern) => onChange(addTag(root, groupId, pattern)),
    addGroup: (groupId) => {
      // A subgroup defaults to the operator its parent does not have —
      // nesting the same operator would say nothing new.
      const parent = findNode(root, groupId);
      const parentOp = parent?.kind === "group" ? parent.op : "AND";
      const created = addGroup(root, groupId, parentOp === "AND" ? "OR" : "AND");
      onChange(created.root);
      setFocusGroup(created.id);
    },
    remove: (id) => {
      const parent = parentOf(id);
      onChange(removeNode(root, id));
      setFocusGroup(parent);
    },
    toggleNot: (id) => onChange(toggleNot(root, id)),
    setOp: (id, op) => onChange(setOp(root, id, op)),
    setPattern: (id, pattern) => onChange(pattern.trim() ? setPattern(root, id, pattern.trim()) : removeNode(root, id)),
    ungroup: (id) => {
      const parent = parentOf(id);
      onChange(ungroup(root, id));
      setFocusGroup(parent);
    },
    pick: onPick,
  };

  return (
    <div className="expr" ref={container}>
      {textMode ? (
        <TextEditor
          initial={formatFilter(toAst(root))}
          onApply={(ast) => {
            onChange(fromAst(ast));
            setTextMode(false);
          }}
          onCancel={() => setTextMode(false)}
        />
      ) : (
        <>
          <Group node={root} isRoot actions={actions} tagListId={tagListId} errorNodeId={errorNodeId} labelOf={labelOf} />
          <div className="expr-tools">
            <button type="button" className="btn btn-small btn-quiet" onClick={() => setTextMode(true)}>
              {t("filter.editAsText")}
            </button>
            {root.children.length > 0 && (
              <button
                type="button"
                className="btn btn-small btn-quiet"
                onClick={() => {
                  onChange(removeNode(root, ROOT_ID));
                  setFocusGroup(ROOT_ID);
                }}
              >
                {t("filter.clear")}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

interface Actions {
  addTag: (groupId: string, pattern: string) => void;
  addGroup: (groupId: string) => void;
  remove: (id: string) => void;
  toggleNot: (id: string) => void;
  setOp: (id: string, op: "AND" | "OR") => void;
  setPattern: (id: string, pattern: string) => void;
  ungroup: (id: string) => void;
  pick: (groupId: string) => void;
}

interface NodeProps {
  actions: Actions;
  tagListId: string;
  errorNodeId: string | null;
  labelOf: (pattern: string) => string | null;
}

function Group({ node, isRoot = false, actions, tagListId, errorNodeId, labelOf }: NodeProps & { node: GroupNode; isRoot?: boolean }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const opId = useId();
  const invalid = errorNodeId === node.id;

  const submit = () => {
    if (!draft.trim()) return;
    actions.addTag(node.id, draft);
    setDraft("");
  };

  return (
    <div
      className={`expr-group${isRoot ? " is-root" : ""}${node.negated ? " is-negated" : ""}${invalid ? " is-invalid" : ""}`}
      role="group"
      aria-label={t(isRoot ? "filter.rootGroup" : "filter.group", { op: t(`filter.op.${node.op}`) })}
    >
      <div className="expr-group-head">
        <button
          type="button"
          className={`toggle-not${node.negated ? " is-on" : ""}`}
          aria-pressed={node.negated}
          onClick={() => actions.toggleNot(node.id)}
          title={t("filter.negateGroup")}
        >
          {t("filter.not")}
          <span className="sr-only"> — {t("filter.negateGroup")}</span>
        </button>
        <label className="sr-only" htmlFor={opId}>
          {t("filter.combine")}
        </label>
        <select
          id={opId}
          className="op-select"
          value={node.op}
          onChange={(event) => actions.setOp(node.id, event.target.value as "AND" | "OR")}
        >
          <option value="AND">{t("filter.opLong.AND")}</option>
          <option value="OR">{t("filter.opLong.OR")}</option>
        </select>
        {!isRoot && (
          <span className="expr-group-tools">
            {!node.negated && (
              <button type="button" className="btn btn-small btn-quiet" onClick={() => actions.ungroup(node.id)}>
                {t("filter.ungroup")}
              </button>
            )}
            <button
              type="button"
              className="icon-btn"
              onClick={() => actions.remove(node.id)}
              aria-label={t("filter.removeGroup")}
              title={t("filter.removeGroup")}
            >
              ×
            </button>
          </span>
        )}
      </div>

      {node.children.length > 0 && (
        <ul className="expr-rules">
          {node.children.map((child, index) => (
            <li key={child.id} className={child.kind === "group" ? "is-group" : undefined}>
              {index > 0 && (
                <span className="expr-joint" aria-hidden="true">
                  {t(`filter.op.${node.op}`)}
                </span>
              )}
              <Rule node={child} actions={actions} tagListId={tagListId} errorNodeId={errorNodeId} labelOf={labelOf} />
            </li>
          ))}
        </ul>
      )}

      <form
        className="expr-add"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <input
          type="text"
          className="input"
          list={tagListId}
          data-add-input={node.id}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={t("filter.addPlaceholder")}
          aria-label={t(isRoot ? "filter.addTag" : "filter.addTagToGroup")}
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" className="btn btn-small" disabled={!draft.trim()}>
          {t("filter.add")}
        </button>
        <button type="button" className="btn btn-small" onClick={() => actions.pick(node.id)}>
          {t("filter.pick")}
        </button>
        <button type="button" className="btn btn-small btn-quiet" onClick={() => actions.addGroup(node.id)}>
          {t("filter.addGroup")}
        </button>
      </form>
    </div>
  );
}

function Rule({ node, ...rest }: NodeProps & { node: EditorNode }) {
  if (node.kind === "group") return <Group node={node} {...rest} />;
  return <Chip node={node} {...rest} />;
}

/** `prefix:Checkout-` → kind "prefix", body "Checkout-". */
export function splitPattern(pattern: string): { kind: "tag" | "prefix" | "regex"; body: string } {
  if (pattern.startsWith("prefix:")) return { kind: "prefix", body: pattern.slice("prefix:".length) };
  if (pattern.startsWith("regex:")) return { kind: "regex", body: pattern.slice("regex:".length) };
  return { kind: "tag", body: pattern };
}

function Chip({ node, actions, tagListId, errorNodeId, labelOf }: NodeProps & { node: TagNode }) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(node.pattern);
  const invalid = errorNodeId === node.id;
  const { kind, body } = splitPattern(node.pattern);
  const everything = node.negated && node.pattern === "regex:^$";
  const label = labelOf(node.pattern);
  const spoken = everything ? t("filter.everything") : node.pattern;

  if (editing) {
    const commit = () => {
      setEditing(false);
      if (draft !== node.pattern) actions.setPattern(node.id, draft);
    };
    return (
      <span className="chip is-editing">
        <input
          type="text"
          className="input chip-input"
          list={tagListId}
          value={draft}
          autoFocus
          spellCheck={false}
          autoComplete="off"
          aria-label={t("filter.editPattern", { pattern: node.pattern })}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit();
            } else if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              setDraft(node.pattern);
              setEditing(false);
            }
          }}
        />
      </span>
    );
  }

  return (
    <span className={`chip${node.negated ? " is-negated" : ""}${invalid ? " is-invalid" : ""}`} data-pattern={node.pattern}>
      {!everything && (
        <button
          type="button"
          className={`toggle-not${node.negated ? " is-on" : ""}`}
          aria-pressed={node.negated}
          aria-label={t("filter.negateRule", { pattern: spoken })}
          title={t("filter.negateRule", { pattern: spoken })}
          onClick={() => actions.toggleNot(node.id)}
        >
          {t("filter.not")}
        </button>
      )}
      <button
        type="button"
        className="chip-text"
        onClick={() => {
          setDraft(node.pattern);
          setEditing(true);
        }}
        title={t("filter.editPattern", { pattern: node.pattern })}
        aria-label={t("filter.editPattern", { pattern: spoken })}
        aria-invalid={invalid || undefined}
      >
        {everything ? (
          t("filter.everything")
        ) : (
          <>
            {kind !== "tag" && <span className="chip-kind">{t(`filter.kind.${kind}`)}</span>}
            <span className={kind === "tag" ? undefined : "mono"}>{label && kind !== "tag" ? `${label} · ${body}` : body}</span>
          </>
        )}
      </button>
      <button
        type="button"
        className="chip-remove"
        onClick={() => actions.remove(node.id)}
        aria-label={t("filter.removeRule", { pattern: spoken })}
        title={t("filter.removeRule", { pattern: spoken })}
      >
        ×
      </button>
    </span>
  );
}

function TextEditor({
  initial,
  onApply,
  onCancel,
}: {
  initial: string;
  onApply: (ast: ReturnType<typeof parseFilter>) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const [text, setText] = useState(initial);
  const [problem, setProblem] = useState<FilterParseError | null>(null);
  const fieldId = useId();
  const errorId = useId();

  const apply = () => {
    try {
      onApply(parseFilter(text));
    } catch (error) {
      if (error instanceof FilterParseError) setProblem(error);
      else throw error;
    }
  };

  return (
    <form
      className="expr-text"
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      <label htmlFor={fieldId}>{t("filter.textLabel")}</label>
      <textarea
        id={fieldId}
        className="input mono"
        rows={3}
        value={text}
        autoFocus
        spellCheck={false}
        aria-invalid={problem !== null || undefined}
        aria-describedby={errorId}
        onChange={(event) => {
          setText(event.target.value);
          setProblem(null);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            apply();
          }
        }}
      />
      <p id={errorId} className={problem ? "field-error" : "hint"} role={problem ? "alert" : undefined}>
        {problem
          ? t(`filter.parse.${problem.problem}`, { position: problem.position + 1, token: problem.token })
          : t("filter.textHint")}
      </p>
      <div className="row">
        <button type="submit" className="btn btn-primary btn-small">
          {t("filter.textApply")}
        </button>
        <button type="button" className="btn btn-small" onClick={onCancel}>
          {t("action.cancel")}
        </button>
      </div>
    </form>
  );
}
