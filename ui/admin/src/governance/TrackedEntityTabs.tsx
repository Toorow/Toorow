/**
 * The two facets of a Competitor — Representations and Coverage.
 *
 * The displayed noun is **Competitor**; the wire token stays `tracked-entity`
 * and the rename of the token is deferred (`shell/navigation/governance.ts`).
 * Both tabs are ratified in `governance.md`, *Amendment, 2026-09-01 — the
 * Competitor workbench*, which describes them from what
 * `governance_read_model._tracked_entity` actually serves.
 *
 * Representations of a tracked entity — the one contracted Governance tab
 * that no module drew.
 *
 * Measured 2026-08-03 with `python scripts/screens.py pages`: 51 tabs resolve
 * through a blanket workbench, and 50 of them are drawn by a named module.
 * `tracked-entity/representations` was the exception. It answered, so no audit
 * of routes could see it; it rendered the chassis' generic field dump, so a
 * person saw a raw record where the tab's whole subject belongs.
 *
 * `capabilities/competitors.md:12` contracts it: a tracked entity "binds
 * source-specific representations without remapping". The payload was already
 * there — `governance_read_model.py:3296-3305` ships connector, account scope,
 * external id, external label and version per representation, and
 * `GovernanceCollection.tsx:352-359` already counts them for the registry lens.
 * Only the reading was missing. (Both citations re-read 2026-09-01; they said
 * `:1947` and `:220`, which the files had moved past.)
 *
 * WHY A NEW FILE. `MasterDataTabs.tsx` is the natural home and carries another
 * session's uncommitted work. A new module plus one line in the chassis takes
 * nothing from anyone.
 */
import {
  Badge,
  EmptyState,
  Metric,
  ObjectId,
  Panel,
  PanelHeader,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  bindingStateGlyph,
  bindingStateLabel,
  bindingStateTone,
} from "../ui";
import type { GovernanceObject } from "./governanceSurface";

interface Representation {
  connector?: string | null;
  account_scope?: string | null;
  external_id?: string | null;
  external_label?: string | null;
  version?: number | null;
}

