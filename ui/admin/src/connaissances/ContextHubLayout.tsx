import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { cn } from "../lib/cn";
import type { GraphBundle, GraphNodeRow } from "../KnowledgeGraphPage";
import {
  Badge,
  Button,
  Cluster,
  ConfirmDialog,
  CopyButton,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  Input,
  NativeSelect,
  ObjectId,
  Panel,
  Stack,
  Status,
  Textarea,
  DESTRUCTIVE_ROW_ACTION,
  FILL_BG,
  TONE_BORDER,
  TONE_SURFACE,
} from "../ui";

import {
  createBusinessLink,
  deleteBusinessLink,
  listCanonicalFields,
  loadContextHub,
  previewBusinessPath,
  BUSINESS_TARGET_TYPES,
} from "./businessTaxonomyApi";
/* THE IDENTITY WRITES LEFT THIS MODULE ON 2026-08-25 AND THEY DID NOT COME BACK
 * HERE. `createBusinessDomain`, `createBusinessClassification` and the two
 * `update*` helpers called the four legacy doors, which refuse since the
 * cutover ratified in `docs/product-architecture/governance.md` — and they are
 * deleted from `businessTaxonomyApi.ts` now (audit 03, 2026-08-25: zero
 * callers). Creating, renaming and
 * archiving a Business Domain or a classification are acts on the IDENTITY, and
 * the authority that holds it is Master Data — so this screen calls the same
 * commands the Governance dialog calls. Two doors, one writer, which is what
 * `context-hub.md` ratified on 2026-08-17 and what a second write path would
 * undo.
 *
 * The LINK helpers stay: a link is not an identity, the convergence does not
 * move it, and `create_link` / `retire_link` are still the writers of it. */
import {
  createBusinessIdentity,
  mintCommandKey,
  runNodeCommand,
} from "../governance/masterDataApi";
import {
  MISSING_REASON_SENTENCE,
  isStaleVersionRefusal,
  masterDataRefusalSentence,
} from "../governance/masterDataRefusals";
import type {
  BusinessClassification,
  BusinessDomain,
  BusinessLink,
  BusinessPath,
  BusinessTargetType,
  TaxonomyType,
} from "./businessTaxonomyApi";

/* The three repeated shapes of this screen, named once.
 *
 * They were rules in a screen-local stylesheet, which is the thing the single
 * visual vocabulary exists to prevent: a sixth spelling of "small uppercase
 * caption" that drifts from the other five the first time a token moves. As
 * constants they are still ONE spelling, and they are made of console tokens
 * rather than of `var(--muted)` and a hardcoded 10px. */
const KICKER = "block text-caption font-bold uppercase tracking-[0.06em] text-text-secondary";
/** The label/value pair inside a tree row: it must ellipsize, never wrap. */
const ROW_TEXT = "flex min-w-0 flex-col overflow-hidden";
const ROW_TITLE = "truncate font-semibold text-text";
const ROW_META = "truncate text-caption text-text-secondary";

export interface SelectedTaxonomy {
  type: TaxonomyType;
  id: string;
}

export interface ContextHubContentContext {
  graph: GraphBundle | null;
  selected: SelectedTaxonomy | null;
  selectedNode: BusinessDomain | BusinessClassification | null;
  domains: BusinessDomain[];
  classifications: BusinessClassification[];
  links: BusinessLink[];
  selectedLinks: BusinessLink[];
  selectTaxonomy: (selection: SelectedTaxonomy | null) => void;
  refresh: () => Promise<void>;
}

type DialogState = "domain" | "layer" | "link" | "edit" | "status" | null;
/** What the link dialog needs of a candidate target, whatever store it came
 *  from: an id to store and a name to recognize it by. */
interface TargetOption {
  id: string;
  title: string;
}
type HubState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | {
      status: "ready";
      domains: BusinessDomain[];
      classifications: BusinessClassification[];
      links: BusinessLink[];
      graph: GraphBundle;
    };

