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
 * Styling: theme utilities and `ui/` primitives only (PageHeader, Panel,
 * Status, Button, Input, Textarea, NativeSelect, ConfirmDialog) — no legacy
 * class remains; the retired knowledge.css vocabulary (card grid, entry cards,
 * topic pills, notices, create/edit editor) lives on as utility compositions
 * below. Colors reference theme tokens only (dark-scheme safe); dates keep the
 * Geist tabular treatment on the card meta line.
 *
 * ── Data ─────────────────────────────────────────────────────────────────
 * `GET /api/context/topics?project_id=<id>` (server/core/context_api.py)
 * returns platform-scope (project_id null) + this project's topics. There is
 * no literal fallback any more: a failed load says so, and zero topics reads
 * as an honest empty state with a working "Add knowledge entry" action.
 *
 * Add/Edit/Archive/Restore call `POST /api/context/topics`, `PATCH
 * /api/context/topics/{id}`, `POST /api/context/topics/{id}/archive` and
 * `POST /api/context/topics/{id}/restore`.
 *
 * Archiving is NOT one-way (2026-08-18): an archive was always a version, not
 * a deletion, and the restore route makes that reversible from here. "Show
 * archived" re-reads the list with `?status=all`, archived rows are marked in
 * place, and each carries a Restore action. Both gestures go through the
 * shared ConfirmDialog, which names the entry — archiving still asks, because
 * agents stop reading the entry the moment it leaves.
 *
 * The editor is v1: a title input + a Markdown textarea + a raw-text preview pane
 * (no WYSIWYG dependency). On a write failure the draft is preserved and the
 * server's exact `{code, message}` is shown; a 403/404 on a platform-scope
 * row's write (deny-by-default platform gate, context_api.py
 * check_platform_write_authorized) marks that row read-only in place rather
 * than treating it as a fatal page error.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpenIcon } from "lucide-react";
import { apiFetch } from "./lib/apiFetch";
import {
  CONTEXT_REQUEST_TIMEOUT_MS,
  errorMessage,
  parseTopic,
  parseTopicsEnvelope,
  readSurfaceFailure,
  type ContextCapabilities,
  type SurfaceFailureKind,
} from "./lib/contextRuntime";
import RawMarkdown from "./lib/RawMarkdown";
import { Button, ConfirmDialog, EmptyState, Input, NativeSelect, PageHeader, Panel, Status, TONE_TEXT, Textarea, formatDate } from "./ui";
import type { ContextHubContentContext } from "./connaissances/ContextHubLayout";
import {
  createBusinessLink,
  deleteBusinessLink,
  type TaxonomyType,
} from "./connaissances/businessTaxonomyApi";

/**
 * The retired knowledge.css, ported rule for rule into the theme's utility
 * vocabulary (docs/ui-css-strategy.md, wave 3). Every color is a theme token,
 * so the page stays dark-scheme safe; tinted grounds mix a tone token into the
 * surface token exactly as the sheet did. The strings are shared with the
 * editor markup below and with nothing else — they are compositions, not a new
 * class vocabulary.
 */
/** .knowledge-load-error — the page's one notice block (status or alert). */
const NOTICE_CLASS =
  "flex flex-col items-start gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-error)_28%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-error)_6%,var(--color-surface-light))] px-[22px] py-5 [&_p]:m-0 [&_p]:text-label [&_p]:text-text-secondary";
/** .knowledge-status — the plain loading / archived-note line. */
const STATUS_LINE_CLASS = "text-label text-text-secondary";
/** .knowledge-editor-error — the compact per-card / in-editor write failure. */
const INLINE_ERROR_CLASS =
  `rounded-[8px] border border-[color-mix(in_srgb,var(--color-error)_30%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-error)_8%,var(--color-surface-light))] px-3.5 py-2.5 text-label ${TONE_TEXT.error}`;
/** .knowledge-topic / .knowledge-platform / .knowledge-readonly pills. */
const PILL_CLASS = "inline-flex items-center rounded-pill border px-[11px] py-1 text-[11px] font-bold";
const TOPIC_PILL_CLASS = `${PILL_CLASS} tracking-[0.01em] border-[color-mix(in_srgb,var(--color-info)_28%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-info)_8%,var(--color-surface-light))] ${TONE_TEXT.info}`;
const PLATFORM_PILL_CLASS = `${PILL_CLASS} tracking-[0.01em] border-[color-mix(in_srgb,var(--color-text-secondary)_30%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-text-secondary)_10%,var(--color-surface-light))] text-text-secondary`;
const READONLY_PILL_CLASS = `${PILL_CLASS} border-[color-mix(in_srgb,var(--color-warning)_30%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-warning)_10%,var(--color-surface-light))] ${TONE_TEXT.warning}`;
/** .knowledge-card h2 / .knowledge-empty h2 — the 17px Lexend entry title. */
const CARD_TITLE_CLASS = "m-0 font-display text-[17px] font-semibold leading-[1.35]";
/** .knowledge-editor-field — label wrapper for one editor field. */
const FIELD_CLASS = "flex flex-col gap-1.5 text-label text-text-secondary";
/** .knowledge-editor-preview — the recessed raw-text preview box, and the
 *  typography its `<pre>` (RawMarkdown) carries. */
