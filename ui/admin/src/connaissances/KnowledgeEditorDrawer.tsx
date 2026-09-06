/**
 * The editor of a Knowledge item, as a component rather than a screen.
 *
 * WHY IT EXISTS. Editing a Knowledge item lived only inside `KnowledgeBasePage`,
 * the collection screen. So a remark filed on a Knowledge item could be read on
 * its workbench and CLOSED there, but never answered — the reader had to walk
 * back to the list to correct anything. `context-hub.md` calls that out by name:
 * "A remark is answered with a new version, not with a closed note … the surface
 * that shows a remark also offers the adjustment it asks for".
 *
 * It is deliberately the SMALL half of what the collection drawer does. The
 * collection also assigns a governed business key on save; that assignment is a
 * taxonomy gesture, not an answer to a remark, and it stays where it is rather
 * than being duplicated into a second half-copy. What travels here is exactly
 * the three fields the PATCH carries: title, owner, body.
 *
 * Same primitives and the same loss guard as `SkillEditorDrawer` — one editor
 * chrome for the hub, so a person who has closed one has closed both.
 */
import { useEffect, useRef, useState } from "react";
import { Button, ConfirmDialog, Input, Status, Textarea } from "../ui";

export interface KnowledgeEditorInitialValue {
  topicId: string;
  title: string;
  owner: string;
  bodyMd: string;
}

export interface KnowledgeEditorSaveValue {
  title: string;
  owner: string;
  bodyMd: string;
}

export default function KnowledgeEditorDrawer({
  initial,
  saving,
  error,
  onClose,
  onSave,
}: {
  initial: KnowledgeEditorInitialValue;
  saving: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (value: KnowledgeEditorSaveValue) => void;
}) {
  const titleRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState(initial.title);
  const [owner, setOwner] = useState(initial.owner);
  const [bodyMd, setBodyMd] = useState(initial.bodyMd);
  /**
   * Has anything been typed since the drawer opened? Escape and a backdrop
   * click used to throw a draft away without asking anywhere in this surface;
   * the Skill editor learned to ask, and a second editor that did not would be
   * two answers to one question.
   */
  const [dirty, setDirty] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);

  const requestClose = () => {
    if (saving) return;
    if (dirty) {
      setDiscardOpen(true);
      return;
    }
    onClose();
  };

  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // The confirmation owns Escape while it is open, or the question would
      // be asked of itself.
      if (event.key !== "Escape" || saving || discardOpen) return;
      if (dirty) {
        setDiscardOpen(true);
        return;
      }
      onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, saving, dirty, discardOpen]);

  const canSave = title.trim().length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-6"
      role="presentation"
      onMouseDown={(event) => { if (event.target === event.currentTarget) requestClose(); }}
    >
      <section
        className="flex max-h-[92vh] w-full max-w-[900px] flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-lg"
        role="dialog"
        aria-modal="true"
        aria-labelledby="knowledge-editor-title"
        onChange={() => setDirty(true)}
      >
        <header className="flex items-start justify-between gap-4 border-b border-border px-6 py-5">
          <div>
            <span className="text-caption uppercase tracking-wide text-text-secondary">Governed knowledge</span>
            <h2 id="knowledge-editor-title">Edit {initial.title}</h2>
            <p className="text-caption text-text-secondary">
              Saving writes a new version. The remark that asked for it stays pinned to the version it spoke of.
            </p>
          </div>
          <Button type="button" onClick={requestClose} disabled={saving} aria-label="Close knowledge editor">×</Button>
        </header>

        <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-6">
          <label>Title
            <Input
              ref={titleRef}
              data-testid="knowledge-editor-title-input"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              disabled={saving}
            />
          </label>
          <label>Owner
            <Input
              data-testid="knowledge-editor-owner"
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              disabled={saving}
              placeholder="owner@example.com"
            />
          </label>
          <label>Content
            <Textarea
              data-testid="knowledge-editor-body"
              rows={18}
              value={bodyMd}
              onChange={(event) => setBodyMd(event.target.value)}
              disabled={saving}
            />
          </label>
        </div>

        <footer className="flex items-center justify-end gap-3 border-t border-border px-6 py-4">
          <div className="mr-auto">{error && <Status as="block" tone="error" data-testid="knowledge-editor-error">{error}</Status>}</div>
          <Button type="button" onClick={requestClose} disabled={saving}>Cancel</Button>
          <Button
            type="button"
            className="is-primary"
            data-testid="knowledge-editor-save"
            onClick={() => onSave({ title: title.trim(), owner, bodyMd })}
            disabled={saving || !canSave}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </footer>
      </section>

      <ConfirmDialog
        open={discardOpen}
        onOpenChange={(open) => { if (!open) setDiscardOpen(false); }}
        title="Discard changes?"
        description={`The edits to “${initial.title}” have not been saved. Closing now loses them — cancel and use Save to keep them.`}
        confirmLabel="Discard changes"
        destructive
        onConfirm={() => { setDiscardOpen(false); onClose(); }}
        data-testid="knowledge-editor-discard-confirm"
        cancelTestId="knowledge-editor-discard-cancel"
        confirmTestId="knowledge-editor-discard-accept"
      />
    </div>
  );
}
