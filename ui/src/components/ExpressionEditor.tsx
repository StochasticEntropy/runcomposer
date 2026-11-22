// The filter, once: an editable expression over the run spec's tag-filter
// grammar (DESIGN.md §3.1). A rule is a chip, a group is a box with its
// operator, NOT is a switch on either — and the same expression can be typed
// as text. Everything is a native control, so the whole editor works from
// the keyboard and reads sensibly to a screen reader.

import { useEffect, useId, useMemo, useRef, useState } from "react";

import { useI18n } from "../hooks/context.tsx";
import {
  ROOT_ID,
  addExpression,
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
import type { FilterAst } from "../lib/filter.ts";
import type { Translator } from "../lib/i18n.ts";
import { patternChecker } from "../lib/tagMatch.ts";
import type { PatternCheck } from "../lib/tagMatch.ts";

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
  /** The expression as it is being typed, or null while it is not edited as
   * text. It is the caller's: while a typed text differs from the filter that
   * is applied, the caller must not run anything. */
  textDraft: string | null;
  onTextDraft: (text: string | null) => void;
  /** The tags of the catalog, once they are known: a rule no tag matches is flagged. */
  knownTags?: readonly string[] | null;
}

/**
 * What is wrong with a typed expression, in the viewer's words, with the
 * place. One mistake gets its own sentence: `and` in small letters is read
 * as a tag — operators are capitals — and the plain message ("an operator is
 * missing") would send the reader looking for the wrong thing.
 */
export function parseMessage(t: Translator["t"], error: FilterParseError): string {
  const vars = { position: error.position + 1, token: error.token };
  if (error.problem === "missing-operator" && /^(and|or|not)$/i.test(error.token) && error.token !== error.token.toUpperCase()) {
    return t("filter.operatorCase", { ...vars, operator: error.token.toUpperCase() });
  }
  return t(`filter.parse.${error.problem}`, vars);
}

export function ExpressionEditor({ root, onChange, onPick, tagListId, errorNodeId, labelOf, textDraft, onTextDraft, knownTags = null }: Props) {
  const { t } = useI18n();
  // One checker per catalog: every rule asks on every render, each pattern is worked out once.
  const check = useMemo(() => (knownTags && knownTags.length > 0 ? patternChecker(knownTags) : null), [knownTags]);
  const isTag = useMemo(() => {
    const exact = new Set(knownTags ?? []);
    return (text: string) => exact.has(text);
  }, [knownTags]);
  const textMode = textDraft !== null;
  const applied = formatFilter(toAst(root));
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
    addExpression: (groupId, ast) => onChange(addExpression(root, groupId, ast)),
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
        <>
          <TextEditor
            text={textDraft}
            onText={onTextDraft}
            onApply={(ast) => {
              onChange(fromAst(ast));
              onTextDraft(null);
            }}
            onCancel={() => onTextDraft(null)}
          />
          {/* What is typed is not the filter until it is applied: the filter that IS applied stays in view. */}
          <p className="expr-applied" data-testid="applied-filter">
            <span className="muted">{t("filter.applied")}</span> {applied ? <code>{applied}</code> : <span className="muted">{t("filter.appliedNone")}</span>}
          </p>
        </>
      ) : (
        <>
          <Group node={root} isRoot actions={actions} tagListId={tagListId} errorNodeId={errorNodeId} labelOf={labelOf} check={check} isTag={isTag} />
          <div className="expr-tools">
            <button type="button" className="btn btn-small btn-quiet" onClick={() => onTextDraft(applied)}>
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
  addExpression: (groupId: string, ast: FilterAst) => void;
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
  /** Whether a pattern matches a tag of the catalog; null while the catalog is not known. */
  check: ((pattern: string) => PatternCheck) | null;
  /** The text is, letter for letter, a tag of the catalog. */
  isTag: (text: string) => boolean;
}

/** A rule that is not "everything" and that no tag of the catalog matches. */
const unmatched = (node: EditorNode, check: NodeProps["check"]): node is TagNode =>
  node.kind === "tag" && !(node.negated && node.pattern === "regex:^$") && check !== null && check(node.pattern).matches === false;

/** How many rules without a match a group explains one by one. */
const EXPLAINED = 4;

// A group with more rules than this keeps its height: the rules scroll inside
// it, and the field that adds the next one stays where it was.
const LONG_GROUP = 16;

