/**
 * The live preview: the validated plan, the disclosures and the accessible table.
 *
 * THE TABLE IS THE ACCESSIBLE FALLBACK, NOT A SECOND RENDERER. The Builder mounts
 * the shared Visualization runtime next to this evidence panel once a Spec is
 * saved. Keeping the table here preserves exact values and keyboard access while
 * Console, MCP App and Share continue to use one canonical chart renderer.
 *
 * THE TEN STATES ARE DISTINCT (AC9). In particular `empty` and `unavailable` are
 * not the same statement and never render the same: `empty` means the query ran
 * on the governed path and nothing matched -- the path is healthy; `unavailable`
 * means we could not ask, and it shows the exact `unavailable_reason` and
 * `missing_link` the Result manifest carries.
 *
 * FRESHNESS IS SHOWN ONLY WHEN IT EXISTS. Story 50.1 states plainly that the
 * Result manifest does not carry freshness yet. Rendering an absent field as
 * "fresh" is the failure this clause exists to prevent, so the absence is
 * printed as an absence.
 */
import {
  EmptyState,
  Panel,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  stateTone,
} from "../../ui";
import type {
  PreviewState,
  ResultDisclosures,
  VisualizationFamily,
  VisualizationRefusal,
} from "../visualizationClient";
import type { Bindings } from "./BindingWells";

export interface PreviewResult {
  outcome: string;
  rows: Record<string, unknown>[];
  schema: { fields?: { name: string }[] };
  manifest: Record<string, unknown>;
  rowCount: number;
  truncated: boolean;
}

/** Which state the preview is in. Derived from server facts only. */
export function previewState(
  result: PreviewResult | null,
  refusals: VisualizationRefusal[],
  loading: boolean,
  denied: boolean,
): PreviewState {
  if (loading) return "loading";
  if (denied) return "denied";
  if (refusals.length > 0) return "refused";
  if (!result) return "unknown";
  if (result.outcome === "unavailable") return "unavailable";
  if (result.outcome === "refused") return "refused";
  if (result.outcome === "empty") return "empty";
  if (result.outcome === "degraded") return "partial";
  if (result.truncated) return "truncated";
  return "ready";
}

const STATE_SENTENCE: Record<PreviewState, string> = {
  loading: "Reading the pinned Result.",
  empty: "The query ran on the governed path and nothing matched. The path is healthy.",
  partial: "The Result is degraded: some of what was asked for is missing, and it is named below.",
  truncated: "The Result was truncated. The counts below are the server's, unchanged.",
  stale: "The Result is older than the freshness the manifest records.",
  incompatible: "This presentation does not fit this Result. Every reason is listed.",
  refused: "The presentation was refused. Every reason is listed, each with one action.",
  unavailable: "The query could not be asked. The missing link is named below.",
  denied: "You do not have access to this object, or it does not exist.",
  unknown: "No Result is pinned to this Builder, so there is nothing to preview yet.",
  ready: "The Result answered. Below is the plan and the accessible table.",
};

/*
 * THE PRIVATE PREVIEW MAP IS GONE (76-2 review). It disagreed with the union
 * about four words at once and declared none of them: `empty` cyan where the
 * union says warning (`info` is a statement about the deployment, never a
 * verdict on a Result), `unavailable` amber where the union says neutral (the
 * server could not ask -- a silence), `denied` amber where the union says error
 * (a refusal is a refusal), and `unknown` GREY, which is exactly the reading
 * README invariant 8 refuses. `loading`, `truncated` and `incompatible` are
 * declared words now. `STATE_SENTENCE` above stays: the sentence explaining a
 * preview is this screen's copy, not the state's name.
 */

function fallbackColumns(family: VisualizationFamily, bindings: Bindings): string[] {
  // Which columns the fallback must carry is DECLARED by the family; the
  // projection that renders them is Story 50.5's. This preview reads the
  // declaration and pulls those members out of the Result payload it already has.
  const columns: string[] = [];
  for (const well of family.table_fallback_wells) {
    for (const member of bindings[well] ?? []) {
      if (!columns.includes(member)) columns.push(member);
    }
  }
  return columns;
}

