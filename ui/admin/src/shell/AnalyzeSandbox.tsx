/**
 * The Analyze and Test screens, rendered on their own so they can be LOOKED AT.
 *
 * Eight screens were invisible to every design pass. `ScreenSandbox` registered
 * none of Explore, Reports, Notebooks, Renders, Golden Questions, Regression
 * Runs or Widget Feedback, and the review of `analyze/explore` could check
 * nothing visual as a result — its own words: "sandbox is up (HTTP 200) but none
 * of these screens is wired into it". A design lens that cannot see a screen
 * reports on its source, which is a different and weaker thing.
 *
 * WHY A PROVIDER. `Explore` calls `useRoute()`, which throws outside
 * `RouterProvider`. That single throw is why the whole family was left out
 * rather than registered — `GovernanceSandbox` solved it the same way for its
 * four screens, and this follows it rather than inventing a second pattern.
 *
 * The refusing variant (`&api=refusing`) works because THIS file installs the
 * stub -- see `useSandboxApi` below. The first version of this docstring claimed
 * it worked by inheriting `DataScreenSandbox`'s installer, which was simply
 * false: nothing here installed anything, and a review found the claim within
 * the hour. A comment that asserts a capability the file does not have is the
 * same defect as a screen that does.
 */
import { RouterProvider } from "./router";
import { installSandboxApi } from "./sandboxApi";
import Explore from "./pages/Explore";
import GoldenQuestions from "./pages/GoldenQuestions";
import RegressionRuns from "./pages/RegressionRuns";
import WidgetFeedback from "./pages/WidgetFeedback";
import { ReportsCollection, ReportWorkbench } from "../analyze-artifacts/Reports";
import { NotebooksCollection } from "../analyze-artifacts/Notebooks";
import { RendersCollection } from "../analyze-artifacts/Renders";

const PROJECT_ID = "proj_EXAMPLE";

/** Every screen here is mounted with its open callbacks SUPPLIED.
 *
 *  Not decoration: three Test collections draw their rows as links only when a
 *  callback is given, and the shell had been forgetting to pass them — every id
 *  was inert text (`411582a4`). A sandbox that mounted them bare would have
 *  rendered the broken state and called it the design. */
const noop = () => undefined;

function sandboxSide(
  datastreamId: string,
  name: string,
  canonicalFieldId: string,
  measureName: string,
) {
  return {
    datastream_id: datastreamId,
    name,
    mapping_version_id: `dmv_${datastreamId}`,
    mapping_version_number: 1,
    published_execution_id: `dxe_${datastreamId}`,
    output_version_id: `dov_${datastreamId}`,
    measures: [{ canonical_field_id: canonicalFieldId, name: measureName, aggregation: "sum" }],
  };
}

function sandboxExtension(
  left: ReturnType<typeof sandboxSide>,
  right: ReturnType<typeof sandboxSide>,
  relationshipName: string,
) {
  return {
    kind: "governed",
    authority: "governed",
    observed_coverage: "exact",
    execution_safety: "ready",
    left,
    right,
    common_key: {
      id: "mck_campaign_day",
      name: "Day and Campaign",
      version_id: "mckv_campaign_day_3",
      version_number: 3,
      components: [
        { canonical_field_id: "mdm_day", canonical_name: "Day" },
        { canonical_field_id: "mdm_campaign", canonical_name: "Campaign" },
      ],
    },
    key_paths: [
      { canonical_name: "Day", canonical_field_id: "mdm_day", left_field: "date", right_field: "date" },
      { canonical_name: "Campaign", canonical_field_id: "mdm_campaign", left_field: "campaign_id", right_field: "campaign_id" },
    ],
    relationship: {
      relationship_name: relationshipName,
      cardinality: "many_to_one",
      fan_out_policy: "aggregate_before_merge",
      bridge_dataset: null,
      view_id: "sv_campaign_performance",
      view_version_id: "svv_campaign_performance_6",
      view_version_number: 6,
      business_domain_refs: ["bdm_growth"],
    },
    unlocked_measures: left.measures.length + right.measures.length,
    analysis: `${left.name} and ${right.name}, by Day and Campaign.`,
    explore_together: {
      datastreams: [left, right].map((side) => ({
        datastream_id: side.datastream_id,
        mapping_version_id: side.mapping_version_id,
        published_execution_id: side.published_execution_id,
        output_version_id: side.output_version_id,
      })),
      common_key_version_id: "mckv_campaign_day_3",
      view_version_id: "svv_campaign_performance_6",
      relationship_name: relationshipName,
    },
  };
}