function Group({ node, isRoot = false, actions, tagListId, errorNodeId, labelOf, check, isTag }: NodeProps & { node: GroupNode; isRoot?: boolean }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<FilterParseError | null>(null);
  const field = useRef<HTMLInputElement | null>(null);
  const opId = useId();
  const problemId = useId();
  const invalid = errorNodeId === node.id;
  const strays = node.children.filter((child) => unmatched(child, check));

  // What is typed here is read with the filter's grammar — the same text
  // means the same in this field, in "Edit as text" and on the command line.
  // One tag or pattern is one more rule; an expression is the rules or the
  // group it says. A tag of the catalog chosen from the list is that tag,
  // whatever characters it has; any other tag with a space or an operator
  // word in it is typed in quotes, as the grammar has it.
  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    if (isTag(text)) {
      actions.addTag(node.id, text);
    } else {
      let ast: FilterAst | null;
      try {
        ast = parseFilter(text);
      } catch (error) {
        if (!(error instanceof FilterParseError)) throw error;
        // Refused in place: nothing is added, the text stays, the caret goes where reading stopped.
        setProblem(error);
        const lead = draft.length - draft.trimStart().length;
        field.current?.focus();
        field.current?.setSelectionRange(lead + error.offset, lead + error.offset);
        return;
      }
      if (ast !== null) actions.addExpression(node.id, ast);
    }
    setDraft("");
    setProblem(null);
  };

  return (
    <div
      className={`expr-group${isRoot ? " is-root" : ""}${node.negated ? " is-negated" : ""}${invalid ? " is-invalid" : ""}`}
      role="group"
      aria-label={t(isRoot ? "filter.rootGroup" : "filter.group", { op: t(`filter.op.${node.op}`) })}
    >
      <div className="expr-group-head">
        {node.negated && (
          <span className="not-mark" aria-hidden="true">
            {t("filter.not")}
          </span>
        )}
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
        <span className="expr-group-tools">
          <NotToggle on={node.negated} label={t(isRoot ? "filter.negateAll" : "filter.negateGroup")} onToggle={() => actions.toggleNot(node.id)} />
          {!isRoot && (
            <>
              {!node.negated && (
                <button type="button" className="btn btn-small btn-quiet" onClick={() => actions.ungroup(node.id)}>
                  {t("filter.ungroup")}
                </button>
              )}
              <button type="button" className="btn btn-small btn-quiet" onClick={() => actions.remove(node.id)}>
                {t("filter.removeGroup")}
              </button>
            </>
          )}
        </span>
      </div>

      {node.children.length > 0 && (
        <ul className={`expr-rules${node.children.length > LONG_GROUP ? " is-long" : ""}`}>
          {node.children.map((child) => (
            // The operator is said once, by the group; the rules are chips that wrap.
            <li key={child.id} className={child.kind === "group" ? "is-group" : undefined}>
              <Rule node={child} actions={actions} tagListId={tagListId} errorNodeId={errorNodeId} labelOf={labelOf} check={check} isTag={isTag} />
            </li>
          ))}
        </ul>
      )}
      {node.children.length > LONG_GROUP && <p className="hint expr-count">{t("filter.ruleCount", { count: node.children.length })}</p>}
      {strays.length > 0 && (
        // A rule no tag matches selects nothing. Said next to the rules, with what was probably meant.
        <ul className="expr-unmatched" role="status" aria-label={t("filter.unmatchedList")}>
          {strays.slice(0, EXPLAINED).map((stray) => {
            const nearest = check?.(stray.pattern).nearest ?? [];
            return (
              <li key={stray.id} data-unmatched={stray.pattern}>
                {t("filter.matchesNoTag", { pattern: stray.pattern })}
                {nearest.length > 0 && (
                  <>
                    {" "}
                    {t("filter.didYouMean")}{" "}
                    {nearest.map((tag) => (
                      <button
                        key={tag}
                        type="button"
                        className="btn btn-small"
                        aria-label={t("filter.replaceWith", { pattern: stray.pattern, tag })}
                        onClick={() => actions.setPattern(stray.id, tag)}
                      >
                        {tag}
                      </button>
                    ))}
                  </>
                )}
              </li>
            );
          })}
          {strays.length > EXPLAINED && <li>{t("filter.moreUnmatched", { count: strays.length - EXPLAINED })}</li>}
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
          ref={field}
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setProblem(null);
          }}
          placeholder={t("filter.addPlaceholder")}
          title={t("filter.addHint")}
          aria-label={t(isRoot ? "filter.addTag" : "filter.addTagToGroup")}
          aria-invalid={problem !== null || undefined}
          aria-describedby={problem ? problemId : undefined}
          autoComplete="off"
          spellCheck={false}
        />
        <button type="submit" className="btn btn-small" disabled={!draft.trim()}>
          {t("filter.add")}
        </button>
        <button type="button" className="btn btn-small" onClick={() => actions.pick(node.id)} aria-label={isRoot ? undefined : t("filter.pickForGroup")}>
          {t("filter.pick")}
        </button>
        <button type="button" className="btn btn-small btn-quiet" onClick={() => actions.addGroup(node.id)} aria-label={isRoot ? undefined : t("filter.addGroupInGroup")}>
          {t("filter.addGroup")}
        </button>
        {problem && (
          <p className="field-error expr-add-error" id={problemId} role="alert">
            {parseMessage(t, problem)}
          </p>
        )}
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

function Chip({ node, actions, tagListId, errorNodeId, labelOf, check }: NodeProps & { node: TagNode }) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(node.pattern);
  const invalid = errorNodeId === node.id;
  const stray = unmatched(node, check);
  const flagId = useId();
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
    <span
      className={`chip${everything ? " is-everything" : node.negated ? " is-negated" : ""}${invalid ? " is-invalid" : ""}${stray ? " is-unmatched" : ""}`}
      data-pattern={everything ? "*" : node.pattern}
    >
      {node.negated && !everything && (
        <span className="not-mark" aria-hidden="true">
          {t("filter.not")}
        </span>
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
        aria-describedby={stray ? flagId : undefined}
      >
        {everything ? (
          t("filter.everything")
        ) : (
          <>
            {kind !== "tag" && <span className="chip-kind">{t(`filter.kind.${kind}`)}</span>}
            {/* A curated name explains a regular expression; a tag or a prefix explains itself. */}
            <span className={kind === "tag" ? undefined : "mono"}>{label && kind === "regex" ? `${label} · ${body}` : body}</span>
          </>
        )}
      </button>
      {stray && (
        <span className="chip-flag" id={flagId} title={t("filter.noTagHint")}>
          {t("filter.noTag")}
        </span>
      )}
      {!everything && <NotToggle on={node.negated} label={t("filter.negateRule", { pattern: spoken })} onToggle={() => actions.toggleNot(node.id)} />}
      {/* The one control that is a sign and no word: a rule is a chip, and chips are closed with ×.
          It has its name for assistive technology and its tooltip (DESIGN.md §10). */}
      <button
        type="button"
        className="chip-remove"
        onClick={() => actions.remove(node.id)}
        aria-label={t("filter.removeRule", { pattern: spoken })}
        title={t("filter.removeRule", { pattern: spoken })}
      >
        <span aria-hidden="true">×</span>
      </button>
    </span>
  );
}

// NOT is a switch on a rule or a group, drawn as a small labelled box behind
// the rule: an empty box while it is off — so nothing in FRONT of the rule
// reads as "NOT" — and a ticked one while it is on, when the rule is also
// marked red and says NOT in front.
function NotToggle({ on, label, onToggle }: { on: boolean; label: string; onToggle: () => void }) {
  const { t } = useI18n();
  return (
    <button type="button" className={`toggle-not${on ? " is-on" : ""}`} aria-pressed={on} aria-label={label} title={label} onClick={onToggle}>
      <span className="toggle-box" aria-hidden="true" />
      {t("filter.not")}
    </button>
  );
}

function TextEditor({
  text,
  onText,
  onApply,
  onCancel,
}: {
  text: string;
  onText: (text: string) => void;
  onApply: (ast: ReturnType<typeof parseFilter>) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const [problem, setProblem] = useState<FilterParseError | null>(null);
  const fieldRef = useRef<HTMLTextAreaElement | null>(null);
  const fieldId = useId();
  const errorId = useId();
  const setText = onText;

  const apply = () => {
    try {
      onApply(parseFilter(text));
    } catch (error) {
      if (error instanceof FilterParseError) {
        setProblem(error);
        // The caret goes where reading stopped (a UTF-16 offset, as the field counts).
        fieldRef.current?.setSelectionRange(error.offset, error.offset);
      } else throw error;
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
        ref={fieldRef}
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
        {problem ? parseMessage(t, problem) : t("filter.textHint")}
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
