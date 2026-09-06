/**
 * The two Master Data workbenches Story 49.2 owed (governance.md `Master Data`).
 *
 * Six `PENDING_TAB_OWNER` entries named this story — Hierarchy and
 * Mappings & Aliases, across Business Domain, Master Data Object and Registry.
 * Those placeholders were the honest inventory of what was missing; they go with
 * this file, which is the only reason it is correct to remove them.
 *
 * Two distinctions run through both tabs, and both exist because collapsing them
 * is how a governance screen starts reporting something it never measured:
 *
 *   1. `unavailable` is not `empty`. A Business Domain has no alias store at all
 *      — `app.master_data_aliases` keys on a registry node — so its Mappings &
 *      Aliases tab says the store is missing. Rendering an empty table there
 *      would read as "this Domain has no aliases", which nobody knows.
 *   2. "no ancestors" is an answer for a Business Domain and a gap for nothing.
 *      Domains ARE the roots, so the tab states rootedness rather than showing
 *      an empty breadcrumb that looks truncated.
 *
 * Everything is read from `detail.summary`, composed server-side by
 * `_enrich_master_data_object`. The browser does not join across owner APIs: a
 * fan-out that half-fails looks authoritative and is wrong.
 */
import { useMemo, useState } from "react";

import { apiFetch } from "../lib/apiFetch";
import {
  Badge,
  Button,
  EmptyState,
  Input,
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
  displayValue,
  formatPercent,
  stateLabel,
  stateTone,
  wireWord,
} from "../ui";
import { buildPath, type CanonicalRoute, useRoute } from "../shell/router";
import { EVIDENCE_UNREADABLE_SENTENCE } from "./masterDataRefusals";
import type { GovernanceObject } from "./governanceSurface";

interface Reason {
  code?: string;
  message?: string;
}

interface HierarchyNode {
  kind: string;
  id: string;
  label: string;
  classification_type?: string | null;
  status?: string | null;
  node_kind?: string | null;
  archived?: boolean;
  /** Decided by the read model from the node's state, never guessed here. */
  command?: "archive" | "restore";
}

interface RefusedConsumer {
  consumer_kind?: string | null;
  consumer_id?: string | null;
  consumer_label?: string | null;
}

interface Refusal {
  message: string;
  consumers: RefusedConsumer[];
}

interface HierarchyFacet {
  state: string;
  is_root: boolean;
  ancestors: HierarchyNode[];
  children: HierarchyNode[];
  reason: Reason | null;
}

interface AliasRow {
  id: string;
  namespace?: string | null;
  locale?: string | null;
  raw_value?: string | null;
  normalized_value?: string | null;
  relation?: string | null;
  confidence?: number | null;
  provenance?: string | null;
  conflict_state?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  node_label?: string | null;
}

interface AliasFacet {
  state: string;
  rows: AliasRow[];
  reason: Reason | null;
}

function summaryOf(detail: GovernanceObject): Record<string, unknown> {
  return (detail.summary ?? {}) as Record<string, unknown>;
}

/** The single place an unreadable store is told apart from a genuinely empty one. */
function Unreadable({ reason, what }: { reason: Reason | null; what: string }) {
  return (
    <Status as="block" tone="warning" title={`${what} could not be read`}>
      {reason?.message ??
        "Its owner did not answer. This is not a count of zero, and nothing should be read as one."}
    </Status>
  );
}

/**
 * One node's governed command, and the refusal it may come back with.
 *
 * The whole reason this is not a plain button: the server refuses an archive
 * whose node still has live consumers, with 409 and the consumers NAMED. That
 * refusal is the useful moment -- it is the only place an operator learns what
 * would break -- so it is rendered in full, and acknowledging it is a second,
 * deliberate click rather than a checkbox ticked in advance.
 *
 * The Idempotency-Key is minted once per node+action and reused for the
 * acknowledged retry: the retry is the SAME command carried through, not a new
 * one, which is exactly what the server's replay path is for.
 */
