// Selections saved under a name, in this browser (lib/saved.ts): save what
// Compose holds right now, and use, rename or delete what was saved before.

import { useId, useState } from "react";

import { useI18n } from "../hooks/context.tsx";
import { deleteSelection, renameSelection, saveSelection } from "../lib/saved.ts";
import type { SavedSelection } from "../lib/saved.ts";
import { Dialog, Notice } from "./ui.tsx";

interface Props {
  saved: SavedSelection[];
  onSaved: (list: SavedSelection[]) => void;
  /** What Compose holds now — what "Save" stores. */
  current: () => Omit<SavedSelection, "name" | "savedAt">;
  /** The filter as text, shown next to the name field so it is clear what is being saved. */
  currentFilter: string;
  onUse: (entry: SavedSelection) => void;
  onClose: () => void;
}

export function SavedSelections({ saved, onSaved, current, currentFilter, onUse, onClose }: Props) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [failed, setFailed] = useState(false);
  const [renaming, setRenaming] = useState<{ from: string; to: string; taken: boolean } | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const nameId = useId();
  const exists = saved.some((entry) => entry.name === name.trim());

  const save = () => {
    if (!name.trim()) return;
    const result = saveSelection({ ...current(), name: name.trim(), savedAt: new Date().toISOString() });
    setFailed(!result.ok);
    onSaved(result.list);
    if (result.ok) setName("");
  };

  return (
    <Dialog title={t("saved.title")} onClose={onClose} size="large">
      <p className="hint">{t("saved.where")}</p>
      <form
        className="saved-form"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <div className="field field-grow">
          <label className="field-label" htmlFor={nameId}>
            {t("saved.name")}
          </label>
          <input id={nameId} className="input" type="text" value={name} maxLength={80} data-autofocus autoComplete="off" onChange={(event) => setName(event.target.value)} />
        </div>
        <button type="submit" className="btn btn-primary" disabled={!name.trim() || !currentFilter}>
          {exists ? t("saved.replace") : t("saved.save")}
        </button>
      </form>
      <p className="hint">
        {currentFilter ? (
          <>
            {t("saved.whatIsSaved")} <code>{currentFilter}</code>
          </>
        ) : (
          t("saved.nothingToSave")
        )}
      </p>
      {failed && <Notice tone="error">{t("saved.failed")}</Notice>}

      <h3>{t("saved.list")}</h3>
      {saved.length === 0 ? (
        <p className="state state-empty">{t("saved.empty")}</p>
      ) : (
        <ul className="saved-list">
          {saved.map((entry) => (
            <li key={entry.name} data-saved={entry.name}>
              {renaming?.from === entry.name ? (
                <form
                  className="saved-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    const result = renameSelection(entry.name, renaming.to);
                    if (result.ok) {
                      onSaved(result.list);
                      setRenaming(null);
                    } else setRenaming({ ...renaming, taken: true });
                  }}
                >
                  <input
                    className="input"
                    type="text"
                    value={renaming.to}
                    maxLength={80}
                    autoFocus
                    aria-label={t("saved.newName", { name: entry.name })}
                    aria-invalid={renaming.taken || undefined}
                    onChange={(event) => setRenaming({ from: entry.name, to: event.target.value, taken: false })}
                  />
                  <button type="submit" className="btn btn-small btn-primary">
                    {t("saved.rename")}
                  </button>
                  <button type="button" className="btn btn-small" onClick={() => setRenaming(null)}>
                    {t("action.cancel")}
                  </button>
                  {renaming.taken && (
                    <span className="field-error" role="alert">
                      {t("saved.nameTaken")}
                    </span>
                  )}
                </form>
              ) : (
                <>
                  <div className="saved-what">
                    <strong>{entry.name}</strong>
                    <span className="cell-sub mono">{entry.filter}</span>
                    {entry.runner && <span className="cell-sub">{t("compose.withRunner", { runner: entry.runner })}</span>}
                  </div>
                  {deleting === entry.name ? (
                    <span className="row" role="group" aria-label={t("saved.deleteAsk", { name: entry.name })}>
                      <span>{t("saved.deleteAsk", { name: entry.name })}</span>
                      <button
                        type="button"
                        className="btn btn-small btn-danger"
                        onClick={() => {
                          onSaved(deleteSelection(entry.name).list);
                          setDeleting(null);
                        }}
                      >
                        {t("saved.delete")}
                      </button>
                      <button type="button" className="btn btn-small" onClick={() => setDeleting(null)}>
                        {t("saved.keep")}
                      </button>
                    </span>
                  ) : (
                    <span className="row">
                      <button type="button" className="btn btn-small btn-primary" aria-label={t("saved.useNamed", { name: entry.name })} onClick={() => onUse(entry)}>
                        {t("compose.usePreset")}
                      </button>
                      <button
                        type="button"
                        className="btn btn-small"
                        aria-label={t("saved.renameNamed", { name: entry.name })}
                        onClick={() => setRenaming({ from: entry.name, to: entry.name, taken: false })}
                      >
                        {t("saved.rename")}
                      </button>
                      <button type="button" className="btn btn-small btn-danger-quiet" aria-label={t("saved.deleteNamed", { name: entry.name })} onClick={() => setDeleting(entry.name)}>
                        {t("saved.delete")}
                      </button>
                    </span>
                  )}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </Dialog>
  );
}
