/**
 * WidgetCardsPage — Analyze › Topics: the Answerable Topic catalog.
 *
 * ── What this screen is ──────────────────────────────────────────────────────
 * The questions THIS project answers. Story 52.1 turned that catalog from a
 * Python list every tenant shared (`server/core/cards.py:CARD_TEMPLATES`) into a
 * governed, project-scoped object — so this screen is also where a project adds,
 * rewords and retires one, without a deploy.
 *
 * ── Data ─────────────────────────────────────────────────────────────────────
 * READ: GET /api/cards/templates?project_id=… → {templates: [...]} — the RESOLVED
 * catalog (defaults, with this project's own entries layered over them) plus
 * `usable` / `missing`: whether the project holds the canonical fields each
 * question needs. Usability is the only claim this page makes, and the server
 * makes it. An entry carrying `origin` was stored by this project; one without
 * is inherited from the platform default set and was never written anywhere.
 *
 * WRITE: POST /api/projects/{id}/answerable-topics (add or reword a default),
 * POST …/{topic_key}/versions (reword), POST …/{topic_key}/retire,
 * POST/DELETE …/{topic_key}/queries (bind, unbind), POST/DELETE
 * …/{topic_key}/knowledge (declare, withdraw) and — story 75-5, in
 * `topics/TopicViewsPanel.tsx` — POST/DELETE …/{topic_key}/views, the Semantic
 * Views a topic may read and the join paths it allows. Each carries an
 * `Idempotency-Key`: these are governed writes, audited with the change.
 *
 * A reword never edits a version — it appends one. Retiring removes a question
 * from the catalog and deletes nothing; it is confirmed first, because this
 * screen offers no way back (review finding C-10).
 *
 * ── Two absences, never one ──────────────────────────────────────────────────
 * "I read the list and it is empty" and "I could not read the list" are
 * different facts and render differently, for the bound queries as for the
 * declared knowledge (review finding D-10). Turning a failed read into `[]` made
 * the panel claim the project had declared nothing — the same lie about absence
 * this epic spends four stories removing from the server.
 *
 * Knowledge could be READ and never declared until review finding C-5/D-9: an
 * operator could tick "answers only from governed knowledge" and then had no
 * console path to give the topic any, so the topic refused for ever.
 *
 * ── What is chosen, and what is still typed ──────────────────────────────────
 * A governed pin is only as good as the operator's memory of an identifier, so
 * every field that names a governed object is a LIST wherever a listing route
 * already exists, and free text only where none does:
 *
 *   knowledge id       CHOSEN — `GET /api/context/{topics,procedures}` , the two
 *                      routes the Context Hub screens already read.
 *   knowledge version  CHOSEN — `GET /api/context/{kind}/{id}/versions`, the
 *                      route `ContextObjectPage` already reads. The head is the
 *                      default AND is named as the head, because a pin that
 *                      follows the newest silently changes what the topic says.
 *   base template      CHOSEN — `GET /api/cards/templates` with no `project_id`
 *                      is `default_catalog()`, exactly the set
 *                      `cards.get_template` accepts.
 *   query spec version CHOSEN — `GET /api/projects/{id}/analyze/query-specs`.
 *                      This field was free text until 2026-08-18, and the reason
 *                      was true at the time: `query_specs_api.py` mounted
 *                      `POST /query-specs` and `GET /query-specs/{id}` and no
 *                      collection, so a picker would have needed an endpoint
 *                      that did not exist. The route exists now, and it carries
 *                      each spec's CURRENT version id — the pinnable one — with
 *                      its version number, so the pin is chosen and the person
 *                      SEES which version they are committing the topic to.
 *
 * Each list keeps a paste escape: a platform-wide object the reader's scope does
 * not enumerate must still be pinnable, and the Query Spec collection is a
 * BOUNDED page — when the server says another page exists, the list on screen is
 * not everything there is, and it says so rather than letting a reader conclude
 * a spec was retired because they cannot find it.
 *
 * ── Which object a declared row names ────────────────────────────────────────
 * A declared knowledge row used to name `knowledge_id vN` and open nothing. The
 * name now links to that object's own page, at the address `objectSurfaces.tsx`
 * declares for it, built by `buildPath` and checked by `resolvableHref` before
 * it is offered — never composed here.
 *
 * History (audit 2026-07-25): the page listed four invented widgets ("KPI Hero
 * Card", "Trend Sparkline Card", …) with no I/O whatsoever, `void projectId`, and
 * a "Preview widget" button on each card that did nothing. The real catalog
 * endpoint existed and was never called. It was then mounted nowhere at all
 * until Story 52.1 gave the catalog a door.
 *
 * Styling: EVERY visual on this screen is a primitive from `ui/index.ts`
 * (AD-35: Tailwind 4.3 + shadcn is the target, MUI and the legacy sheets are
 * not). The screen imported `widgets.css` and used eleven `widget-*` classes --
 * a component re-implemented locally, which is exactly what the audit reports.
 * PageHeader, Panel, Cluster, Stack, SectionHeader, Badge, Status and Button
 * replaced them; no stylesheet is imported here any more.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, apiJson } from "./lib/apiFetch";
import TopicViewsPanel from "./topics/TopicViewsPanel";
import { buildPath, useRoute } from "./shell/router";
import { resolvableHref } from "./shell/routeHref";
import { Badge, Button, Checkbox, Cluster, Collapsible, CollapsibleContent, CollapsibleTrigger, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, EmptyState, Field, Input, NativeSelect, ObjectId, PageHeader, Panel, SectionHeader, Stack, Status, Textarea, wireWord } from "./ui";

interface CardTemplate {
  id: string;
  title?: string | null;
  answers_question?: string | null;
  widget_uri?: string | null;
  kind?: string | null;
  required_metrics?: string[];
  required_dimensions?: string[];
  comment_builder?: boolean;
  usable?: boolean;
  missing?: { metrics?: string[]; dimensions?: string[] };
  /** Present only on an entry this project stored. Absent = inherited default. */
  origin?: string;
  base_template_id?: string;
  version_number?: number;
  requires_knowledge?: boolean;
}

interface TemplatesResponse {
  templates?: CardTemplate[];
}

/** Story 52.2 - one governed query bound to a topic, with its role and exact pin. */
interface QueryBinding {
  binding_id: string;
  topic_key: string;
  role: string;
  position: number;
  query_spec_id: string;
  query_spec_version_id: string;
  query_spec_version_number?: number | null;
}

interface BindingsResponse {
  queries?: QueryBinding[];
}

/** Story 52.3 - one governed knowledge version a topic declares it may read. */
interface KnowledgePin {
  binding_id: string;
  knowledge_kind: string;
  knowledge_id: string;
  knowledge_version: number;
  title?: string | null;
  /** False when the pinned version cannot be read. Shown, never hidden: an
   *  unreadable declaration is "context missing", not "nothing declared". */
  readable: boolean;
}

interface KnowledgeResponse {
  knowledge?: KnowledgePin[];
}