const SANDBOX_CONVERSIONS = sandboxSide(
  "ds_conversions", "Conversions", "mdm_conversions", "Conversions",
);
const SANDBOX_REVENUE = sandboxSide(
  "ds_revenue", "Attributed revenue", "mdm_revenue", "Revenue",
);
const SANDBOX_PIPELINE = sandboxSide(
  "ds_pipeline", "CRM pipeline", "mdm_pipeline", "Pipeline",
);

/** The refusing variant, which this file forgot on its first pass.
 *
 *  A review found it within the hour: `&api=refusing` was INERT for every
 *  screen here, because nothing installed the stub. So the harness offered an
 *  error capture it could not produce -- exactly the defect repaired in
 *  `c1d5ae6e` for the Data sandbox, reintroduced by me in a new file.
 *
 *  The fixtures stay empty on purpose. These screens answer their own honest
 *  empty state, which is the state worth looking at while nothing has been
 *  created; a fabricated Report or Notebook would be evidence about nothing.
 *  What the stub adds is the ability to ASK for the refusal. */
function useSandboxApi() {
  installSandboxApi((url) => {
    if (url.includes("/test/golden-questions?")) {
      return {
        body: {
          golden_questions: [{
            id: "gq_campaign_efficiency",
            title: "Are campaign investments producing qualified conversions?",
            current_version_id: "gqv_campaign_efficiency_3",
            current_version: {
              version_number: 3,
              business_domain_id: "bdm_growth",
              business_domain_version_number: 4,
              semantic_view_version_id: "svv_campaign_performance_6",
            },
          }],
        },
        status: 200,
      };
    }
    if (url.includes("/api/context/procedures?")) {
      return {
        body: {
          procedures: [{
            id: "proc_paid_media_investigation",
            name: "Paid media investigation",
            version_number: 7,
          }],
        },
        status: 200,
      };
    }
    if (url.includes("/api/context/business-taxonomy?")) {
      return {
        body: {
          domains: [
            {
              id: "bdm_growth",
              name: "Growth",
              status: "active",
              version_number: 4,
            },
          ],
          classifications: [],
        },
        status: 200,
      };
    }
    if (url.includes("/analyze/matches/profile")) {
      return {
        body: {
          profile: {
            components: [
              { canonical_field_id: "mdm_day", canonical_name: "Day" },
              { canonical_field_id: "mdm_campaign", canonical_name: "Campaign" },
            ],
            left: {
              datastream_id: "ds_spend",
              name: "Campaign spend",
              state: "exact",
              total_rows: 1240,
              null_key_rows: 0,
              distinct_keys: 318,
              duplicate_state: "exact",
              duplicated_keys: 12,
              max_rows_per_key: 4,
            },
            right: {
              datastream_id: "ds_conversions",
              name: "Conversions",
              state: "exact",
              total_rows: 876,
              null_key_rows: 3,
              distinct_keys: 302,
              duplicate_state: "exact",
              duplicated_keys: 0,
              max_rows_per_key: 1,
            },
            matched: {
              state: "exact",
              matched_keys: 286,
              left_unmatched_keys: 32,
              right_unmatched_keys: 16,
            },
            multiplication: {
              state: "exact",
              worst_case_rows_per_key: 4,
              explanation: "Twelve campaign keys repeat on Campaign spend. Review the measured multiplication before running.",
            },
            execution_safety: "review_required",
          },
        },
        status: 200,
      };
    }
    if (url.endsWith("/analyze/matches")) {
      return {
        body: {
          matches: [
            {
              kind: "governed",
              authority: "governed",
              observed_coverage: "exact",
              execution_safety: "review_required",
              left: {
                datastream_id: "ds_spend",
                name: "Campaign spend",
                mapping_version_id: "dmv_spend",
                mapping_version_number: 4,
                published_execution_id: "dxe_spend_7",
                output_version_id: "dov_spend_7",
                measures: [
                  { canonical_field_id: "mdm_spend", name: "Spend", aggregation: "sum" },
                ],
              },
              right: {
                datastream_id: "ds_conversions",
                name: "Conversions",
                mapping_version_id: "dmv_conversions",
                mapping_version_number: 2,
                published_execution_id: "dxe_conversions_4",
                output_version_id: "dov_conversions_4",
                measures: [
                  { canonical_field_id: "mdm_conversions", name: "Conversions", aggregation: "sum" },
                  { canonical_field_id: "mdm_revenue", name: "Revenue", aggregation: "sum" },
                ],
              },
              common_key: {
                id: "mck_campaign_day",
                name: "Day and Campaign",
                version_id: "mckv_campaign_day_3",
                version_number: 3,
                components: [
                  { canonical_field_id: "mdm_day", canonical_name: "Day" },
                  { canonical_field_id: "mdm_campaign", canonical_name: "Campaign" },
                ],
              },
              key_paths: [
                { canonical_name: "Day", canonical_field_id: "mdm_day", left_field: "date", right_field: "event_date" },
                { canonical_name: "Campaign", canonical_field_id: "mdm_campaign", left_field: "campaign_id", right_field: "campaign_key" },
              ],
              relationship: {
                relationship_name: "spend_to_conversions",
                cardinality: "many_to_one",
                fan_out_policy: "aggregate_before_merge",
                bridge_dataset: null,
                view_id: "sv_campaign_performance",
                view_version_id: "svv_campaign_performance_6",
                view_version_number: 6,
                business_domain_refs: ["bdm_growth"],
              },
              unlocked_measures: 3,
              analysis: "Campaign spend and Conversions, by Day and Campaign.",
              explore_together: {
                datastreams: [
                  { datastream_id: "ds_spend", mapping_version_id: "dmv_spend", published_execution_id: "dxe_spend_7", output_version_id: "dov_spend_7" },
                  { datastream_id: "ds_conversions", mapping_version_id: "dmv_conversions", published_execution_id: "dxe_conversions_4", output_version_id: "dov_conversions_4" },
                ],
                common_key_version_id: "mckv_campaign_day_3",
                view_version_id: "svv_campaign_performance_6",
                relationship_name: "spend_to_conversions",
              },
            },
            sandboxExtension(
              SANDBOX_CONVERSIONS,
              SANDBOX_REVENUE,
              "conversions_to_revenue",
            ),
            sandboxExtension(
              SANDBOX_REVENUE,
              SANDBOX_PIPELINE,
              "revenue_to_pipeline",
            ),
          ],
          counts: { datastreams_published: 4, governed: 3, candidates: 0, returned: 3 },
          bounds: { max_datastreams_scanned: 200, max_matches: 50, truncated: false },
          observed_coverage_state: "exact",
          empty_reason: null,
        },
        status: 200,
      };
    }
    if (url.endsWith("/analyze/multi-source/plans")) {
      return {
        body: {
          query_spec_id: "qs_SANDBOX_CROSS",
          query_spec_version_id: "qsv_SANDBOX_CROSS_1",
          content_hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          plan: {
            contract_version: "multi-source-plan.v1",
            analysis_context: {
              contract_version: "analysis-context.v1",
              semantic_view_version_id: "svv_campaign_performance_6",
              business_domain: {
                id: "bdm_growth", version_number: 4,
                version_id: "bdm_growth:4", name: "Growth",
              },
              golden_question: {
                id: "gq_campaign_efficiency",
                version_id: "gqv_campaign_efficiency_3",
                version_number: 3,
                title: "Are campaign investments producing qualified conversions?",
                content_hash: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
              },
              requested_skills: [{
                procedure_id: "proc_paid_media_investigation",
                version_number: 7,
                version_id: "proc_paid_media_investigation@7",
                name: "Paid media investigation",
              }],
            },
            members: [
              {
                datastream_id: "ds_spend",
                published_execution_id: "dxe_spend_7",
                output_version_id: "dov_spend_7",
                measures: [{ canonical_field_id: "mdm_spend", result_field: "m_mdm_spend" }],
              },
              {
                datastream_id: "ds_conversions",
                published_execution_id: "dxe_conversions_4",
                output_version_id: "dov_conversions_4",
                measures: [
                  { canonical_field_id: "mdm_conversions", result_field: "m_mdm_conversions" },
                  { canonical_field_id: "mdm_revenue", result_field: "m_mdm_revenue" },
                ],
              },
            ],
          },
        },
        status: 201,
      };
    }
    if (url.endsWith("/analyze/multi-source/plans/qsv_SANDBOX_CROSS_1/execute")) {
      return {
        body: {
          result: { result_id: "qr_SANDBOX_CROSS", outcome: "success", row_count: 4 },
        },
        status: 201,
      };
    }
    if (url.endsWith("/analyze/results/qr_SANDBOX_CROSS/evidence")) {
      return {
        body: {
          result_id: "qr_SANDBOX_CROSS",
          content_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          outcome: "success",
          row_count: 4,
          truncated: false,
          schema: {
            // Shaped like the real Result: a money column says it is money and
            // in which currency, because it is stored in canonical micros and a
            // reader that ignores that prints 124000000 for 124 EUR.
            fields: [
              { name: "k_mdm_day" },
              { name: "k_mdm_campaign" },
              { name: "m_mdm_spend", value_type: "money", unit: "EUR" },
              { name: "m_mdm_conversions", value_type: "integer", unit: null },
              { name: "m_mdm_revenue", value_type: "money", unit: "EUR" },
            ],
          },
          manifest: { contract_version: "multi-source-result.v1" },
          rows: [
            { k_mdm_day: "2026-08-11", k_mdm_campaign: "Brand", m_mdm_spend: 124000000, m_mdm_conversions: 42, m_mdm_revenue: 318000000 },
            { k_mdm_day: "2026-08-11", k_mdm_campaign: "Prospecting", m_mdm_spend: 87000000, m_mdm_conversions: 19, m_mdm_revenue: 142000000 },
            { k_mdm_day: "2026-08-12", k_mdm_campaign: "Brand", m_mdm_spend: 119000000, m_mdm_conversions: 38, m_mdm_revenue: 301000000 },
            { k_mdm_day: "2026-08-12", k_mdm_campaign: "Prospecting", m_mdm_spend: null, m_mdm_conversions: 21, m_mdm_revenue: 156000000 },
          ],
        },
        status: 200,
      };
    }
    if (url.endsWith("/analyze/results/qr_SANDBOX_CROSS/pivot")) {
      return {
        body: {
          pivot: {
            result_id: "qr_SANDBOX_CROSS",
            content_hash: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            row_fields: ["k_mdm_day"],
            column_fields: ["k_mdm_campaign"],
            value_fields: [
              { name: "m_mdm_spend", canonical_field_id: "mdm_spend", role: "measure", aggregation: "sum", datastream_id: "ds_spend", value_type: "money", unit: "EUR" },
              { name: "m_mdm_conversions", canonical_field_id: "mdm_conversions", role: "measure", aggregation: "sum", datastream_id: "ds_conversions", value_type: "integer", unit: null },
              { name: "m_mdm_revenue", canonical_field_id: "mdm_revenue", role: "measure", aggregation: "sum", datastream_id: "ds_conversions", value_type: "money", unit: "EUR" },
            ],
            row_keys: [["2026-08-11"], ["2026-08-12"]],
            column_keys: [["Brand"], ["Prospecting"]],
            cells: [
              { row_key: ["2026-08-11"], column_key: ["Brand"], values: { m_mdm_spend: { value: 124000000 }, m_mdm_conversions: { value: 42 }, m_mdm_revenue: { value: 318000000 } }, contributing_rows: 1, contributing_row_indexes: [0] },
              { row_key: ["2026-08-11"], column_key: ["Prospecting"], values: { m_mdm_spend: { value: 87000000 }, m_mdm_conversions: { value: 19 }, m_mdm_revenue: { value: 142000000 } }, contributing_rows: 1, contributing_row_indexes: [1] },
              { row_key: ["2026-08-12"], column_key: ["Brand"], values: { m_mdm_spend: { value: 119000000 }, m_mdm_conversions: { value: 38 }, m_mdm_revenue: { value: 301000000 } }, contributing_rows: 1, contributing_row_indexes: [2] },
              { row_key: ["2026-08-12"], column_key: ["Prospecting"], values: { m_mdm_spend: { value: null, absent_reason: "no_contributing_row" }, m_mdm_conversions: { value: 21 }, m_mdm_revenue: { value: 156000000 } }, contributing_rows: 1, contributing_row_indexes: [3] },
            ],
            bounds: {
              max_cells: 20000,
              max_row_keys: 500,
              max_column_keys: 100,
              rows_truncated: false,
              columns_truncated: false,
              rows_dropped: 0,
              columns_dropped: 0,
              cells_truncated: false,
            },
          },
        },
        status: 200,
      };
    }
    if (url.includes("/analyze/legacy-notebooks")) {
      return { body: { notebooks: [] }, status: 200 };
    }
    if (url.includes("/analyze/notebooks")) {
      return { body: { notebooks: [] }, status: 200 };
    }
    if (url.includes("/analyze/reports/rep_EXAMPLE")) {
      return {
        body: {
          id: "rep_EXAMPLE",
          label: "Weekly traffic",
          description: "Traffic and acquisition intent for the weekly review.",
          seed_origin: "project",
          seed: null,
          current_version_id: "repv_EXAMPLE",
          archived: false,
          created_by: "owner@example.com",
          created_at: "2026-08-12T09:00:00Z",
          updated_at: "2026-08-12T09:00:00Z",
          current_version_number: 1,
          query_spec_version_id: "qsv_EXAMPLE",
          presentation_absent: "No accepted presentation contract",
          run_count: 2,
          versions: [
            {
              id: "repv_EXAMPLE",
              version_number: 1,
              label: "Weekly traffic",
              query_spec_id: "qs_EXAMPLE",
              query_spec_version_id: "qsv_EXAMPLE",
              presentation: { kind: null, version_id: null, absent_literal: "No accepted presentation contract" },
              content_hash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
              predecessor_version_id: null,
              created_by: "owner@example.com",
              created_at: "2026-08-12T09:00:00Z",
            },
          ],
          runs: [],
        },
        status: 200,
      };
    }
    if (url.includes("/analyze/reports")) {
      return {
        body: {
          reports: [
            {
              id: "rep_EXAMPLE",
              label: "Weekly traffic",
              description: "Traffic and acquisition intent for the weekly review.",
              seed_origin: "project",
              seed: null,
              current_version_id: "repv_EXAMPLE",
              archived: false,
              created_by: "owner@example.com",
              created_at: "2026-08-12T09:00:00Z",
              updated_at: "2026-08-12T09:00:00Z",
              current_version_number: 1,
              query_spec_version_id: "qsv_EXAMPLE",
              presentation_absent: "No accepted presentation contract",
              run_count: 2,
            },
          ],
          seeds: [],
          presentation_contract: { available: true, missing: [] },
        },
        status: 200,
      };
    }
    if (url.includes("/golden-questions/options")) {
      return {
        body: {
          business_domains: [
            { id: "dom_EXAMPLE", name: "Media Performance", status: "active", latest_version_number: 3 },
          ],
          business_classifications: [],
          semantic_view_versions: [
            { semantic_view_version_id: "svv_EXAMPLE", semantic_view_id: "sv_EXAMPLE", name: "Campaign Performance", version_number: 2, status: "published" },
          ],
          semantic_view_version_roles: ["baseline", "candidate"],
          result_types: ["scalar", "series", "breakdown", "table", "refusal"],
          severities: ["critical", "major", "minor"],
          lifecycles: ["draft", "active", "deprecated", "archived"],
          lifecycle_transitions: { draft: ["active", "archived"], active: ["deprecated", "archived"], deprecated: ["active", "archived"], archived: [] },
          assertion_types: ["value", "row_set", "ordering", "cardinality", "invariant", "empty", "degraded", "refused"],
          tolerance_kinds: ["numeric", "temporal", "set"],
          provenance_link_kinds: ["source", "mapping", "publication", "semantic_view", "citation"],
          path_step_kinds: ["tool_call", "knowledge_read", "skill_step", "semantic_query", "data_read", "handoff"],
          path_owner_workspaces: ["data", "governance", "analyze", "context-hub", "test"],
          reference_path_roles: ["canonical", "alternative"],
          capability_rule: "A capability names a governed product capability.",
        },
        status: 200,
      };
    }
    if (url.includes("/golden-questions")) {
      return {
        body: {
          golden_questions: [],
          total: 0,
          bound: 25,
          next_cursor: null,
          applied_filters: {},
          filter_options: { lifecycles: ["draft", "active", "deprecated", "archived"] },
        },
        status: 200,
      };
    }
    return { body: {}, status: 200 };
  });
}

