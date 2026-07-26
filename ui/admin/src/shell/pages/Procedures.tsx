/**
 * Procedures — Context surface for the governed AGENT PROCEDURE corpus.
 *
 * Story 44.1: rewired onto the governed, versioned context store
 * (`app.procedures` / `/api/context/procedures`, server/core/context_api.py).
 * A procedure here is documented guidance an AI agent follows during analysis
 * — the frontmatter (`name` + `description`) plus a Markdown body. This is
 * NOT the per-metric calculation/reconciliation list (`app.metric_procedures`
 * / GET /api/procedures): that read-only list moved to Governance, see
 * shell/pages/ReconciliationMethods.tsx.
 *
 * Mounted in the Context workspace as a sibling of Knowledge and Events. The
 * application shell already renders the frame, sidebar, topbar, and <main>;
 * this component renders ONLY the page content inside <main>.
 *
 * Styling: application.css (global) for shell/layout classes (page-header,
 * header-actions, panel, primary-button, secondary-button) + procedures.css
 * for the card grid and meta lines, mirroring knowledge.css/KnowledgeBasePage.
 *
 * ── Data ─────────────────────────────────────────────────────────────────
 * `GET /api/context/procedures?project_id=<id>` returns platform + project
 * procedures, each `{id, project_id, name, description, frontmatter_yaml,
 * body_md, status, created_by, created_at, updated_at, version_number}`.
 * The editor collects Name + Description (built into the required YAML
 * frontmatter client-side) and a Markdown body + preview pane (no WYSIWYG
 * dependency). On save failure the exact server message is shown and the
 * draft is preserved:
 *   - 422 `frontmatter_invalide` — the YAML/required-keys validation failed.
 *   - 409 `nom_deja_utilise` — a procedure with that name already exists in
 *     this scope.
 * A 403/404 on a platform-scope row's write (deny-by-default platform gate)
 * marks that row read-only in place rather than a fatal page error.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import "../application.css";
import "./procedures.css";
// Shares the editor/loading/error/read-only classes with KnowledgeBasePage
// (knowledge-editor-*, knowledge-status, knowledge-load-error, knowledge-card-
// actions, knowledge-readonly) rather than duplicating them — same v1 editor
// shape (title/name input(s) + Markdown textarea + preview pane).
import "../../knowledge.css";
import { apiFetch } from "../../lib/apiFetch";

/** Shape of one row returned by GET /api/context/procedures. */
interface ContextProcedure {
  id: string;
  project_id: string | null;
  name: string;
  description: string;
  frontmatter_yaml: string;
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
  | { status: "ok"; procedures: ContextProcedure[] };

type EditorState =
  | { mode: "closed" }
  | {
      mode: "create";
      name: string;
      description: string;
      body_md: string;
      // No prior frontmatter to preserve on create — starts empty and gains
      // only the name/description keys once saved.
      frontmatterYaml: string;
      /** Raw explicit owner text (Story 44.11), e.g. an email. Empty = unset. */
      owner: string;
      error: string | null;
      saving: boolean;
    }
  | {
      mode: "edit";
      procedure: ContextProcedure;
      name: string;
      description: string;
      body_md: string;
      // The as-loaded frontmatter, kept verbatim so any keys beyond
      // name/description survive an edit (Story 44.1 review).
      frontmatterYaml: string;
      owner: string;
      error: string | null;
      saving: boolean;
    };

function formatUpdated(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { message?: string; code?: string };
    return body.message ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

/**
 * Update ONLY the `name` and `description` top-level keys of an existing
 * frontmatter YAML blob, preserving every other line verbatim (Story 44.1
 * review: the server accepts any valid YAML mapping and preserves extra keys
 * across an edit — the client must not silently drop them by rebuilding the
 * frontmatter from just these two fields).
 *
 * This is a minimal flat-key round-trip, not a general YAML parser: it only
 * recognises un-indented `key: value` lines at the top level (which is all
 * `name`/`description` ever are) and leaves every other line — including
 * nested mappings/lists under other keys — untouched and in place. JSON is a
 * valid YAML flow-scalar subset, so the emitted name/description values
 * always round-trip through yaml.safe_load server-side regardless of quotes
 * or newlines in the input.
 */
function updateFrontmatterYaml(rawYaml: string, name: string, description: string): string {
  const lines = rawYaml.length > 0 ? rawYaml.split(/\r?\n/) : [];
  const out: string[] = [];
  let sawName = false;
  let sawDescription = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const match = /^([A-Za-z0-9_-]+):/.exec(line);
    const key = match?.[1];
    if (key === "name" || key === "description") {
      if (key === "name") {
        out.push(`name: ${JSON.stringify(name)}`);
        sawName = true;
      } else {
        out.push(`description: ${JSON.stringify(description)}`);
        sawDescription = true;
      }
      // Consume the replaced key's continuation lines (block/folded scalars,
      // e.g. `description: >` followed by indented lines — the server stores
      // them verbatim): re-emitting them after the flow-scalar replacement
      // would orphan indented lines and produce invalid YAML (44.1
      // re-review). Cosmetic blank lines directly after the replaced key are
      // consumed too — an accepted, content-free loss.
      while (i + 1 < lines.length && (lines[i + 1].trim() === "" || /^\s/.test(lines[i + 1]))) {
        i++;
      }
      continue;
    }
    out.push(line);
  }

  // Drop trailing blank lines left over from the split so appended keys land
  // cleanly, without touching any other verbatim content above them.
  while (out.length > 0 && out[out.length - 1].trim() === "") {
    out.pop();
  }

  if (!sawName) out.push(`name: ${JSON.stringify(name)}`);
  if (!sawDescription) out.push(`description: ${JSON.stringify(description)}`);

  return `${out.join("\n")}\n`;
}

export default function Procedures({ projectId = "default" }: { projectId?: string }) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [editor, setEditor] = useState<EditorState>({ mode: "closed" });
  const [readOnlyIds, setReadOnlyIds] = useState<Set<string>>(new Set());
  // Archive failures are not silent (Story 44.1 review): the failing card
  // keeps its own {id, message}, cleared on the next successful archive of
  // that same card (or when a fresh archive attempt starts).
  const [archiveError, setArchiveError] = useState<{ id: string; message: string } | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
      const res = await apiFetch(
        `/api/context/procedures?project_id=${encodeURIComponent(projectId)}`,
      );
      if (!res.ok) {
        setState({ status: "error", message: await readErrorMessage(res) });
        return;
      }
      const body = (await res.json()) as { procedures?: ContextProcedure[] };
      setState({ status: "ok", procedures: body.procedures ?? [] });
    } catch (err) {
      setState({ status: "error", message: err instanceof Error ? err.message : "Network error" });
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Initial focus on the name input when the dialog opens (Story 44.1 review).
  useEffect(() => {
    if (editor.mode !== "closed") {
      nameInputRef.current?.focus();
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
    setEditor({
      mode: "create",
      name: "",
      description: "",
      body_md: "",
      frontmatterYaml: "",
      owner: "",
      error: null,
      saving: false,
    });
  }

  function openEdit(procedure: ContextProcedure) {
    setEditor({
      mode: "edit",
      procedure,
      name: procedure.name,
      description: procedure.description,
      body_md: procedure.body_md,
      frontmatterYaml: procedure.frontmatter_yaml,
      owner: procedure.owner ?? "",
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

    const isPlatformRow = editor.mode === "edit" && editor.procedure.project_id === null;
    const editedProcedureId = editor.mode === "edit" ? editor.procedure.id : null;
    const frontmatter_yaml = updateFrontmatterYaml(
      editor.frontmatterYaml,
      editor.name,
      editor.description,
    );
    // Empty owner text means "no explicit owner" (Story 44.11) -- sent as
    // null so the server clears/leaves-unset rather than storing "".
    const owner = editor.owner.trim() || null;

    try {
      const res =
        editor.mode === "create"
          ? await apiFetch("/api/context/procedures", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                project_id: projectId,
                frontmatter_yaml,
                body_md: editor.body_md,
                owner,
              }),
            })
          : await apiFetch(`/api/context/procedures/${encodeURIComponent(editor.procedure.id)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ frontmatter_yaml, body_md: editor.body_md, owner }),
            });

      if (!res.ok) {
        const message = await readErrorMessage(res);
        if (isPlatformRow && editedProcedureId && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(editedProcedureId));
        }
        // Functional update: never resurrect a dialog the user closed while
        // the save was in flight (44.1 re-review).
        setEditor((prev) => (prev.mode === "closed" ? prev : { ...prev, saving: false, error: message }));
        return;
      }

      const saved = (await res.json()) as ContextProcedure;
      setState((prev) => {
        if (prev.status !== "ok") return prev;
        const exists = prev.procedures.some((p) => p.id === saved.id);
        return {
          status: "ok",
          procedures: exists
            ? prev.procedures.map((p) => (p.id === saved.id ? saved : p))
            : [saved, ...prev.procedures],
        };
      });
      closeEditor();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Network error";
      setEditor((prev) => (prev.mode === "closed" ? prev : { ...prev, saving: false, error: message }));
    }
  }

  async function archiveProcedure(procedure: ContextProcedure) {
    const isPlatformRow = procedure.project_id === null;
    setArchiveError((prev) => (prev?.id === procedure.id ? null : prev));
    try {
      const res = await apiFetch(
        `/api/context/procedures/${encodeURIComponent(procedure.id)}/archive`,
        { method: "POST" },
      );
      if (!res.ok) {
        const message = await readErrorMessage(res);
        if (isPlatformRow && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(procedure.id));
        }
        setArchiveError({ id: procedure.id, message });
        return;
      }
      const archived = (await res.json()) as ContextProcedure;
      setState((prev) =>
        prev.status === "ok"
          ? { status: "ok", procedures: prev.procedures.filter((p) => p.id !== archived.id) }
          : prev,
      );
      setArchiveError((prev) => (prev?.id === procedure.id ? null : prev));
    } catch (err) {
      setArchiveError({
        id: procedure.id,
        message: err instanceof Error ? err.message : "Network error",
      });
    }
  }

  const procedures =
    state.status === "ok" ? state.procedures.filter((p) => p.status === "active") : [];

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Procedures</h1>
          <p>Documented guidance AI agents follow during analysis for this project.</p>
        </div>
        <div className="header-actions">
          <button className="primary-button" type="button" onClick={openCreate}>
            + Add procedure
          </button>
        </div>
      </div>

      {state.status === "loading" && (
        <p className="knowledge-status" role="status">
          Loading procedures…
        </p>
      )}

      {state.status === "error" && (
        <div className="knowledge-load-error" role="alert" data-testid="procedures-error">
          <span className="signal-label error">
            <span className="signal-mark" />
            Couldn&rsquo;t load procedures
          </span>
          <p>{state.message}</p>
          <button className="secondary-button" type="button" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {state.status === "ok" && procedures.length === 0 && (
        <section className="panel procedure-empty" data-testid="procedures-empty">
          <p>
            No procedures defined yet. Document the guidance analysis must follow — GA4 versus
            platform attribution, how to interpret an anomaly, escalation rules — so the agent
            has one agreed source to read.
          </p>
        </section>
      )}

      {state.status === "ok" && procedures.length > 0 && (
        <section className="procedure-grid">
          {procedures.map((procedure) => {
            const isPlatform = procedure.project_id === null;
            const isReadOnly = isPlatform && readOnlyIds.has(procedure.id);
            return (
              <article key={procedure.id} className="panel procedure-card">
                <div className="procedure-head">
                  <h2>{procedure.name}</h2>
                  <div className="procedure-head-badges">
                    {isPlatform && <span className="procedure-platform">Platform</span>}
                    {isReadOnly && <span className="knowledge-readonly">Read-only</span>}
                  </div>
                </div>
                <p className="procedure-desc">{procedure.description}</p>
                <div className="procedure-meta">
                  <span>
                    Author: <b>{procedure.created_by}</b>
                  </span>
                  <span>
                    {/* Same resolution chain as the mindmap (44.11 re-review):
                        explicit owner -> created_by -> "auto". */}
                    Owner: <b>{procedure.owner || procedure.created_by || "auto"}</b>
                  </span>
                  <span>
                    Last updated: <time>{formatUpdated(procedure.updated_at)}</time>
                  </span>
                  <span>
                    Version: <b>v{procedure.version_number}</b>
                  </span>
                </div>
                <div className="knowledge-card-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => openEdit(procedure)}
                    disabled={isReadOnly}
                  >
                    Edit procedure
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void archiveProcedure(procedure)}
                    disabled={isReadOnly}
                  >
                    Archive
                  </button>
                </div>
                {archiveError && archiveError.id === procedure.id && (
                  <div
                    className="knowledge-editor-error"
                    role="alert"
                    data-testid={`procedure-archive-error-${procedure.id}`}
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
          aria-labelledby="procedure-editor-heading"
        >
          <div className="panel knowledge-editor">
            <h2 id="procedure-editor-heading">
              {editor.mode === "create" ? "New procedure" : "Edit procedure"}
            </h2>

            {editor.error && (
              <div
                className="knowledge-editor-error"
                role="alert"
                data-testid="procedure-editor-error"
              >
                {editor.error}
              </div>
            )}

            <label className="knowledge-editor-field">
              <span>Name</span>
              <input
                ref={nameInputRef}
                type="text"
                value={editor.name}
                onChange={(e) => setEditor({ ...editor, name: e.target.value })}
                placeholder="e.g. Post-click attribution reconciliation"
              />
            </label>

            <label className="knowledge-editor-field">
              <span>Description</span>
              <input
                type="text"
                value={editor.description}
                onChange={(e) => setEditor({ ...editor, description: e.target.value })}
                placeholder="One line: what this procedure governs"
              />
            </label>

            <label className="knowledge-editor-field">
              <span>Owner (email)</span>
              <input
                type="text"
                data-testid="procedure-editor-owner"
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
                  placeholder="Write the procedure in Markdown…"
                />
              </label>
              <div className="knowledge-editor-preview" data-testid="procedure-preview">
                <span className="knowledge-editor-preview-label">Preview</span>
                <pre>{editor.body_md || "The preview appears here."}</pre>
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
