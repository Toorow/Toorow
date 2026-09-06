/**
 * The `lineage` tab of a Canonical Field: which source columns FEED this
 * dimension, and what the client calls it.
 *
 * WHY IT EXISTS. `core/dimension_lineage.py` and its four routes shipped, and
 * the audit of 2026-08-17 measured that no `.tsx` anywhere read
 * `dimension-lineage` or `metric-semantics` -- three of the four planes built,
 * the screen absent, and the "API without a screen" posture ratified nowhere
 * (unlike fee/tax, which wrote its posture down). This tab is that reading.
 *
 * THREE STATES, NEVER FOLDED. `get_fed_by` is deliberately fail-soft: it answers
 * with whatever it could compose PLUS a `gaps[]` naming what it could not. So a
 * short list here is not a small dimension -- it may be a dimension whose plan
 * version could not be read. The gaps are rendered as loudly as the rows, and a
 * failed read renders no table at all: a panel that could not read must not look
 * like a panel that read and found nothing.
 *
 * LINEAGE IS A DIMENSION QUESTION. A canonical field of kind `metric` has no
 * fed-by reading -- nothing feeds a metric the way a column feeds a dimension --
 * so the tab says so in one sentence instead of showing an empty table that
 * would read as "nothing feeds it".
 *
 * The label shown is the CLIENT's, resolved by the same cascade every render
 * uses (`label_source` says whether a client named it or whether the stable
 * identifier is standing in). No new base class, no literal spacing: every
 * element is a primitive from `ui/index.ts`.
 *
 * AND THE NAME IS NAMEABLE HERE. Until 2026-08-24 this tab PRINTED "No client
 * name: the stable identifier is standing in" and offered nothing -- the
 * console door `governance.md:1950` ratifies (`POST /api/dimension-lineage/labels`)
 * had no caller anywhere in `ui/admin/src`. A screen that states a gap and
 * sends a person elsewhere for the gesture that closes it is unfinished, so the
 * reading and the gesture are one panel now (`DimensionLabelPanel`), and the
 * label is read ONCE on this screen: the fed-by envelope already carries
 * `display_label`, `label_scope` and `label_source`, and a second read of the
 * same cascade would be a second answer free to disagree with the first.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, apiGet } from "../lib/apiFetch";
import { useRoute } from "../shell/router";
import {
  EmptyState,
  ObjectId,
  Panel,
  PanelHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  stateLabel,
  stateTone,
} from "../ui";
import DimensionLabelPanel from "./DimensionLabelPanel";
import type { GovernanceObject } from "./governanceSurface";

/** The gap reasons `dimension_lineage` emits, in the words a reader can act on. */
const GAP_REASON: Record<string, string> = {
  no_plan_version: "This connector has no published plan version, so its columns are unknown.",
  connector_unknown: "This connector is not in the manifest index.",
  schema_mapping_unavailable: "The connector manifest declares no canonical dimension mapping.",
  no_declared_dimensions: "The connector manifest declares no dimensions at all.",
  no_plan_evidence: "The plan version names this connector but no field evidence was recorded.",
};

/*
 * THE PRIVATE MAPPING-STATE MAP IS GONE (76-2). Four of its five words were the
 * union's already; `none` is declared, neutral, spelled "Not mapped".
 */

interface FedByRow {
  connector: string;
  report_id: string;
  source_field: string;
  schema_link: string;
  datastreams?: unknown[];
  mapping?: Record<string, unknown> | null;
}

interface Gap {
  connector: string;
  reason: string;
  datastream_id?: string;
}

/** What ONE scope stores in its own right — `null` where it stores nothing. */
export interface ScopeLabel {
  display_label: string | null;
  description?: string | null;
  scope_level?: string | null;
}

interface FedByEnvelope {
  canonical_dimension: string;
  display_label: string | null;
  label_scope: string | null;
  label_source: string;
  /**
   * The word EACH scope carries, winner and non-winner alike. The three keys
   * above say what a READER sees; this says what a WRITER would rename, and
   * they are different questions the moment a project overrides an
   * organization. Same read, same envelope — asking the cascade twice on one
   * screen is two answers free to disagree.
   */
  scope_labels?: Record<string, ScopeLabel | null> | null;
  scope: { project_id: string | null; org_id: string | null };
  fed_by: FedByRow[];
  gaps: Gap[];
}

