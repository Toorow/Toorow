/**
 * KnowledgeBasePage — v3 restyle of the shared knowledge base.
 *
 * Story 44.1: rewired off the demo `app.knowledge_entries` surface onto the
 * governed, versioned context store. Mounted at Context/knowledge via
 * shell/ContentRouter.tsx as <KnowledgeBasePage projectId={projectId} />. The
 * application shell already renders the frame, sidebar, topbar, and <main>.
 * This component renders ONLY the page content inside <main>: the page
 * header, an "Add knowledge entry" action, and the grid of governed topics.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, header-actions, primary-button, secondary-button, panel) +
 * knowledge.css for the page-specific card grid, entry cards, topic pill and
 * the create/edit editor. Colors come exclusively from the application.css
 * CSS variables (dark-theme safe); dates use Geist tabular via .knowledge-meta.
 *
 * ── Data ─────────────────────────────────────────────────────────────────
 * `GET /api/context/topics?project_id=<id>` (server/core/context_api.py)
 * returns platform-scope (project_id null) + this project's topics. There is
 * no literal fallback any more: a failed load says so, and zero topics reads
 * as an honest empty state with a working "Add knowledge entry" action.
 *
 * Add/Edit/Archive call `POST /api/context/topics`, `PATCH
 * /api/context/topics/{id}` and `POST /api/context/topics/{id}/archive`. The
 * editor is v1: a title input + a Markdown textarea + a raw-text preview pane
 * (no WYSIWYG dependency). On a write failure the draft is preserved and the
 * server's exact `{code, message}` is shown; a 403/404 on a platform-scope
 * row's write (deny-by-default platform gate, context_api.py
 * check_platform_write_authorized) marks that row read-only in place rather
 * than treating it as a fatal page error.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import "./shell/application.css";
import "./knowledge.css";
import { apiFetch } from "./lib/apiFetch";
import RawMarkdown from "./lib/RawMarkdown";

/** Shape of one row returned by GET /api/context/topics. */
interface ContextTopic {
  id: string;
  project_id: string | null;
  title: string;
  body_md: string;
  status: "active" | "archived";
  /** Story 44.11: explicit human owner (free-form, email by convention); null = unset. */
  owner?: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  version_number: number;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; topics: ContextTopic[] };

type EditorState =
  | { mode: "closed" }
  | {
      mode: "create";
      title: string;
      body_md: string;
      /** Raw explicit owner text (Story 44.11), e.g. an email. Empty = unset. */
      owner: string;
      error: string | null;
      saving: boolean;
    }
  | {
      mode: "edit";
      topic: ContextTopic;
      title: string;
      body_md: string;
      owner: string;
      error: string | null;
      saving: boolean;
    };

