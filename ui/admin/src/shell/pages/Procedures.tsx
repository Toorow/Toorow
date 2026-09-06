/**
 * Procedures — Context surface for the governed AGENT PROCEDURE corpus.
 *
 * Story 44.1: rewired onto the governed, versioned context store
 * (`app.procedures` / `/api/context/procedures`, server/core/context_api.py).
 * A procedure here is documented guidance an AI agent follows during analysis
 * — the frontmatter (`name` + `description`) plus a Markdown body. This is
 * NOT the per-metric calculation/reconciliation list (`app.metric_procedures`
 * / GET /api/procedures): that read-only list moved to Governance, see
 * the Governance Controls & Quality workbenches (Story 49.4).
 *
 * Mounted in the Context workspace as a sibling of Knowledge and Events. The
 * application shell already renders the frame, sidebar, topbar, and <main>;
 * this component renders ONLY the page content inside <main>.
 *
 * Styling: primitives from `ui/index.ts` (PageHeader, Panel, Status, Badge,
 * Button) plus Tailwind utilities, per docs/ui-css-strategy.md (wave 2). The
 * legacy procedures.css and knowledge.css sheets are no longer used here.
 *
 * ── Data ─────────────────────────────────────────────────────────────────
 * `GET /api/context/procedures?project_id=<id>` returns platform + project
 * procedures, each `{id, project_id, name, description, frontmatter_yaml,
 * body_md, status, created_by, created_at, updated_at, version_number}`.
 * The editor collects Name + Description (built into the required YAML
 * frontmatter client-side) and a Markdown body + preview pane (no WYSIWYG
 * dependency). On save failure the exact server message is shown and the
 * draft is preserved:
 *   - 422 `invalid_frontmatter` — the YAML/required-keys validation failed.
 *   - 409 `duplicate_name` — a procedure with that name already exists in
 *     this scope.
 * A 403/404 on a platform-scope row's write (deny-by-default platform gate)
 * marks that row read-only in place rather than a fatal page error.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import SkillEditorDrawer, { type SkillEditorSaveValue } from "../../connaissances/SkillEditorDrawer";
// ONE writer of the frontmatter, shared with the object workbench — which now
// saves a Skill too, so that a remark can be acted on where it is read. Two
// copies of this merge would drift the first time one of them learns a key.
import { updateSkillFrontmatter } from "../../connaissances/skillFrontmatter";
import NodeRemarks, { type ReviewRequestRow } from "../../connaissances/NodeRemarks";
import type { ContextHubContentContext } from "../../connaissances/ContextHubLayout";
import { createBusinessLink } from "../../connaissances/businessTaxonomyApi";
import { ScrollTextIcon } from "lucide-react";
import { Badge, Button, ConfirmDialog, EmptyState, PageHeader, Panel, Status, formatDate, stateLabel, stateTone } from "../../ui";
// application.css is GONE from this page: NodeRemarks — the child that still
// rendered `secondary-button` and `number` — moved to the `Button` primitive
// and the theme's numeric tokens on 2026-09-01, and this page never used a
// class from that sheet itself.
import { apiFetch } from "../../lib/apiFetch";
import {
  CONTEXT_REQUEST_TIMEOUT_MS,
  errorMessage,
  parseProcedure,
  parseProceduresEnvelope,
  readSurfaceFailure,
  type ContextCapabilities,
  type MdmReferences,
  type SurfaceFailureKind,
} from "../../lib/contextRuntime";

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
  capabilities: ContextCapabilities | null;
  /** AI-157 : porte par la route de DETAIL seulement ; `null` sur une ligne de liste. */
  mdm_references: MdmReferences | null;
}

type LoadState =
  | { status: "loading" }
  | { status: SurfaceFailureKind; message: string }
  | { status: "ok"; procedures: ContextProcedure[]; capabilities: ContextCapabilities | null };

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
      /**
       * AI-157 : ce que la Skill designe du modele de donnees, resolu par le
       * serveur. La LISTE ne le porte pas (une lecture de `target_fields` par
       * ligne pour un panneau qu'on n'ouvre pas), donc il arrive apres coup, par
       * la route de detail. `null` = pas encore lu, et le panneau se tait.
       */
      mdmReferences: MdmReferences | null;
    };

function formatUpdated(iso: string): string {
  return formatDate(iso);
}

interface ErrorDetails {
  code: string;
  message: string;
}