function NodeCommand({
  projectId,
  node,
  onDone,
}: {
  projectId: string;
  node: HierarchyNode;
  onDone: () => void;
}) {
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [key] = useState(() => `md-${node.command}-${node.id}-${Date.now()}`);

  const action = node.command ?? "archive";

  async function run(acknowledge: boolean) {
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/governance/master-data/nodes/${encodeURIComponent(node.id)}/commands`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": key },
          body: JSON.stringify({ action, acknowledge_impact: acknowledge }),
        },
      );
      if (res.status === 409) {
        const body = await res.json();
        setRefusal({
          message: String(body?.message ?? "This command was refused."),
          consumers: Array.isArray(body?.impact?.consumers) ? body.impact.consumers : [],
        });
        return;
      }
      if (!res.ok) {
        // 503 is the fail-closed answer when the used-by store is unreadable.
        // It is shown as itself: "could not check" must never read as "done".
        const body = await res.json().catch(() => ({}));
        setError(
          res.status === 503
            ? EVIDENCE_UNREADABLE_SENTENCE
            : String(body?.message ?? `The command failed (${res.status}).`),
        );
        return;
      }
      setRefusal(null);
      onDone();
    } catch {
      setError("The command could not be sent.");
    } finally {
      setBusy(false);
    }
  }

  if (refusal) {
    return (
      <div className="flex flex-col gap-2">
        <Status as="block" tone="warning" title="Archiving this node would break its consumers">
          {refusal.message}
        </Status>
        {refusal.consumers.length > 0 && (
          <ul className="text-ui text-text-secondary">
            {refusal.consumers.map((consumer, index) => (
              <li key={`${consumer.consumer_id ?? index}`}>
                {/* ONE WORD FOR ONE STATE, ACROSS THE THREE SCREENS THAT LIST
                    CONSUMERS. `CountryWorkspace` already said "Unnamed consumer"
                    where the store holds no label; this said the identifier, so
                    the same refusal read two ways depending on which workbench a
                    person opened it from. The kind is beside it either way. */}
                {consumer.consumer_label ?? "Unnamed consumer"}
                <Badge outline className="ml-2">{displayValue(consumer.consumer_kind)}</Badge>
              </li>
            ))}
          </ul>
        )}
        <div className="flex gap-2">
          <Button variant="destructive" disabled={busy} onClick={() => run(true)}>
            Archive anyway
          </Button>
          <Button variant="secondary" disabled={busy} onClick={() => setRefusal(null)}>
            Keep it
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <Button variant="secondary" disabled={busy} onClick={() => run(false)}>
        {action === "restore" ? "Restore" : "Archive"}
      </Button>
      {error && (
        <Status as="block" tone="warning" title="Nothing was changed">
          {error}
        </Status>
      )}
    </div>
  );
}

export function MasterDataHierarchyTab({
  detail,
  projectId,
  onChanged,
}: {
  detail: GovernanceObject;
  /** Absent on the object types whose children are not commandable nodes. */
  projectId?: string;
  onChanged?: () => void;
}) {
  const facet = summaryOf(detail).hierarchy as HierarchyFacet | undefined;
  // Only registry nodes are commandable: the command route addresses
  // `app.master_data_nodes`, and a Business Domain's classifications are a
  // different owner with a different lifecycle. Offering a button that would
  // 404 is worse than offering none.
  const commandable = Boolean(
    projectId && (facet?.children ?? []).some((child) => child.kind === "registry-node"),
  );

  if (!facet || facet.state === "unavailable") {
    return (
      <Panel>
        <PanelHeader title="Hierarchy" description="Where this object sits, and what sits under it." />
        <Unreadable reason={facet?.reason ?? null} what="The hierarchy" />
      </Panel>
    );
  }

  return (
    <>
      <Panel>
        <PanelHeader
          title="Position"
          description="The chain from this object up to its root, innermost first."
        />
        {facet.ancestors.length === 0 ? (
          <p className="text-ui text-text-secondary">
            {facet.is_root
              ? "This object is a root. It has no parent, and that is an answer rather than a missing one."
              : "No parent chain is recorded for this object."}
          </p>
        ) : (
          <ol className="flex flex-wrap items-center gap-2 text-ui">
            <li className="font-semibold text-text">{detail.object_ref.label}</li>
            {facet.ancestors.map((node) => (
              <li key={node.id} className="flex items-center gap-2 text-text-secondary">
                <span aria-hidden="true">←</span>
                <span>{node.label}</span>
                <Badge outline>{node.kind === "business-domain" ? "Domain" : "Classification"}</Badge>
              </li>
            ))}
          </ol>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Children"
          description="What resolves through this object today."
        />
        {facet.children.length === 0 ? (
          <EmptyState
            title="Nothing sits under this object"
            description="Its owner answered, and the answer is none."
          />
        ) : (
          <TableScroll label="Children">
            <Table>
              <caption className="sr-only">Children</caption>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Status</TableHead>
                  {commandable && <TableHead>Lifecycle</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {facet.children.map((node) => (
                  <TableRow key={node.id} data-archived={node.archived ? "true" : undefined}>
                    <TableCell className="font-semibold text-text">
                      {node.label}
                      {/* An archived node stays listed and says so. Hiding it
                          would make an archive look like a deletion. */}
                      {node.archived && (
                        <Badge tone={stateTone("archived")} className="ml-2">
                          {stateLabel("archived")}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {displayValue(node.classification_type ?? node.node_kind ?? node.kind)}
                    </TableCell>
                    <TableCell>{displayValue(node.status)}</TableCell>
                    {commandable && (
                      <TableCell>
                        <NodeCommand
                          projectId={projectId as string}
                          node={node}
                          onDone={onChanged ?? (() => undefined)}
                        />
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>
    </>
  );
}

/** A row the alias store itself calls conflicted. `none` is an answer, not a state. */
function inConflict(row: AliasRow): boolean {
  return Boolean(row.conflict_state) && row.conflict_state !== "none";
}

/** The two columns a person searches an alias by: the value that arrives, and
 *  the governed object it resolves to. Namespace, relation and provenance are
 *  narrowings a reader makes by eye over a short list; the incoming value is the
 *  one they arrive knowing. */
function aliasMatches(row: AliasRow, term: string): boolean {
  const haystack = [row.raw_value, row.normalized_value, row.node_label, row.locale];
  return haystack.some((value) => (value ?? "").toLowerCase().includes(term));
}

export function MasterDataMappingsAliasesTab({ detail }: { detail: GovernanceObject }) {
  const { route, navigate } = useRoute();
  // A registry alias store holds hundreds of rows — every country spelling a
  // connector ever emitted. The table listed them all with no way to reach one,
  // so the tab answered "does this object have aliases" and never "how does THIS
  // value resolve", which is the question somebody opens it with.
  const [term, setTerm] = useState("");
  const [conflictsOnly, setConflictsOnly] = useState(false);
  const facet = summaryOf(detail).aliases as AliasFacet | undefined;
  const rows = useMemo(() => facet?.rows ?? [], [facet]);
  const conflicted = useMemo(() => rows.filter(inConflict).length, [rows]);
  const trimmed = term.trim().toLowerCase();
  const visible = useMemo(
    () =>
      rows.filter(
        (row) =>
          (!conflictsOnly || inConflict(row)) && (!trimmed || aliasMatches(row, trimmed)),
      ),
    [rows, conflictsOnly, trimmed],
  );

  /**
   * WHERE A CONFLICT IS ARBITRATED, and it is not here.
   *
   * The badge announced `ambiguous` on a row and offered nothing: the reader
   * learned a decision was owed and not who owes it. The decision lives in
   * Controls & Quality › Conflicts (`governance.md:44` — "Conflicts,
   * Reconciliation, Data Quality and Rule Sets"), the lens
   * `GovernanceCollection` mounts `MdmConflictsPanel` on.
   *
   * The address is built by the ROUTER, never composed as a string: `buildPath`
   * validates the whole combination against the registry, so a lens that stopped
   * existing fails here rather than landing a person on the unknown-route
   * screen. It is offered only from a Project workspace route — from anywhere
   * else there is no Project to arbitrate in, and a guessed address is worse
   * than none (`routeHref.ts:19-25`).
   */
  const conflictsHref =
    route.scope === "project" && route.globalSurface === null
      ? buildPath({
          ...route,
          workspace: "governance",
          section: "controls-quality",
          lens: "conflicts",
          objectType: null,
          objectId: null,
          tab: null,
          versionId: null,
          action: null,
          query: {},
        } as CanonicalRoute)
      : null;

  const openConflicts = () =>
    navigate({
      workspace: "governance",
      section: "controls-quality",
      lens: "conflicts",
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
      query: {},
    });

  if (!facet || facet.state === "unavailable") {
    return (
      <Panel>
        <PanelHeader
          title="Mappings & Aliases"
          description="How incoming values resolve to this governed object."
        />
        <Unreadable reason={facet?.reason ?? null} what="The alias store" />
      </Panel>
    );
  }

  if (rows.length === 0) {
    return (
      <Panel>
        <PanelHeader
          title="Mappings & Aliases"
          description="How incoming values resolve to this governed object."
        />
        <EmptyState
          title="No alias recorded"
          description="The alias store answered, and there is none yet."
        />
      </Panel>
    );
  }

  return (
    <Panel flush>
      <PanelHeader
        title="Mappings & Aliases"
        description="How incoming values resolve to this governed object, and on whose word."
      />

      {/* The narrowing is deliberately in the browser and says so below: the
          alias facet is composed whole by `_enrich_master_data_object`, so every
          row is already here and nothing is being asked of an owner again. */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-divider-base bg-surface-subtle/30 px-5 py-2.5">
        <div className="flex flex-wrap items-center gap-3">
          <Input
            type="search"
            aria-label="Filter aliases"
            placeholder="Filter by incoming value or target"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            className="h-8 w-72 max-w-full text-caption"
          />
          {/* Offered only where there is something to see: a chip that always
              narrows to nothing teaches a person the filter is broken. */}
          {conflicted > 0 && (
            <Button
              type="button"
              size="sm"
              variant={conflictsOnly ? "default" : "secondary"}
              aria-pressed={conflictsOnly}
              onClick={() => setConflictsOnly((value) => !value)}
            >
              In conflict ({conflicted})
            </Button>
          )}
        </div>
        <span className="text-caption font-mono text-text-secondary">
          Showing {visible.length} of {rows.length} aliases
        </span>
      </div>

      {visible.length === 0 ? (
        /* NOT "No alias recorded". The store answered with rows and the reader
           put them out of sight themselves — so the sentence names the term they
           typed and the filter that is on, because those are what they undo. */
        <EmptyState
          title={
            trimmed
              ? `No alias matching "${term.trim()}"`
              : "No alias is in conflict"
          }
          description={
            trimmed && conflictsOnly
              ? `${rows.length} aliases are recorded here. None of them both matches this term and is in conflict — clear the term, or turn the conflict filter off.`
              : trimmed
                ? `${rows.length} aliases are recorded here, and none of them carries this value. Clear the term to see them all.`
                : "Every alias this store returned resolves without contest. Turn the conflict filter off to see them all."
          }
        />
      ) : (
      <TableScroll label="Aliases">
        <Table>
          <caption className="sr-only">Aliases</caption>
          <TableHeader>
            <TableRow>
              <TableHead>Incoming value</TableHead>
              <TableHead>Resolves to</TableHead>
              <TableHead>Namespace</TableHead>
              <TableHead>Relation</TableHead>
              <TableHead>Confidence</TableHead>
              <TableHead>Provenance</TableHead>
              <TableHead>Conflict</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((row) => (
              <TableRow key={row.id}>
                <TableCell className="font-semibold text-text">
                  {displayValue(row.raw_value)}
                  {row.locale ? <Badge outline className="ml-2">{row.locale}</Badge> : null}
                </TableCell>
                <TableCell>{displayValue(row.node_label ?? row.normalized_value)}</TableCell>
                <TableCell>{displayValue(row.namespace)}</TableCell>
                <TableCell>{displayValue(row.relation)}</TableCell>
                <TableCell>
                  {/* A confidence is shown as recorded. It is never rounded to a
                      reassuring 100%, and its absence is not 0. */}
                  {row.confidence === null || row.confidence === undefined
                    ? displayValue(null)
                    : formatPercent(row.confidence)}
                </TableCell>
                <TableCell>{displayValue(row.provenance)}</TableCell>
                <TableCell>
                  {inConflict(row) ? (
                    <span className="flex items-center gap-2">
                      <Badge tone="warning">{stateLabel(row.conflict_state)}</Badge>
                      {conflictsHref && (
                        <a
                          className="text-caption font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                          href={conflictsHref}
                          aria-label={`Arbitrate the ${row.conflict_state} conflict on ${row.raw_value ?? row.id} in Controls & Quality`}
                          onClick={(event) => {
                            if (
                              event.button !== 0 ||
                              event.metaKey ||
                              event.ctrlKey ||
                              event.shiftKey ||
                              event.altKey
                            )
                              return;
                            event.preventDefault();
                            openConflicts();
                          }}
                        >
                          Arbitrate
                        </a>
                      )}
                    </span>
                  ) : (
                    displayValue(null)
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
      )}
      {conflicted > 0 && (
        <p className="m-0 border-t border-divider-base px-5 py-2.5 text-caption text-text-secondary">
          A conflict is decided in Controls &amp; Quality › Conflicts, not here: this tab reports
          what the alias store recorded, and never arbitrates it.
        </p>
      )}
    </Panel>
  );
}

/**
 * What feeds a client-declared object kind (Story 64.1/64.13, AI-232).
 *
 * The epic wrote this expectation for itself — "par type : le registre, le
 * nombre d'instances, la version courante, le Datastream qui l'alimente" — and
 * the server composed exactly that answer for months. It reached the MCP door
 * only, so a person could declare an object kind and find no screen that says
 * what feeds it.
 *
 * Three distinctions are load-bearing here, and each is a refusal:
 *
 *   1. `unavailable` is not `empty`. A Country registry has no source-binding
 *      store — it is fed by its capability — so its panel says the store is
 *      missing. An empty table there would read as "nothing feeds this".
 *   2. A grain is shown only for a `source_key` source. Printing identity fields
 *      beside a `governed_label` source would claim the file carries a key when
 *      the whole point of that mode is that it does not.
 *   3. The Datastream is NAMED, not just referenced. A governed object whose
 *      feeder is an opaque id sends the reader to Data to decode it.
 */
interface SourceRow {
  namespace?: string | null;
  datastream_id?: string | null;
  datastream_label?: string | null;
  identity_mode?: string | null;
  identity_fields?: string[];
  label_field?: string | null;
  mapping_version_id?: string | null;
}

interface SourceFacet {
  state: string;
  rows: SourceRow[];
  reason: Reason | null;
}

/** How a row of this source becomes an object, in the reader's words. */
const IDENTITY_MODE_LABEL: Record<string, string> = {
  source_key: "Identified by its key",
  governed_label: "Resolved by its label",
};

export function ClientObjectSourcesPanel({ detail }: { detail: GovernanceObject }) {
  const summary = summaryOf(detail);
  const facet = summary.sources as SourceFacet | undefined;
  const objectKind = summary.object_kind as string | undefined;
  const instances = summary.instance_count as number | undefined;
  const perNode = summary.version_scope === "node";

  if (!facet || facet.state === "unavailable") {
    return (
      <Panel>
        <PanelHeader
          title="Fed by"
          description="The Datastreams whose rows become instances of this object."
        />
        <Unreadable reason={facet?.reason ?? null} what="The source bindings" />
      </Panel>
    );
  }

  return (
    <>
      <Panel flush>
        <PanelHeader
          title="Object kind"
          description="What the client declared, and what a version of it covers."
        />
        <TableScroll label="Object kind">
          <Table>
            <caption className="sr-only">Object kind</caption>
            <TableBody>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">Kind</TableCell>
                <TableCell>{displayValue(objectKind)}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">Instances</TableCell>
                {/* Counted live, archived excluded. A count of zero is an answer:
                    the kind is declared and nothing has landed yet. */}
                <TableCell>{instances === undefined ? displayValue(null) : instances}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="w-64 font-semibold text-text">A version covers</TableCell>
                <TableCell>
                  {perNode ? "One instance" : "Every instance of this kind"}
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Fed by"
          description="The Datastreams whose rows become instances of this object, and how each one is identified."
        />
        {facet.rows.length === 0 ? (
          <EmptyState
            title="No source feeds this object"
            description="Every binding has been released. The kind and its instances stay, and nothing new lands until a Datastream is bound again."
          />
        ) : (
          <TableScroll label="Sources">
            <Table>
              <caption className="sr-only">Sources</caption>
              <TableHeader>
                <TableRow>
                  <TableHead>Datastream</TableHead>
                  <TableHead>Namespace</TableHead>
                  <TableHead>Identified by</TableHead>
                  <TableHead>On</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {facet.rows.map((row) => (
                  <TableRow key={`${row.namespace}:${row.datastream_id}`}>
                    {/* THE SERVED WORD, OR WHAT ITS ABSENCE MEANS. The read
                        model LEFT JOINs `app.datastreams` (`d.name`, `NOT NULL`),
                        so a missing label is not a nameless Datastream -- it is a
                        binding whose Datastream is no longer in this Project, and
                        that is what the cell has to say. `?? row.datastream_id`
                        printed `ds_<ULID>` under a "Datastream" heading instead,
                        which reads as a name and names nothing. */}
                    <TableCell className="font-semibold text-text">
                      {row.datastream_label ?? "No longer in this Project"}
                    </TableCell>
                    <TableCell>{displayValue(row.namespace)}</TableCell>
                    <TableCell>
                      {row.identity_mode ? (
                        <Badge outline>
                          {IDENTITY_MODE_LABEL[row.identity_mode] ?? wireWord(row.identity_mode)}
                        </Badge>
                      ) : (
                        displayValue(null)
                      )}
                    </TableCell>
                    <TableCell>
                      {row.identity_mode === "source_key"
                        ? displayValue((row.identity_fields ?? []).join(", ") || null)
                        : displayValue(row.label_field)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>
    </>
  );
}