export function AnalyzeExplore() {
  useSandboxApi();
  return (
    <RouterProvider>
      <Explore projectId={PROJECT_ID} />
    </RouterProvider>
  );
}

export function AnalyzeReports() {
  useSandboxApi();
  return (
    <RouterProvider>
      <ReportsCollection projectId={PROJECT_ID} onOpenReport={noop} />
    </RouterProvider>
  );
}

export function AnalyzeReportWorkbench() {
  useSandboxApi();
  return (
    <RouterProvider>
      <ReportWorkbench
        projectId={PROJECT_ID}
        reportId="rep_EXAMPLE"
        tab="presentation"
        onNavigateTab={noop}
        tabHref={(tab) => `#${tab}`}
        collectionHref="#reports"
        onOpenResult={noop}
      />
    </RouterProvider>
  );
}

export function AnalyzeNotebooks() {
  useSandboxApi();
  return (
    <RouterProvider>
      <NotebooksCollection projectId={PROJECT_ID} onOpenNotebook={noop} />
    </RouterProvider>
  );
}

export function AnalyzeRenders() {
  useSandboxApi();
  return (
    <RouterProvider>
      <RendersCollection projectId={PROJECT_ID} onOpenRender={noop} onOpenResult={noop} />
    </RouterProvider>
  );
}

export function TestGoldenQuestions() {
  useSandboxApi();
  return (
    <RouterProvider>
      <GoldenQuestions projectId={PROJECT_ID} onOpenGoldenQuestion={noop} />
    </RouterProvider>
  );
}

export function TestRegressionRuns() {
  useSandboxApi();
  return (
    <RouterProvider>
      <RegressionRuns
        projectId={PROJECT_ID}
        onOpenEvaluationRun={noop}
        onOpenTraceObservation={noop}
      />
    </RouterProvider>
  );
}

export function TestWidgetFeedback() {
  useSandboxApi();
  return (
    <RouterProvider>
      <WidgetFeedback projectId={PROJECT_ID} onOpenFeedback={noop} />
    </RouterProvider>
  );
}