/** Format an ISO updated_at to a short human date, e.g. "12 May 2026". */
function formatUpdated(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** Parse the {code, message} envelope the context API returns on failure. */
async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { message?: string; code?: string };
    return body.message ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export default function KnowledgeBasePage({ projectId = "default" }: { projectId?: string }) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [editor, setEditor] = useState<EditorState>({ mode: "closed" });
  // ids of platform-scope topics we now know cannot be written by this identity
  // (a prior write attempt came back 403/404) — surfaced as read-only.
  const [readOnlyIds, setReadOnlyIds] = useState<Set<string>>(new Set());
  // Archive failures are not silent (Story 44.1 review): the failing card
  // keeps its own {id, message}, cleared on the next successful archive of
  // that same card (or when a fresh archive attempt starts).
  const [archiveError, setArchiveError] = useState<{ id: string; message: string } | null>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
      const res = await apiFetch(`/api/context/topics?project_id=${encodeURIComponent(projectId)}`);
      if (!res.ok) {
        setState({ status: "error", message: await readErrorMessage(res) });
        return;
      }
      const body = (await res.json()) as { topics?: ContextTopic[] };
      setState({ status: "ok", topics: body.topics ?? [] });
    } catch (err) {
      setState({
        status: "error",
        message: err instanceof Error ? err.message : "Network error",
      });
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Initial focus on the title input when the dialog opens (Story 44.1 review).
  useEffect(() => {
    if (editor.mode !== "closed") {
      titleInputRef.current?.focus();
    }
  }, [editor.mode]);

  // Escape-to-close (Story 44.1 review) — active only while the dialog is
  // open, and never while a save is in flight (44.1 re-review: closing
  // mid-save silently discarded the draft).
  const editorSaving = editor.mode !== "closed" && editor.saving;
  useEffect(() => {
    if (editor.mode === "closed") return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !editorSaving) {
        closeEditor();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor.mode, editorSaving]);

  function openCreate() {
    setEditor({ mode: "create", title: "", body_md: "", owner: "", error: null, saving: false });
  }

  function openEdit(topic: ContextTopic) {
    setEditor({
      mode: "edit",
      topic,
      title: topic.title,
      body_md: topic.body_md,
      owner: topic.owner ?? "",
      error: null,
      saving: false,
    });
  }

  function closeEditor() {
    setEditor({ mode: "closed" });
  }

  async function submitEditor() {
    if (editor.mode === "closed") return;
    setEditor({ ...editor, saving: true, error: null });

    const isPlatformRow = editor.mode === "edit" && editor.topic.project_id === null;
    const editedTopicId = editor.mode === "edit" ? editor.topic.id : null;
    // Empty owner text means "no explicit owner" (Story 44.11) -- sent as
    // null so the server clears/leaves-unset rather than storing "".
    const owner = editor.owner.trim() || null;

    try {
      const res =
        editor.mode === "create"
          ? await apiFetch("/api/context/topics", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                project_id: projectId,
                title: editor.title,
                body_md: editor.body_md,
                owner,
              }),
            })
          : await apiFetch(`/api/context/topics/${encodeURIComponent(editor.topic.id)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ title: editor.title, body_md: editor.body_md, owner }),
            });

      if (!res.ok) {
        const message = await readErrorMessage(res);
        if (isPlatformRow && editedTopicId && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(editedTopicId));
        }
        // Functional update: never resurrect a dialog the user closed while
        // the save was in flight (44.1 re-review).
        setEditor((prev) => (prev.mode === "closed" ? prev : { ...prev, saving: false, error: message }));
        return;
      }

      const saved = (await res.json()) as ContextTopic;
      setState((prev) => {
        if (prev.status !== "ok") return prev;
        const exists = prev.topics.some((t) => t.id === saved.id);
        return {
          status: "ok",
          topics: exists
            ? prev.topics.map((t) => (t.id === saved.id ? saved : t))
            : [saved, ...prev.topics],
        };
      });
      closeEditor();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Network error";
      setEditor((prev) => (prev.mode === "closed" ? prev : { ...prev, saving: false, error: message }));
    }
  }

  async function archiveTopic(topic: ContextTopic) {
    const isPlatformRow = topic.project_id === null;
    setArchiveError((prev) => (prev?.id === topic.id ? null : prev));
    try {
      const res = await apiFetch(`/api/context/topics/${encodeURIComponent(topic.id)}/archive`, {
        method: "POST",
      });
      if (!res.ok) {
        const message = await readErrorMessage(res);
        if (isPlatformRow && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(topic.id));
        }
        setArchiveError({ id: topic.id, message });
        return;
      }
      const archived = (await res.json()) as ContextTopic;
      setState((prev) =>
        prev.status === "ok"
          ? { status: "ok", topics: prev.topics.filter((t) => t.id !== archived.id) }
          : prev,
      );
      setArchiveError((prev) => (prev?.id === topic.id ? null : prev));
    } catch (err) {
      setArchiveError({
        id: topic.id,
        message: err instanceof Error ? err.message : "Network error",
      });
    }
  }

  const topics = state.status === "ok" ? state.topics.filter((t) => t.status === "active") : [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Shared knowledge base</h1>
          <p>
            Governed business definitions and guidance read by AI agents during analysis.
          </p>
        </div>
        <div className="header-actions">
          <button className="primary-button" type="button" onClick={openCreate}>
            + Add knowledge entry
          </button>
        </div>
      </div>

      {state.status === "loading" && (
        <p className="knowledge-status" role="status">
          Loading the knowledge base…
        </p>
      )}

      {state.status === "error" && (
        <div className="knowledge-load-error" role="alert" data-testid="knowledge-error">
          <span className="signal-label error">
            <span className="signal-mark" />
            Couldn&rsquo;t load the knowledge base
          </span>
          <p>{state.message}</p>
          <button className="secondary-button" type="button" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {state.status === "ok" && topics.length === 0 && (
        <section className="panel knowledge-empty" data-testid="knowledge-empty">
          <h2>No knowledge entries yet</h2>
          <p>
            Governed definitions added here are read by AI agents during analysis. Add your
            first entry to start building this project&rsquo;s shared knowledge base.
          </p>
        </section>
      )}

      {state.status === "ok" && topics.length > 0 && (
        <section className="knowledge-grid">
          {topics.map((topic) => {
            const isPlatform = topic.project_id === null;
            const isReadOnly = isPlatform && readOnlyIds.has(topic.id);
            return (
              <article key={topic.id} className="panel knowledge-card">
                <div className="knowledge-card-head">
                  {isPlatform && <span className="knowledge-topic knowledge-platform">Platform</span>}
                  {isReadOnly && <span className="knowledge-readonly">Read-only</span>}
                </div>
                <h2>{topic.title}</h2>
                <div className="knowledge-meta">
                  <span>
                    Author: <b>{topic.created_by}</b>
                  </span>
                  <span>
                    {/* Same resolution chain as the mindmap (44.11 re-review):
                        explicit owner -> created_by -> "auto". */}
                    Owner: <b>{topic.owner || topic.created_by || "auto"}</b>
                  </span>
                  <span>
                    Last updated: <time>{formatUpdated(topic.updated_at)}</time>
                  </span>
                  <span>
                    Version: <b>v{topic.version_number}</b>
                  </span>
                </div>
                <div className="knowledge-card-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => openEdit(topic)}
                    disabled={isReadOnly}
                  >
                    Edit entry
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void archiveTopic(topic)}
                    disabled={isReadOnly}
                  >
                    Archive
                  </button>
                </div>
                {archiveError && archiveError.id === topic.id && (
                  <div
                    className="knowledge-editor-error"
                    role="alert"
                    data-testid={`knowledge-archive-error-${topic.id}`}
                  >
                    {archiveError.message}
                  </div>
                )}
              </article>
            );
          })}
        </section>
      )}

      {editor.mode !== "closed" && (
        <div
          className="knowledge-editor-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="knowledge-editor-heading"
        >
          <div className="panel knowledge-editor">
            <h2 id="knowledge-editor-heading">
              {editor.mode === "create" ? "New knowledge entry" : "Edit knowledge entry"}
            </h2>

            {editor.error && (
              <div className="knowledge-editor-error" role="alert" data-testid="knowledge-editor-error">
                {editor.error}
              </div>
            )}

            <label className="knowledge-editor-field">
              <span>Title</span>
              <input
                ref={titleInputRef}
                type="text"
                value={editor.title}
                onChange={(e) => setEditor({ ...editor, title: e.target.value })}
                placeholder="e.g. ROAS calculation & deduplication policy"
              />
            </label>

            <label className="knowledge-editor-field">
              <span>Owner (email)</span>
              <input
                type="text"
                data-testid="knowledge-editor-owner"
                value={editor.owner}
                onChange={(e) => setEditor({ ...editor, owner: e.target.value })}
                placeholder="Unset — falls back to created_by, then &ldquo;auto&rdquo;"
              />
            </label>

            <div className="knowledge-editor-split">
              <label className="knowledge-editor-field">
                <span>Body (Markdown)</span>
                <textarea
                  rows={12}
                  value={editor.body_md}
                  onChange={(e) => setEditor({ ...editor, body_md: e.target.value })}
                  placeholder="Write the entry in Markdown…"
                />
              </label>
              <div className="knowledge-editor-preview" data-testid="knowledge-preview">
                <span className="knowledge-editor-preview-label">Preview</span>
                <RawMarkdown
                  text={editor.body_md}
                  placeholder="The preview appears here."
                />
              </div>
            </div>

            <div className="knowledge-editor-actions">
              <button className="secondary-button" type="button" onClick={closeEditor}>
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => void submitEditor()}
                disabled={editor.saving}
              >
                {editor.saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
