/**
 * NewSemanticViewDialog — Creation dialog for a governed Semantic View.
 *
 * Uses adaptive shadcn/ui components.
 * Executes Change Set creation via:
 *   POST /api/projects/{projectId}/governance/semantic-model/change-sets (action: "create_view")
 *
 * EDIT MODE (2026-08-18). The same lifecycle with `action: "edit_view"`, the
 * exact object and the exact base version — the shape `create_change_set`
 * requires of an `edit_*` intent (`semantic_model.py:541-548`) — and
 * `_apply_view` appends version N+1 rather than rewriting the one on screen.
 *
 * What is seeded comes from the object's summary and nothing else, and what the
 * summary does not carry is refused rather than reconstructed: a published
 * relationship this dialog cannot recompose would be DROPPED by an edit that
 * ignored it, and the canonical name is written into every version.
 */
import { useEffect, useMemo, useState } from "react";
import { apiGet, apiPost, apiPut, ApiError } from "../lib/apiFetch";
import { getGovernanceCollection, type GovernanceObject } from "./governanceSurface";
import { martDatasetDeclaration, MART_RELATION } from "./martContract";
import ConceptDatastreamBindings, {
  type BindingChoice,
  type ConceptChoice,
  type ConceptFeeds,
} from "./ConceptDatastreamBindings";
import {
  gateBlocksPublication,
  OVERRIDE_MINIMUM_REASON,
  TestGateOverridePanel,
  type TestGate,
} from "./TestGateOverride";
import BusinessDomainPicker from "./BusinessDomainPicker";
import type { SemanticEditTarget } from "./NewConceptDialog";
import {
  Button,
  Checkbox,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  NativeSelect,
  Status,
  Textarea,
  notify,
} from "../ui";

interface NewSemanticViewDialogProps {
  open: boolean;
  projectId: string;
  onClose: () => void;
  /** Called once the change set is confirmed — a creation or an appended
   *  version. The caller re-reads the object from its owner. */
  onCreated: () => void;
  /** Absent for a creation. Present for an edit of this exact View. */
  edit?: SemanticEditTarget | null;
}

/** One member of the published version, as `_enrich_semantic_object` composes
 *  it (`governance_read_model.py:3715-3732`). It is the only place the read
 *  model names the exact Concept versions a View pins. */
interface ViewMember {
  concept_id?: string;
  concept_version_id?: string;
  /** The number the person reads for the pinned version, served by
   *  `_enrich_semantic_object` off `semantic_concept_versions.version_number`.
   *  Absent only from an envelope written before that read carried it. */
  version_number?: number | null;
  role?: string;
  name?: string;
  label?: string;
}

/** One published relationship, as `load_view_relationships` returns it. */
interface ViewRelationship {
  mdm_common_key_version_id?: string | null;
  left_datastream_id?: string | null;
  right_datastream_id?: string | null;
  cardinality_type?: string | null;
  name?: string | null;
}

function viewMembers(summary: Record<string, unknown>): ViewMember[] {
  return Array.isArray(summary.members) ? (summary.members as ViewMember[]) : [];
}

function viewRelationships(summary: Record<string, unknown>): ViewRelationship[] {
  return Array.isArray(summary.relationships) ? (summary.relationships as ViewRelationship[]) : [];
}

/** The canonical name every published version writes (`_apply_view` :1797), or
 *  `null` when the read model does not carry it.
 *
 *  `_semantic_view` composes `label` from `label or name`, so until 2026-08-18
 *  this answered `null` for every View and no edit could be composed at all. The
 *  owner now carries `summary.name` (`governance_read_model.py`), and the
 *  refusal below stays: an envelope that arrives without it still gets a named
 *  refusal rather than a name reconstructed from a display label. */
export function viewCanonicalName(summary: Record<string, unknown>): string | null {
  return typeof summary.name === "string" && summary.name.trim() ? summary.name.trim() : null;
}


/** The published Concepts this Project offers, each with its exact version. A
 *  Concept with no active version cannot be pinned, so it is not offered: an
 *  option that cannot be chosen is a dead end drawn as a choice. */
type ConceptChoiceState =
  | { status: "loading" }
  | { status: "ready"; options: Array<{ value: string; label: string }> }
  | { status: "error"; message: string };