export default function ContextHubLayout({
  projectId,
  activeView,
  onNavigate,
  renderContent,
  editable = true,
  title = "Context Hub",
  showViewSwitch = true,
}: {
  projectId: string;
  activeView: "library" | "skills" | "graph";
  onNavigate: (view: "library" | "skills" | "graph") => void;
  renderContent: (context: ContextHubContentContext) => ReactNode;
  editable?: boolean;
  title?: string;
  showViewSwitch?: boolean;
}) {
  const [state, setState] = useState<HubState>({ status: "loading" });
  const [selected, setSelected] = useState<SelectedTaxonomy | null>(null);
  const [search, setSearch] = useState("");
  const [dialog, setDialog] = useState<DialogState>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [classificationType, setClassificationType] = useState("product_line");
  const [targetType, setTargetType] = useState<BusinessTargetType>("target_field");
  const [targetId, setTargetId] = useState("");
  const [relationType, setRelationType] = useState("explains");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // A stale-version refusal ENDS this submission. The sentence tells the curator
  // to reopen the entry and read what the other person wrote; a Save button that
  // still posts would contradict it, and every further press would send the same
  // base to be refused again.
  const [staleRefusal, setStaleRefusal] = useState(false);
  /** Minted on the first submission of a dialog, reused by its retries. */
  const [commandKey, setCommandKey] = useState<string | null>(null);
  const [path, setPath] = useState<BusinessPath | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [pathLoading, setPathLoading] = useState(false);
  /* Unlinking was the ONE governed write of this surface that asked nothing: it
     fired on the click and sent a hardcoded sentence as its reason, while every
     other change here is refused without one. A link is the whole evidence path
     between a business key and a governed object -- removing it silently is the
     write that most deserves the confirmation. */
  const [pendingUnlink, setPendingUnlink] = useState<BusinessLink | null>(null);
  const [unlinkReason, setUnlinkReason] = useState("");
  const [unlinkError, setUnlinkError] = useState<string | null>(null);
  const [unlinking, setUnlinking] = useState(false);
  /* What the governed-change dialog looked like the moment it OPENED. Closing
     it costs something as soon as anything was typed, and the backdrop and
     Escape used to discard silently -- the same defect `SkillEditorDrawer` was
     repaired for this wave. A baseline rather than a boolean because `edit`
     opens PREFILLED: a flag set on the first keystroke would call an untouched
     edit form dirty the moment a curator clicked into it and back out. */
  const [dialogBaseline, setDialogBaseline] = useState("");
  const [discardOpen, setDiscardOpen] = useState(false);
  /* Three states, never two: not read yet, read, and READ AND FAILED. An
     unreadable registry that fell back to an empty list would tell the operator
     the vocabulary is empty, and the empty vocabulary has its own instruction. */
  const [canonicalFields, setCanonicalFields] =
    useState<{ status: "idle" | "loading" | "error" } | { status: "ready"; rows: TargetOption[] }>({
      status: "idle",
    });

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
      const data = await loadContextHub(projectId);
      setState({
        status: "ready",
        domains: data.taxonomy.domains,
        classifications: data.taxonomy.classifications,
        links: data.links,
        graph: data.graph,
      });
      setSelected((current) =>
        current &&
        (data.taxonomy.domains.some((item) => item.id === current.id) ||
          data.taxonomy.classifications.some((item) => item.id === current.id))
          ? current
          : null,
      );
    } catch (error) {
      setState({ status: "error", message: error instanceof Error ? error.message : "Context Hub is unavailable." });
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const domains = state.status === "ready" ? state.domains : [];
  const classifications = state.status === "ready" ? state.classifications : [];
  const links = state.status === "ready" ? state.links : [];
  const graph = state.status === "ready" ? state.graph : null;
  const selectedDomain = selected?.type === "business_domain"
    ? domains.find((item) => item.id === selected.id) ?? null
    : null;
  const selectedClassification = selected?.type === "business_classification"
    ? classifications.find((item) => item.id === selected.id) ?? null
    : null;
  const selectedNode = selectedDomain ?? selectedClassification;
  const selectedArchived = selectedNode?.status === "archived";
  const selectedLinks = selected ? links.filter((link) => link.taxonomy_id === selected.id) : [];
  const childMap = useMemo(() => {
    const map = new Map<string, BusinessClassification[]>();
    for (const item of classifications) {
      const key = item.parent_id ?? item.domain_id;
      map.set(key, [...(map.get(key) ?? []), item]);
    }
    for (const rows of map.values()) rows.sort((a, b) => a.name.localeCompare(b.name));
    return map;
  }, [classifications]);
  const normalizedSearch = search.trim().toLowerCase();
  const visibleDomains = domains.filter((domain) => {
    if (!normalizedSearch) return true;
    if (`${domain.name} ${domain.description}`.toLowerCase().includes(normalizedSearch)) return true;
    return classifications.some(
      (item) => item.domain_id === domain.id && `${item.name} ${item.description}`.toLowerCase().includes(normalizedSearch),
    );
  });
  /* A canonical field is named by an ID nobody memorizes, and it is not a graph
     node until a link already points at it -- so the graph could never suggest
     the first one. It is read from the registry Governance reads (AI-298). */
  useEffect(() => {
    if (dialog !== "link" || targetType !== "canonical_field") return;
    let cancelled = false;
    setCanonicalFields({ status: "loading" });
    listCanonicalFields(projectId)
      .then((payload) => {
        if (cancelled) return;
        setCanonicalFields({
          status: "ready",
          rows: (payload.fields ?? []).map((field) => ({
            id: field.id,
            // The scope travels with the name: a project and the platform may
            // each carry a `revenue_net`, and the list has to say which is which.
            title: `${field.canonical_name} (${field.scope})`,
          })),
        });
      })
      .catch(() => {
        if (!cancelled) setCanonicalFields({ status: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [dialog, targetType, projectId]);

  /* Every node the bundle carries, by id. The link cards read it to show a
     governed object's NAME instead of the opaque id the row stores: a card
     headlined `ds_01JAB...` names nothing, and the title is already in memory --
     `graph_projection` synthesizes a node, title read from its owner, for every
     target a link points at (`_owned_target_nodes`, Story 49.6). */
  const nodeTitleById = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of graph?.nodes ?? []) map.set(node.id, node.title);
    return map;
  }, [graph]);

  /* Suggestions come from the graph bundle for EVERY type whose nodes it
     carries -- `report_view`, `datastream`, `semantic_view` and
     `semantic_concept` included, since `graph_projection` emits a node for each
     one a link already reaches. The old filter carried a `business_` guard that
     could never fire (no `BUSINESS_TARGET_TYPE` starts with it) and the comment
     beside it still claimed three types were not graph nodes at all -- which is
     how four of the nine target types kept sending the operator to paste an id
     while the console already held the list. */
  const targetOptions = useMemo<TargetOption[]>(() => {
    if (targetType === "canonical_field") {
      return canonicalFields.status === "ready" ? canonicalFields.rows : [];
    }
    return (graph?.nodes ?? [])
      .filter((node) => node.node_type === targetType)
      .map((node: GraphNodeRow) => ({ id: node.id, title: node.title }));
  }, [graph, targetType, canonicalFields]);
  /* A type whose nodes the bundle does not carry keeps the honest sentence: the
     field still accepts the id, because the server validates it against its
     owner. `report_view` adds the SHAPE of its id, which is the one thing a
     person cannot guess -- it is `module/report_id`, not an opaque token. */
  const targetPlaceholder = useMemo(() => {
    if (targetType === "canonical_field") {
      // An unreadable registry is not an empty one, and neither is a read still
      // in flight: each says its own sentence, and only the third names a paste.
      if (canonicalFields.status === "error") {
        return "The canonical vocabulary could not be read - reopen this dialog to try again";
      }
      if (canonicalFields.status !== "ready") return "Loading the canonical vocabulary...";
      if (targetOptions.length === 0) {
        return "No canonical field is declared yet - declare one from the source that feeds it";
      }
      return "Search by name or ID";
    }
    // Gated on the graph having ARRIVED, not on the current count. Keying it to
    // `targetOptions.length` alone made the prompt flicker through "none are
    // suggested" on every open, before the fetch resolved -- an honest sentence
    // said at a moment when it was not yet true.
    if (graph && targetOptions.length === 0) {
      if (targetType === "report_view") {
        return "Paste the report view ID as connector/report_id - none are suggested here";
      }
      return `Paste the ${targetType.replaceAll("_", " ")} ID - none are suggested here`;
    }
    return "Search by name or ID";
  }, [targetType, targetOptions, graph, canonicalFields]);

  /** Every field the dialog can carry, in one comparable value. */
  const formSignature = (parts: readonly string[]) => JSON.stringify(parts);

  const resetDialog = (next: DialogState) => {
    const nextName = next === "edit" ? selectedNode?.name ?? "" : "";
    const nextDescription = next === "edit" ? selectedNode?.description ?? "" : "";
    const nextClassificationType =
      next === "edit" && selectedClassification
        ? selectedClassification.classification_type
        : "product_line";
    const nextTargetType: BusinessTargetType = activeView === "skills" ? "procedure" : "target_field";
    const nextRelationType = activeView === "skills" ? "applies_to" : "explains";
    setDialog(next);
    setName(nextName);
    setDescription(nextDescription);
    setClassificationType(nextClassificationType);
    setTargetType(nextTargetType);
    setTargetId("");
    setRelationType(nextRelationType);
    setReason("");
    setActionError(null);
    setStaleRefusal(false);
    // A new dialog is a new act, so it gets a new key on its first submission.
    setCommandKey(null);
    setDiscardOpen(false);
    setDialogBaseline(
      formSignature([nextName, nextDescription, nextClassificationType, nextTargetType, "", nextRelationType, ""]),
    );
  };

  const dialogDirty =
    dialog !== null &&
    formSignature([name, description, classificationType, targetType, targetId, relationType, reason]) !==
      dialogBaseline;

  /* Escape, the backdrop and Cancel all arrive here. What was typed is nowhere
     else, so the question is asked before it is thrown away -- and the safe path
     is cancelling the question, exactly as `SkillEditorDrawer` argues. An
     untouched dialog closes immediately: a confirmation nobody needs is a
     confirmation people learn to dismiss without reading. */
  const requestCloseDialog = () => {
    if (saving) return;
    if (dialogDirty) {
      setDiscardOpen(true);
      return;
    }
    setDialog(null);
  };

  const taxonomyContext = () => {
    if (!selected) return null;
    const classification = selectedClassification;
    return {
      taxonomy_type: selected.type,
      taxonomy_id: selected.id,
      domain_id: selectedDomain?.id ?? classification?.domain_id ?? "",
      parent_id: classification?.id ?? null,
    };
  };

  const submit = async () => {
    const context = taxonomyContext();
    if (!dialog || !reason.trim()) {
      setActionError(MISSING_REASON_SENTENCE);
      return;
    }
    setSaving(true);
    setActionError(null);
    // ONE KEY PER SUBMISSION, reused by every retry of it: the authority answers
    // 428 without one, and a key minted per attempt would let a client timeout
    // mint a second identity.
    const key = commandKey ?? mintCommandKey("md-hub");
    setCommandKey(key);
    try {
      if (dialog === "domain") {
        await createBusinessIdentity(
          projectId,
          { kind: "business_domain", name, description, reason },
          key,
        );
      } else if (dialog === "layer" && context) {
        await createBusinessIdentity(
          projectId,
          {
            kind: "business_classification",
            name,
            description,
            reason,
            classification_type: classificationType,
            domain_node_id: context.domain_id,
            parent_node_id: context.parent_id,
          },
          key,
        );
      } else if (dialog === "link" && context) {
        await createBusinessLink(projectId, {
          taxonomy_type: context.taxonomy_type,
          taxonomy_id: context.taxonomy_id,
          target_type: targetType,
          target_id: targetId,
          relation_type: relationType,
          reason,
        });
      } else if (dialog === "edit" && selectedNode && selected) {
        // A RENAME IS ONE COMMAND ON THE IDENTITY, and it carries the three
        // fields that live in the same published version: the name, the
        // description, and — for a classification — the word the organization
        // chose for its type. Sending only the label would publish a version
        // that erased the other two.
        //
        // AND IT STATES THE BASE IT RENAMES FROM (`governance.md`, amendment of
        // 2026-08-30). `expected_version` is the AUTHORITY's current revision,
        // by id, as the list gave it — not the `version_number` printed beside
        // the entry, which unions two ledgers that count independently and would
        // compare two counters that cannot disagree. `null` is a base too: it
        // says this reader saw no published revision, and the authority holds
        // the rename to exactly that.
        await runNodeCommand(
          projectId,
          selected.id,
          {
            action: "rename",
            label: name,
            description,
            reason,
            expected_version: selectedNode.current_version_id,
            ...(selected.type === "business_classification"
              ? { classification_type: classificationType }
              : {}),
          },
          key,
        );
      } else if (dialog === "status" && selectedNode && selected) {
        // Archiving is guarded by the authority: it refuses while a live
        // Semantic Model version, a live business link or a registered consumer
        // still names the identity, and the refusal travels with the list.
        await runNodeCommand(
          projectId,
          selected.id,
          { action: selectedArchived ? "restore" : "archive", reason },
          key,
        );
      }
      setDialog(null);
      await load();
    } catch (error) {
      // A 409 is not a failure to save, it is a REFUSAL to overwrite, and the
      // two call for opposite moves: "could not be saved" sends the curator to
      // press Save again on the same stale edit.
      //
      // The dialog STAYS OPEN, and the Hub underneath is deliberately NOT
      // reloaded. Reloading would refresh `selectedNode.version_number`, so the
      // next Save would carry the newer version and quietly overwrite the very
      // change that was just protected -- a refusal that repairs itself into
      // the overwrite it refused. Held as it is, every further Save is refused
      // the same way until the curator closes and reopens, which is the act of
      // reading what the other person wrote.
      //
      // WHICH SENTENCE, FOR WHICH CODE, IS NOT DECIDED HERE. The authority
      // refuses under four named codes, each naming its own repair, and the
      // Governance Master Data workbench runs the SAME commands through the same
      // door -- so the mapping lives in `governance/masterDataRefusals.ts` and
      // both screens read one alert language. Collapsing the four into the
      // stale-version sentence would tell the curator that somebody else edited
      // the entry: false, and it hides the gesture that works.
      setActionError(masterDataRefusalSentence(error));
      setStaleRefusal(isStaleVersionRefusal(error));
    } finally {
      setSaving(false);
    }
  };

  const askUnlink = (link: BusinessLink) => {
    setPendingUnlink(link);
    setUnlinkReason("");
    setUnlinkError(null);
  };

  const closeUnlink = () => {
    setPendingUnlink(null);
    setUnlinkReason("");
    setUnlinkError(null);
  };

  /* The reason travels to the server, so the audit trail carries WHY the route
     was cut. "Removed from the Context Hub" -- the sentence this used to send --
     restates the endpoint and answers nothing, which is the same as no reason at
     all. The refusal wording is the one `submit` already uses: one surface, one
     sentence for the same missing thing. */
  const unlink = async () => {
    const link = pendingUnlink;
    if (!link) return;
    if (!unlinkReason.trim()) {
      setUnlinkError(MISSING_REASON_SENTENCE);
      return;
    }
    setUnlinking(true);
    setUnlinkError(null);
    try {
      await deleteBusinessLink(projectId, link.id, unlinkReason);
      closeUnlink();
      await load();
    } catch (error) {
      setUnlinkError(error instanceof Error ? error.message : "The link could not be removed.");
    } finally {
      setUnlinking(false);
    }
  };

  const openPath = async (link: BusinessLink) => {
    setPath(null);
    setPathError(null);
    setPathLoading(true);
    try {
      setPath(await previewBusinessPath(projectId, link.target_type, link.target_id, link.taxonomy_id));
    } catch (error) {
      setPathError(error instanceof Error ? error.message : "The path could not be resolved.");
    } finally {
      setPathLoading(false);
    }
  };

  /* The dashed indent guide is the whole point of this element: it is what makes
     a nested business layer legible AS nested. The rule and the element the
     stylesheet era left behind were spelled differently, so it had never once
     rendered -- the hierarchy was invisible on the one surface whose job is to
     show it. It now lives on the element, in tokens, where nothing can silently
     unhook it.
     The nesting is structural (a branch renders its children inside itself), so
     the offset compounds on its own and `depth` no longer has to be carried. */
  const renderBranches = (parentId: string): ReactNode =>
    (childMap.get(parentId) ?? []).map((item) => (
      <div key={item.id} className="mt-0.5 ml-[18px] flex flex-col gap-1 border-l border-dashed border-divider-base pl-2.5">
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-2 rounded-sm border px-2.5 py-2 text-left text-ui transition-colors",
            selected?.id === item.id
              ? `${TONE_BORDER.info} ${TONE_SURFACE.info}`
              : "border-transparent hover:bg-background-light",
            item.status === "archived" && "opacity-60",
          )}
          aria-label={`${item.name}${item.status === "archived" ? ", Archived" : ""}`}
          onClick={() => setSelected({ type: "business_classification", id: item.id })}
        >
          <span aria-hidden className={cn("size-1.5 shrink-0 rounded-pill", FILL_BG.info)} />
          <span className={cn(ROW_TEXT, "gap-px")}>
            <strong className={cn(ROW_TITLE, "text-caption")}>{item.name}</strong>
            <small className={ROW_META}>{item.classification_type.replaceAll("_", " ")} · v{item.version_number}</small>
          </span>
        </button>
        {renderBranches(item.id)}
      </div>
    ));

  const contentContext: ContextHubContentContext = {
    graph,
    selected,
    selectedNode,
    domains,
    classifications,
    links,
    selectedLinks,
    selectTaxonomy: setSelected,
    refresh: load,
  };
  const contentSurface = (
    <div
      className={cn(
        "w-full rounded-large border border-divider-base bg-surface-light",
        activeView === "graph" ? "min-h-[520px] overflow-hidden" : "min-h-[400px] p-5",
      )}
    >
      {renderContent(contentContext)}
    </div>
  );

  return (
    <Stack className="w-full text-text">
      <Panel className="flex flex-wrap items-start justify-between gap-6">
        <div className="min-w-0">
          {/* The eyebrow keeps what it always was — an uppercase, letter-spaced
              caption in the recommending colour — said in tokens instead of a
              hardcoded 11px and `var(--rose)`. */}
          <span className="inline-block text-caption font-bold uppercase tracking-[0.05em] text-primary">
            Governed business context
          </span>
          <h1 className="mt-1 mb-1.5 font-display text-h2 font-h2 leading-tight text-text">{title}</h1>
          <p className="m-0 max-w-[60ch] text-ui leading-[var(--body-line-height)] text-text-secondary">
            Connect business meaning, agent skills and reporting views through versioned evidence paths.
          </p>
        </div>
        <Cluster
          className="shrink-0 gap-4 rounded-control border border-divider-base bg-background-light px-4 py-2.5"
          aria-label={`${title} metrics`}
        >
          {[
            [domains.length, "domains"],
            [classifications.length, "layers"],
            [links.length, "direct links"],
          ].map(([count, noun]) => (
            <span key={String(noun)} className="flex items-center gap-1 text-caption text-text-secondary">
              <strong className="font-numeric text-ui font-bold text-text">{count}</strong> {noun}
            </span>
          ))}
        </Cluster>
      </Panel>

      {showViewSwitch ? (
        <div
          className="flex w-fit items-center gap-1.5 rounded-control border border-divider-base bg-background-light p-1"
          role="tablist"
          aria-label="Context Hub views"
        >
          {([
            ["graph", "Knowledge Graph"],
            ["library", "Knowledge Library"],
            ["skills", "Skills Registry"],
          ] as const).map(([view, label]) => (
            <button
              key={view}
              type="button"
              role="tab"
              aria-selected={activeView === view}
              onClick={() => onNavigate(view)}
              className={cn(
                "rounded-sm px-4 py-2 text-ui font-semibold transition-colors",
                activeView === view
                  ? "bg-surface-light text-text shadow-xs"
                  : "text-text-secondary hover:text-text",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      ) : null}

      {/* 900px, not a default breakpoint: the rail needs its 320px before the
          content column becomes unusable, and that is where it happens. */}
      <div className="grid min-h-[500px] w-full grid-cols-1 gap-5 [@media(min-width:900px)]:grid-cols-[320px_1fr]">
        <aside
          className="flex flex-col gap-4 overflow-hidden rounded-large border border-divider-base bg-surface-light p-5"
          aria-label="Business taxonomy"
        >
          <div className="flex items-center justify-between gap-3">
            <div>
              <span className={KICKER}>MDM taxonomy</span>
              <h2 className="m-0 font-display text-h3 font-h3 text-text">Business map</h2>
            </div>
            {editable ? (
              <Button type="button" variant="secondary" size="icon-sm" onClick={() => resetDialog("domain")} aria-label="Add business domain">+</Button>
            ) : null}
          </div>
          <label className="flex flex-col gap-1">
            <span className="text-caption font-semibold text-text-secondary">Search taxonomy</span>
            <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Domain or layer" />
          </label>
          {state.status === "loading" && <p className="my-2 text-ui text-text-secondary" role="status">Loading business taxonomy…</p>}
          {/* `Status` block already carries `role="alert"` for the error tone —
              the hand-rolled banner it replaces declared it by hand, one screen
              at a time. */}
          {state.status === "error" && (
            <Status
              tone="error"
              as="block"
              action={<Button type="button" variant="secondary" size="sm" onClick={() => void load()}>Retry</Button>}
            >
              {state.message}
            </Status>
          )}
          {/* Two different nothings, and they were saying the same sentence. A
              project with no taxonomy at all read "No matching business layer."
              -- the sentence a query that matched nothing earns -- under a box
              nobody had typed in, with a "+" as the only way forward. An empty
              list says what a Business Domain is for and names the gesture that
              fills it; a fruitless search keeps the sentence it earned. */}
          {state.status === "ready" && domains.length === 0 && (
            <EmptyState
              title="No business domain yet"
              description="A Business Domain is the top of this map — the area of the business a person works in, like Retail or Subscriptions. Knowledge, Skills, Datastreams and reporting views hang off it, which is what makes them findable together."
              action={
                editable ? (
                  <Button type="button" onClick={() => resetDialog("domain")}>
                    Create your first business domain
                  </Button>
                ) : null
              }
            />
          )}
          {state.status === "ready" && domains.length > 0 && visibleDomains.length === 0 && (
            <p className="my-2 text-ui text-text-secondary">No matching business layer.</p>
          )}
          <div className="flex max-h-[calc(100vh-340px)] flex-col gap-2 overflow-y-auto pr-1">
            {visibleDomains.map((domain) => (
              <div className="flex flex-col gap-1" key={domain.id}>
                <button
                  type="button"
                  aria-label={`${domain.name}${domain.status === "archived" ? ", Archived" : ""}`}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-control border p-2.5 text-left transition-colors",
                    selected?.id === domain.id
                      ? "border-primary bg-primary-container"
                      : "border-divider-base bg-background-light hover:border-primary",
                    domain.status === "archived" && "opacity-60",
                  )}
                  onClick={() => setSelected({ type: "business_domain", id: domain.id })}
                >
                  <span aria-hidden className="inline-flex size-7 shrink-0 items-center justify-center rounded-sm bg-primary-container text-caption font-bold text-primary">
                    {domain.name.slice(0, 2).toUpperCase()}
                  </span>
                  <span className={cn(ROW_TEXT, "gap-0.5")}>
                    <strong className={cn(ROW_TITLE, "text-ui")}>{domain.name}</strong>
                    <small className={ROW_META}>{(childMap.get(domain.id) ?? []).length} top-level layers · v{domain.version_number}</small>
                  </span>
                </button>
                {renderBranches(domain.id)}
              </div>
            ))}
          </div>
        </aside>

        <main className="flex w-full min-w-0 flex-col gap-5">
          {activeView === "graph" && contentSurface}

          {/* The bar and its actions in the console's own vocabulary. One rose
              button, and it is the recommended next step -- `Link resource`,
              the gesture this whole surface exists for. */}
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-large border border-divider-base bg-surface-light px-6 py-5">
            <div>
              <span className={KICKER}>Active business key</span>
              <h2 className="mt-0.5 mb-1 font-display text-h3 font-h3 text-text">
                {selectedNode?.name ?? "Select a business domain"}
              </h2>
              <p className="m-0 text-caption text-text-secondary">
                {selectedNode?.description || "Choose a domain or classification to govern its connections."}
              </p>
            </div>
            {editable ? (
              <Cluster className="shrink-0 flex-nowrap">
                <Button type="button" variant="secondary" size="sm" onClick={() => resetDialog("edit")} disabled={!selected || selectedArchived}>Edit</Button>
                <Button type="button" variant="secondary" size="sm" onClick={() => resetDialog("status")} disabled={!selected}>{selectedArchived ? "Restore" : "Archive"}</Button>
                <Button type="button" variant="secondary" size="sm" onClick={() => resetDialog("layer")} disabled={!selected || selectedArchived}>Add layer</Button>
                <Button type="button" size="sm" onClick={() => resetDialog("link")} disabled={!selected || selectedArchived}>Link resource</Button>
              </Cluster>
            ) : null}
          </div>

          {selected && (
            <div className="flex flex-col gap-3 rounded-large border border-divider-base bg-surface-light px-6 py-5" aria-label="Governed resource links">
              <span className={KICKER}>Traceable routes</span>
              {selectedLinks.length === 0 ? (
                <EmptyState
                  title="No resource is linked to this business key yet"
                  description="Knowledge, Skills, Datastreams and reporting views become findable together once they hang off a key. A link is made from the object's own screen."
                />
              ) : selectedLinks.map((link) => {
                // The name if the bundle knows it, the id if it does not -- and
                // then the id is shown AS an id, in mono, so nobody reads an
                // unresolved token as a title somebody chose.
                const resolved = nodeTitleById.get(link.target_id);
                return (
                  <article key={link.id} className="flex items-center gap-3.5 rounded-control border border-divider-base bg-background-light px-4 py-3">
                    {/* The origin is a label naming a kind, which is what a
                        `Badge` is — the screen-local pill it replaces re-spelled
                        the tone, the radius and the uppercase by hand. */}
                    <Badge tone="info" className="uppercase">direct</Badge>
                    <div className="min-w-0">
                      <strong className="text-ui font-semibold text-text">
                        {resolved ?? (
                          // `ObjectId` is the console's one rendering of an
                          // immutable identifier: mono, demoted, full value in
                          // the title. A screen-local `<code>` rule was a second
                          // spelling of a decision the library already carries.
                          <ObjectId value={link.target_id} title={link.target_type.replaceAll("_", " ")} />
                        )}
                      </strong>
                      <small className="block truncate text-caption text-text-secondary">
                        {link.target_type.replaceAll("_", " ")} · {link.relation_type.replaceAll("_", " ")}
                      </small>
                    </div>
                    <Button type="button" variant="secondary" size="sm" className="ml-auto" onClick={() => void openPath(link)}>View path</Button>
                    {editable ? (
                      // A row action, not the page's recommendation: bordered and
                      // error-coloured rather than a solid red block, which is
                      // what `destructive` would put in every link row.
                      <Button type="button" variant="secondary" size="sm" className={DESTRUCTIVE_ROW_ACTION} onClick={() => askUnlink(link)}>Unlink</Button>
                    ) : null}
                  </article>
                );
              })}
            </div>
          )}

          {activeView !== "graph" && contentSurface}
        </main>
      </div>

      {/* The governed change, on the console's shared dialog. The hand-rolled
          backdrop it replaces re-declared the overlay, the card, the field
          geometry and the button row -- five screen-local spellings of
          decisions the library already carries, and one of them (the backdrop's
          `onMouseDown`) discarded a typed form without asking.
          Every close gesture Radix owns -- Escape, the overlay, the X -- comes
          back through `onOpenChange`, so there is ONE place that decides
          whether closing is free. */}
      <Dialog
        open={editable && dialog !== null}
        onOpenChange={(open) => { if (!open) requestCloseDialog(); }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto" aria-describedby="governed-change-purpose">
          <DialogHeader>
            <span className={KICKER}>Governed change</span>
            <DialogTitle>
              {dialog === "domain" ? "Add business domain"
                : dialog === "layer" ? "Add descriptive layer"
                  : dialog === "link" ? "Link governed resource"
                    : dialog === "edit" ? `Edit ${selectedNode?.name ?? "business key"}`
                      : `${selectedArchived ? "Restore" : "Archive"} ${selectedNode?.name ?? "business key"}`}
            </DialogTitle>
            {/* The `status` dialog's own copy — it used to be a paragraph in the
                body under a screen-local class, and it is exactly what a dialog
                description is for. */}
            <DialogDescription id="governed-change-purpose">
              {dialog === "status"
                ? selectedArchived
                  ? "Restore this business key so it can be edited and linked again."
                  : "Archive this business key without deleting its history or evidence paths."
                : "This change is versioned, and the reason you give is kept with it."}
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-4">
            {(dialog === "domain" || dialog === "layer" || dialog === "edit") && <>
              <label className="flex flex-col gap-1.5 text-label font-label text-text">Name
                <Input value={name} onChange={(event) => setName(event.target.value)} />
              </label>
              {(dialog === "layer" || (dialog === "edit" && selected?.type === "business_classification")) && (
                <label className="flex flex-col gap-1.5 text-label font-label text-text">Layer type
                  <Input value={classificationType} onChange={(event) => setClassificationType(event.target.value)} placeholder="product_line" />
                </label>
              )}
              <label className="flex flex-col gap-1.5 text-label font-label text-text">Description
                <Textarea value={description} onChange={(event) => setDescription(event.target.value)} rows={3} />
              </label>
            </>}

            {dialog === "link" && <>
              <label className="flex flex-col gap-1.5 text-label font-label text-text">Resource type
                <NativeSelect value={targetType} onChange={(event) => { setTargetType(event.target.value as BusinessTargetType); setTargetId(""); }}>
                  {BUSINESS_TARGET_TYPES.map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}
                </NativeSelect>
              </label>
              {/* The datalist sits OUTSIDE the label on purpose: nested in it,
                  every suggestion's text joined the field's accessible name, so
                  a screen reader announced the whole vocabulary as the name of
                  one input. It was invisible while the list was empty. */}
              <label className="flex flex-col gap-1.5 text-label font-label text-text">Resource
                <Input list="business-target-options" value={targetId} onChange={(event) => setTargetId(event.target.value)} placeholder={targetPlaceholder} />
              </label>
              <datalist id="business-target-options">{targetOptions.map((option) => <option key={option.id} value={option.id}>{option.title}</option>)}</datalist>
              <label className="flex flex-col gap-1.5 text-label font-label text-text">Relationship
                <NativeSelect value={relationType} onChange={(event) => setRelationType(event.target.value)}>
                  <option value="explains">explains</option>
                  <option value="owns">owns</option>
                  <option value="applies_to">applies to</option>
                  <option value="uses">uses</option>
                </NativeSelect>
              </label>
            </>}

            <label className="flex flex-col gap-1.5 text-label font-label text-text">Reason
              <Textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={2} placeholder="Why this governed change is needed" />
            </label>
            {/* A `Status` block already carries `role="alert"` on the error
                tone: the refusal has to be announced, and a 409 read silently is
                a 409 the curator presses Save through. */}
            {actionError && <Status tone="error" as="block">{actionError}</Status>}
          </div>

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={requestCloseDialog}>Cancel</Button>
            <Button
              type="button"
              disabled={saving || staleRefusal || ((dialog === "domain" || dialog === "layer" || dialog === "edit") && !name.trim()) || (dialog === "link" && !targetId.trim())}
              onClick={() => void submit()}
            >
              {saving ? "Saving..." : "Save change"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* What was typed is nowhere else. The backdrop used to throw it away on
          a stray click, which is the defect `SkillEditorDrawer` was repaired for
          in this same wave -- so it is repaired here the same way, with the same
          component and the same safe path: cancelling keeps the work. */}
      <ConfirmDialog
        open={discardOpen}
        onOpenChange={(open) => { if (!open) setDiscardOpen(false); }}
        title="Discard changes?"
        description="This governed change has not been saved. Closing now loses what you typed — cancel and use Save change to keep it."
        confirmLabel="Discard changes"
        destructive
        onConfirm={() => { setDiscardOpen(false); setDialog(null); }}
        data-testid="context-hub-discard-confirm"
        cancelTestId="context-hub-discard-cancel"
        confirmTestId="context-hub-discard-accept"
      />
      <ConfirmDialog
        open={editable && pendingUnlink !== null}
        onOpenChange={(open) => { if (!open) closeUnlink(); }}
        title={
          pendingUnlink
            ? `Unlink ${nodeTitleById.get(pendingUnlink.target_id) ?? pendingUnlink.target_id}?`
            : "Unlink this resource?"
        }
        description={
          pendingUnlink ? (
            <>
              <span>
                {selectedNode?.name ?? "This business key"} stops explaining this{" "}
                {pendingUnlink.target_type.replaceAll("_", " ")}, and the evidence path between
                them is no longer resolvable. The resource itself is untouched, and the link can
                be made again.
              </span>
              {/* A `label` wrapping its control, and no `div` around it: the
                  dialog's description IS a `<p>`, so a block wrapper would be
                  invalid nesting. Both of these are phrasing content. */}
              <label className="mt-4 flex flex-col gap-1.5 text-label font-label text-text">
                Reason
                <Textarea
                  value={unlinkReason}
                  onChange={(event) => setUnlinkReason(event.target.value)}
                  rows={2}
                  placeholder="Why this governed change is needed"
                />
              </label>
            </>
          ) : (
            ""
          )
        }
        confirmLabel="Unlink resource"
        destructive
        busy={unlinking}
        error={unlinkError}
        onConfirm={() => void unlink()}
        data-testid="context-hub-unlink-confirm"
        cancelTestId="context-hub-unlink-cancel"
        confirmTestId="context-hub-unlink-accept"
      />
      {/* The drawer carried six classes that no rule ever defined — the key,
          its row, the segment list and the error line, plus the edge/node
          variants. The evidence path, the one thing this surface exists to
          produce, was rendering unstyled: an undecorated `<ol>` with browser
          bullets and a bare `<code>`. Migrating it is a repair, not a
          re-skin. */}
      {(pathLoading || path || pathError) && (
        <aside
          className="fixed inset-y-0 right-0 z-[900] flex w-[380px] flex-col gap-4 overflow-y-auto border-l border-divider-base bg-surface-light p-6 shadow-overlay"
          aria-label="Business evidence path"
        >
          <div className="flex items-center justify-between gap-3">
            <div>
              <span className={KICKER}>Evaluation evidence</span>
              <h2 className="m-0 font-display text-h3 font-h3 text-text">Resolved path</h2>
            </div>
            <Button type="button" variant="secondary" size="icon-sm" onClick={() => { setPath(null); setPathError(null); }} aria-label="Close path">×</Button>
          </div>
          {pathLoading && <p className="m-0 text-ui text-text-secondary">Resolving path…</p>}
          {pathError && <Status tone="error" as="block">{pathError}</Status>}
          {path && (
            <>
              <div className="flex items-center justify-between gap-2">
                <ObjectId value={path.path_key} title="Path key" />
                {/* The old copy declared success it had not observed: it wrote
                    with `.then(() => setCopied(true))` and no `catch`, and did
                    nothing at all where `navigator.clipboard` is undefined — a
                    button that silently does nothing on an insecure origin.
                    `CopyButton` reports the refusal and names the way out. */}
                <CopyButton value={path.path_key} label="Copy key" size="sm" />
              </div>
              <ol className="m-0 flex list-none flex-col gap-2 p-0">
                {path.ordered_path.map((segment, index) => (
                  <li
                    key={`${String(segment.id)}-${index}`}
                    className={cn(
                      "flex flex-col gap-0.5 rounded-control border px-3 py-2",
                      // An edge is the RELATION between two nodes, so it reads
                      // as a connector — quieter, dashed — and a node as a card.
                      segment.kind === "edge"
                        ? "border-dashed border-divider-base bg-transparent"
                        : "border-divider-base bg-background-light",
                    )}
                  >
                    <span className={KICKER}>
                      {segment.kind === "edge" ? String(segment.edge_type) : String(segment.node_type).replaceAll("_", " ")}
                    </span>
                    <strong className="text-ui font-semibold text-text">{String(segment.title ?? segment.id)}</strong>
                    {segment.version_number ? (
                      <small className="text-caption text-text-secondary">v{String(segment.version_number)}</small>
                    ) : null}
                  </li>
                ))}
              </ol>
            </>
          )}
        </aside>
      )}
    </Stack>
  );
}