const PREVIEW_CLASS =
  "flex min-h-[220px] flex-col gap-1.5 overflow-auto rounded-[8px] border border-[color-mix(in_srgb,var(--color-text-secondary)_20%,var(--color-surface-light))] bg-[color-mix(in_srgb,var(--color-text-secondary)_4%,var(--color-surface-light))] px-3 py-[9px] [&_pre]:m-0 [&_pre]:font-primary [&_pre]:text-label [&_pre]:leading-[1.6] [&_pre]:whitespace-pre-wrap [&_pre]:break-words [&_pre]:text-text";

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
  capabilities: ContextCapabilities | null;
}

type LoadState =
  | { status: "loading" }
  | { status: SurfaceFailureKind; message: string }
  | { status: "ok"; topics: ContextTopic[]; capabilities: ContextCapabilities | null };

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
  return formatDate(iso);
}

interface ErrorDetails {
  code: string;
  message: string;
}

/** Parse the {code, message} envelope the context API returns on failure. */
async function readErrorDetails(res: Response): Promise<ErrorDetails> {
  try {
    const body = (await res.json()) as { message?: string; code?: string };
    return { code: body.code ?? "", message: body.message ?? `HTTP ${res.status}` };
  } catch {
    return { code: "", message: `HTTP ${res.status}` };
  }
}

async function fetchLatestTopic(topicId: string, projectId: string, signal: AbortSignal): Promise<ContextTopic> {
  const response = await apiFetch(
    `/api/context/topics/${encodeURIComponent(topicId)}?project_id=${encodeURIComponent(projectId)}`,
    { signal, cache: "no-store" },
  );
  if (!response.ok) throw new Error((await readErrorDetails(response)).message);
  return parseTopic(await response.json(), projectId) as ContextTopic;
}