type MatchChoice = {
  value: string;
  label: string;
  commonKeyVersionId: string;
  componentIds: string[];
  leftDatastreamId: string;
  rightDatastreamId: string;
};

type MatchChoiceState =
  | { status: "loading" }
  | { status: "ready"; options: MatchChoice[] }
  | { status: "error"; message: string };

type MatchCatalog = {
  matches?: Array<{
    kind?: string;
    left?: { datastream_id?: string; name?: string };
    right?: { datastream_id?: string; name?: string };
    common_key?: {
      version_id?: string;
      name?: string;
      components?: Array<{ canonical_field_id?: string; canonical_name?: string }>;
    };
  }>;
};

function relationshipChoices(catalog: MatchCatalog): MatchChoice[] {
  return (catalog.matches ?? []).flatMap((match) => {
    const key = match.common_key;
    const leftId = match.left?.datastream_id;
    const rightId = match.right?.datastream_id;
    const componentIds = (key?.components ?? [])
      .map((component) => component.canonical_field_id ?? "")
      .filter(Boolean);
    if (
      match.kind !== "candidate_key_missing" ||
      !key?.version_id ||
      !leftId ||
      !rightId ||
      componentIds.length === 0
    ) {
      return [];
    }
    // BOTH SIDES ARE NAMED BY THE SERVER. `datastream_matches._side` carries
    // `d.name` off `app.datastreams` (`NOT NULL`, migration 023) for every
    // candidate it scans, so `?? leftId` / `?? rightId` were unreachable and
    // would have offered a person a relationship to pick spelt
    // `ds_<ULID> ↔ ds_<ULID>`. An unnamed side says so and stays pickable.
    return [{
      value: `${leftId}|${rightId}|${key.version_id}`,
      label: `${match.left?.name ?? "Unnamed Datastream"} ↔ ${match.right?.name ?? "Unnamed Datastream"} via ${key.name ?? "common key"}`,
      commonKeyVersionId: key.version_id,
      componentIds,
      leftDatastreamId: leftId,
      rightDatastreamId: rightId,
    }];
  });
}

function conceptChoices(items: GovernanceObject[]): Array<{ value: string; label: string }> {
  return items
    .filter((item) => item.active_version_ref?.id)
    .map((item) => ({
      value: `${item.object_ref.id}|${item.active_version_ref!.id}`,
      label: `${item.object_ref.label} · version ${item.active_version_ref!.version ?? "?"}`,
    }));
}