async function readErrorDetails(res: Response): Promise<ErrorDetails> {
  try {
    const body = (await res.json()) as { message?: string; code?: string };
    return { code: body.code ?? "", message: body.message ?? `HTTP ${res.status}` };
  } catch {
    return { code: "", message: `HTTP ${res.status}` };
  }
}

async function fetchLatestProcedure(procedureId: string, projectId: string, signal: AbortSignal): Promise<ContextProcedure> {
  const response = await apiFetch(
    `/api/context/procedures/${encodeURIComponent(procedureId)}?project_id=${encodeURIComponent(projectId)}`,
    { signal, cache: "no-store" },
  );
  if (!response.ok) throw new Error((await readErrorDetails(response)).message);
  return parseProcedure(await response.json(), projectId) as ContextProcedure;
}

/** Un candidat que la recherche a atteint puis ecarte, assez souvent pour compter. */
interface RecurrentFate {
  candidate_id: string;
  candidate_kind: string;
  reason: string;
  times: number;
  last_query: string | null;
}

export default function Procedures({
  projectId,
  onOpenProcedure,
  businessContext,
}: {
  projectId: string;
  onOpenProcedure?: (id: string) => void;
  /** Story 45.2: the governed taxonomy the drawer assigns a skill to. Context Hub
   *  passes it; a caller that mounts Procedures on its own gets a drawer with no
   *  business assignment rather than a fabricated one. */
  businessContext?: ContextHubContentContext;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [editor, setEditor] = useState<EditorState>({ mode: "closed" });
  const [readOnlyIds, setReadOnlyIds] = useState<Set<string>>(new Set());
  const [recurrentFates, setRecurrentFates] = useState<RecurrentFate[]>([]);
  const [fatesMinimum, setFatesMinimum] = useState(3);
  const [archiveError, setArchiveError] = useState<{ id: string; message: string } | null>(null);
  // Archiving appends a version and the context API HAS a restore route
  // (`_restore_procedure`, context_api.py:2298) — so the row waits here until
  // the confirmation names it AND names the way back.
  const [pendingArchive, setPendingArchive] = useState<ContextProcedure | null>(null);
  const [pendingRestore, setPendingRestore] = useState<ContextProcedure | null>(null);
  // Archived Skills are shown on demand — never by default, because the
  // registry is what agents follow TODAY (context-hub.md, 2026-08-18).
  const [showArchived, setShowArchived] = useState(false);
  // Story 45.2: a saved skill whose business assignment failed. Distinct from
  // archiveError because the skill exists and the operator must not re-save it.
  const [assignmentError, setAssignmentError] = useState<{ id: string; message: string } | null>(null);
  // ── AI-158 : VOIR le retour, et AGIR dessus.
  //
  // Une file qu'on ne peut que fermer est une file qu'on ferme sans rien
  // corriger. La remarque est epinglee a une VERSION : la reponse juste n'est
  // pas d'effacer la note, c'est de sortir une nouvelle version de la Skill.
  // C'est ICI que la boucle se ferme, parce que c'est ici que vit l'editeur --
  // la monter sur l'etabli aurait duplique un chemin de sauvegarde de 90 lignes
  // avec sa reprise de conflit de version (§4 : traiter la classe, pas
  // l'instance -- et deux chemins d'ecriture sont deux classes de bugs).
  //
  // UNE SEULE LECTURE POUR TOUTE LA PAGE. `node_id` est optionnel sur la route
  // (`context_review.list_open`) : lire la file du projet une fois et la
  // distribuer par Skill vaut mieux que N requetes pour N cartes.
  const [remarks, setRemarks] = useState<{
    byNode: Map<string, ReviewRequestRow[]>;
    canResolve: boolean;
  }>({ byNode: new Map(), canResolve: false });
  const [remarksReload, setRemarksReload] = useState(0);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const loadGenerationRef = useRef(0);
  const loadAbortRef = useRef<AbortController | null>(null);
  const mutationGenerationRef = useRef(0);
  const mutationAbortRef = useRef<AbortController | null>(null);
  //: AI-157 : la lecture de detail qui remplit `mdmReferences`. Un controleur
  //: dedie, sinon l'ouverture d'une seconde Skill laisserait la premiere reponse
  //: atterrir dans le drawer de la seconde.
  const detailAbortRef = useRef<AbortController | null>(null);
  const scopeRef = useRef("");
  const focusReturnRef = useRef<HTMLElement | null>(null);
  const scopedProjectId = projectId.trim();
  scopeRef.current = scopedProjectId;

  const load = useCallback(async () => {
    if (!scopedProjectId) {
      setState({ status: "capability", message: "Select a project before opening Skills." });
      return;
    }
    const generation = ++loadGenerationRef.current;
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => { timedOut = true; controller.abort(); }, CONTEXT_REQUEST_TIMEOUT_MS);
    setState({ status: "loading" });
    try {
      const res = await apiFetch(
        `/api/context/procedures?project_id=${encodeURIComponent(scopedProjectId)}${showArchived ? "&status=all" : ""}`,
        { signal: controller.signal, cache: "no-store" },
      );
      if (!res.ok) {
        const failure = await readSurfaceFailure(res);
        if (generation === loadGenerationRef.current) setState({ status: failure.kind, message: failure.message });
        return;
      }
      const parsed = parseProceduresEnvelope(await res.json(), scopedProjectId);
      if (!controller.signal.aborted && generation === loadGenerationRef.current) {
        setState({ status: "ok", procedures: parsed.procedures, capabilities: parsed.capabilities });
      }
    } catch (err) {
      if (generation !== loadGenerationRef.current) return;
      if (controller.signal.aborted && !timedOut) return;
      setState({ status: "error", message: timedOut ? "Reading the Skills of this project timed out." : errorMessage(err) });
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

  function rememberEditorTrigger() {
    focusReturnRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  function openCreate() {
    rememberEditorTrigger();
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

  useEffect(() => {
    if (!scopedProjectId) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await apiFetch(
          `/api/context/recurrent-fates?project_id=${encodeURIComponent(scopedProjectId)}`,
        );
        if (cancelled || !res.ok) return;
        const body = (await res.json()) as { fates?: RecurrentFate[]; minimum?: number };
        if (cancelled) return;
        setRecurrentFates(body.fates ?? []);
        if (typeof body.minimum === "number") setFatesMinimum(body.minimum);
      } catch {
        // Le repli est un COMPLEMENT : son indisponibilite ne retire pas la page.
        if (!cancelled) setRecurrentFates([]);
      }
    })();
    return () => { cancelled = true; };
  }, [scopedProjectId]);

  useEffect(() => {
    if (!scopedProjectId) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await apiFetch(
          `/api/context/review-requests?project_id=${encodeURIComponent(scopedProjectId)}`,
        );
        if (cancelled || !res.ok) {
          // Une file indisponible ne doit pas emporter la page : les Skills
          // restent lisibles et editables, on perd le retour, pas l'outil.
          if (!cancelled) setRemarks({ byNode: new Map(), canResolve: false });
          return;
        }
        const body = (await res.json()) as {
          requests?: ReviewRequestRow[];
          can_resolve?: boolean;
        };
        if (cancelled) return;
        const byNode = new Map<string, ReviewRequestRow[]>();
        for (const row of body.requests ?? []) {
          const bucket = byNode.get(row.node_id);
          if (bucket) bucket.push(row);
          else byNode.set(row.node_id, [row]);
        }
        setRemarks({ byNode, canResolve: body.can_resolve === true });
      } catch {
        if (!cancelled) setRemarks({ byNode: new Map(), canResolve: false });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [scopedProjectId, remarksReload]);

  function openEdit(procedure: ContextProcedure) {
    rememberEditorTrigger();
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
      mdmReferences: procedure.mdm_references,
    });
    void loadMdmReferences(procedure);
  }

  /**
   * AI-157 : ce que la Skill designe du modele de donnees, lu APRES l'ouverture.
   *
   * La resolution vivait uniquement dans l'outil MCP `get_procedure` : un agent
   * savait de quoi parlait une Skill, la personne qui l'ecrit ne le savait pas.
   * La route de detail la porte maintenant -- mais la LISTE, non, et c'est
   * voulu : la resoudre par ligne couterait une lecture de `app.target_fields`
   * par Skill pour un panneau qu'on n'ouvre pas.
   *
   * Le drawer s'ouvre donc TOUT DE SUITE avec ce que la liste sait, et le
   * panneau apparait quand la lecture rend. Un echec ne dit rien : c'est un
   * complement de lecture, pas la Skill -- et faire echouer l'edition d'une
   * Skill parce qu'un catalogue n'a pas repondu serait un mauvais echange.
   */
  async function loadMdmReferences(procedure: ContextProcedure) {
    const requestScope = scopedProjectId;
    detailAbortRef.current?.abort();
    const controller = new AbortController();
    detailAbortRef.current = controller;
    try {
      const latest = await fetchLatestProcedure(procedure.id, requestScope, controller.signal);
      if (controller.signal.aborted || requestScope !== scopeRef.current) return;
      setEditor((prev) =>
        prev.mode === "edit" && prev.procedure.id === procedure.id
          ? { ...prev, mdmReferences: latest.mdm_references }
          : prev,
      );
    } catch {
      // Silencieux par construction -- voir la docstring.
    }
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

  /**
   * Story 45.2: the drawer owns the form, this owns persistence.
   *
   * `value` carries the normalized ordered body, the validated `tool_bindings`,
   * the `mdm_tags` and an optional business assignment. Concurrency, permissions,
   * the 409 reload and draft retention are unchanged — the raw modal was replaced,
   * not the save path.
   */
  async function submitEditor(value: SkillEditorSaveValue) {
    if (editor.mode === "closed" || state.status !== "ok" || state.capabilities?.can_write !== true) return;
    const draft = {
      ...editor,
      name: value.name,
      description: value.description,
      owner: value.owner,
      body_md: value.bodyMd,
    };
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

    const isPlatformRow = draft.mode === "edit" && draft.procedure.project_id === null;
    const editedProcedureId = draft.mode === "edit" ? draft.procedure.id : null;
    const frontmatter_yaml = updateSkillFrontmatter(draft.frontmatterYaml, {
      name: value.name,
      description: value.description,
      toolBindings: value.toolBindings,
      mdmTags: value.mdmTags,
      standard: value.standard,
    });
    const owner = draft.owner.trim() || null;

    try {
      const res = draft.mode === "create"
        ? await apiFetch(`/api/context/procedures?project_id=${encodeURIComponent(requestScope)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: requestScope, frontmatter_yaml, body_md: draft.body_md, owner }),
            signal: controller.signal,
          })
        : await apiFetch(`/api/context/procedures/${encodeURIComponent(draft.procedure.id)}?project_id=${encodeURIComponent(requestScope)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ expected_version: draft.procedure.version_number, frontmatter_yaml, body_md: draft.body_md, owner }),
            signal: controller.signal,
          });

      if (!res.ok) {
        const failure = await readErrorDetails(res);
        if (!mutationIsCurrent(generation, requestScope)) return;
        if (isPlatformRow && editedProcedureId && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(editedProcedureId));
        }
        if (res.status === 409 && failure.code === "version_conflict" && draft.mode === "edit") {
          try {
            const latest = await fetchLatestProcedure(draft.procedure.id, requestScope, controller.signal);
            if (!mutationIsCurrent(generation, requestScope)) return;
            latest.capabilities = draft.procedure.capabilities;
            setState((prev) => prev.status === "ok" ? {
              ...prev,
              procedures: prev.procedures.map((procedure) => procedure.id === latest.id ? latest : procedure),
            } : prev);
            setEditor((prev) => prev.mode === "edit" && prev.procedure.id === latest.id ? {
              ...prev,
              procedure: latest,
              frontmatterYaml: latest.frontmatter_yaml,
              // La relecture porte le catalogue du MEME instant que la version
              // rechargee : le garder perime dirait « ce champ existe » alors
              // que la version d'a cote ne le cite plus.
              mdmReferences: latest.mdm_references,
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

      const saved = parseProcedure(await res.json(), requestScope) as ContextProcedure;
      if (!mutationIsCurrent(generation, requestScope)) return;
      // AI-158 : la Skill vient de changer de version. Les remarques epinglees
      // a l'ancienne doivent maintenant se lire « older version » — c'est TOUT
      // l'interet de l'epinglage. On relit plutot que de recalculer de tete.
      setRemarksReload((n) => n + 1);
      saved.capabilities = draft.mode === "edit" ? draft.procedure.capabilities : state.capabilities;
      setState((prev) => {
        if (prev.status !== "ok") return prev;
        const exists = prev.procedures.some((procedure) => procedure.id === saved.id);
        return {
          ...prev,
          procedures: exists
            ? prev.procedures.map((procedure) => procedure.id === saved.id ? saved : procedure)
            : [saved, ...prev.procedures],
        };
      });

      // The business assignment uses the existing typed association store, after
      // the procedure exists. Story 45.2's contract: the save stays successful if
      // the association fails, with a precise follow-up error rather than a
      // rollback of work the operator already did.
      if (value.businessLink) {
        try {
          await createBusinessLink(requestScope, {
            taxonomy_type: value.businessLink.taxonomy_type,
            taxonomy_id: value.businessLink.taxonomy_id,
            target_type: "procedure",
            target_id: saved.id,
            relation_type: value.businessLink.relation_type,
            reason: `Assigned while saving skill ${saved.name}`,
          });
          await businessContext?.refresh();
        } catch (linkError) {
          if (!mutationIsCurrent(generation, requestScope)) return;
          // The skill IS saved; re-opening the drawer would invite a second POST.
          // The follow-up error belongs to the row, named precisely.
          setAssignmentError({
            id: saved.id,
            message: `The skill was saved. Its business assignment failed: ${errorMessage(linkError)}`,
          });
          closeEditor();
          return;
        }
      }
      closeEditor();
    } catch (err) {
      if (!mutationIsCurrent(generation, requestScope) || (controller.signal.aborted && !timedOut)) return;
      setEditor((prev) => prev.mode === "closed" ? prev : {
        ...prev,
        saving: false,
        error: timedOut ? "Saving this Skill timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async function archiveProcedure(procedure: ContextProcedure) {
    if (state.status !== "ok" || procedure.capabilities?.can_write !== true) return;
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
    const isPlatformRow = procedure.project_id === null;
    setArchiveError((prev) => prev?.id === procedure.id ? null : prev);
    try {
      const res = await apiFetch(
        `/api/context/procedures/${encodeURIComponent(procedure.id)}/archive?project_id=${encodeURIComponent(requestScope)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_version: procedure.version_number }),
          signal: controller.signal,
        },
      );
      if (!res.ok) {
        const failure = await readErrorDetails(res);
        if (!mutationIsCurrent(generation, requestScope)) return;
        if (isPlatformRow && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(procedure.id));
        }
        if (res.status === 409 && failure.code === "version_conflict") {
          try {
            const latest = await fetchLatestProcedure(procedure.id, requestScope, controller.signal);
            if (!mutationIsCurrent(generation, requestScope)) return;
            latest.capabilities = procedure.capabilities;
            setState((prev) => prev.status === "ok" ? {
              ...prev,
              procedures: prev.procedures.map((row) => row.id === latest.id ? latest : row),
            } : prev);
            setArchiveError({ id: procedure.id, message: `${failure.message} Latest version v${latest.version_number} loaded; retry archive.` });
          } catch (reloadError) {
            if (mutationIsCurrent(generation, requestScope)) {
              setArchiveError({ id: procedure.id, message: `${failure.message} Could not reload the latest version: ${errorMessage(reloadError)}` });
            }
          }
          return;
        }
        setArchiveError({ id: procedure.id, message: failure.message });
        return;
      }
      const archived = parseProcedure(await res.json(), requestScope) as ContextProcedure;
      if (!mutationIsCurrent(generation, requestScope)) return;
      archived.capabilities = procedure.capabilities;
      // With "Show archived" on, the row stays and changes state under the
      // person's eyes — removing it would hide the very Skill they can now
      // bring back.
      setState((prev) => prev.status === "ok" ? {
        ...prev,
        procedures: showArchived
          ? prev.procedures.map((row) => row.id === archived.id ? archived : row)
          : prev.procedures.filter((row) => row.id !== archived.id),
      } : prev);
      setArchiveError((prev) => prev?.id === procedure.id ? null : prev);
    } catch (err) {
      if (!mutationIsCurrent(generation, requestScope) || (controller.signal.aborted && !timedOut)) return;
      setArchiveError({
        id: procedure.id,
        message: timedOut ? "Archiving this Skill timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  /**
   * Bring an archived Skill back — the mirror of `archiveProcedure`.
   *
   * POST /api/context/procedures/{id}/restore appends a version exactly as the
   * archive did, so the same optimistic-concurrency dance applies: on a
   * version_conflict the authoritative row is re-read and the person retries
   * against it.
   */
  async function restoreProcedure(procedure: ContextProcedure) {
    if (state.status !== "ok" || procedure.capabilities?.can_write !== true) return;
    const requestScope = scopedProjectId;
    const generation = ++mutationGenerationRef.current;
    mutationAbortRef.current?.abort();
    const controller = new AbortController();
    mutationAbortRef.current = controller;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => { timedOut = true; controller.abort(); }, CONTEXT_REQUEST_TIMEOUT_MS);
    const isPlatformRow = procedure.project_id === null;
    setArchiveError((prev) => prev?.id === procedure.id ? null : prev);
    try {
      const res = await apiFetch(
        `/api/context/procedures/${encodeURIComponent(procedure.id)}/restore?project_id=${encodeURIComponent(requestScope)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_version: procedure.version_number }),
          signal: controller.signal,
        },
      );
      if (!res.ok) {
        const failure = await readErrorDetails(res);
        if (!mutationIsCurrent(generation, requestScope)) return;
        if (isPlatformRow && (res.status === 403 || res.status === 404)) {
          setReadOnlyIds((prev) => new Set(prev).add(procedure.id));
        }
        if (res.status === 409 && failure.code === "version_conflict") {
          try {
            const latest = await fetchLatestProcedure(procedure.id, requestScope, controller.signal);
            if (!mutationIsCurrent(generation, requestScope)) return;
            latest.capabilities = procedure.capabilities;
            setState((prev) => prev.status === "ok" ? {
              ...prev,
              procedures: prev.procedures.map((row) => row.id === latest.id ? latest : row),
            } : prev);
            setArchiveError({ id: procedure.id, message: `${failure.message} Latest version v${latest.version_number} loaded; retry restore.` });
          } catch (reloadError) {
            if (mutationIsCurrent(generation, requestScope)) {
              setArchiveError({ id: procedure.id, message: `${failure.message} Could not reload the latest version: ${errorMessage(reloadError)}` });
            }
          }
          return;
        }
        setArchiveError({ id: procedure.id, message: failure.message });
        return;
      }
      const restored = parseProcedure(await res.json(), requestScope) as ContextProcedure;
      if (!mutationIsCurrent(generation, requestScope)) return;
      restored.capabilities = procedure.capabilities;
      setState((prev) => prev.status === "ok" ? {
        ...prev,
        procedures: prev.procedures.map((row) => row.id === restored.id ? restored : row),
      } : prev);
      setArchiveError((prev) => prev?.id === procedure.id ? null : prev);
    } catch (err) {
      if (!mutationIsCurrent(generation, requestScope) || (controller.signal.aborted && !timedOut)) return;
      setArchiveError({
        id: procedure.id,
        message: timedOut ? "Restoring this Skill timed out." : errorMessage(err),
      });
    } finally {
      window.clearTimeout(timeoutId);
    }
  }
  const procedures = state.status === "ok"
    ? state.procedures.filter((p) => showArchived || p.status === "active")
    : [];
  const canWrite = state.status === "ok" && state.capabilities?.can_write === true;
  const capabilityMessage = state.status === "ok" && !state.capabilities
    ? "Write permissions could not be verified. Editing is disabled."
    : state.status === "ok" && !canWrite
      ? "You have read-only access to Skills."
      : null;

  return (
    <>
      <PageHeader
        title="Skills"
        description="Documented guidance AI agents follow during analysis for this project."
        actions={
          <>
            <Button
              variant="secondary"
              type="button"
              aria-pressed={showArchived}
              data-testid="procedures-show-archived"
              onClick={() => setShowArchived((shown) => !shown)}
            >
              {showArchived ? "Hide archived" : "Show archived"}
            </Button>
            <Button type="button" onClick={openCreate} disabled={!canWrite} title={capabilityMessage ?? undefined}>
              + Add Skill
            </Button>
          </>
        }
      />

      {state.status === "loading" && (
        <p className="text-label font-normal text-text-secondary" role="status">
          Loading Skills…
        </p>
      )}

      {state.status !== "loading" && state.status !== "ok" && (
        <Status
          as="block"
          tone="error"
          data-testid="procedures-error"
          title={state.status === "denied" ? "Skills unavailable" : state.status === "capability" ? "Versioned Skills are not enabled" : "Skills could not be read"}
          action={
            <Button variant="secondary" type="button" onClick={() => void load()}>
              Retry
            </Button>
          }
        >
          {state.message}
        </Status>
      )}

      {capabilityMessage && (
        <Status as="block" tone="warning" data-testid="procedures-capability">
          {capabilityMessage}
        </Status>
      )}

      {/* Le repli, lisible. Un candidat ecarte a repetition pour des questions
          d'un metier auquel il n'est pas lie est un LIEN MANQUANT, pas un
          accident de formulation -- et la reparation est une affectation de
          taxonomie, qui se fait dans l'editeur de la Skill. */}
      {recurrentFates.length > 0 && (
        <Panel data-testid="procedures-recurrent-fates">
          <h2 className="m-0 text-h3 font-h3 text-text">Recurring rejections</h2>
          <p className="mt-2 mb-0 max-w-[70ch] text-body text-text-secondary">
            The search reached these and dropped them at least {fatesMinimum} times. A candidate
            that keeps being dropped for questions of a business it is not linked to is a missing
            link — assign it in the skill editor.
          </p>
          <ul className="mt-3 mb-0 flex list-none flex-col gap-2 p-0">
            {recurrentFates.map((fate) => {
              const known = procedures.find((procedure) => procedure.id === fate.candidate_id);
              return (
                <li key={`${fate.candidate_id}-${fate.reason}`} data-testid={`recurrent-fate-${fate.candidate_id}`}>
                  <strong>{known?.name ?? fate.candidate_id}</strong>{" "}
                  <span>{fate.reason.replaceAll("_", " ")} · {fate.times} times</span>
                  {fate.last_query && <em> · last query “{fate.last_query}”</em>}
                  {known && (
                    <Button variant="secondary" type="button" onClick={() => openEdit(known)}>
                      Open skill
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        </Panel>
      )}

      {/* THE ONE EMPTY STATE (76-4). It was a centred paragraph in a Panel, a
          fifth shape for one statement. The sentence already named where the
          control is (`Add Skill`, above), and it keeps saying so rather than
          mounting a second copy of that button. */}
      {state.status === "ok" && procedures.length === 0 && (
        <Panel data-testid="procedures-empty">
          <EmptyState
            icon={<ScrollTextIcon />}
            title="No Skill has been written yet"
            description="Write down the guidance analysis must follow — GA4 versus platform attribution, how to read an anomaly, when to escalate — with Add Skill, above, so the agent has one agreed source to read."
          />
        </Panel>
      )}

      {state.status === "ok" && procedures.length > 0 && (
        <section className="grid gap-4.5 md:grid-cols-2 lg:grid-cols-3">
          {procedures.map((procedure) => {
            const isPlatform = procedure.project_id === null;
            const isArchived = procedure.status === "archived";
            const isReadOnly = procedure.capabilities?.can_write !== true || (isPlatform && readOnlyIds.has(procedure.id));
            return (
              /* An archived Skill is shown, never hidden — but it must not
                 read as part of what agents follow today. Dashed border +
                 recessed surface, the console's "present, not in force". */
              <Panel
                key={procedure.id}
                className={`flex min-h-[236px] flex-col items-start gap-3 p-5.5${isArchived ? " border-dashed bg-[color-mix(in_srgb,var(--color-text-secondary)_6%,var(--color-surface-light))]" : ""}`}
                data-testid={isArchived ? `procedure-card-archived-${procedure.id}` : undefined}
              >
                <div className="flex w-full items-center justify-between gap-3">
                  <h2 className={`m-0 font-display text-[17px] leading-[1.35] font-semibold ${isArchived ? "text-text-secondary" : "text-text"}`}>{procedure.name}</h2>
                  <div className="flex shrink-0 items-center gap-2">
                    {/* `Archived` is a state — the union draws it `error`,
                        because an archived object cannot be bound. `Platform` is
                        WHERE the Skill comes from, a label, so it is outline. */}
                    {isArchived && (
                      <Badge tone={stateTone("archived")}>{stateLabel("archived")}</Badge>
                    )}
                    {isPlatform && <Badge outline>Platform</Badge>}
                    {isReadOnly && <Badge tone="warning">Read-only</Badge>}
                  </div>
                </div>
                <p className="m-0 text-label font-normal leading-normal text-text-secondary">{procedure.description}</p>
                <div className="mt-auto grid gap-1 text-caption leading-normal text-text-secondary [&_b]:font-semibold [&_time]:font-numeric [&_time]:tabular-nums">
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
                {isArchived && (
                  <p className="m-0 text-caption leading-normal text-text-secondary" data-testid={`procedure-archived-note-${procedure.id}`}>
                    Archived — agents no longer follow this Skill. Restore it to put it back.
                  </p>
                )}
                {/* AI-158 : ce qu'on reproche a CETTE Skill, et le geste qui y
                    repond. « Adjust this Skill » ouvre l'editeur deja monte
                    plus bas ; la sauvegarde PATCH avec `expected_version`, donc
                    la reponse a une remarque est une NOUVELLE VERSION. La
                    remarque, elle, reste epinglee a l'ancienne — et se marque
                    « older version » d'elle-meme au rechargement. Pas sur une
                    archivee : le seul geste qu'elle accepte est le retour. */}
                {!isArchived && (
                  <NodeRemarks
                    projectId={scopedProjectId}
                    nodeId={procedure.id}
                    nodeType="procedure"
                    nodeVersion={procedure.version_number}
                    rows={remarks.byNode.get(procedure.id) ?? []}
                    canResolve={remarks.canResolve}
                    onResolved={() => setRemarksReload((n) => n + 1)}
                    onAdjust={() => openEdit(procedure)}
                    adjustDisabled={isReadOnly}
                    testIdPrefix={`procedure-review-${procedure.id}`}
                  />
                )}
                <div className="mt-3.5 flex gap-2.5">
                  <Button variant="secondary" type="button" onClick={() => onOpenProcedure?.(procedure.id)}>Open details</Button>
                  {/* An archived row refuses a PATCH server-side, so the
                      console does not offer one: the single gesture it accepts
                      is the way back. */}
                  {isArchived ? (
                    <Button
                      variant="secondary"
                      type="button"
                      onClick={() => setPendingRestore(procedure)}
                      disabled={isReadOnly}
                    >
                      Restore
                    </Button>
                  ) : (
                    <>
                      <Button
                        variant="secondary"
                        type="button"
                        onClick={() => openEdit(procedure)}
                        disabled={isReadOnly}
                      >
                        Edit Skill
                      </Button>
                      <Button
                        variant="secondary"
                        type="button"
                        onClick={() => setPendingArchive(procedure)}
                        disabled={isReadOnly}
                      >
                        Archive
                      </Button>
                    </>
                  )}
                </div>
                {archiveError && archiveError.id === procedure.id && (
                  <Status
                    as="block"
                    tone="error"
                    data-testid={`procedure-archive-error-${procedure.id}`}
                  >
                    {archiveError.message}
                  </Status>
                )}
                {assignmentError && assignmentError.id === procedure.id && (
                  <Status
                    as="block"
                    tone="error"
                    data-testid={`procedure-assignment-error-${procedure.id}`}
                  >
                    {assignmentError.message}
                  </Status>
                )}
              </Panel>
            );
          })}
        </section>
      )}

      {editor.mode !== "closed" && (
        <SkillEditorDrawer
          projectId={scopedProjectId}
          initial={{
            mode: editor.mode,
            procedureId: editor.mode === "edit" ? editor.procedure.id : null,
            name: editor.name,
            description: editor.description,
            owner: editor.owner,
            frontmatterYaml: editor.frontmatterYaml,
            bodyMd: editor.body_md,
            mdmReferences: editor.mode === "edit" ? editor.mdmReferences : null,
          }}
          businessContext={businessContext}
          saving={editor.saving}
          error={editor.error}
          onClose={closeEditor}
          onSave={(value) => void submitEditor(value)}
        />
      )}

      {/* Archiving still asks — agents stop following the Skill the moment it
          leaves — but it no longer claims to be final, because it is not: the
          confirmation names the Skill AND names the way back. A warning that
          overstates what a gesture costs is a warning people learn to skip. */}
      <ConfirmDialog
        open={pendingArchive !== null}
        onOpenChange={(open) => { if (!open) setPendingArchive(null); }}
        title={pendingArchive ? `Archive “${pendingArchive.name}”?` : "Archive this Skill?"}
        description={
          pendingArchive
            ? `“${pendingArchive.name}” leaves this project and agents stop following it during analysis. Nothing is deleted: turn on “Show archived” to find it again and restore it.`
            : ""
        }
        confirmLabel="Archive Skill"
        destructive
        onConfirm={() => {
          const procedure = pendingArchive;
          setPendingArchive(null);
          if (procedure) void archiveProcedure(procedure);
        }}
        data-testid="procedure-archive-confirm"
        cancelTestId="procedure-archive-confirm-cancel"
        confirmTestId="procedure-archive-confirm-accept"
      />

      {/* Restoring is not destructive, but it changes what every agent reads
          on the next analysis — so it is confirmed by name too. */}
      <ConfirmDialog
        open={pendingRestore !== null}
        onOpenChange={(open) => { if (!open) setPendingRestore(null); }}
        title={pendingRestore ? `Restore “${pendingRestore.name}”?` : "Restore this Skill?"}
        description={
          pendingRestore
            ? `“${pendingRestore.name}” returns to this project and agents follow it again during analysis. Its history is kept: the restore is recorded as the next version.`
            : ""
        }
        confirmLabel="Restore Skill"
        onConfirm={() => {
          const procedure = pendingRestore;
          setPendingRestore(null);
          if (procedure) void restoreProcedure(procedure);
        }}
        data-testid="procedure-restore-confirm"
        cancelTestId="procedure-restore-confirm-cancel"
        confirmTestId="procedure-restore-confirm-accept"
      />
    </>
  );
}