export default function KnowledgeBasePage({
  projectId,
  onOpenTopic,
  businessContext,
}: {
  projectId: string;
  onOpenTopic?: (id: string) => void;
  businessContext?: ContextHubContentContext;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [editor, setEditor] = useState<EditorState>({ mode: "closed" });
  // ids of platform-scope topics we now know cannot be written by this identity
  // (a prior write attempt came back 403/404) — surfaced as read-only.
  const [readOnlyIds, setReadOnlyIds] = useState<Set<string>>(new Set());
  // Archive/restore failures are not silent (Story 44.1 review): the failing
  // card keeps its own {id, message}, cleared on the next successful write of
  // that same card (or when a fresh attempt starts). One slot per card, one
  // alert language — a restore failure speaks where an archive failure does.
  const [archiveError, setArchiveError] = useState<{ id: string; message: string } | null>(null);
  // Both gestures are confirmed by name before anything is sent.
  const [pendingArchive, setPendingArchive] = useState<ContextTopic | null>(null);
  const [pendingRestore, setPendingRestore] = useState<ContextTopic | null>(null);
  // Archived entries are not shown by default — the base is what agents read
  // today. Turning this on re-reads the list with ?status=all, so the archived
  // rows appear beside the active ones rather than on a screen of their own.
  const [showArchived, setShowArchived] = useState(false);
  const [taxonomySelection, setTaxonomySelection] = useState("");
  const [bindingNotice, setBindingNotice] = useState<string | null>(null);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const loadGenerationRef = useRef(0);
  const loadAbortRef = useRef<AbortController | null>(null);
  const mutationGenerationRef = useRef(0);
  const mutationAbortRef = useRef<AbortController | null>(null);
  const scopeRef = useRef("");
  const focusReturnRef = useRef<HTMLElement | null>(null);

  const scopedProjectId = projectId.trim();
  scopeRef.current = scopedProjectId;

  const load = useCallback(async () => {
    if (!scopedProjectId) {
      setState({ status: "capability", message: "Select a project before opening the knowledge base." });
      return;
    }
    const generation = ++loadGenerationRef.current;
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, CONTEXT_REQUEST_TIMEOUT_MS);
    setState({ status: "loading" });
    try {
      const res = await apiFetch(
        `/api/context/topics?project_id=${encodeURIComponent(scopedProjectId)}${showArchived ? "&status=all" : ""}`,
        { signal: controller.signal },
      );
      if (!res.ok) {
        const failure = await readSurfaceFailure(res);
        if (generation === loadGenerationRef.current) {
          setState({ status: failure.kind, message: failure.message });
        }
        return;
      }
      const parsed = parseTopicsEnvelope(await res.json(), scopedProjectId);
      if (!controller.signal.aborted && generation === loadGenerationRef.current) {
        setState({ status: "ok", topics: parsed.topics, capabilities: parsed.capabilities });
      }
    } catch (err) {
      if (generation !== loadGenerationRef.current) return;
      if (controller.signal.aborted && !timedOut) return;
      setState({
        status: "error",
        message: timedOut ? "The knowledge request timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }, [scopedProjectId, showArchived]);

  useEffect(() => {
    focusReturnRef.current = null;
    setEditor({ mode: "closed" });
    setArchiveError(null);
    setPendingArchive(null);
    setPendingRestore(null);
    void load();
    return () => {
      loadGenerationRef.current += 1;
      loadAbortRef.current?.abort();
      mutationGenerationRef.current += 1;
      mutationAbortRef.current?.abort();
    };
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
    if (editor.mode === "closed" || state.status !== "ok" || state.capabilities?.can_write !== true) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !editorSaving) {
        closeEditor();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor.mode, editorSaving]);

  function rememberEditorTrigger() {
    focusReturnRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  function openCreate() {
    rememberEditorTrigger();
    setEditor({ mode: "create", title: "", body_md: "", owner: "", error: null, saving: false });
    setTaxonomySelection(
      businessContext?.selected
        ? `${businessContext.selected.type}:${businessContext.selected.id}`
        : "",
    );
  }

  function openEdit(topic: ContextTopic) {
    rememberEditorTrigger();
    setEditor({
      mode: "edit",
      topic,
      title: topic.title,
      body_md: topic.body_md,
      owner: topic.owner ?? "",
      error: null,
      saving: false,
    });
    const link = businessContext?.links.find(
      (entry) => entry.target_type === "topic" && entry.target_id === topic.id,
    );
    setTaxonomySelection(link ? `${link.taxonomy_type}:${link.taxonomy_id}` : "");
  }

  function closeEditor() {
    const returnTo = focusReturnRef.current;
    focusReturnRef.current = null;
    setEditor({ mode: "closed" });
    window.setTimeout(() => returnTo?.focus(), 0);
  }

  function mutationIsCurrent(generation: number, scope: string): boolean {
    return generation === mutationGenerationRef.current && scope === scopeRef.current;
  }

  async function submitEditor() {
    if (editor.mode === "closed" || state.status !== "ok" || state.capabilities?.can_write !== true) return;
    const draft = editor;
    const requestScope = scopedProjectId;
    const generation = ++mutationGenerationRef.current;
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, CONTEXT_REQUEST_TIMEOUT_MS);
    setEditor({ ...draft, saving: true, error: null });

    const isPlatformRow = draft.mode === "edit" && draft.topic.project_id === null;
    const editedTopicId = draft.mode === "edit" ? draft.topic.id : null;
    const owner = draft.owner.trim() || null;

    try {
      const res = draft.mode === "create"
        ? await apiFetch(`/api/context/topics?project_id=${encodeURIComponent(requestScope)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: requestScope, title: draft.title, body_md: draft.body_md, owner }),
            signal: controller.signal,
          })
        : await apiFetch(`/api/context/topics/${encodeURIComponent(draft.topic.id)}?project_id=${encodeURIComponent(requestScope)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ expected_version: draft.topic.version_number, title: draft.title, body_md: draft.body_md, owner }),
            signal: controller.signal,
          });

      if (!res.ok) {
        const failure = await readErrorDetails(res);
        if (!mutationIsCurrent(generation, requestScope)) return;
        if (isPlatformRow && editedTopicId && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(editedTopicId));
        }
        if (res.status === 409 && failure.code === "version_conflict" && draft.mode === "edit") {
          try {
            const latest = await fetchLatestTopic(draft.topic.id, requestScope, controller.signal);
            if (!mutationIsCurrent(generation, requestScope)) return;
            latest.capabilities = draft.topic.capabilities;
            setState((prev) => prev.status === "ok" ? {
              ...prev,
              topics: prev.topics.map((topic) => topic.id === latest.id ? latest : topic),
            } : prev);
            setEditor((prev) => prev.mode === "edit" && prev.topic.id === latest.id ? {
              ...prev,
              topic: latest,
              saving: false,
              error: `${failure.message} Latest version v${latest.version_number} loaded; your draft was preserved.`,
            } : prev);
          } catch (reloadError) {
            if (mutationIsCurrent(generation, requestScope)) {
              setEditor((prev) => prev.mode === "closed" ? prev : {
                ...prev,
                saving: false,
                error: `${failure.message} Could not reload the latest version: ${errorMessage(reloadError)}`,
              });
            }
          }
          return;
        }
        setEditor((prev) => prev.mode === "closed" ? prev : { ...prev, saving: false, error: failure.message });
        return;
      }

      const saved = parseTopic(await res.json(), requestScope) as ContextTopic;
      if (!mutationIsCurrent(generation, requestScope)) return;
      saved.capabilities = draft.mode === "edit" ? draft.topic.capabilities : state.capabilities;
      setState((prev) => {
        if (prev.status !== "ok") return prev;
        const exists = prev.topics.some((topic) => topic.id === saved.id);
        return {
          ...prev,
          topics: exists
            ? prev.topics.map((topic) => topic.id === saved.id ? saved : topic)
            : [saved, ...prev.topics],
        };
      });
      const existingLinks = businessContext?.links.filter(
        (entry) => entry.target_type === "topic" && entry.target_id === saved.id,
      ) ?? [];
      if (taxonomySelection) {
        const separator = taxonomySelection.indexOf(":");
        const taxonomyType = taxonomySelection.slice(0, separator) as TaxonomyType;
        const taxonomyId = taxonomySelection.slice(separator + 1);
        const alreadyLinked = existingLinks.some(
          (entry) =>
            entry.target_type === "topic" &&
            entry.target_id === saved.id &&
            entry.taxonomy_type === taxonomyType &&
            entry.taxonomy_id === taxonomyId,
        );
        if (!alreadyLinked && taxonomyId) {
          try {
            await createBusinessLink(requestScope, {
              taxonomy_type: taxonomyType,
              taxonomy_id: taxonomyId,
              target_type: "topic",
              target_id: saved.id,
              relation_type: "governs",
              reason: "Bind this governed knowledge entry to its business context.",
            });
            await Promise.all(
              existingLinks.map((entry) =>
                deleteBusinessLink(
                  requestScope,
                  entry.id,
                  "Replaced by the taxonomy selected in the Knowledge editor.",
                ),
              ),
            );
            try {
              await businessContext?.refresh();
              setBindingNotice("Knowledge saved and linked to its governed business context.");
            } catch (refreshError) {
              setBindingNotice(
                `Knowledge and link were saved, but refreshing the context failed: ${errorMessage(refreshError)}`,
              );
            }
          } catch (linkError) {
            setEditor({
              mode: "edit",
              topic: saved,
              title: draft.title,
              body_md: draft.body_md,
              owner: draft.owner,
              saving: false,
              error: `Knowledge was saved, but its business-context link was not changed: ${errorMessage(linkError)}. Retry to apply the preserved selection.`,
            });
            return;
          }
        } else if (alreadyLinked) {
          const obsolete = existingLinks.filter(
            (entry) =>
              entry.taxonomy_type !== taxonomyType || entry.taxonomy_id !== taxonomyId,
          );
          if (obsolete.length > 0) {
            try {
              await Promise.all(
                obsolete.map((entry) =>
                  deleteBusinessLink(
                    requestScope,
                    entry.id,
                    "Removed after selecting the authoritative Knowledge taxonomy.",
                  ),
                ),
              );
              await businessContext?.refresh();
            } catch (linkError) {
              setEditor({
                mode: "edit",
                topic: saved,
                title: draft.title,
                body_md: draft.body_md,
                owner: draft.owner,
                saving: false,
                error: `Knowledge was saved, but obsolete context links remain: ${errorMessage(linkError)}. Retry with the preserved selection.`,
              });
              return;
            }
          }
        }
      } else {
        try {
          await Promise.all(
            existingLinks.map((entry) =>
              deleteBusinessLink(
                requestScope,
                entry.id,
                "Removed from the Knowledge editor.",
              ),
            ),
          );
          await businessContext?.refresh();
          setBindingNotice("Knowledge saved without a business-context link.");
        } catch (linkError) {
          setEditor({
            mode: "edit",
            topic: saved,
            title: draft.title,
            body_md: draft.body_md,
            owner: draft.owner,
            saving: false,
            error: `Knowledge was saved, but its previous business-context link could not be removed: ${errorMessage(linkError)}. Retry with the preserved selection.`,
          });
          return;
        }
      }
      closeEditor();
    } catch (err) {
      if (!mutationIsCurrent(generation, requestScope) || (controller.signal.aborted && !timedOut)) return;
      setEditor((prev) => prev.mode === "closed" ? prev : {
        ...prev,
        saving: false,
        error: timedOut ? "The knowledge save timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async function archiveTopic(topic: ContextTopic) {
    if (state.status !== "ok" || topic.capabilities?.can_write !== true) return;
    const requestScope = scopedProjectId;
    const generation = ++mutationGenerationRef.current;
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, CONTEXT_REQUEST_TIMEOUT_MS);
    const isPlatformRow = topic.project_id === null;
    setArchiveError((prev) => prev?.id === topic.id ? null : prev);
    try {
      const res = await apiFetch(`/api/context/topics/${encodeURIComponent(topic.id)}/archive?project_id=${encodeURIComponent(requestScope)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_version: topic.version_number }),
        signal: controller.signal,
      });
      if (!res.ok) {
        const failure = await readErrorDetails(res);
        if (!mutationIsCurrent(generation, requestScope)) return;
        if (isPlatformRow && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(topic.id));
        }
        if (res.status === 409 && failure.code === "version_conflict") {
          try {
            const latest = await fetchLatestTopic(topic.id, requestScope, controller.signal);
            if (!mutationIsCurrent(generation, requestScope)) return;
            latest.capabilities = topic.capabilities;
            setState((prev) => prev.status === "ok" ? {
              ...prev,
              topics: prev.topics.map((row) => row.id === latest.id ? latest : row),
            } : prev);
            setArchiveError({ id: topic.id, message: `${failure.message} Latest version v${latest.version_number} loaded; retry archive.` });
          } catch (reloadError) {
            if (mutationIsCurrent(generation, requestScope)) {
              setArchiveError({ id: topic.id, message: `${failure.message} Could not reload the latest version: ${errorMessage(reloadError)}` });
            }
          }
          return;
        }
        setArchiveError({ id: topic.id, message: failure.message });
        return;
      }
      const archived = parseTopic(await res.json(), requestScope) as ContextTopic;
      if (!mutationIsCurrent(generation, requestScope)) return;
      archived.capabilities = topic.capabilities;
      // With "Show archived" on, the row stays and changes state under the
      // person's eyes — removing it would hide the very entry they can now
      // bring back.
      setState((prev) => prev.status === "ok" ? {
        ...prev,
        topics: showArchived
          ? prev.topics.map((row) => row.id === archived.id ? archived : row)
          : prev.topics.filter((row) => row.id !== archived.id),
      } : prev);
      setArchiveError((prev) => prev?.id === topic.id ? null : prev);
    } catch (err) {
      if (!mutationIsCurrent(generation, requestScope) || (controller.signal.aborted && !timedOut)) return;
      setArchiveError({
        id: topic.id,
        message: timedOut ? "The knowledge archive request timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  /**
   * Bring an archived entry back — the mirror of `archiveTopic`.
   *
   * POST /api/context/topics/{id}/restore appends a version exactly as the
   * archive did, so the same optimistic-concurrency dance applies: on a
   * version_conflict the authoritative row is re-read and the person retries
   * against it.
   */
  async function restoreTopic(topic: ContextTopic) {
    if (state.status !== "ok" || topic.capabilities?.can_write !== true) return;
    const requestScope = scopedProjectId;
    const generation = ++mutationGenerationRef.current;
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, CONTEXT_REQUEST_TIMEOUT_MS);
    const isPlatformRow = topic.project_id === null;
    setArchiveError((prev) => prev?.id === topic.id ? null : prev);
    try {
      const res = await apiFetch(`/api/context/topics/${encodeURIComponent(topic.id)}/restore?project_id=${encodeURIComponent(requestScope)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_version: topic.version_number }),
        signal: controller.signal,
      });
      if (!res.ok) {
        const failure = await readErrorDetails(res);
        if (!mutationIsCurrent(generation, requestScope)) return;
        if (isPlatformRow && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(topic.id));
        }
        if (res.status === 409 && failure.code === "version_conflict") {
          try {
            const latest = await fetchLatestTopic(topic.id, requestScope, controller.signal);
            if (!mutationIsCurrent(generation, requestScope)) return;
            latest.capabilities = topic.capabilities;
            setState((prev) => prev.status === "ok" ? {
              ...prev,
              topics: prev.topics.map((row) => row.id === latest.id ? latest : row),
            } : prev);
            setArchiveError({ id: topic.id, message: `${failure.message} Latest version v${latest.version_number} loaded; retry restore.` });
          } catch (reloadError) {
            if (mutationIsCurrent(generation, requestScope)) {
              setArchiveError({ id: topic.id, message: `${failure.message} Could not reload the latest version: ${errorMessage(reloadError)}` });
            }
          }
          return;
        }
        setArchiveError({ id: topic.id, message: failure.message });
        return;
      }
      const restored = parseTopic(await res.json(), requestScope) as ContextTopic;
      if (!mutationIsCurrent(generation, requestScope)) return;
      restored.capabilities = topic.capabilities;
      setState((prev) => prev.status === "ok" ? {
        ...prev,
        topics: prev.topics.map((row) => row.id === restored.id ? restored : row),
      } : prev);
      setArchiveError((prev) => prev?.id === topic.id ? null : prev);
    } catch (err) {
      if (!mutationIsCurrent(generation, requestScope) || (controller.signal.aborted && !timedOut)) return;
      setArchiveError({
        id: topic.id,
        message: timedOut ? "The knowledge restore request timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }
  const topics = state.status === "ok"
    ? state.topics.filter((t) => showArchived || t.status === "active")
    : [];
  const canWrite = state.status === "ok" && state.capabilities?.can_write === true;
  const capabilityMessage = state.status === "ok" && !state.capabilities
    ? "Write permissions could not be verified. Editing is disabled."
    : state.status === "ok" && !canWrite
      ? "You have read-only access to this knowledge base."
      : null;
  const [generatingSchema, setGeneratingSchema] = useState(false);
  const [schemaNotice, setSchemaNotice] = useState<{ tone: "info" | "error"; message: string } | null>(null);

  /**
   * What a failed generation asks the person to DO.
   *
   * The route is platform-admin-only (schema_context_api._check_admin_authorized),
   * so its refusals are about who is asking and which project — never about the
   * generator's internals, which nobody here can act on.
   */
  function schemaFailureMessage(status: number): string {
    if (status === 401) return "Sign in again, then run the generation.";
    if (status === 403) return "Ask a platform administrator to generate the schema context for this project.";
    if (status === 422) return "Select a project before generating the schema context.";
    return "Nothing was generated. Run it again; if it keeps refusing, ask a platform administrator to check the warehouse connection.";
  }

  const handleGenerateSchemaContext = async () => {
    if (!canWrite || !scopedProjectId) return;
    setGeneratingSchema(true);
    setSchemaNotice(null);
    try {
      // The handler reads project_id from the JSON BODY (schema_context_api.py):
      // passing it only in the query string was a guaranteed 422.
      const res = await apiFetch("/api/admin/context/generate-schema-context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: scopedProjectId }),
      });
      if (res.ok) {
        const result = (await res.json()) as {
          processed?: number;
          updated?: number;
          skipped?: number;
        };
        const processed = result.processed ?? 0;
        const updated = result.updated ?? 0;
        const skipped = result.skipped ?? 0;
        setSchemaNotice({
          tone: "info",
          message: `Read ${processed} warehouse tables: ${updated} entries written, ${skipped} already up to date.`,
        });
        await load();
      } else {
        setSchemaNotice({ tone: "error", message: schemaFailureMessage(res.status) });
      }
    } catch {
      setSchemaNotice({
        tone: "error",
        message: "The generation never reached the server. Check your connection and run it again.",
      });
    } finally {
      setGeneratingSchema(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Shared knowledge base"
        description="Governed business definitions and guidance read by AI agents during analysis."
        actions={
          <>
            <Button
              variant="secondary"
              type="button"
              aria-pressed={showArchived}
              data-testid="knowledge-show-archived"
              onClick={() => setShowArchived((shown) => !shown)}
            >
              {showArchived ? "Hide archived" : "Show archived"}
            </Button>
            <Button
              variant="secondary"
              type="button"
              onClick={() => void handleGenerateSchemaContext()}
              disabled={!canWrite || generatingSchema}
              title={capabilityMessage ?? undefined}
            >
              {generatingSchema ? "Generating schema context…" : "⚡ Generate schema context"}
            </Button>
            <Button type="button" onClick={openCreate} disabled={!canWrite} title={capabilityMessage ?? undefined}>
              + Add knowledge entry
            </Button>
          </>
        }
      />

      {bindingNotice && (
        <div className={NOTICE_CLASS} role="status" data-testid="knowledge-binding-notice">
          {bindingNotice}
        </div>
      )}

      {/* One alert language for the page: the generation speaks in the same
          block as every other notice, not in a lone caption under the title. */}
      {schemaNotice && (
        <div
          className={NOTICE_CLASS}
          role={schemaNotice.tone === "error" ? "alert" : "status"}
          data-testid="knowledge-schema-notice"
        >
          {schemaNotice.message}
        </div>
      )}

      {state.status === "loading" && (
        <p className={STATUS_LINE_CLASS} role="status">
          Loading the knowledge base…
        </p>
      )}

      {state.status !== "loading" && state.status !== "ok" && (
        <div className={NOTICE_CLASS} role="alert" data-testid="knowledge-error">
          <Status tone="error">
            {state.status === "denied" ? "Knowledge unavailable" : state.status === "capability" ? "Versioned knowledge is not enabled" : "Could not load the knowledge base"}
          </Status>
          <p>{state.message}</p>
          <p>No fallback knowledge has been substituted.</p>
          <Button variant="secondary" type="button" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      )}

      {capabilityMessage && (
        <div className={NOTICE_CLASS} role="status" data-testid="knowledge-capability">
          {capabilityMessage}
        </div>
      )}

      {/* THE ONE EMPTY STATE (76-4). It was a hand-rolled `<h2>` and a
          paragraph inside a Panel -- a fourth shape for one statement. The
          gesture it names is `Add knowledge entry`, which the page header
          already carries; the sentence says where it is rather than mounting a
          second copy of one control. */}
      {state.status === "ok" && topics.length === 0 && (
        <Panel flush data-testid="knowledge-empty">
          <EmptyState
            icon={<BookOpenIcon />}
            title="No knowledge entries yet"
            description="Governed definitions added here are read by AI agents during analysis. The first entry starts this project's shared knowledge base, with Add knowledge entry, above."
          />
        </Panel>
      )}

      {state.status === "ok" && topics.length > 0 && (
        <section className="grid grid-cols-3 gap-[18px] max-[1080px]:grid-cols-2 max-[720px]:grid-cols-1">
          {topics.map((topic) => {
            const isPlatform = topic.project_id === null;
            const isArchived = topic.status === "archived";
            const isReadOnly = topic.capabilities?.can_write !== true || (isPlatform && readOnlyIds.has(topic.id));
            const businessLinks = businessContext?.links.filter(
              (entry) => entry.target_type === "topic" && entry.target_id === topic.id,
            ) ?? [];
            return (
              /* An archived entry is shown, never hidden — but it must not
                 read as part of what agents consult today. Dashed border +
                 recessed surface, the console's "present, not in force". */
              <article
                key={topic.id}
                className={`overflow-hidden rounded-large border border-divider-base bg-surface-light shadow-card-light flex min-h-[200px] flex-col items-start gap-3 p-[22px]${isArchived ? " border-dashed bg-[color-mix(in_srgb,var(--color-text-secondary)_6%,var(--color-surface-light))]" : ""}`}
                data-testid={isArchived ? `knowledge-card-archived-${topic.id}` : undefined}
              >
                <div className="flex min-h-5 items-center gap-2">
                  {isArchived && <span className={READONLY_PILL_CLASS}>Archived</span>}
                  {isPlatform && <span className={PLATFORM_PILL_CLASS}>Platform</span>}
                  {isReadOnly && <span className={READONLY_PILL_CLASS}>Read-only</span>}
                  {/* THE LINK CARRIES ITS OWN WORD NOW. This pill resolved the
                      taxonomy's name against whichever catalogue the page had
                      loaded and printed `bdom_<ULID>` when that catalogue did not
                      carry the node — a second authority on the vocabulary, and
                      an address on a pill. `business_taxonomy.list_links` serves
                      `taxonomy_name`, and `null` means the node is no longer
                      readable, which the pill says. */}
                  {businessLinks.map((link) => (
                    <span key={link.id} className={TOPIC_PILL_CLASS}>
                      {link.taxonomy_name ?? "Business key no longer readable"}
                    </span>
                  ))}
                </div>
                <h2 className={`${CARD_TITLE_CLASS} ${isArchived ? "text-text-secondary" : "text-text"}`}>{topic.title}</h2>
                <div className="mt-auto grid gap-1 text-caption leading-[1.5] text-text-secondary [&_b]:font-semibold [&_time]:font-numeric [&_time]:[font-variant-numeric:tabular-nums]">
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
                {isArchived && (
                  <p className={STATUS_LINE_CLASS} data-testid={`knowledge-archived-note-${topic.id}`}>
                    Archived — agents no longer read this entry. Restore it to put it back.
                  </p>
                )}
                <div className="mt-[14px] flex gap-2.5">
                  <Button variant="secondary" type="button" onClick={() => onOpenTopic?.(topic.id)}>Open details</Button>
                  {/* An archived entry refuses a PATCH server-side, so the
                      console does not offer one: the single gesture it accepts
                      is the way back. */}
                  {isArchived ? (
                    <Button
                      variant="secondary"
                      type="button"
                      onClick={() => setPendingRestore(topic)}
                      disabled={isReadOnly}
                    >
                      Restore
                    </Button>
                  ) : (
                    <>
                      <Button
                        variant="secondary"
                        type="button"
                        onClick={() => openEdit(topic)}
                        disabled={isReadOnly}
                      >
                        Edit entry
                      </Button>
                      <Button
                        variant="secondary"
                        type="button"
                        onClick={() => setPendingArchive(topic)}
                        disabled={isReadOnly}
                      >
                        Archive
                      </Button>
                    </>
                  )}
                </div>
                {archiveError && archiveError.id === topic.id && (
                  <div
                    className={INLINE_ERROR_CLASS}
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
          className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-[color-mix(in_srgb,var(--color-text)_45%,transparent)] px-5 py-12"
          role="dialog"
          aria-modal="true"
          aria-labelledby="knowledge-editor-heading"
        >
          <Panel flush className="flex w-full max-w-[760px] flex-col gap-4 p-7">
            <h2 id="knowledge-editor-heading" className="m-0 font-display text-[18px] font-semibold leading-[1.3] text-text">
              {editor.mode === "create" ? "New knowledge entry" : "Edit knowledge entry"}
            </h2>

            {editor.error && (
              <div className={INLINE_ERROR_CLASS} role="alert" data-testid="knowledge-editor-error">
                {editor.error}
              </div>
            )}

            <label className={FIELD_CLASS}>
              <span>Title</span>
              <Input
                ref={titleInputRef}
                type="text"
                value={editor.title}
                disabled={editor.saving}
                onChange={(e) => setEditor({ ...editor, title: e.target.value })}
                placeholder="e.g. ROAS calculation & deduplication policy"
              />
            </label>

            <label className={FIELD_CLASS}>
              <span>Owner (email)</span>
              <Input
                type="text"
                data-testid="knowledge-editor-owner"
                value={editor.owner}
                disabled={editor.saving}
                onChange={(e) => setEditor({ ...editor, owner: e.target.value })}
                placeholder="Unset — falls back to created_by, then &ldquo;auto&rdquo;"
              />
            </label>

            {businessContext ? (
              <label className={FIELD_CLASS}>
                <span>Governed business context</span>
                <NativeSelect
                  data-testid="knowledge-editor-business-context"
                  value={taxonomySelection}
                  disabled={editor.saving}
                  onChange={(event) => setTaxonomySelection(event.target.value)}
                >
                  <option value="">No business-context link</option>
                  <optgroup label="Domains">
                    {businessContext.domains.map((domain) => (
                      <option key={domain.id} value={`business_domain:${domain.id}`}>
                        {domain.name}
                      </option>
                    ))}
                  </optgroup>
                  <optgroup label="Classifications">
                    {businessContext.classifications.map((classification) => (
                      <option
                        key={classification.id}
                        value={`business_classification:${classification.id}`}
                      >
                        {classification.name}
                      </option>
                    ))}
                  </optgroup>
                </NativeSelect>
                <small>
                  The link is created only after the knowledge entry is acknowledged by the server.
                </small>
              </label>
            ) : null}

            <div className="grid grid-cols-2 gap-4 max-[720px]:grid-cols-1">
              <label className={FIELD_CLASS}>
                <span>Body (Markdown)</span>
                <Textarea
                  className="resize-y"
                  rows={12}
                  value={editor.body_md}
                  disabled={editor.saving}
                  onChange={(e) => setEditor({ ...editor, body_md: e.target.value })}
                  placeholder="Write the entry in Markdown…"
                />
              </label>
              <div className={PREVIEW_CLASS} data-testid="knowledge-preview">
                <span className="text-[11px] font-bold uppercase tracking-[0.04em] text-text-secondary">Preview</span>
                <RawMarkdown
                  text={editor.body_md}
                  placeholder="The preview appears here."
                />
              </div>
            </div>

            <div className="flex justify-end gap-2.5">
              <Button variant="secondary" type="button" onClick={closeEditor} disabled={editor.saving}>
                Cancel
              </Button>
              <Button
                type="button"
                onClick={() => void submitEditor()}
                disabled={editor.saving}
              >
                {editor.saving ? "Saving…" : "Save"}
              </Button>
            </div>
          </Panel>
        </div>
      )}

      {/* Archiving still asks — agents stop reading the entry the moment it
          leaves — but it no longer claims to be final, because it is not: the
          confirmation names the entry AND names the way back. A warning that
          overstates what a gesture costs is a warning people learn to skip. */}
      <ConfirmDialog
        open={pendingArchive !== null}
        onOpenChange={(open) => { if (!open) setPendingArchive(null); }}
        title={pendingArchive ? `Archive “${pendingArchive.title}”?` : "Archive this entry?"}
        description={
          pendingArchive
            ? `“${pendingArchive.title}” leaves the knowledge base and agents stop reading it during analysis. Nothing is deleted: turn on “Show archived” to find it again and restore it.`
            : ""
        }
        confirmLabel="Archive entry"
        destructive
        onConfirm={() => {
          const topic = pendingArchive;
          setPendingArchive(null);
          if (topic) void archiveTopic(topic);
        }}
        data-testid="knowledge-archive-confirm"
        cancelTestId="knowledge-archive-confirm-cancel"
        confirmTestId="knowledge-archive-confirm-accept"
      />

      {/* Restoring is not destructive, but it changes what every agent reads
          on the next analysis — so it is confirmed by name too. */}
      <ConfirmDialog
        open={pendingRestore !== null}
        onOpenChange={(open) => { if (!open) setPendingRestore(null); }}
        title={pendingRestore ? `Restore “${pendingRestore.title}”?` : "Restore this entry?"}
        description={
          pendingRestore
            ? `“${pendingRestore.title}” returns to the knowledge base and agents read it again during analysis. Its history is kept: the restore is recorded as the next version.`
            : ""
        }
        confirmLabel="Restore entry"
        onConfirm={() => {
          const topic = pendingRestore;
          setPendingRestore(null);
          if (topic) void restoreTopic(topic);
        }}
        data-testid="knowledge-restore-confirm"
        cancelTestId="knowledge-restore-confirm-cancel"
        confirmTestId="knowledge-restore-confirm-accept"
      />
    </>
  );
}