export function DimensionLineageTab({ detail }: { detail: GovernanceObject }) {
  // The scope comes from the ROUTER, exactly as `SemanticsTab` reads it: this
  // tab is only ever mounted on a project address, and a second copy of the
  // scope travelling through the workbench is a second thing to keep in step.
  const { route } = useRoute();
  const projectId = route.projectId ?? "";
  const summary = (detail.summary ?? {}) as Record<string, unknown>;
  const conceptKind = typeof summary.concept_kind === "string" ? summary.concept_kind : "";
  // The STABLE identifier, never the display label -- the server carries it
  // beside the label for exactly this read.
  const canonicalName =
    typeof summary.canonical_name === "string" && summary.canonical_name
      ? summary.canonical_name
      : detail.object_ref.label;

  const [payload, setPayload] = useState<FedByEnvelope | null>(null);
  const [broken, setBroken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const isDimension = conceptKind === "dimension";

  const load = useCallback(async () => {
    if (!isDimension || !projectId || !canonicalName) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const body = await apiGet<FedByEnvelope>(
        `/api/dimension-lineage/fed-by?project_id=${encodeURIComponent(projectId)}` +
          `&canonical_dimension=${encodeURIComponent(canonicalName)}`,
      );
      // A 200 carrying the wrong shape is a failed read, not a reading.
      if (!body || typeof body !== "object" || !Array.isArray(body.fed_by)) {
        setPayload(null);
        setBroken("The answer did not carry a lineage reading.");
        return;
      }
      setPayload({ ...body, fed_by: body.fed_by ?? [], gaps: body.gaps ?? [] });
      setBroken(null);
    } catch (err) {
      setPayload(null);
      setBroken(err instanceof ApiError ? err.message : "The lineage reading failed.");
    } finally {
      setLoading(false);
    }
  }, [isDimension, projectId, canonicalName]);

  useEffect(() => {
    void load();
  }, [load]);

  // A metric has no fed-by reading. Saying so is the honest answer; an empty
  // table would read as "no column feeds this dimension", which was never asked.
  if (!isDimension) {
    return (
      <EmptyState
        title="Lineage is a dimension reading"
        description={
          `This canonical field is declared as ${conceptKind || "an unknown kind"}. ` +
          "Only a dimension is fed by source columns; a metric is computed, and what " +
          "governs its aggregation is on its Definition tab."
        }
      />
    );
  }

  if (loading) {
    return <EmptyState title="Reading the lineage…" />;
  }

  // A panel that could not read must not look like a panel that read and found
  // nothing: no table is drawn at all.
  if (broken) {
    return (
      <Status as="block" tone="error" title="The lineage could not be read"
          action={<Retry onClick={() => void load()} />}
        >
        {broken} This is not a dimension with no sources: nothing was measured.
      </Status>
    );
  }

  if (!payload) {
    return (
      <Status as="block" tone="error" title="The lineage could not be read"
          action={<Retry onClick={() => void load()} />}
        >
        No reading was returned. This is not a dimension with no sources.
      </Status>
    );
  }

  const rows = payload.fed_by;
  const gaps = payload.gaps ?? [];

  return (
    <Stack className="gap-6" data-testid="dimension-lineage">
      <DimensionLabelPanel
        canonicalDimension={canonicalName}
        projectId={projectId}
        organizationId={route.organizationId ?? ""}
        displayLabel={payload.display_label}
        labelScope={payload.label_scope}
        labelSource={payload.label_source}
        scopeLabels={payload.scope_labels ?? null}
        onChanged={load}
      />

      {/* The gaps are as loud as the rows: a short list may be an unread plan. */}
      {gaps.length > 0 && (
        <Status
          as="block"
          tone="warning"
          title={`${gaps.length} connector(s) could not be read`}
        >
          <Stack className="gap-1">
            {gaps.map((gap, index) => (
              <div key={`${gap.connector}-${index}`}>
                <ObjectId value={gap.connector} title="Connector key" />{" "}
                {GAP_REASON[gap.reason] ?? gap.reason}
              </div>
            ))}
          </Stack>
          The list below is therefore partial: it is not the whole of what feeds
          this dimension.
        </Status>
      )}

      {rows.length === 0 ? (
        <EmptyState
          title="No published plan declares a column feeding this dimension"
          description={
            "A column starts feeding a dimension when a Datastream's published mapping " +
            "binds it to this canonical field. Bind one on the Datastream's Map tab."
          }
        />
      ) : (
        <Panel>
          <PanelHeader
            title="Fed by"
            description={
              `${rows.length} source column(s) the published plans bind to this ` +
              "dimension."
            }
          />
          <TableScroll label="Source columns feeding this dimension">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Connector</TableHead>
                  <TableHead>Report</TableHead>
                  <TableHead>Source field</TableHead>
                  <TableHead>Schema link</TableHead>
                  <TableHead>Mapping</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row, index) => {
                  const status = String(row.mapping?.status ?? "none");
                  return (
                    <TableRow
                      key={`${row.connector}-${row.report_id}-${row.source_field}-${index}`}
                      data-testid={`lineage-row-${row.connector}-${row.source_field}`}
                    >
                      <TableCell>
                        <ObjectId value={row.connector} title="Connector key" />
                      </TableCell>
                      <TableCell><ObjectId value={row.report_id} title="Report profile key" /></TableCell>
                      <TableCell>
                        <ObjectId value={row.source_field} title="Source field name" />
                      </TableCell>
                      <TableCell>
                        {row.schema_link === "manifest" ? (
                          "Declared in the manifest"
                        ) : (
                          <Status tone="warning">
                            No manifest declaration
                          </Status>
                        )}
                      </TableCell>
                      <TableCell>
                        {/* The sentence comes from the vocabulary too: `none` reads "Not mapped"
                            there, and the ternary that used to say it here was the
                            only reason the raw token reached the cell otherwise. */}
                        <Status tone={stateTone(status)}>{stateLabel(status)}</Status>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      )}
    </Stack>
  );
}
