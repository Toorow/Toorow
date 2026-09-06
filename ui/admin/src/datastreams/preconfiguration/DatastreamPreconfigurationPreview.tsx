import DatastreamPreconfiguration from "./DatastreamSetupWizard";
import type {
  DatastreamPreconfigurationProposal,
  DatastreamSetupDraft,
  PreconfigurationProposalItem,
} from "../wizard/wizardApi";

const fingerprint = "7b6f20fa2a65d2ec6d8aa90255f31e79c9e04196b86bb95e8a63d3c90abfa120";

function item(
  overrides: Partial<PreconfigurationProposalItem> & Pick<
    PreconfigurationProposalItem,
    "key" | "section" | "status" | "requirement" | "proposed_value"
  >,
): PreconfigurationProposalItem {
  return {
    evidence_refs: [
      {
        kind: "connector_contract",
        object_type: "connector_contract",
        object_id: "ccv_preview",
        version_id: "ccv_7",
        fingerprint,
        observed_at: "2026-07-29T08:00:00Z",
      },
    ],
    confidence: { level: "high", rationale: "Supported by the pinned Connector contract." },
    coverage: { state: "covered" },
    exceptions: [],
    blockers: [],
    warnings: [],
    owner_links: [],
    downstream_impact: [],
    dependency_fingerprint: fingerprint,
    ...overrides,
  };
}

const draft: DatastreamSetupDraft = {
  draft_ref: "dsd_preview",
  project_ref: "proj_preview",
  state: "draft",
  current_revision_ref: "dsdr_3",
  current_revision: 3,
  current_proposal_ref: "dspp_2",
  first_incomplete_section: "proposal_review",
  invalidation_causes: [],
  // Empty, like the wire: `resume_href` has carried "" since story 57.4
  // (`wizardApi.ts:48`) because `/projects/{id}/…` is refused by `parsePath` on
  // its first segment and resuming goes through `resume_reference` instead. A
  // preview fixture carrying the retired address is a screen showing a state the
  // product cannot produce.
  resume_href: "",
  idempotent_replay: false,
  operator_input: {
    mode: "connector_pull",
    source: {
      source_account_ref: "sacct_preview", connector_ref: "generic",
      connector_contract_version_ref: "ccv_7", report_ref: "daily", observation_ref: "dso_preview",
    },
    configure: {
      date_field: "date", metrics: "spend", dimensions: "date,country", date_window: "90 days",
      filters: "", history_intent: "90 days", cadence_intent: "daily", grain: "date,country",
    },
    name: "Daily acquisition",
  },
};

const proposal: DatastreamPreconfigurationProposal = {
  schema_version: "1",
  draft_ref: draft.draft_ref,
  draft_revision_ref: draft.current_revision_ref,
  proposal_ref: "dspp_2",
  dependency_fingerprint: fingerprint,
  proposal_token: `${fingerprint}${fingerprint}`,
  content_hash: fingerprint,
  is_stale: false,
  invalidation_causes: [],
  resume_href: draft.resume_href,
  idempotent_replay: false,
  configuration_summary: {
    existing: [{ object: "Project configuration", version_id: "pcv_3" }],
    will_be_created: [{ object: "Datastream setup artifacts", authority: "none until confirmation" }],
    will_remain_a_proposal: [{ object: "Country capability" }, { object: "Semantic View" }],
    downstream_impact: ["Later confirmation may create immutable plan and mapping versions."],
  },
  sections: [
    {
      key: "source",
      status: "complete",
      dependency_fingerprint: fingerprint,
      items: [item({
        key: "source.scope",
        section: "source",
        requirement: "required",
        status: "complete",
        proposed_value: { mode: "connector_pull", connector_id: "generic" },
      })],
    },
    {
      key: "classification",
      status: "warning",
      dependency_fingerprint: fingerprint,
      items: [item({
        key: "classification.fields",
        section: "classification",
        requirement: "recommended",
        status: "warning",
        proposed_value: [{ field_id: "country", role: "dimension", status: "suggested" }],
        confidence: { level: "medium", rationale: "Operator confirmation is still required." },
        coverage: { state: "partial" },
        warnings: [{ cause: "operator_confirmation_required" }],
      })],
    },
    {
      key: "outputs",
      status: "warning",
      dependency_fingerprint: fingerprint,
      items: [item({
        key: "outputs.full_grain",
        section: "outputs",
        requirement: "automatic",
        status: "warning",
        proposed_value: { label: "Will be created", kind: "full_grain_output" },
        coverage: { state: "pending" },
        downstream_impact: ["No Output exists until a reviewed candidate is published."],
      })],
    },
  ],
};


/** WHICH STOP THE SANDBOX OPENS ON.
 *
 *  The wizard is five sections and only one is drawn at a time
 *  (`DatastreamSetupWizard#sectionVisible`), so a sandbox that can only open on
 *  `Source` can only ever be looked at one fifth. The rail is no way in either:
 *  a step is offered only once every step before it is complete, and a preview
 *  draft carries no observation.
 *
 *  The wizard already reads its opening stop from the draft it is handed
 *  (`restoreSection(previewInput?.wizard_state)`), so `?section=` is written
 *  into the FIXTURE rather than passed as a new prop: this is what a resumed
 *  draft looks like on the wire, and no production path changes.
 *
 *      /debug/screen?name=DatastreamPreconfiguration&section=preview_validate
 */
const SANDBOX_SECTIONS = [
  "source", "configure", "classify_and_map", "preview_validate", "schedule_activate",
] as const;

export default function DatastreamPreconfigurationPreview() {
  const params = new URLSearchParams(window.location.search);
  const mode = params.get("mode");
  const asked = params.get("section");
  const section = SANDBOX_SECTIONS.includes(asked as (typeof SANDBOX_SECTIONS)[number])
    ? (asked as string)
    : "source";
  const operatorInput = mode === "external_bq"
    ? {
      mode,
      source: {
        access_ref: "", object_ref: "analytics.raw.events",
        declared_writer: "Agency ETL", readonly_acknowledged: true,
      },
      configure: {
        watermark_semantics: "event_date", logical_dataset_name: "daily_events",
        expected_freshness: "24h", verification_window: "7 days", expected_history: "90 days",
        row_filters: "",
      },
    }
    : mode === "managed_feed"
      ? {
        mode,
        source: {
          channel: "file_upload", source_account_ref: "", template_ref: "",
          staged_asset_ref: "dsa_preview", sheet_ref: "",
        },
        configure: {
          input_ref: "dsa_preview", parsing_contract: "header_row=1",
          logical_dataset_name: "daily_feed", date_semantics: "date",
          grain: "date,country", write_mode: "replace",
        },
      }
      : draft.operator_input;
  const previewDraft = {
    ...draft,
    operator_input: {
      ...(operatorInput as Record<string, unknown>),
      wizard_state: { active_section_ref: section, first_incomplete: section },
    },
  };
  return <DatastreamPreconfiguration projectId="proj_preview" preview={{ draft: previewDraft, proposal }} />;
}
