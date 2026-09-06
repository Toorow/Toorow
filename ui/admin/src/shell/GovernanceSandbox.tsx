/**
 * GovernanceSandbox — Renders Governance collections and workbenches directly
 * in `/debug/screen` without needing a signed-in session or full shell context.
 */

import GovernanceCollection from "../governance/GovernanceCollection";
import GovernanceObjectWorkbench from "../governance/GovernanceObjectWorkbench";
import CanonicalFields from "../governance/CanonicalFields";
import NewSemanticViewDialog from "../governance/NewSemanticViewDialog";
import { RouterProvider } from "./router";
import { installSandboxApi } from "./sandboxApi";

const PROJECT_ID = "proj_EXAMPLE";

export function GovernanceMasterData() {
  return (
    <RouterProvider>
      <GovernanceCollection projectId={PROJECT_ID} section="master-data" lens="business-domains" />
    </RouterProvider>
  );
}

export function GovernanceSemanticModel() {
  return (
    <RouterProvider>
      <GovernanceCollection projectId={PROJECT_ID} section="semantic-model" lens="concepts" />
    </RouterProvider>
  );
}

export function GovernanceCommonKeys() {
  installSandboxApi((url) => {
    if (url.includes("/api/context/business-taxonomy")) {
      return { status: 200, body: { domains: [
        { id: "bd_marketing", name: "Marketing", status: "active" },
      ] } };
    }
    if (url.includes("/mdm/canonical-fields")) {
      return {
        status: 200,
        body: {
          project_id: PROJECT_ID,
          organization_id: "org_EXAMPLE",
          fields: [
            { id: "mdm_day", canonical_name: "Day", concept_kind: "dimension", value_type: "date", aggregation: null, object_kind: null, non_additive: false, unit: null, description: "Reporting calendar day", scope: "platform" },
            { id: "mdm_campaign", canonical_name: "Campaign", concept_kind: "dimension", value_type: "string", aggregation: null, object_kind: null, non_additive: false, unit: null, description: "Governed campaign identity", scope: "platform" },
            { id: "mdm_country", canonical_name: "Country", concept_kind: "dimension", value_type: "string", aggregation: null, object_kind: null, non_additive: false, unit: null, description: "ISO country", scope: "platform" },
            { id: "mdm_spend", canonical_name: "Spend", concept_kind: "metric", value_type: "money", aggregation: "sum", object_kind: null, non_additive: false, unit: "EUR", description: null, scope: "platform" },
          ],
          scope_counts: { platform: 4, project: 0 },
          empty_reason: null,
        },
      };
    }
    if (url.includes("/mdm/common-keys")) {
      return {
        status: 200,
        body: {
          project_id: PROJECT_ID,
          organization_id: "org_EXAMPLE",
          common_keys: [{
            id: "mck_campaign_day",
            name: "Campaign + day",
            description: "Cross paid-media and conversion streams at their shared daily campaign grain.",
            status: "active",
            current_version: {
              id: "mckv_campaign_day_3",
              version_number: 3,
              content_hash: "a".repeat(64),
              components: [
                { canonical_field_id: "mdm_campaign", canonical_name: "Campaign" },
                { canonical_field_id: "mdm_day", canonical_name: "Day" },
              ],
            },
            component_count: 2,
            updated_at: "2026-08-13T09:00:00Z",
          }],
          empty_reason: null,
        },
      };
    }
    return { status: 404, body: { code: "not_found", message: "No sandbox fixture." } };
  });
  return (
    <RouterProvider>
      <CanonicalFields projectId={PROJECT_ID} />
    </RouterProvider>
  );
}

export function GovernanceRelationship() {
  installSandboxApi((url) => {
    if (url.includes("/analyze/matches")) {
      return { status: 200, body: { matches: [{
        kind: "candidate_key_missing",
        left: { datastream_id: "ds_paid", name: "Paid media daily" },
        right: { datastream_id: "ds_sales", name: "Sales conversions" },
        common_key: {
          version_id: "mckv_campaign_day_3",
          name: "Campaign + day",
          components: [
            { canonical_field_id: "mdm_campaign", canonical_name: "Campaign" },
            { canonical_field_id: "mdm_day", canonical_name: "Day" },
          ],
        },
      }] } };
    }
    if (url.includes("/governance/semantic-model")) {
      return { status: 200, body: {
        schema_version: "governance-collection.v1",
        project_ref: { object_type: "project", id: PROJECT_ID },
        organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
        section: "semantic-model",
        generated_at: "2026-08-13T09:00:00Z",
        evidence_as_of: "2026-08-13T09:00:00Z",
        lens: "concepts",
        default_lens: "concepts",
        available_lenses: ["concepts"],
        items: [{
          object_ref: { type: "semantic-concept", id: "mdm_spend", label: "Spend" },
          active_version_ref: { id: "scv_spend_4", version: 4 },
        }],
        coverage: { state: "available", count: 1, returned: 1, total: 1, truncated: false },
        unavailable_reasons: [],
      } };
    }
    if (url.includes("/governance/master-data")) {
      return { status: 200, body: {
        schema_version: "governance-collection.v1",
        project_ref: { object_type: "project", id: PROJECT_ID },
        organization_ref: { object_type: "organization", id: "org_EXAMPLE" },
        section: "master-data",
        generated_at: "2026-08-13T09:00:00Z",
        evidence_as_of: "2026-08-13T09:00:00Z",
        lens: "business-domains",
        default_lens: "business-domains",
        available_lenses: ["business-domains"],
        items: [],
        coverage: { state: "available", count: 0, returned: 0, total: 0, truncated: false },
        unavailable_reasons: [],
      } };
    }
    return { status: 404, body: { code: "not_found", message: "No sandbox fixture." } };
  });
  return (
    <NewSemanticViewDialog
      open
      projectId={PROJECT_ID}
      onClose={() => undefined}
      onCreated={() => undefined}
    />
  );
}

export function GovernanceControlsQuality() {
  return (
    <RouterProvider>
      <GovernanceCollection projectId={PROJECT_ID} section="controls-quality" lens="conflicts" />
    </RouterProvider>
  );
}

export function GovernanceEvidence() {
  return (
    <RouterProvider>
      <GovernanceCollection projectId={PROJECT_ID} section="evidence" lens="lineage-provenance" />
    </RouterProvider>
  );
}

export function GovernanceObjectWorkbenchSandbox({
  section = "master-data",
  objectType = "business-domain",
  objectId = "bd_sales",
  tab = "overview",
}: {
  section?: string;
  objectType?: string;
  objectId?: string;
  tab?: string;
}) {
  return (
    <RouterProvider>
      <GovernanceObjectWorkbench
        projectId={PROJECT_ID}
        section={section}
        objectType={objectType}
        objectId={objectId}
        tab={tab}
        versionId={null}
      />
    </RouterProvider>
  );
}