/** One governed knowledge object the Context Hub can offer to a pin.
 *
 *  Read from the SAME two routes the Context Hub screens read
 *  (`KnowledgeBasePage.tsx:181` → `GET /api/context/topics?project_id=…`,
 *  `contextApi.ts:100` → `GET /api/context/procedures?project_id=…`). Nothing
 *  here is a new endpoint: the console already lists both, and this screen used
 *  to make an operator retype an id it could have shown. */
interface KnowledgeChoice {
  id: string;
  label: string;
}

/** Rows of the two list routes above. `version_number` on those rows is
 *  `COALESCE(MAX(v.version_number), 1)` (`context_store.py:953`) — it is 1 for an
 *  object with NO version row at all, which is exactly the pin the server
 *  refuses. So the head is read from `…/{id}/versions`, the route
 *  `ContextObjectPage.tsx:84` already calls, and never guessed from this field. */
interface ContextListResponse {
  topics?: Array<{ id?: string; title?: string | null }>;
  procedures?: Array<{ id?: string; name?: string | null }>;
}

interface ContextVersionsResponse {
  versions?: Array<{ version_number?: number | null }>;
}

/** One governed Query Spec this Project can offer to a binding.
 *
 *  Read from `GET /api/projects/{id}/analyze/query-specs`, whose row carries the
 *  head's CURRENT version — the identity `bind_query` accepts. `label` is the
 *  author's own `name` when the store holds one; that column is nullable, and an
 *  unnamed spec is shown BY ITS IDENTIFIER rather than given a title this screen
 *  made up. `named` is what lets the option say which of the two it is showing.
 *
 *  `versionId` is null for a head with no version yet: a real row, and nothing
 *  that can be pinned to it — so the option is offered and refused, never
 *  hidden, which would read as "that spec does not exist". */
interface QuerySpecChoice {
  id: string;
  label: string;
  named: boolean;
  versionId: string | null;
  versionNumber: number | null;
}

interface QuerySpecsResponse {
  query_specs?: Array<{
    id?: string;
    name?: string | null;
    current_version_id?: string | null;
    current_version_number?: number | null;
  }>;
  /** Non-null means the server has more than it sent. The picker is then not a
   *  complete list, and the paste escape stops being a fallback. */
  next_cursor?: string | null;
}

type LoadState = "loading" | "error" | "ready";

/** Add (a new question) or Reword (an existing one) — the same form, two verbs. */
type EditorMode = "add" | "reword";

interface EditorState {
  mode: EditorMode;
  topicKey: string;
  title: string;
  question: string;
  baseTemplateId: string;
  /** Story 52.3 AC5: this topic answers ONLY from the project's governed
   *  knowledge, so a missing context is a stated refusal rather than a card
   *  rendered without the half that explains it. */
  requiresKnowledge: boolean;
  /** True when the entry being reworded has never been stored (a platform default). */
  createsFirstVersion: boolean;
}

/** The knowledge declaration in progress.
 *
 *  `pasted` is the ESCAPE, not the default: the list is the way in, and free
 *  text stays available for an object the list cannot carry (one the reader may
 *  not see, one the two routes do not enumerate). */
interface PinnerState {
  kind: string;
  id: string;
  version: string;
  pasted: boolean;
}

/** The query binding in progress.
 *
 *  `versionId` is what is SENT — an exact Query Spec version. `specId` only says
 *  which row of the picker is selected, so the chosen line stays visible while
 *  the version it pinned is what travels. `pasted` is the same escape the
 *  knowledge pinner keeps, for a spec beyond the page the server returned. */
interface BinderState {
  specId: string;
  versionId: string;
  role: string;
  pasted: boolean;
}

function kindLabel(kind: string | null | undefined): string {
  if (kind === "context") return "Context";
  if (kind === "kpi") return "KPI";
  return kind || "Card";
}

/** Stable per-attempt key: a retried submit must not create a second version. */
function idempotencyKey(action: string, topicKey: string): string {
  return `topic-${action}-${topicKey}-${Date.now()}`;
}

