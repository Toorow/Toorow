import type { ReactNode } from "react";
import {
  Panel, PanelHeader, Status, summarizeObject,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  formatBytes, formatNumber,
} from "../../ui";
import type { DatastreamSetupObservation, DatastreamSetupObservedField } from "../wizard/wizardApi";

/** What an observation SAYS about the object it read, drawn once for every mode.
 *
 *  Step 2 is required to state a schema, and before this it rendered six free
 *  text inputs and one line of history/cadence/cost — `safe_metadata.fields` was
 *  on the wire and nothing drew it. It lives here rather than inline because the
 *  Google Sheets pair (57.2) and the channel pair (57.3) need the same block:
 *  one shape of evidence, one rendering, or the three drift apart.
 *
 *  NO EXAMPLE VALUE, and that is a decision rather than an omission. A row value
 *  cannot travel in discovery evidence — `raw_sample` and `sample_rows` are
 *  refused by the normalizer — and the bounded masked sample belongs to step 5.
 *  What a field carries instead is its dotted path, its leaf type, its mode and
 *  the schema's own description, which says more about a column than one of its
 *  rows would. */

const SCHEMA_DESCRIPTION = "Read from the object itself, never typed here. No value is read out of it: "
  + "the bounded masked sample belongs to Preview and validate.";

/** A folder, not a column. The SAME rule the compiler refuses on
 *  (`core.datastream_setup_observations.is_container_field`), reading the same
 *  two fields the server sends — a type and a mode — rather than a second
 *  verdict this screen would compute on its own. */
function isContainerField(field: DatastreamSetupObservedField): boolean {
  const physical = (field.type ?? "").trim().toLowerCase();
  const mode = (field.mode ?? "").trim().toLowerCase();
  return ["record", "struct", "object", "array", "repeated"].includes(physical) || mode === "repeated";
}

export interface DiscoveredSchemaProps {
  observation: DatastreamSetupObservation;
  /** Said by the caller, because the repair differs by mode: an empty BigQuery
   *  object is an IAM grant written in Google Cloud, an empty sheet is a header
   *  row. A generic "nothing found" would send everyone to the wrong place. */
  emptyState: ReactNode;
}

/** Bytes as a person reads them. The exact count stays beside it: a scan
 *  estimate that rounds is a number nobody can check against a bill. */
function byteText(value: number): string {
  return value < 1024
    ? formatBytes(value)
    : `${formatBytes(value)} (${formatNumber(value)} bytes)`;
}

function scanEstimate(cost: unknown): { text: string; measures: string } | null {
  if (!cost || typeof cost !== "object") return null;
  const record = cost as { bytes_scanned_estimate?: unknown; measures?: unknown };
  const bytes = record.bytes_scanned_estimate;
  if (typeof bytes !== "number" || !Number.isFinite(bytes)) return null;
  return {
    text: byteText(bytes),
    measures: typeof record.measures === "string" ? record.measures : "",
  };
}

/** Every evidence line names where it came from — the rule the wizard document
 *  states for every proposal. `Observed schema` is one of its four sources, and
 *  a value with no origin is not evidence. */
function EvidenceLine({ label, value }: { label: string; value: unknown }) {
  const text = value == null || value === "" ? null : String(value);
  return (
    <div className="grid gap-1">
      <span className="text-caption text-text-secondary">{label}</span>
      <span className="text-body text-text">{text ?? "Not observed"}</span>
      <span className="text-caption text-text-secondary">
        {text ? "Observed schema" : "The read returned none; nothing is filled in for you."}
      </span>
    </div>
  );
}

export default function DiscoveredSchema({ observation, emptyState }: DiscoveredSchemaProps) {
  const metadata = observation.safe_metadata;
  const fields = metadata.fields ?? [];
  const coverage = observation.coverage;
  const codes = observation.exceptions.map((item) => item.code);
  const estimate = scanEstimate(metadata.quota_cost);
  const containers = fields.filter(isContainerField).length;
  return (
    <Panel className="grid gap-4 p-4">
      <PanelHeader
        title="Discovered schema"
        description={SCHEMA_DESCRIPTION}
      />

      {fields.length === 0 ? emptyState : (
        <TableScroll label="Discovered schema">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Field</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Mode</TableHead>
                <TableHead>Selectable</TableHead>
                <TableHead>Description</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {fields.map((field, index) => (
                <TableRow key={field.field_id ?? field.name ?? index}>
                  <TableCell className="font-mono text-caption">{field.name ?? field.field_id}</TableCell>
                  <TableCell>{field.type ?? "Unknown"}</TableCell>
                  <TableCell>{field.mode ?? (field.nullable === false ? "REQUIRED" : "NULLABLE")}</TableCell>
                  <TableCell>{isContainerField(field) ? "No — a group, not a column" : "Yes"}</TableCell>
                  <TableCell>{field.description ?? "None declared"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      )}

      {/* THE LIST SAYS HOW LONG IT REALLY IS. Evidence is bounded in bytes, so a
          wide object is listed in part; a partial list that stays silent reads
          as the whole schema, which is the same fault as an object listing that
          hid its bound. The counts come from the server, never from this array's
          own length — that length IS the truncated one. */}
      {coverage.field_list === "truncated" && (
        <Status as="block" tone="warning" title="This list is shorter than the object">
          {`${coverage.fields_listed ?? fields.length} of ${coverage.fields_observed ?? "?"} columns are `}
          {"listed here; discovery evidence is bounded in size. The columns beyond that bound exist and "}
          {"are not shown."}
        </Status>
      )}
      {/* Nested fields are DESCRIBED and refused as columns. Both halves are said:
          hiding them made a table look like it had half its columns. */}
      {containers > 0 && (
        <Status
          as="block"
          tone="neutral"
          title={`${containers} field${containers > 1 ? "s" : ""} cannot be selected as a column`}
        >
          A STRUCT is a folder and a REPEATED field is an array; this product emits no UNNEST, so neither is offered.
        </Status>
      )}
      {codes.includes("schema_depth_truncated") && (
        <Status as="block" tone="warning" title="This schema is deeper than the field catalog reads">
          At least one nested record was not walked, so the list above is not the whole object.
        </Status>
      )}

      <div className="grid grid-cols-3 gap-4">
        <EvidenceLine label="Location" value={metadata.location} />
        <EvidenceLine label="Watermark" value={metadata.watermark} />
        <EvidenceLine label="Freshness" value={metadata.freshness} />
      </div>

      {/* THE COST OF THE READ THAT HAS NOT HAPPENED YET. Rendered by what the
          evidence actually carries: a byte estimate reads as bytes, any other
          quota record reads field by field, and no record at all is stated. The
          consequence of a missing estimate belongs to the mode that has one —
          only External BigQuery refuses a preview over it — so it is said by the
          caller rather than guessed here. */}
      {estimate ? (
        <Status as="block" tone="neutral" title={`Estimated scan: ${estimate.text}`}>
          {estimate.measures || "What one read of this object would scan, planned without being executed."}
        </Status>
      ) : metadata.quota_cost ? (
        <Status as="block" tone="neutral" title="Quota and cost">
          {typeof metadata.quota_cost === "object"
            ? summarizeObject(metadata.quota_cost as Record<string, unknown>)
            : String(metadata.quota_cost)}
        </Status>
      ) : (
        <Status as="block" tone="warning" title="No cost evidence">
          The discovery read carried no quota or cost figure for this object.
        </Status>
      )}
    </Panel>
  );
}