export function RepresentationsTab({ detail }: { detail: GovernanceObject }) {
  const summary = detail.summary as Record<string, unknown>;
  const rows = (Array.isArray(summary.representations) ? summary.representations : []) as Representation[];

  if (rows.length === 0) {
    return (
      <Panel>
        <PanelHeader
          title="Representations"
          description="How each source names this entity. Bound, never remapped."
        />
        {/* Its owner answered and the answer is none — not "unavailable". The
            entity exists and no source has been bound to it yet. */}
        <EmptyState
          title="No source representation is bound"
          description="This entity is defined here, and no connector has been bound to a name for it yet."
        />
      </Panel>
    );
  }

  return (
    <Panel flush>
      <PanelHeader
        title="Representations"
        description={`${rows.length} source name(s) bound to this entity. Each is the name a connector uses, kept as the source states it.`}
      />
      <TableScroll label="Entity representations">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Connector</TableHead>
              <TableHead>Account scope</TableHead>
              <TableHead>External name</TableHead>
              <TableHead>External id</TableHead>
              <TableHead numeric>Version</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, index) => (
              <TableRow key={`${row.connector ?? "?"}:${row.external_id ?? index}`}>
                <TableCell>{row.connector ?? "—"}</TableCell>
                {/* An absent scope is "every account of this connector", which is
                    a different statement from "unknown" and is written as one. */}
                <TableCell>{row.account_scope ?? "All accounts"}</TableCell>
                <TableCell>{row.external_label ?? "—"}</TableCell>
                <TableCell>
                  <span className="font-mono text-caption">{row.external_id ?? "—"}</span>
                </TableCell>
                <TableCell numeric>{row.version ?? "—"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

interface MatrixRow {
  datastream_ref?: { object_type?: string; id?: string } | null;
  state?: string | null;
  direction?: string | null;
  report_id?: string | null;
  exception_reason_code?: string | null;
  exception_reason?: string | null;
}

/**
 * The Coverage of a Competitor — where this entity is actually collected.
 *
 * SAME DEFECT AS `Representations`, ONE TAB LATER. It was contracted by the
 * route (`shell/navigation/governance.ts`) and by the read model
 * (`governance_read_model.py:126`), its owner served the whole matrix
 * (`:3309-3324`), and no branch of `GovernanceObjectWorkbench` claimed it — so
 * it fell to the chassis' `Object.entries(summary)` dump and a person read
 * `matrix: [object Object], published_count: 2` where the tab's subject belongs.
 *
 * Three things it does NOT do, each one a decision its owner already made:
 *
 * - it does not reduce a state to a tick. `published`, `candidate` and
 *   `excluded` are three different facts, and the words, glyphs and tones come
 *   from `ui/EntityMatrix` so the registry lens and this tab cannot drift apart;
 * - it does not derive "is this collected?" from the length of a list that also
 *   holds candidates — `published_count` and `bound_count` are stated by the
 *   server and read here;
 * - it does not resolve a Datastream name. The envelope carries ids, and an id
 *   shown as an id is honest where a second read composed in the browser to
 *   decorate a table is the defect `finished_work_audit` counts as "the console
 *   composes labels in the browser".
 */
export function CoverageTab({ detail }: { detail: GovernanceObject }) {
  const summary = detail.summary as Record<string, unknown>;
  const rows = (Array.isArray(summary.matrix) ? summary.matrix : []) as MatrixRow[];
  const publishedCount = typeof summary.published_count === "number" ? summary.published_count : null;
  const boundCount = typeof summary.bound_count === "number" ? summary.bound_count : rows.length;

  if (rows.length === 0) {
    return (
      <Panel>
        <PanelHeader
          title="Coverage"
          description="Where this entity is collected, and whether that is approved or only proposed."
        />
        {/* Its owner answered and the answer is none. The entity is defined and
            no Datastream has been bound to it — which is a state, not a gap, and
            the gesture that fills it is named. */}
        <EmptyState
          title="No Datastream is bound to this entity"
          description="Nothing collects it yet. Bind it to a compatible Datastream from the Competitor Registry matrix, in Governance > Master Data."
        />
      </Panel>
    );
  }

  return (
    <Panel flush>
      <PanelHeader
        title="Coverage"
        description="Each Datastream this entity is bound to, with the binding state as its owner recorded it."
      />
      <div className="grid grid-cols-2 divide-x divide-divider-base border-b border-divider-base">
        {/* Stated, never derived: a candidate binding is an approved intention
            and a published one is collected data, so one count cannot stand in
            for the other. */}
        <Metric
          label="Published"
          value={publishedCount ?? "—"}
          hint="Bindings that are collecting data."
          data-testid="coverage-published-count"
        />
        <Metric
          label="Bound"
          value={boundCount}
          hint="Every binding, published or not."
          data-testid="coverage-bound-count"
        />
      </div>
      <TableScroll label="Entity coverage by Datastream">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Datastream</TableHead>
              <TableHead>State</TableHead>
              <TableHead>Direction</TableHead>
              <TableHead>Report</TableHead>
              <TableHead>Exception</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, index) => {
              const state = row.state ?? "none";
              return (
                <TableRow key={`${row.datastream_ref?.id ?? "?"}:${index}`}>
                  <TableCell>
                    <ObjectId value={row.datastream_ref?.id} title="Datastream" />
                  </TableCell>
                  <TableCell>
                    {/* A word AND a glyph: colour alone survives neither a
                        monochrome print nor a screen reader. */}
                    <span className="inline-flex items-center gap-2">
                      <span aria-hidden="true">{bindingStateGlyph(state)}</span>
                      <Badge tone={bindingStateTone(state)}>{bindingStateLabel(state)}</Badge>
                    </span>
                  </TableCell>
                  <TableCell>{row.direction ?? "—"}</TableCell>
                  <TableCell>
                    {row.report_id ? <ObjectId value={row.report_id} title="Report" /> : "—"}
                  </TableCell>
                  <TableCell>
                    {/* The reason its owner recorded, in its own words. The code
                        is kept beside it because it is what a support
                        conversation can be held on. */}
                    {row.exception_reason ?? row.exception_reason_code ?? "—"}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}