export default function NewSemanticViewDialog({
  open,
  projectId,
  onClose,
  onCreated,
  edit = null,
}: NewSemanticViewDialogProps) {
  const [name, setName] = useState("");
  const [labelStr, setLabelStr] = useState("");
  const [businessScope, setBusinessScope] = useState("general");
  const [description, setDescription] = useState("");
  const [businessDomainRefs, setBusinessDomainRefs] = useState<string[]>([]);
  /** WHICH CONCEPTS THIS VIEW PUBLISHES. The server refuses a View with no
   *  metric -- "publishing a View nobody can measure anything with would be
   *  publishing an empty promise" -- and this dialog used to send an empty
   *  list, so every View it created was refused. Each entry pins the Concept
   *  AND its exact version: `latest` is not a version. */
  const [chosenConcepts, setChosenConcepts] = useState<string[]>([]);
  const [concepts, setConcepts] = useState<ConceptChoiceState>({ status: "loading" });
  /** WHERE each chosen Concept is measured. A View without these bindings is
   *  publishable and answers nothing -- the refusal names a table, not a
   *  gesture, so the gesture lives here. */
  const [bindings, setBindings] = useState<BindingChoice>({});
  const [pendingBindings, setPendingBindings] = useState<string[]>([]);
  /** CHANTIER B: every Datastream that feeds each Concept. The View binds them
   *  all, so a measure can be asked at each of their grains; `bindings` above
   *  keeps only the answer to "which one holds the total", which is a
   *  Governance declaration and not a binding. */
  const [conceptFeeds, setConceptFeeds] = useState<ConceptFeeds>({});
  /** Rebuilt only when the choice or the catalogue moves: a fresh array on
   *  every render turns the panel's effects into a loop. */
  const boundableConcepts = useMemo<ConceptChoice[]>(
    () =>
      concepts.status === "ready"
        ? concepts.options
            .filter((option) => chosenConcepts.includes(option.value))
            .map((option) => ({
              value: option.value,
              name: option.label.split(" · ")[0],
              label: option.label.split(" · ")[0],
            }))
        : [],
    [concepts, chosenConcepts],
  );
  const [matches, setMatches] = useState<MatchChoiceState>({ status: "loading" });
  const [chosenMatch, setChosenMatch] = useState("");
  const [cardinality, setCardinality] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The server's refusals, KEPT APART (2026-08-16). They used to be flattened
   *  into one sentence joined by ` · `: three refusals became one unreadable
   *  line, and the `code` a support conversation quotes and the `path` that says
   *  WHICH field to change were buried mid-string. The sibling dialogs
   *  (`CaseDecisionDialog`, `DeclareConceptPanel`) already render them whole and
   *  one per line; this one was the odd surface out. */
  const [refusalList, setRefusalList] = useState<
    Array<{ code?: string; message?: string; path?: string }>
  >([]);
  // The gate that blocked the last attempt, the reason written for it, and
  // the change set it belongs to: re-preparing THAT change set is what
  // carries the override; a second one would publish a different View.
  const [blockedGate, setBlockedGate] = useState<TestGate | null>(null);
  const [overrideReason, setOverrideReason] = useState("");
  const [pendingChangeSetId, setPendingChangeSetId] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !projectId) return;
    const controller = new AbortController();
    setConcepts({ status: "loading" });
    void getGovernanceCollection(projectId, "semantic-model", "concepts", {}, controller.signal)
      .then((envelope) => setConcepts({ status: "ready", options: conceptChoices(envelope.items) }))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setConcepts({
          status: "error",
          message: reason instanceof Error ? reason.message : "The request failed.",
        });
      });
    setMatches({ status: "loading" });
    void apiGet<MatchCatalog>(
      `/api/projects/${encodeURIComponent(projectId)}/analyze/matches`,
      { signal: controller.signal },
    )
      .then((catalog) => setMatches({ status: "ready", options: relationshipChoices(catalog) }))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setMatches({
          status: "error",
          message: reason instanceof Error ? reason.message : "The request failed.",
        });
      });
    return () => controller.abort();
  }, [open, projectId]);

  /** Seeded from the View's own summary, once per opening. */
  useEffect(() => {
    if (!open || !edit) return;
    const summary = edit.summary;
    setName(viewCanonicalName(summary) ?? "");
    setLabelStr(edit.label);
    setBusinessScope(typeof summary.business_scope === "string" ? summary.business_scope : "");
    setDescription(typeof summary.description === "string" ? summary.description : "");
    setBusinessDomainRefs(
      (Array.isArray(summary.business_domain_refs) ? summary.business_domain_refs : []).filter(
        (value): value is string => typeof value === "string",
      ),
    );
    // The EXACT versions the published version pins, not the current ones. A
    // seed that re-pinned every member to `current` would silently republish
    // the View against Concepts nobody re-read.
    setChosenConcepts(
      viewMembers(summary)
        .filter((member) => member.concept_id && member.concept_version_id)
        .map((member) => `${member.concept_id}|${member.concept_version_id}`),
    );
  }, [open, edit]);

  /** The published relationship, found again among the candidates this dialog
   *  can compose. `null` means it is not there — which is a refusal, not a
   *  reason to publish a version without it. */
  const recomposedMatch = useMemo(() => {
    if (!edit || matches.status !== "ready") return null;
    const published = viewRelationships(edit.summary)[0];
    if (!published) return null;
    return (
      matches.options.find(
        (option) =>
          option.commonKeyVersionId === published.mdm_common_key_version_id &&
          option.leftDatastreamId === published.left_datastream_id &&
          option.rightDatastreamId === published.right_datastream_id,
      ) ?? null
    );
  }, [edit, matches]);

  useEffect(() => {
    if (!open || !edit || !recomposedMatch) return;
    setChosenMatch(recomposedMatch.value);
    const published = viewRelationships(edit.summary)[0];
    if (typeof published?.cardinality_type === "string" && published.cardinality_type) {
      setCardinality(published.cardinality_type);
    }
  }, [open, edit, recomposedMatch]);

  /** What makes THIS edit impossible to compose honestly, named. Every branch
   *  is something the published version carries and this dialog would drop. */
  const editBlock = useMemo(() => {
    if (!edit) return null;
    if (!viewCanonicalName(edit.summary)) {
      return (
        "This Semantic View's canonical name is not carried by the Governance read model, and " +
        "every published version writes it. Nothing was sent: an edit that took the display " +
        "title for the name would publish this View under a different identity."
      );
    }
    const published = viewRelationships(edit.summary);
    if (published.length > 1) {
      return (
        `This View publishes ${published.length} relationships and this dialog composes one. ` +
        "Editing it here would publish a version without the others."
      );
    }
    if (published.length === 1 && matches.status === "error") {
      return (
        "This View publishes a cross-source relationship and the MDM candidate list could not be " +
        `read: ${matches.message} The relationship cannot be carried into a new version until it ` +
        "answers."
      );
    }
    if (published.length === 1 && matches.status === "ready" && !recomposedMatch) {
      return (
        `This View publishes the relationship ${published[0].name ?? "it declared"}, which is no ` +
        "longer among the approved common-key candidates. Editing it here would publish a version " +
        "that cannot cross those Datastreams at all."
      );
    }
    return null;
  }, [edit, matches, recomposedMatch]);

  /** Members pinned to a version the Concept no longer publishes. They stay
   *  pinned — that is what an immutable pin means — and the person is told,
   *  because a checkbox list that cannot show them would read as a View with
   *  fewer Concepts than it has. */
  const unlistedMembers = useMemo(() => {
    if (!edit || concepts.status !== "ready") return [];
    const offered = new Set(concepts.options.map((option) => option.value));
    return viewMembers(edit.summary)
      .filter((member) => member.concept_id && member.concept_version_id)
      .filter((member) => !offered.has(`${member.concept_id}|${member.concept_version_id}`));
  }, [edit, concepts]);

  const resetForm = () => {
    setName("");
    setLabelStr("");
    setBusinessScope("general");
    setDescription("");
    setBusinessDomainRefs([]);
    setChosenConcepts([]);
    setChosenMatch("");
    setCardinality("");
    setError(null);
    setRefusalList([]);
    setBlockedGate(null);
    setOverrideReason("");
    setPendingChangeSetId(null);
  };

  const handleClose = () => {
    resetForm();
    onClose();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (editBlock) {
      setError(editBlock);
      return;
    }
    if (!name.trim()) {
      setError("Semantic View name is required.");
      return;
    }
    // On an edit the identity is the published one, verbatim: re-cleaning it
    // could publish a version under a name the object does not carry.
    const cleanName = edit
      ? (viewCanonicalName(edit.summary) as string)
      : name.trim().toLowerCase().replace(/\s+/g, "_");

    if (pendingBindings.length > 0) {
      // ONLY AN ARBITRATION BLOCKS. A Concept fed by several Datastreams has a
      // question with no default; one the dictionary feeds with nothing is a
      // fact stated in place, and other paths name their Datastreams elsewhere
      // -- an MDM pair carries them on its relationship, not here. Refusing on
      // an empty read would block those paths for a question nobody was asked.
      setError(
        `${pendingBindings.length} Concept(s) are measured by several Datastreams. ` +
          "Name which one holds the total.",
      );
      return;
    }
    if (chosenConcepts.length === 0) {
      // The server refuses this too, and says it better -- but making the round
      // trip to be told what the screen already knows is a refusal the person
      // waits for instead of one they can act on.
      setError(
        "A Semantic View publishes at least one Concept. Choose what this View lets " +
          "anyone measure.",
      );
      return;
    }
    if (chosenMatch && !cardinality) {
      setError("Choose the relationship cardinality before approving this Datastream cross.");
      return;
    }
    const selectedMatch =
      matches.status === "ready"
        ? matches.options.find((option) => option.value === chosenMatch)
        : undefined;
    setSubmitting(true);
    setError(null);
    //  Une tentative neuve ne porte pas les refus de la precedente : les laisser
    //  affiches ferait lire comme actuel ce que le serveur vient de ne pas redire.
    setRefusalList([]);

    const idempotencyKey = edit
      ? `scs-view-edit-${edit.objectId}-${Date.now()}`
      : `scs-view-${cleanName}-${Date.now()}`;

    try {
      const changeSet = pendingChangeSetId
        ? { change_set_id: pendingChangeSetId }
        : await apiPost<{ change_set_id: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets`,
        {
          object_type: "semantic-view",
          // THE EXACT OBJECT AND THE EXACT BASE, or neither: `create_change_set`
          // refuses `missing_exact_base` for an `edit_*` action without both.
          object_id: edit ? edit.objectId : null,
          base_version_id: edit ? edit.baseVersionId : null,
          // NESTED under `view`, and that is not a style choice. The server
          // reads `intent.get("view")` in seven places (`server/core/
          // semantic_model.py` :900, :959, :1197, :1393, :1399, :1453, :1582).
          // A flat intent resolves to an EMPTY payload there, and `_apply_view`
          // would write `str(payload.get("name"))` -- the string "None" -- into
          // an IMMUTABLE published version.
          intent: {
            action: edit ? "edit_view" : "create_view",
            view: {
              name: cleanName,
              label: labelStr.trim() || cleanName,
              business_scope: businessScope.trim(),
              description: description.trim(),
              business_domain_refs: businessDomainRefs,
              // CARRIED THROUGH, not recomposed. `_apply_view` writes
              // `payload.query_policy or {}` into the new version, so an edit
              // that omitted it would silently retire the policy the published
              // version was compiled under.
              ...(edit && edit.summary.query_policy
                ? { query_policy: edit.summary.query_policy }
                : {}),
              // Each entry pins the Concept AND its version, and names the
              // relation it sits on -- the compiler refuses a Concept "bound to
              // dataset '', which this Semantic View does not declare".
              concepts: chosenConcepts.map((entry) => {
                const [conceptId, versionId] = entry.split("|");
                return {
                  concept_id: conceptId,
                  concept_version_id: versionId,
                  dataset: MART_RELATION,
                };
              }),
              datasets: [martDatasetDeclaration()],
              // The Datastream is named here; the SERVER pins its published
              // mapping version, inside the same transaction. A version pinned
              // by the browser is the one it read a moment earlier.
              // CHANTIER B: one entry per (Concept, feed), not one per Concept.
              // A View that pinned a single feed of a measure eight Datastreams
              // carry made every question at another grain read as a cross-source
              // one. The fallback covers a Concept whose feeds could not be read:
              // binding the answered total is strictly better than binding nothing.
              bindings: chosenConcepts
                .map((entry) => entry.split("|")[0])
                .flatMap((conceptId) => {
                  const feeds = conceptFeeds[conceptId]?.length
                    ? conceptFeeds[conceptId]
                    : bindings[conceptId]
                      ? [bindings[conceptId]]
                      : [];
                  return feeds.map((datastreamId) => ({
                    concept_id: conceptId,
                    datastream_id: datastreamId,
                    output_ref: { relation: MART_RELATION },
                    binding_state: "active",
                  }));
                }),
              relationships: selectedMatch
                ? [{
                    name: `cross_${selectedMatch.leftDatastreamId}_${selectedMatch.rightDatastreamId}`,
                    from_dataset: MART_RELATION,
                    to_dataset: MART_RELATION,
                    from_columns: selectedMatch.componentIds,
                    to_columns: selectedMatch.componentIds,
                    cardinality_type: cardinality,
                    fan_out_policy: "forbid",
                    bridge_dataset: null,
                    mdm_common_key_version_id: selectedMatch.commonKeyVersionId,
                    left_datastream_id: selectedMatch.leftDatastreamId,
                    right_datastream_id: selectedMatch.rightDatastreamId,
                  }]
                : [],
            },
          },
          idempotency_key: idempotencyKey,
        }
      );

      setPendingChangeSetId(changeSet.change_set_id);

      // `prepare` carried `.catch(() => null)`. That is what made the defect
      // invisible: its refusal was swallowed, `confirm` was then skipped for
      // want of a token, and the dialog announced success over a change set that
      // published nothing. A validation refusal is the most useful answer this
      // screen can give -- it names the field -- so it is surfaced, not eaten.
      const prepared = await apiPost<{
        confirmation_token?: string;
        refusals?: Array<{ code?: string; message?: string; path?: string }>;
        validation?: {
          publishable?: boolean;
          refusals?: Array<{ code?: string; message?: string; path?: string }>;
          test_gate?: { state?: string; message?: string };
        };
      }>(
        `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets/${changeSet.change_set_id}/prepare`,
        overrideReason.trim().length >= OVERRIDE_MINIMUM_REASON
          ? { test_gate_override: { reason: overrideReason.trim() } }
          : {}
      );

      // THE BELIEF THIS BLOCK USED TO CARRY WAS MEASURED FALSE (story 60.2). It
      // treated an absent confirmation token as proof of a validation refusal.
      // `prepare_change_set` mints that token UNCONDITIONALLY
      // (`server/core/semantic_model.py:1140-1148`) and returns the refusals
      // beside it, so the branch never fired on a refusal: a refused change set
      // was read as an accepted one, `confirm` ran anyway, and what the person
      // saw was whatever `confirm` happened to fail with.
      //
      // `validation.publishable` is the field that answers the question. The
      // token only reports that a token was minted.
      if (prepared?.validation?.publishable !== true) {
        const refusals = prepared?.refusals ?? prepared?.validation?.refusals ?? [];
        const gate = prepared?.validation?.test_gate as TestGate | undefined;
        // THE GATE IS A QUESTION, not a wall -- and it only becomes one once it
        // has spoken, which is why the panel does not exist before this point.
        if (gate && gateBlocksPublication(gate, refusals)) {
          setBlockedGate(gate);
          setError(null);
          setRefusalList([]);
          return;
        }
        setRefusalList(refusals);
        setError(
          refusals.length > 0
            ? null
            : gate?.message
              ? `This Semantic View was not published. ${gate.message}`
              : "This Semantic View was not published: preparation did not clear it. Nothing was created.",
        );
        return;
      }
      setBlockedGate(null);
      if (!prepared.confirmation_token) {
        setError(
          "This Semantic View was not published: preparation returned no confirmation. Nothing was created.",
        );
        return;
      }

      await apiPost(
        `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/change-sets/${changeSet.change_set_id}/confirm`,
        { confirmation_token: prepared.confirmation_token }
      );

      // CHANTIER B -- the total is declared where it was answered. It is a
      // Governance declaration scoped to the Project, not to this View: it
      // outlives the View, and every later Result of that measure reads it to
      // state a breakdown's distance from the whole figure.
      //
      // A declaration that fails does NOT undo a published View. The View is
      // valid without it; what is lost is the arbitration, and the screen says
      // so rather than reporting a success it did not get.
      const undeclared: string[] = [];
      for (const [conceptId, datastreamId] of Object.entries(bindings)) {
        if (!datastreamId || (conceptFeeds[conceptId]?.length ?? 0) <= 1) continue;
        try {
          await apiPut(
            `/api/projects/${encodeURIComponent(projectId)}/governance/metric-grain/` +
              `${encodeURIComponent(conceptId)}/total`,
            { total_datastream_id: datastreamId },
          );
        } catch {
          undeclared.push(conceptId);
        }
      }

      notify(
        edit
          ? `Semantic View edited: a new version of ${cleanName} was published. The version it was edited from stays readable.`
          : undeclared.length > 0
          ? `Semantic View created: View ${cleanName} was successfully registered. ` +
              `${undeclared.length} measure(s) still have no declared total — name it ` +
              "from Governance before reading them at two grains."
          : `Semantic View created: View ${cleanName} was successfully registered.`,
      );

      handleClose();
      onCreated();
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.message || `API Error (${err.status})`);
      } else {
        setError(err instanceof Error ? err.message : "Error creating semantic view.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(val) => !val && handleClose()}>
      <DialogContent className="sm:max-w-[540px]">
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>{edit ? "Edit Semantic View" : "New Semantic View"}</DialogTitle>
            <DialogDescription>
              {edit
                ? `Editing from version ${edit.baseVersionId}. Publishing appends a new immutable version from that exact base; the version you are reading is never rewritten.`
                : "Create a reusable semantic view grouping metrics and dimensions for analysis."}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            {editBlock && (
              <Status
                as="block"
                tone="error"
                title="This Semantic View cannot be edited from here"
                data-testid="view-edit-block"
              >
                {editBlock}
              </Status>
            )}
            {/* THE SERVER'S OWN SENTENCES, WHOLE AND ONE PER LINE. The `code` is
                what a support conversation quotes and the `path` says WHICH field
                to change; a single joined line loses both. */}
            {refusalList.length > 0 && (
              <Status
                as="block"
                tone="error"
                title="This Semantic View was not published"
                data-testid="new-view-refusals"
              >
                <ul className="m-0 list-none space-y-1 p-0">
                  {refusalList.map((refusal, index) => (
                    <li key={`${refusal.code ?? "refusal"}-${index}`} className="text-caption">
                      <strong>{refusal.code ?? "refused"}</strong>
                      {refusal.path ? ` at ${refusal.path}` : ""}
                      {refusal.message ? ` — ${refusal.message}` : ""}
                    </li>
                  ))}
                </ul>
              </Status>
            )}
            {error && (
              <Status as="block" tone="error" title={edit ? "This edit was not published" : "Creation failed"}>
                {error}
              </Status>
            )}

            {blockedGate && (
              <TestGateOverridePanel
                gate={blockedGate}
                reason={overrideReason}
                onReasonChange={setOverrideReason}
                objectNoun="Semantic View"
              />
            )}

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label htmlFor="view-name">View Identifier</Label>
                <Input
                  id="view-name"
                  placeholder="e.g. monthly_sales_view"
                  value={name}
                  readOnly={Boolean(edit)}
                  onChange={(e) => setName(e.target.value)}
                  required
                />
                {edit && (
                  <p className="mb-0 text-caption text-text-secondary">
                    The identity every published version pins. It is shown as published and is not
                    changed by an edit.
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="view-label">Display Title</Label>
                <Input
                  id="view-label"
                  placeholder="e.g. Monthly Sales View"
                  value={labelStr}
                  onChange={(e) => setLabelStr(e.target.value)}
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="view-relationship">Datastreams this View may cross</Label>
              {matches.status === "loading" && (
                <p className="mb-0 text-caption text-text-secondary">
                  Reading MDM common-key candidates…
                </p>
              )}
              {matches.status === "error" && (
                <Status as="block" tone="error" title="Matching candidates unavailable">
                  {matches.message} You can still create a single-source View; no cross-source
                  authority will be invented.
                </Status>
              )}
              {matches.status === "ready" && (
                <>
                  <NativeSelect
                    id="view-relationship"
                    value={chosenMatch}
                    disabled={submitting}
                    onChange={(event) => {
                      setChosenMatch(event.target.value);
                      if (!event.target.value) setCardinality("");
                    }}
                  >
                    <option value="">No cross-source relationship</option>
                    {matches.options.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </NativeSelect>
                  {matches.options.length === 0 && (
                    <p className="mb-0 text-caption text-text-secondary">
                      No pair currently implements the same published MDM common key.
                    </p>
                  )}
                </>
              )}
            </div>

            {chosenMatch && (
              <div className="space-y-1.5">
                <Label htmlFor="view-cardinality">Relationship cardinality</Label>
                <NativeSelect
                  id="view-cardinality"
                  value={cardinality}
                  disabled={submitting}
                  required
                  onChange={(event) => setCardinality(event.target.value)}
                >
                  <option value="">Choose from observed business semantics…</option>
                  <option value="one_to_one">One to one</option>
                  <option value="many_to_one">Left many to right one</option>
                  <option value="one_to_many">Left one to right many</option>
                </NativeSelect>
                <p className="mb-0 text-caption text-text-secondary">
                  This approves only this exact Datastream pair and common-key version. It does
                  not authorize every source that happens to implement the same key.
                </p>
              </div>
            )}

            <div className="space-y-1.5">
              <Label htmlFor="view-scope">Business Scope</Label>
              <Input
                id="view-scope"
                placeholder="e.g. sales, finance, marketing"
                value={businessScope}
                onChange={(e) => setBusinessScope(e.target.value)}
              />
            </div>

            <div className="space-y-1.5">
              {/* `governance.md:72` -- a Semantic View is linkable to Business
                  Domains. The picker offers only the ACTIVE domains of this
                  organization, which is exactly the set
                  `_validate_business_domain_refs` accepts server-side: a free
                  field would let someone type an id the server then refuses. */}
              <Label>Business Domains</Label>
              <BusinessDomainPicker
                projectId={projectId}
                value={businessDomainRefs}
                onChange={setBusinessDomainRefs}
                disabled={submitting}
              />
            </div>

            {/* WHAT THIS VIEW LETS ANYONE MEASURE. A View with no metric is
                refused by the server, and this question is where that is
                decided -- not a detail to fill in afterwards. */}
            <div className="space-y-1.5">
              <Label htmlFor="view-concepts">Concepts this View publishes</Label>
              {concepts.status === "loading" && (
                <p className="mb-0 text-caption text-text-secondary">
                  Reading this Project's published Concepts…
                </p>
              )}
              {concepts.status === "error" && (
                <Status as="block" tone="error" title="The Concept list could not be read">
                  {concepts.message} No View can be pinned until it answers — an empty list
                  here would read as "this Project has no Concepts", which is a different fact.
                </Status>
              )}
              {concepts.status === "ready" && concepts.options.length === 0 && (
                <p className="mb-0 text-caption text-text-secondary">
                  No published Concept to publish yet. A Semantic View publishes at least one
                  metric, pinned to its exact version, and this Project has none: a Concept is
                  authored from New Concept in Governance, and published before it can be pinned.
                </p>
              )}
              {concepts.status === "ready" && concepts.options.length > 0 && (
                <div className="grid gap-1.5">
                  {concepts.options.map((option) => (
                    <label key={option.value} className="flex items-center gap-2 text-body">
                      <Checkbox
                        checked={chosenConcepts.includes(option.value)}
                        disabled={submitting}
                        onCheckedChange={(checked) =>
                          setChosenConcepts((current) =>
                            checked
                              ? [...current, option.value]
                              : current.filter((entry) => entry !== option.value),
                          )
                        }
                      />
                      <span>{option.label}</span>
                    </label>
                  ))}
                  <p className="mb-0 text-caption text-text-secondary">
                    Read from <code>{MART_RELATION}</code>, the governed relation every
                    Datastream of this Project lands in.
                  </p>
                </div>
              )}
              {unlistedMembers.length > 0 && (
                <Status
                  as="block"
                  tone="info"
                  title="Concepts pinned to a version that is no longer current"
                  data-testid="view-edit-stale-pins"
                >
                  <ul className="m-0 list-none space-y-1 p-0">
                    {unlistedMembers.map((member) => (
                      <li key={`${member.concept_id}|${member.concept_version_id}`} className="text-caption">
                        {/* THE VERSION HAS A NUMBER, so this line prints it. It
                            read "· version scv_01KZ..." -- an identifier in a
                            name's position, one line under a fallback chain that
                            already resolves the Concept's own word. The number is
                            served by the read model; when an older envelope
                            carries none, the line says nothing about the version
                            rather than falling back to the id. */}
                        {/* `app.semantic_concept_versions.label` and `.name` are
                            both `NOT NULL` (migration 142) and the read model
                            serves both on every member, so `?? member.concept_id`
                            was the third term of a chain whose first two cannot
                            be empty -- and would have printed `sc_<ULID>` in the
                            one panel whose job is to name pins a person can no
                            longer see in the list. */}
                        {member.label ?? member.name ?? "Unnamed Concept"}
                        {typeof member.version_number === "number"
                          ? ` · version ${member.version_number}`
                          : ""}
                      </li>
                    ))}
                  </ul>
                  They stay pinned exactly as published — the list above offers current versions
                  only, and unpinning one is a different decision from editing this View.
                </Status>
              )}
            </div>

            <ConceptDatastreamBindings
              projectId={projectId}
              concepts={boundableConcepts}
              value={bindings}
              onChange={setBindings}
              onPendingChange={setPendingBindings}
              onFeedsChange={setConceptFeeds}
              disabled={submitting}
            />

            <div className="space-y-1.5">
              <Label htmlFor="view-desc">Description</Label>
              <Textarea
                id="view-desc"
                placeholder="Specify the analytical purpose of this semantic view..."
                rows={3}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={handleClose} disabled={submitting}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                submitting ||
                editBlock !== null ||
                (blockedGate !== null &&
                  overrideReason.trim().length < OVERRIDE_MINIMUM_REASON)
              }
            >
              {submitting
                ? edit ? "Publishing..." : "Creating..."
                : blockedGate
                  ? "Publish with this reason"
                  : edit ? "Edit Semantic View" : "Create Semantic View"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