export default function BuilderPreview({
  state,
  family,
  bindings,
  result,
  disclosures,
  refusals,
  rendererMounted = false,
}: {
  state: PreviewState;
  family: VisualizationFamily;
  bindings: Bindings;
  result: PreviewResult | null;
  disclosures: ResultDisclosures | null;
  refusals: VisualizationRefusal[];
  rendererMounted?: boolean;
}) {
  const declared = fallbackColumns(family, bindings);
  // Never invent a column: fall back to the schema the server returned, which is
  // the only other list of field names that exists.
  const columns =
    declared.length > 0 ? declared : (result?.schema.fields ?? []).map((f) => f.name);
  const freshness = result?.manifest?.["freshness"];

  return (
    <Panel flush aria-labelledby="preview-heading">
      <PanelHeader
        title="Preview"
        description={`${family.label} · ${family.description}`}
      />
      <div className="flex flex-col gap-4 p-5">
        <Status
          as="block"
          tone={stateTone(state)}
          title={`Preview state: ${state}`}
          data-testid={`preview-state-${state}`}
        >
          <span role="status">{STATE_SENTENCE[state]}</span>
        </Status>

        {!rendererMounted ? (
          <Status as="block" tone="info" title="Save to render this draft" data-testid="no-renderer">
            The shared runtime renders only an immutable Visualization Spec version. Save this
            draft to preview the chart; the accessible table below remains the exact Result.
          </Status>
        ) : null}

        {state === "unavailable" && result && (
          <Status as="block" tone="warning" title="The query could not be asked">
            <span data-testid="unavailable-reason">
              {String(result.manifest?.["unavailable_reason"] ?? "no reason recorded")}
            </span>
            {" · missing link: "}
            <span data-testid="missing-link">
              {String(result.manifest?.["missing_link"] ?? "not recorded")}
            </span>
          </Status>
        )}

        {state === "empty" && (
          <EmptyState
            title="No rows matched"
            description="The governed query executed and returned nothing. That is an answer, not a failure."
          />
        )}

        {refusals.length > 0 && (
          <ul className="m-0 flex list-none flex-col gap-2 p-0" data-testid="preview-refusals">
            {refusals.map((refusal, index) => (
              <li key={`${refusal.code}-${index}`}>
                <Status as="block" tone="error" title={refusal.code.replace(/_/g, " ")}>
                  {refusal.message}
                  {refusal.remedy ? ` ${refusal.remedy}` : ""}
                  {refusal.subject ? ` (${refusal.subject})` : ""}
                </Status>
              </li>
            ))}
          </ul>
        )}

        <section aria-labelledby="preview-heading">
          <h3 id="preview-heading" className="m-0 mb-2 text-h3 font-h3 text-text">
            The validated plan
          </h3>
          <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-caption">
            <dt className="text-text-secondary">Family</dt>
            <dd className="m-0 text-text">{family.label}</dd>
            {family.wells
              .filter((well) => (bindings[well.name] ?? []).length > 0)
              .map((well) => (
                <div key={well.name} className="contents">
                  <dt className="text-text-secondary">{well.label}</dt>
                  <dd className="m-0 text-text">{(bindings[well.name] ?? []).join(", ")}</dd>
                </div>
              ))}
            <dt className="text-text-secondary">Accessible table fallback</dt>
            <dd className="m-0 text-text">{family.table_fallback}</dd>
          </dl>
        </section>

        {disclosures && (
          <section aria-labelledby="preview-disclosures">
            <h3 id="preview-disclosures" className="m-0 mb-2 text-h3 font-h3 text-text">
              Disclosures for this Result
            </h3>
            <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-caption">
              <dt className="text-text-secondary">Returned rows</dt>
              <dd className="m-0 text-text" data-testid="returned-row-count">
                {disclosures.returned_row_count}
              </dd>
              <dt className="text-text-secondary">Truncated</dt>
              <dd className="m-0 text-text" data-testid="truncated-flag">
                {disclosures.truncated ? "yes, the server truncated this Result" : "no"}
              </dd>
              <dt className="text-text-secondary">Marks</dt>
              <dd className="m-0 text-text">
                {disclosures.marks} of at most {disclosures.family_max_marks}
              </dd>
              <dt className="text-text-secondary">Freshness</dt>
              <dd className="m-0 text-text" data-testid="freshness">
                {freshness
                  ? `stale-since ${String(freshness)}`
                  : "the Result manifest does not carry a freshness field yet"}
              </dd>
            </dl>
          </section>
        )}

        <section aria-labelledby="preview-table">
          <h3 id="preview-table" className="m-0 mb-2 text-h3 font-h3 text-text">
            Accessible table
          </h3>
          {columns.length === 0 || !result || result.rows.length === 0 ? (
            <EmptyState
              title="No rows to tabulate"
              description="The fallback carries whatever the Result carries. It carries nothing here."
            />
          ) : (
            <TableScroll label="Accessible table fallback">
              <Table>
                <TableHeader>
                  <TableRow>
                    {columns.map((column) => (
                      <TableHead key={column}>{column}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {result.rows.map((row, index) => (
                    <TableRow key={index}>
                      {columns.map((column) => (
                        <TableCell key={column}>{String(row[column] ?? "")}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </section>
      </div>
    </Panel>
  );
}