function write<T>(path: string, action: string, topicKey: string, body?: unknown): Promise<T> {
  return apiJson<T>(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey(action, topicKey),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export default function WidgetCardsPage({ projectId }: { projectId?: string }) {
  // The ONE router. A declared knowledge row names an object that has a page in
  // this console, and the address of that page is built by the router that has
  // to resolve it -- never composed here (`routeHref.ts:9-18` records the four
  // times a second address grammar was paid for).
  const { route, navigate } = useRoute();
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [templates, setTemplates] = useState<CardTemplate[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [busy, setBusy] = useState(false);
  const [writeError, setWriteError] = useState("");
  // Story 52.2: the governed queries bound to the topic currently opened. Loaded on
  // demand rather than for every card: a catalog of nine would otherwise fire nine
  // reads to show, in production today, nine empty lists.
  const [openQueries, setOpenQueries] = useState<string | null>(null);
  const [bindings, setBindings] = useState<QueryBinding[] | null>(null);
  const [binder, setBinder] = useState<BinderState | null>(null);
  // The Query Specs this Project has authored, read when the binding form opens
  // and not before — the catalog above would otherwise fire this read for every
  // card that is merely displayed. Same third state as every other list here:
  // `null` + a message is "I could not read", `[]` is "I read, and there is none".
  const [querySpecs, setQuerySpecs] = useState<QuerySpecChoice[] | null>(null);
  const [querySpecsError, setQuerySpecsError] = useState("");
  // The server paged. What is on screen is then not everything there is, and the
  // reader is told so rather than concluding a spec they cannot find is gone.
  const [querySpecsTruncated, setQuerySpecsTruncated] = useState(false);
  const [knowledge, setKnowledge] = useState<KnowledgePin[] | null>(null);
  // D-10: "I could not read this list" is a THIRD state, next to "still reading"
  // (`null`) and "read, and it is empty" (`[]`). Collapsing it into `[]` made the
  // panel assert an absence -- the exact lie the whole epic fights on the server.
  const [bindingsError, setBindingsError] = useState("");
  const [knowledgeError, setKnowledgeError] = useState("");
  // C-5 / D-9: the declaration form. `requires_knowledge` without this was a
  // one-way trap -- a topic could be told to answer only from governed knowledge
  // and then given none.
  const [pinner, setPinner] = useState<PinnerState | null>(null);
  // The governed knowledge this project can actually pin, and the versions the
  // chosen object actually has. Both keep the D-10 third state: `null` + an
  // error message is "I could not read", `[]` is "I read, and there is none".
  const [choices, setChoices] = useState<KnowledgeChoice[] | null>(null);
  const [choicesError, setChoicesError] = useState("");
  const [versions, setVersions] = useState<number[] | null>(null);
  const [versionsError, setVersionsError] = useState("");
  // The PLATFORM DEFAULT SET -- what `base_template_id` is checked against
  // (`answerable_topics.py:1163`, `cards.get_template`). It is the SAME endpoint
  // this page already reads, asked WITHOUT a project: with `project_id` the
  // route answers the resolved catalog, without it `default_catalog()`, which is
  // the registry of cards a topic may render as.
  const [baseTemplates, setBaseTemplates] = useState<CardTemplate[] | null>(null);
  const [baseTemplatesError, setBaseTemplatesError] = useState("");
  // C-10: retiring removes a question from the project catalog and nothing in
  // this screen brings it back, so it is confirmed before it is sent.
  const [pendingRetire, setPendingRetire] = useState<CardTemplate | null>(null);
  // The catalog arrives whole -- `_list_templates` (`cards_api.py:214`) pages
  // nothing and takes no cursor -- so narrowing it is a question this screen can
  // answer without asking the server, and without pretending to page.
  const [filter, setFilter] = useState("");
  const [usability, setUsability] = useState("all");

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  /** The declared address of one governed knowledge object, or `null`.
   *
   *  `objectSurfaces.tsx:225-260` is what answers it: a Knowledge item opens
   *  under `context-hub/knowledge-library` as `context-topic`, a Skill under
   *  `context-hub/skills-registry` as `context-procedure`, both on their
   *  `content` tab (`navigation/contextHub.ts:22-45` declares the three tabs).
   *  It is asked whether it resolves before it is offered: outside a Project
   *  scope -- the sandbox mounts collections there -- the row keeps its name and
   *  loses its link, rather than offering a live-looking control that lands on
   *  the unknown-route screen. */
  const contextObjectRoute = useCallback(
    (kind: string) => ({
      section: kind === "procedure" ? "skills-registry" : "knowledge-library",
      objectType: kind === "procedure" ? "context-procedure" : "context-topic",
    }),
    [],
  );

  const contextObjectHref = useCallback(
    (kind: string, id: string): string | null => {
      if (route.scope !== "project" || route.globalSurface !== null || !id) return null;
      const { section, objectType } = contextObjectRoute(kind);
      try {
        return resolvableHref(
          buildPath({
            ...route,
            workspace: "context-hub",
            section,
            lens: null,
            objectType,
            objectId: id,
            tab: "content",
            versionId: null,
            evidenceId: null,
            action: null,
            query: {},
          }),
        );
      } catch {
        return null;
      }
    },
    [route, contextObjectRoute],
  );

  /** Narrowed by what the reader typed, and by the server's own verdict.
   *
   *  `usable` is the ONE claim this page makes and the server makes it, so it is
   *  also the only status worth filtering on. An entry the server did not judge
   *  (`usable === undefined`) is never swept into either bucket. */
  const visible = useMemo(() => {
    const term = filter.trim().toLowerCase();
    return templates.filter((card) => {
      if (usability === "usable" && card.usable !== true) return false;
      if (usability === "missing" && card.usable !== false) return false;
      if (!term) return true;
      return [card.title, card.answers_question, card.id]
        .some((value) => (value || "").toLowerCase().includes(term));
    });
  }, [templates, filter, usability]);

  useEffect(() => {
    let alive = true;
    setState("loading");
    setError("");

    (async () => {
      try {
        // project_id is what makes the server resolve THIS project's catalog and
        // compute `usable` / `missing`; without it the catalog is the platform
        // default set. It is always present in the shell.
        const path = projectId
          ? `/api/cards/templates?project_id=${encodeURIComponent(projectId)}`
          : "/api/cards/templates";
        const body = await apiGet<TemplatesResponse>(path);
        if (!alive) return;
        setTemplates(Array.isArray(body.templates) ? body.templates : []);
        setState("ready");
      } catch (err) {
        if (!alive) return;
        setTemplates([]);
        setError(err instanceof Error ? err.message : "Request failed");
        setState("error");
      }
    })();

    return () => {
      alive = false;
    };
  }, [projectId, attempt]);

  /** The cards a topic may render as. Read once, when a form that asks for one
   *  opens: a catalog of nine must not spend a request on a question nobody has
   *  asked yet. */
  const loadBaseTemplates = useCallback(async () => {
    setBaseTemplatesError("");
    try {
      const body = await apiGet<TemplatesResponse>("/api/cards/templates");
      setBaseTemplates(Array.isArray(body.templates) ? body.templates : []);
    } catch (err) {
      // NOT `[]`: an empty select would read "this platform ships no card", and
      // the field falls back to free text instead of claiming that.
      setBaseTemplates(null);
      setBaseTemplatesError(err instanceof Error ? err.message : "Request failed");
    }
  }, []);

  const openAdd = () => {
    setEditor({
      mode: "add",
      topicKey: "",
      title: "",
      question: "",
      baseTemplateId: templates[0]?.base_template_id || templates[0]?.id || "kpi",
      requiresKnowledge: false,
      createsFirstVersion: true,
    });
    if (baseTemplates === null) void loadBaseTemplates();
  };

  const openReword = (card: CardTemplate) => {
    setEditor({
      mode: "reword",
      topicKey: card.id,
      title: card.title || "",
      question: card.answers_question || "",
      baseTemplateId: card.base_template_id || card.id,
      requiresKnowledge: Boolean(card.requires_knowledge),
      // An inherited default has no stored head yet: rewording it CREATES one,
      // which is why this goes to the collection route and not to /versions.
      createsFirstVersion: card.origin === undefined,
    });
    if (baseTemplates === null) void loadBaseTemplates();
  };

  const submitEditor = async () => {
    if (!editor || !projectId) return;
    setBusy(true);
    setWriteError("");
    const base = `/api/projects/${encodeURIComponent(projectId)}/answerable-topics`;
    const payload = {
      topic_key: editor.topicKey,
      title: editor.title,
      answers_question: editor.question,
      base_template_id: editor.baseTemplateId,
      requires_knowledge: editor.requiresKnowledge,
    };
    try {
      if (editor.mode === "add" || editor.createsFirstVersion) {
        await write(base, "create", editor.topicKey, payload);
      } else {
        await write(
          `${base}/${encodeURIComponent(editor.topicKey)}/versions`,
          "reword",
          editor.topicKey,
          payload,
        );
      }
      setEditor(null);
      retry();
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const loadKnowledge = useCallback(
    async (topicKey: string) => {
      if (!projectId) return;
      setKnowledge(null);
      setKnowledgeError("");
      try {
        const body = await apiGet<KnowledgeResponse>(
          `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/knowledge`,
        );
        setKnowledge(Array.isArray(body.knowledge) ? body.knowledge : []);
      } catch (err) {
        // NOT `setKnowledge([])`: an empty list would render "this topic
        // elaborates from no governed knowledge", which is a claim about the
        // project made out of a failure of ours.
        setKnowledge(null);
        setKnowledgeError(err instanceof Error ? err.message : "Request failed");
      }
    },
    [projectId],
  );

  /** The governed knowledge objects of this Project, for the kind being pinned.
   *
   *  Both routes return the project's own rows AND the platform-wide ones
   *  (`contextApi.ts:40-43`), which is exactly the scope `bind_knowledge` checks
   *  before it accepts a pin (`answerable_topics.py:1005-1023`) -- so what the
   *  list offers is what the server will accept. */
  const loadChoices = useCallback(
    async (kind: string) => {
      if (!projectId) return;
      setChoices(null);
      setChoicesError("");
      const base = kind === "procedure" ? "/api/context/procedures" : "/api/context/topics";
      try {
        const body = await apiGet<ContextListResponse>(
          `${base}?project_id=${encodeURIComponent(projectId)}`,
        );
        const rows = kind === "procedure" ? body.procedures : body.topics;
        setChoices(
          (Array.isArray(rows) ? rows : [])
            .map((row) => {
              const id = String(row?.id ?? "");
              const named =
                "name" in (row ?? {}) ? (row as { name?: string | null }).name : undefined;
              const titled =
                "title" in (row ?? {}) ? (row as { title?: string | null }).title : undefined;
              return { id, label: (named || titled || id).trim() || id };
            })
            .filter((choice) => choice.id !== ""),
        );
      } catch (err) {
        // D-10 again: an unreadable list is not an empty one. Falling back to
        // `[]` would render "this project has authored no knowledge", which is
        // a claim about the project made out of a failure of ours.
        setChoices(null);
        setChoicesError(err instanceof Error ? err.message : "Request failed");
      }
    },
    [projectId],
  );

  /** The versions that EXIST for the chosen object, newest first.
   *
   *  The head is read here rather than taken from the list row, because that
   *  row's `version_number` is `COALESCE(MAX(...), 1)`: it says 1 for an object
   *  with no version row, and pinning that 1 is refused as
   *  `unknown_knowledge_version`. Reading the versions means the number this
   *  form defaults to is a version that can actually be pinned. */
  const loadVersions = useCallback(
    async (kind: string, id: string): Promise<number[] | null> => {
      if (!projectId || !id) {
        setVersions(null);
        setVersionsError("");
        return null;
      }
      setVersions(null);
      setVersionsError("");
      const base = kind === "procedure" ? "/api/context/procedures" : "/api/context/topics";
      try {
        const body = await apiGet<ContextVersionsResponse>(
          `${base}/${encodeURIComponent(id)}/versions?project_id=${encodeURIComponent(projectId)}`,
        );
        const numbers = (Array.isArray(body.versions) ? body.versions : [])
          .map((row) => Number(row?.version_number))
          .filter((n) => Number.isInteger(n) && n > 0)
          .sort((a, b) => b - a);
        setVersions(numbers);
        return numbers;
      } catch (err) {
        setVersions(null);
        setVersionsError(err instanceof Error ? err.message : "Request failed");
        return null;
      }
    },
    [projectId],
  );

  /** Open the declaration form on a kind, and read what that kind offers. */
  const openPinner = (kind: string) => {
    setPinner({ kind, id: "", version: "", pasted: false });
    setVersions(null);
    setVersionsError("");
    void loadChoices(kind);
  };

  /** Choosing an object pins its CURRENT HEAD, and the head is shown as such.
   *  A pin is the version the topic commits to; defaulting to it silently would
   *  be the same failure as following `latest`. */
  const chooseKnowledge = async (kind: string, id: string) => {
    if (id === "") {
      setPinner((p) => (p ? { ...p, id: "", version: "", pasted: true } : p));
      setVersions(null);
      setVersionsError("");
      return;
    }
    setPinner((p) => (p ? { ...p, id, version: "" } : p));
    const numbers = await loadVersions(kind, id);
    const head = numbers && numbers.length > 0 ? numbers[0] : null;
    setPinner((p) => (p ? { ...p, id, version: head === null ? "" : String(head) } : p));
  };

  /** The Query Specs this Project can bind, with the version each one is at.
   *
   *  The route answers a BOUNDED page (`query_specs.list_query_specs` clamps
   *  it), so `next_cursor` is kept: a picker that silently shows the first page
   *  of a longer collection is a picker that hides the spec its reader came
   *  for. */
  const loadQuerySpecs = useCallback(async () => {
    if (!projectId) return;
    setQuerySpecs(null);
    setQuerySpecsError("");
    setQuerySpecsTruncated(false);
    try {
      const body = await apiGet<QuerySpecsResponse>(
        `/api/projects/${encodeURIComponent(projectId)}/analyze/query-specs`,
      );
      const rows = Array.isArray(body.query_specs) ? body.query_specs : [];
      setQuerySpecs(
        rows
          .map((row) => {
            const id = String(row?.id ?? "");
            const name = (row?.name || "").trim();
            const versionId = row?.current_version_id ? String(row.current_version_id) : null;
            const versionNumber = Number(row?.current_version_number);
            return {
              id,
              // NEVER a fabricated title: an unnamed spec is shown by its id, and
              // `named` is what lets the option say so out loud.
              label: name || id,
              named: name !== "",
              versionId,
              versionNumber: Number.isInteger(versionNumber) ? versionNumber : null,
            };
          })
          .filter((choice) => choice.id !== ""),
      );
      setQuerySpecsTruncated(Boolean(body.next_cursor));
    } catch (err) {
      // NOT `[]`: an empty select would read "this project has authored no
      // governed query", which is a claim about the project made out of a
      // failure of ours. The field falls back to paste instead.
      setQuerySpecs(null);
      setQuerySpecsError(err instanceof Error ? err.message : "Request failed");
    }
  }, [projectId]);

  /** Open the binding form, and read what this Project has to offer it. */
  const openBinder = () => {
    setBinder({ specId: "", versionId: "", role: "headline", pasted: false });
    void loadQuerySpecs();
  };

  /** Choosing a Query Spec pins its CURRENT version, and the form says which.
   *  A pin is what the topic commits to; taking the head silently would be the
   *  same failure as following `latest`. */
  const chooseQuerySpec = (specId: string) => {
    if (specId === "") {
      setBinder((b) => (b ? { ...b, specId: "", versionId: "", pasted: true } : b));
      return;
    }
    const choice = (querySpecs || []).find((c) => c.id === specId);
    setBinder((b) =>
      b ? { ...b, specId, versionId: choice?.versionId || "" } : b,
    );
  };

  const loadBindings = useCallback(
    async (topicKey: string) => {
      if (!projectId) return;
      setBindings(null);
      setBindingsError("");
      setOpenQueries(topicKey);
      try {
        const body = await apiGet<BindingsResponse>(
          `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/queries`,
        );
        setBindings(Array.isArray(body.queries) ? body.queries : []);
      } catch (err) {
        setBindings(null);
        setBindingsError(err instanceof Error ? err.message : "Request failed");
      }
      // Story 52.3: the two halves of an answer -- what computes it and what
      // explains it -- are shown together. Splitting them into two panels made
      // the explaining half look optional. Read outside the try: a query read
      // that fails must not leave the knowledge half stuck on "Loading...".
      await loadKnowledge(topicKey);
    },
    [projectId, loadKnowledge],
  );

  const bindQuery = async (topicKey: string) => {
    if (!projectId || !binder) return;
    setBusy(true);
    setWriteError("");
    try {
      await write(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/queries`,
        "bind",
        topicKey,
        { query_spec_version_id: binder.versionId.trim(), role: binder.role },
      );
      setBinder(null);
      await loadBindings(topicKey);
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const unbindQuery = async (topicKey: string, bindingId: string) => {
    if (!projectId) return;
    setBusy(true);
    setWriteError("");
    try {
      await apiJson(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/queries/${encodeURIComponent(bindingId)}`,
        {
          method: "DELETE",
          headers: { "Idempotency-Key": idempotencyKey("unbind", bindingId) },
        },
      );
      await loadBindings(topicKey);
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  /** C-5: declare one exact governed knowledge version this topic may read. */
  const bindKnowledge = async (topicKey: string) => {
    if (!projectId || !pinner) return;
    setBusy(true);
    setWriteError("");
    try {
      await write(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/knowledge`,
        "knowledge-bind",
        topicKey,
        {
          knowledge_kind: pinner.kind,
          knowledge_id: pinner.id.trim(),
          // The server refuses anything that is not an exact version number; it
          // is sent as one so a typo is refused here, not stored as a pin.
          knowledge_version: Number.parseInt(pinner.version, 10),
        },
      );
      setPinner(null);
      setVersions(null);
      setVersionsError("");
      await loadKnowledge(topicKey);
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  /** C-5: withdraw a declaration. The knowledge itself is untouched. */
  const unbindKnowledge = async (topicKey: string, bindingId: string) => {
    if (!projectId) return;
    setBusy(true);
    setWriteError("");
    try {
      await apiJson(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(topicKey)}/knowledge/${encodeURIComponent(bindingId)}`,
        {
          method: "DELETE",
          headers: { "Idempotency-Key": idempotencyKey("knowledge-unbind", bindingId) },
        },
      );
      await loadKnowledge(topicKey);
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const retire = async (card: CardTemplate) => {
    if (!projectId) return;
    setBusy(true);
    setWriteError("");
    try {
      await write(
        `/api/projects/${encodeURIComponent(projectId)}/answerable-topics/${encodeURIComponent(card.id)}/retire`,
        "retire",
        card.id,
      );
      setPendingRetire(null);
      retry();
    } catch (err) {
      setWriteError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader
        title="Topics"
        description="The questions this project answers, and whether it holds the canonical fields each one needs. Adding, rewording or retiring one takes effect for this project only."
        actions={
          <Button type="button" onClick={openAdd} disabled={!projectId || state !== "ready"}>
            Add topic
          </Button>
        }
      />

      {writeError ? (
        <Status tone="error" data-testid="topics-write-error">
          {writeError}
        </Status>
      ) : null}

      {state === "loading" ? (
        <Panel data-testid="widgets-loading">
          <p>Loading the topic catalog…</p>
        </Panel>
      ) : null}

      {state === "error" ? (
        <Panel data-testid="widgets-error">
          {/* ONE BLOCK, NOT A DOT AND A LOOSE BUTTON (76-4). The mark was
              `inline` -- a dot for a table cell -- with the way out floating
              under it; `Status` carries its own action slot, and that is the
              console's one shape for an error a person can act on. */}
          <Status
            as="block"
            tone="error"
            title="The topic catalog could not be read"
            action={<Button type="button" variant="secondary" onClick={retry}>Retry</Button>}
          >
            {error}
          </Status>
        </Panel>
      ) : null}

      {/* An empty list says WHY it is empty and names the gesture that fills it,
          in the shape every other collection of this console uses. A bare
          sentence left the one thing to do on the far side of the page. */}
      {state === "ready" && templates.length === 0 ? (
        <Panel data-testid="widgets-empty">
          <EmptyState
            title="This project answers no question yet."
            description="A topic is one business question a caller can ask for, with the fields its answer needs. It takes effect for this project only."
            action={
              <Button type="button" onClick={openAdd} disabled={!projectId}>
                Add the first topic
              </Button>
            }
          />
        </Panel>
      ) : null}

      {/* The whole catalog is already in memory, so narrowing it is a question
          this screen answers itself. Each control REDUCES the next: the term
          narrows the list, the verdict narrows what is left, and the count says
          what the two together left standing. */}
      {state === "ready" && templates.length > 0 ? (
        <Panel data-testid="topics-filter">
          <Cluster className="items-end gap-3">
            <Field label="Find a question" hint="Matches the title, the question, and the key.">
              {(p) => (
                <Input
                  {...p}
                  type="search"
                  value={filter}
                  placeholder="pacing, spend, where did…"
                  onChange={(e) => setFilter(e.target.value)}
                  data-testid="topics-search"
                />
              )}
            </Field>
            <Field label="Inputs" hint="Whether this project holds the canonical fields the question needs.">
              {(p) => (
                <NativeSelect
                  {...p}
                  value={usability}
                  onChange={(e) => setUsability(e.target.value)}
                  data-testid="topics-usability"
                >
                  <option value="all">Any</option>
                  <option value="usable">Usable</option>
                  <option value="missing">Missing inputs</option>
                </NativeSelect>
              )}
            </Field>
            <p data-testid="topics-count">
              {visible.length} of {templates.length} question{templates.length === 1 ? "" : "s"}
            </p>
          </Cluster>
        </Panel>
      ) : null}

      {/* Narrowed to nothing is NOT "this project answers no question": it names
          the term that emptied the list, and undoes it. */}
      {state === "ready" && templates.length > 0 && visible.length === 0 ? (
        <Panel data-testid="topics-no-match">
          <EmptyState
            title="No question matches what you are looking for."
            description={
              filter.trim()
                ? `Nothing in this catalog mentions “${filter.trim()}” with the inputs verdict you asked for.`
                : "No question in this catalog carries that inputs verdict."
            }
            action={
              <Button
                type="button"
                variant="ghost"
                onClick={() => {
                  setFilter("");
                  setUsability("all");
                }}
              >
                Show every question
              </Button>
            }
          />
        </Panel>
      ) : null}

      {state === "ready" && visible.length > 0 ? (
        <section className="grid grid-cols-2 gap-4">
          {visible.map((card) => {
            const missingMetrics = card.missing?.metrics ?? [];
            const missingDimensions = card.missing?.dimensions ?? [];
            const missingAll = [...missingMetrics, ...missingDimensions];
            const required = [
              ...(card.required_metrics ?? []),
              ...(card.required_dimensions ?? []),
            ];
            return (
              <Panel key={card.id} className="flex flex-col gap-2" data-testid={`card-${card.id}`}>
                <Cluster className="justify-between">
                  <Badge outline>{kindLabel(card.kind)}</Badge>
                  {card.origin ? (
                    <Badge tone="info" data-testid={`origin-${card.id}`}>
                      {card.origin === "project" ? "Project topic" : "Reworded default"}
                    </Badge>
                  ) : null}
                  {/* The version is the wording this project committed to
                      (`glossary.md`: "a reword appends, it never relabels"). It
                      was on the payload and rendered nowhere, so an operator
                      about to reword could not see what they were rewording. An
                      inherited default carries none and claims none. */}
                  {typeof card.version_number === "number" ? (
                    <Badge outline data-testid={`version-${card.id}`}>
                      Version {card.version_number}
                    </Badge>
                  ) : null}
                  {card.usable === undefined ? null : (
                    <Status tone={card.usable ? "success" : "warning"}>
                      {card.usable ? "Usable" : "Missing inputs"}
                    </Status>
                  )}
                </Cluster>
                <h3>{card.title || card.id}</h3>
                <p>{card.answers_question || "No question recorded for this topic."}</p>
                {/* The lineage the payload actually carries, and nothing more:
                    version N means N-1 rewords since this project first stored
                    it. No predecessor is served here, so none is named. */}
                {typeof card.version_number === "number" && card.version_number > 1 ? (
                  <p data-testid={`lineage-${card.id}`}>
                    Reworded {card.version_number - 1} time
                    {card.version_number - 1 === 1 ? "" : "s"} since this project first stored it.
                    Every earlier wording is kept.
                  </p>
                ) : null}

                {required.length > 0 ? (
                  <Stack className="gap-2">
                    <SectionHeader title="Requires" />
                    <Cluster>
                      {required.map((f) => (
                        <Badge
                          key={f}
                          tone={missingAll.includes(f) ? "warning" : "neutral"}
                          outline={!missingAll.includes(f)}
                        >
                          {f}
                        </Badge>
                      ))}
                    </Cluster>
                  </Stack>
                ) : (
                  <SectionHeader title="No canonical field required" />
                )}

                {openQueries === card.id ? (
                  <Stack className="gap-2" data-testid={`queries-${card.id}`}>
                    <SectionHeader title="Governed queries" />
                    {bindingsError ? (
                      // D-10: what this screen knows is that it could not read.
                      // Saying "none is bound" here would answer, with a made-up
                      // fact, the one question the operator opened this panel for.
                      <Status tone="error" data-testid={`queries-unavailable-${card.id}`}>
                        Couldn&apos;t read the queries bound to this topic, so this list is
                        not an answer. {bindingsError}
                      </Status>
                    ) : bindings === null ? (
                      <p>Loading...</p>
                    ) : bindings.length === 0 ? (
                      // The honest empty state. Production holds zero Query Specs, so
                      // this is what an operator will actually see - and it names where
                      // one comes from instead of offering a picker over nothing.
                      <p data-testid={`queries-empty-${card.id}`}>
                        No governed query is bound to this topic. A query is authored in
                        Analyze &rsaquo; Explore as a Query Spec, then bound here by its
                        exact version.
                      </p>
                    ) : (
                      <ul>
                        {bindings.map((b) => (
                          <li key={b.binding_id}>
                            <Badge tone="info">{wireWord(b.role)}</Badge>{" "}
                            <ObjectId value={b.query_spec_version_id} title="Query Spec version" />
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              disabled={busy}
                              onClick={() => unbindQuery(card.id, b.binding_id)}
                            >
                              Unbind
                            </Button>
                          </li>
                        ))}
                      </ul>
                    )}
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={openBinder}
                    >
                      Bind a query
                    </Button>
                    <SectionHeader title="Governed knowledge" />
                    {knowledgeError ? (
                      <Status tone="error" data-testid={`knowledge-unavailable-${card.id}`}>
                        Couldn&apos;t read the knowledge this topic declares, so this list is
                        not an answer. {knowledgeError}
                      </Status>
                    ) : knowledge === null ? (
                      <p>Loading...</p>
                    ) : knowledge.length === 0 ? (
                      // Production holds 41 knowledge VERSION rows and zero heads,
                      // so this is what an operator meets today. It names the
                      // Context Hub instead of pretending the feature is absent.
                      <p data-testid={`knowledge-empty-${card.id}`}>
                        This topic elaborates from no governed Knowledge. A Knowledge
                        item or a Skill is authored in the Context Hub, then
                        declared here by its exact version.
                      </p>
                    ) : (
                      <ul data-testid={`knowledge-${card.id}`}>
                        {knowledge.map((k) => {
                          const href = contextObjectHref(k.knowledge_kind, k.knowledge_id);
                          const name = (k.readable && k.title) ? k.title : k.knowledge_id;
                          return (
                          <li key={k.binding_id}>
                            <Badge tone="info">{wireWord(k.knowledge_kind)}</Badge>{" "}
                            <Badge outline>
                              {k.knowledge_id} v{k.knowledge_version}
                            </Badge>{" "}
                            {/* The row named an object and opened nothing. It
                                now opens the object's own page at the address
                                the registry declares -- a real href, so a middle
                                click and a copied link both work, and a plain
                                click stays inside the shell. */}
                            {href ? (
                              <a
                                href={href}
                                data-testid={`knowledge-link-${k.binding_id}`}
                                onClick={(event) => {
                                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                                  event.preventDefault();
                                  const target = contextObjectRoute(k.knowledge_kind);
                                  navigate({
                                    workspace: "context-hub",
                                    section: target.section,
                                    lens: null,
                                    objectType: target.objectType,
                                    objectId: k.knowledge_id,
                                    tab: "content",
                                    versionId: null,
                                    evidenceId: null,
                                    action: null,
                                    query: {},
                                  });
                                }}
                              >
                                {name}
                              </a>
                            ) : (
                              <span>{name}</span>
                            )}{" "}
                            {k.readable ? null : (
                              <Status tone="warning">Context missing</Status>
                            )}
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              disabled={busy}
                              onClick={() => unbindKnowledge(card.id, k.binding_id)}
                            >
                              Withdraw
                            </Button>
                          </li>
                          );
                        })}
                      </ul>
                    )}
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => openPinner("topic")}
                    >
                      Declare knowledge
                    </Button>

                    {pinner ? (
                      <Stack className="gap-2" data-testid={`knowledge-form-${card.id}`}>
                        <Field
                          label="Kind"
                          hint="A knowledge item, or a Skill the project wrote down."
                        >
                          {(p) => (
                            <NativeSelect
                              {...p}
                              value={pinner.kind}
                              onChange={(e) => {
                                // The kind chooses the LIST. Keeping the id
                                // across a kind change would carry a Knowledge
                                // item's identifier into the Skills registry.
                                const kind = e.target.value;
                                setPinner({ kind, id: "", version: "", pasted: false });
                                setVersions(null);
                                setVersionsError("");
                                void loadChoices(kind);
                              }}
                              data-testid="knowledge-kind"
                            >
                              <option value="topic">Knowledge item</option>
                              <option value="procedure">Skill</option>
                            </NativeSelect>
                          )}
                        </Field>

                        {/* THE LIST, not a retyped identifier. The console
                            already reads both Context Hub collections; asking an
                            operator to remember `ctx_01KYJ…` was asking them to
                            leave this screen to find it. */}
                        {choicesError ? (
                          <Status tone="error" data-testid="knowledge-choices-unavailable">
                            Couldn&apos;t read what this project has authored, so this is not a
                            list of everything there is. Paste an identifier below instead.{" "}
                            {choicesError}
                          </Status>
                        ) : null}

                        {choices !== null && choices.length > 0 && !pinner.pasted ? (
                          <Field
                            label={pinner.kind === "procedure" ? "Skill" : "Knowledge item"}
                            hint="Authored in the Context Hub. Choosing one pins the version it is at today."
                          >
                            {(p) => (
                              <NativeSelect
                                {...p}
                                value={pinner.id}
                                onChange={(e) => void chooseKnowledge(pinner.kind, e.target.value)}
                                data-testid="knowledge-pick"
                              >
                                <option value="">Paste an identifier instead…</option>
                                {choices.map((choice) => (
                                  <option key={choice.id} value={choice.id}>
                                    {choice.label}
                                  </option>
                                ))}
                              </NativeSelect>
                            )}
                          </Field>
                        ) : (
                          <Field
                            label="Knowledge id"
                            hint={
                              choices !== null && choices.length === 0
                                ? "This project has authored none of this kind yet — one is written in the Context Hub. An identifier can still be pasted for a platform-wide object."
                                : "Its identifier in the Context Hub. It must belong to this project, or be platform-wide."
                            }
                          >
                            {(p) => (
                              <Input
                                {...p}
                                value={pinner.id}
                                onChange={(e) => {
                                  const id = e.target.value;
                                  setPinner({ ...pinner, id, version: "" });
                                  setVersions(null);
                                  setVersionsError("");
                                }}
                                data-testid="knowledge-id"
                              />
                            )}
                          </Field>
                        )}
                        {pinner.pasted && choices !== null && choices.length > 0 ? (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              setPinner({ ...pinner, id: "", version: "", pasted: false });
                              setVersions(null);
                              setVersionsError("");
                            }}
                          >
                            Choose from the list instead
                          </Button>
                        ) : null}

                        {/* WHICH VERSION IS BEING PINNED IS SAID OUT LOUD. The
                            head is the default because it is what the reader
                            means by "this knowledge"; it is named, and every
                            earlier version stays choosable, because a pin that
                            silently follows the newest changes what the topic
                            says without a record. */}
                        {versions !== null && versions.length > 0 ? (
                          <Field
                            label="Version"
                            hint={`Version ${versions[0]} is the current head — that is what this pin defaults to.`}
                          >
                            {(p) => (
                              <NativeSelect
                                {...p}
                                value={pinner.version}
                                onChange={(e) => setPinner({ ...pinner, version: e.target.value })}
                                data-testid="knowledge-version"
                              >
                                {versions.map((n) => (
                                  <option key={n} value={String(n)}>
                                    {n === versions[0] ? `v${n} — current head` : `v${n}`}
                                  </option>
                                ))}
                              </NativeSelect>
                            )}
                          </Field>
                        ) : (
                          <Field
                            label="Version"
                            hint={
                              versionsError
                                ? `Couldn't read this object's versions, so no head is proposed. ${versionsError}`
                                : versions !== null && pinner.id.trim()
                                  ? "This object has no governed version yet — a version is what a pin names, and it is created by saving it in the Context Hub."
                                  : "An exact version number. A pin that follows the newest silently changes what the topic says."
                            }
                          >
                            {(p) => (
                              <Input
                                {...p}
                                type="number"
                                min={1}
                                value={pinner.version}
                                onChange={(e) => setPinner({ ...pinner, version: e.target.value })}
                                data-testid="knowledge-version"
                              />
                            )}
                          </Field>
                        )}
                        <Cluster>
                          <Button
                            type="button"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => setPinner(null)}
                          >
                            Cancel
                          </Button>
                          <Button
                            type="button"
                            disabled={busy || !pinner.id.trim() || !pinner.version.trim()}
                            onClick={() => bindKnowledge(card.id)}
                          >
                            Declare
                          </Button>
                        </Cluster>
                      </Stack>
                    ) : null}

                    {binder ? (
                      <Stack className="gap-2" data-testid={`binding-form-${card.id}`}>
                        {/* THE LIST, not a retyped identifier. Until 2026-08-18
                            no route enumerated Query Specs, so this asked an
                            operator to remember a `qsv_…`; the collection route
                            exists now and this reads it. */}
                        {querySpecsError ? (
                          <Status tone="error" data-testid="query-specs-unavailable">
                            Couldn&apos;t read this project&apos;s governed queries, so this is not
                            a list of everything there is. Paste a version id below instead.{" "}
                            {querySpecsError}
                          </Status>
                        ) : null}
                        {querySpecsTruncated ? (
                          <Status tone="warning" data-testid="query-specs-truncated">
                            This is the first page of this project&apos;s governed queries, not all
                            of them. A query that is not listed can still be pinned by pasting its
                            version id.
                          </Status>
                        ) : null}

                        {querySpecs !== null && querySpecs.length > 0 && !binder.pasted ? (
                          <Field
                            label="Query Spec"
                            hint="Authored in Analyze › Explore. Choosing one pins the version it is at today."
                          >
                            {(p) => (
                              <NativeSelect
                                {...p}
                                value={binder.specId}
                                onChange={(e) => chooseQuerySpec(e.target.value)}
                                data-testid="binding-spec"
                              >
                                <option value="">Paste a version id instead…</option>
                                {querySpecs.map((choice) => (
                                  <option
                                    key={choice.id}
                                    value={choice.id}
                                    // A head with no version has nothing to pin.
                                    // Shown, and refused: hiding it would read as
                                    // "that query does not exist".
                                    disabled={choice.versionId === null}
                                  >
                                    {choice.named ? choice.label : `${choice.label} (unnamed)`}
                                    {choice.versionId === null
                                      ? " — no version to pin yet"
                                      : choice.versionNumber !== null
                                        ? ` — v${choice.versionNumber}, current version`
                                        : " — current version"}
                                  </option>
                                ))}
                              </NativeSelect>
                            )}
                          </Field>
                        ) : (
                          <Field
                            label="Query Spec version"
                            hint={
                              querySpecs !== null && querySpecs.length === 0
                                ? "This project has authored no governed query yet — one is written in Analyze › Explore. A version id can still be pasted."
                                : "An exact version id. `latest` is refused: a pin that follows the newest silently changes what the topic answers."
                            }
                          >
                            {(p) => (
                              <Input
                                {...p}
                                value={binder.versionId}
                                onChange={(e) =>
                                  // `pasted` is set here too: the list is read
                                  // lazily, and a version typed while it was in
                                  // flight must not be swept away when it lands.
                                  setBinder({
                                    ...binder,
                                    specId: "",
                                    versionId: e.target.value,
                                    pasted: true,
                                  })
                                }
                                data-testid="binding-version"
                              />
                            )}
                          </Field>
                        )}
                        {binder.pasted && querySpecs !== null && querySpecs.length > 0 ? (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              setBinder({ ...binder, specId: "", versionId: "", pasted: false })
                            }
                          >
                            Choose from the list instead
                          </Button>
                        ) : null}

                        {/* WHICH VERSION IS BEING PINNED IS SAID OUT LOUD, and
                            it is the id that travels — not the spec's. */}
                        {binder.specId && binder.versionId ? (
                          <p data-testid="binding-pinned">
                            Pins <ObjectId value={binder.versionId} title="Version" />
                          </p>
                        ) : null}

                        <Field label="Role" hint="What this query does for the question.">
                          {(p) => (
                            <Input
                              {...p}
                              value={binder.role}
                              onChange={(e) => setBinder({ ...binder, role: e.target.value })}
                              data-testid="binding-role"
                            />
                          )}
                        </Field>
                        <Cluster>
                          <Button
                            type="button"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => setBinder(null)}
                          >
                            Cancel
                          </Button>
                          <Button
                            type="button"
                            disabled={busy || !binder.versionId.trim() || !binder.role.trim()}
                            onClick={() => bindQuery(card.id)}
                          >
                            Bind
                          </Button>
                        </Cluster>
                      </Stack>
                    ) : null}

                    {/* Story 75-5: the THIRD binding family, in the same panel
                        as the two beside it. What computes the answer, what
                        explains it, and what it may read and cross are three
                        sections of one question — splitting them into panels is
                        what made the explaining half look optional (52.3), and a
                        topic whose joins nobody declared is a topic an agent
                        guesses the joins of. */}
                    <TopicViewsPanel
                      projectId={projectId}
                      topicKey={card.id}
                      busy={busy}
                      setBusy={setBusy}
                    />
                  </Stack>
                ) : null}

                <Cluster className="mt-auto">
                  {card.comment_builder ? <Badge outline>Commentary</Badge> : null}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy || !projectId}
                    onClick={() =>
                      openQueries === card.id ? setOpenQueries(null) : loadBindings(card.id)
                    }
                  >
                    Queries
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy || !projectId}
                    onClick={() => openReword(card)}
                  >
                    Reword
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={busy || !projectId}
                    onClick={() => setPendingRetire(card)}
                  >
                    Retire
                  </Button>
                </Cluster>

                {/* The widget URI addresses the built bundle that renders this
                    question. It is a fact for whoever debugs a render, not a
                    label of the question, and as a chip it read like one — so it
                    is kept, named for what it is, and folded away. */}
                <Collapsible>
                  <CollapsibleTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      data-testid={`technical-${card.id}`}
                    >
                      Technical reference
                    </Button>
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <Stack className="gap-1">
                      <ObjectId value={card.id} title="Topic key" />
                      <ObjectId
                        value={card.widget_uri || null}
                        title="Widget URI"
                      />
                    </Stack>
                  </CollapsibleContent>
                </Collapsible>
              </Panel>
            );
          })}
        </section>
      ) : null}

      <Dialog open={editor !== null} onOpenChange={(open) => (open ? null : setEditor(null))}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editor?.mode === "add" ? "Add a topic" : "Reword this topic"}
            </DialogTitle>
          </DialogHeader>
          {editor ? (
            <Stack className="gap-2">
              <Field label="Key" hint="Lowercase, no spaces. It is how a caller asks for this topic.">
                {(p) => (
                  <Input
                    {...p}
                    value={editor.topicKey}
                    disabled={editor.mode === "reword"}
                    onChange={(e) => setEditor({ ...editor, topicKey: e.target.value })}
                    data-testid="topic-key"
                  />
                )}
              </Field>
              <Field label="Title">
                {(p) => (
                  <Input
                    {...p}
                    value={editor.title}
                    onChange={(e) => setEditor({ ...editor, title: e.target.value })}
                    data-testid="topic-title"
                  />
                )}
              </Field>
              <Field label="Question" hint="The one business question this topic answers.">
                {(p) => (
                  <Textarea
                    {...p}
                    value={editor.question}
                    onChange={(e) => setEditor({ ...editor, question: e.target.value })}
                    data-testid="topic-question"
                  />
                )}
              </Field>
              <Field
                label="Answers only from governed knowledge"
                hint="When its declared knowledge cannot be read, this topic refuses instead of answering with the computable half alone."
              >
                {(p) => (
                  <Checkbox
                    {...p}
                    checked={editor.requiresKnowledge}
                    onCheckedChange={(next) =>
                      setEditor({ ...editor, requiresKnowledge: next === true })
                    }
                    data-testid="topic-requires-knowledge"
                  />
                )}
              </Field>
              {/* THE CARDS THAT EXIST, not a remembered identifier. The server
                  refuses an unknown one as `unknown_base_template`
                  (`answerable_topics.py:1163`), so the only values worth
                  offering are the platform default set — read from the same
                  `/api/cards/templates` this page already calls, asked without a
                  project. The value in hand is always among the options, so
                  opening the form can never silently re-render a topic. */}
              {baseTemplates !== null && baseTemplates.length > 0 ? (
                <Field
                  label="Renders as"
                  hint="An existing card. A new visual composition ships with a release, not with a form."
                >
                  {(p) => (
                    <NativeSelect
                      {...p}
                      value={editor.baseTemplateId}
                      onChange={(e) => setEditor({ ...editor, baseTemplateId: e.target.value })}
                      data-testid="topic-base"
                    >
                      {baseTemplates.some((t) => t.id === editor.baseTemplateId)
                        ? null
                        : (
                          <option value={editor.baseTemplateId}>
                            {editor.baseTemplateId} — not in the platform set
                          </option>
                        )}
                      {baseTemplates.map((t) => (
                        <option key={t.id} value={t.id}>
                          {t.title || t.id}
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
              ) : (
                <Field
                  label="Renders as"
                  hint={
                    baseTemplatesError
                      ? `Couldn't read the cards this platform ships, so this is typed rather than chosen. ${baseTemplatesError}`
                      : "An existing card. A new visual composition ships with a release, not with a form."
                  }
                >
                  {(p) => (
                    <Input
                      {...p}
                      value={editor.baseTemplateId}
                      onChange={(e) => setEditor({ ...editor, baseTemplateId: e.target.value })}
                      data-testid="topic-base"
                    />
                  )}
                </Field>
              )}
            </Stack>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => setEditor(null)} disabled={busy}>
              Cancel
            </Button>
            <Button type="button" onClick={submitEditor} disabled={busy}>
              {editor?.mode === "add" ? "Add topic" : "Save new version"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* C-10: irreversible from this screen. Nothing here brings a retired
          question back -- creating it again on the same key is refused, and a new
          version leaves the head retired -- so the one click is confirmed. */}
      <Dialog
        open={pendingRetire !== null}
        onOpenChange={(open) => (open ? null : setPendingRetire(null))}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Retire this topic?</DialogTitle>
            <DialogDescription>
              {pendingRetire?.title || pendingRetire?.id} leaves this project&apos;s catalog:
              callers stop being offered it and no card answers it. Nothing is deleted, and
              the change is recorded — but this screen offers no way back.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setPendingRetire(null)}
              disabled={busy}
            >
              Keep it
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              onClick={() => pendingRetire && void retire(pendingRetire)}
            >
              Retire this topic
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
