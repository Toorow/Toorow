import {
  Badge, EmptyState, Metric, ObjectId, Panel, PanelHeader, Status,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
} from "../../../ui";
import { DatastreamPublicationDialog, DatastreamRollbackDialog } from "../DatastreamPublicationDialog";
import EventStreamPanel from "../EventStreamPanel";
import { dateTime, records, text, titleCase } from "../evidence";
import { resolvableHref } from "../../../shell/routeHref";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../workbenchTypes";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";

/** An execution id that OPENS the run it names — lot A4 of issue #68.
 *
 *  This tab named nine execution ids and opened none of them: three publication
 *  pointers and one per Output version, every one of them the id of a run that
 *  lives one tab away. `what is served, and to whom` was the tab's whole subject
 *  and it was the only Workbench tab with no outgoing gesture at all.
 *
 *  IT IS NOT AN `OwnerReference`, AND THAT IS MEASURED. The `datastream`
 *  contract in `shell/navigation.ts` declares `evidenceTabs: ["runs"]`, so
 *  `…/tab/runs/evidence/{execution}` parses — but `ContentRouter.openOwner`
 *  builds its `navigate` call without `evidenceId`, and the branch that mounts
 *  `DatastreamWorkbenchRoute` never hands `route.evidenceId` down. A reference
 *  carrying one would open the `Runs` tab with nothing selected: on a Datastream
 *  with sixty runs that is the "gesture in appearance only" story 58.2 named
 *  when it made the day grid carry its `execution_id` instead of a tab name.
 *
 *  So the id travels the way that one does — through the route, which holds the
 *  selection and switches the tab. Without a handler the id keeps its NAME and
 *  says, on hover, that nothing opens from here: visible, inert, honest. */
function RunLink({
  executionId,
  what,
  onOpenRun,
}: {
  executionId?: string | null;
  what: string;
  onOpenRun?: (executionId: string) => void;
}) {
  if (!executionId) return <ObjectId value={null} title={what} />;
  if (!onOpenRun) {
    return (
      <ObjectId
        value={executionId}
        title={`${what} — this Workbench was opened without a way to reach a run, so this identifier opens nothing`}
      />
    );
  }
  return (
    <button
      type="button"
      className="inline-block max-w-full truncate align-bottom font-mono text-caption text-primary underline"
      title={`${what}: ${executionId} — opens this run`}
      aria-label={`Open the run ${executionId}`}
      onClick={() => onOpenRun(executionId)}
    >
      {executionId}
    </button>
  );
}

/** The address of one downstream consumer, built from the ROUTER'S OWN registry.
 *
 *  `shell/navigation.ts` is the only place an object type, a tab or an action is
 *  declared, and `ContentRouter.openOwner` returns SILENTLY for a reference the
 *  registry does not carry — `object_type` without `object_id`, a tab a contract
 *  never declared. A dead click shipped on this surface twice this week and a
 *  mocked test cannot see a router refusal, so every branch below names the
 *  declaration it was read from, and a kind with no declaration returns `null`
 *  so the caller can render the consumer inert instead.
 *
 *  `consumer_kind` is a CHECK-constrained enum of exactly four values
 *  (`app.datastream_output_used_by`, migration 138): `semantic_view`, `report`,
 *  `result`, `delivery`. Three are declared object types. `delivery` is not an
 *  object of this console at all — no workspace registers one — and it is the
 *  one that stays a name. */
function consumerOwner(
  consumerKind: string,
  consumerRef: string,
  consumerVersionRef: string | null,
): OwnerReference | null {
  if (!consumerRef) return null;
  const base = {
    surface: "workspace",
    global_surface: null,
    global_section: null,
    lens: null,
    object_id: consumerRef,
    action: null,
    evidence_id: null,
  };
  if (consumerKind === "semantic_view") {
    // `semantic-view` declares `versions` among its tabs and
    // `GovernanceObjectWorkbench` reads `versionId` on that tab — it marks the
    // pinned row `aria-current` — so this is the one kind where the version this
    // panel promises to be bound to is genuinely openable.
    return consumerVersionRef
      ? { ...base, workspace: "governance", section: "semantic-model", object_type: "semantic-view", tab: "versions", version_id: consumerVersionRef }
      : { ...base, workspace: "governance", section: "semantic-model", object_type: "semantic-view", tab: "definition", version_id: null };
  }
  if (consumerKind === "report") {
    // NOT PINNED, deliberately. `report` declares a `versions` tab, but
    // `objectSurfaces.tsx` keeps `VERSION_READING_OBJECT_TYPES` to the types
    // whose workbench actually opens a pinned version and answers every other
    // one with a named `unavailable` screen. `report` is still one of the
    // refused ones (`Reports.tsx` accepts no version identifier), so a pinned
    // report version would open that refusal, which is worse than opening the
    // object.
    return { ...base, workspace: "analyze", section: "reports", object_type: "report", tab: "overview", version_id: null };
  }
  if (consumerKind === "result") {
    // `result` declares no `versions` tab at all, so there is nothing to pin.
    // `view` is its declared default and `ResultWorkbench` reads it as its lens.
    return { ...base, workspace: "analyze", section: "explore", object_type: "result", tab: "view", version_id: null };
  }
  return null;
}

/** AC8: Candidate, Current, Last-known-good and the eligible rollback target are
 *  separate explicit roles, not destinations. They are named here, on the tab
 *  that owns publication, rather than left implicit in the two dialogs. */
/** Candidate readiness — the comparison the tab exists to make.
 *
 *  `datastream-workbench-and-wizard.md:79` requires "candidate diff/readiness"
 *  here, and the page named the three pointers without ever comparing two of
 *  them: an operator was asked to `Publish` a candidate while being shown its id
 *  and nothing else. Promoting a version you cannot compare is the one action
 *  this whole tab is built to make safe.
 *
 *  Nothing is computed that is not already on the wire. Every output version
 *  carries `execution_id`, `schema_hash`, `grain_evidence`, `plan_version_id`
 *  and `mapping_version_id` (migration 138), so the candidate row and the
 *  current row are found by their execution and read side by side. No threshold
 *  is invented and no verdict is given — "changed" is a fact, "acceptable" is
 *  the operator's call.
 */
function CandidateReadiness({
  header,
  outputs,
}: {
  header: WorkbenchHeader;
  outputs: ReadonlyArray<Record<string, unknown>>;
}) {
  const candidateId = header.publications.candidate;
  const currentId = header.publications.current;

  if (!candidateId) {
    return (
      <Panel flush>
        <PanelHeader title="Candidate readiness" description="What would change if the candidate were promoted." />
        <div className="p-5">
          <EmptyState
            title="No candidate to review"
            description="No isolated candidate is waiting. A run produces one; it is never promoted on its own."
          />
        </div>
      </Panel>
    );
  }

  const byExecution = (executionId: string | null) =>
    executionId ? outputs.find((row) => text(row.execution_id) === executionId) ?? null : null;
  const candidate = byExecution(candidateId);
  const current = byExecution(currentId);

  // NOTHING IN THIS TABLE BECOMES A LINK, and that is a decision. `Plan version`
  // and `Mapping version` name real objects, but the `datastream` contract in
  // `shell/navigation.ts` declares no `versions` tab and no plan or mapping
  // object type — so there is no address to build for them, and a control here
  // would be exactly the dead click this lot exists to remove. They stay text.
  // The two executions this table compares ARE reachable, and they are reachable
  // from the pointers above and from the Output rows below, where each appears
  // once rather than four times in a comparison of properties.
  /** `absent` is the sentence the `Not comparable` cell carries — WHAT is
   *  missing and why it will not arrive, per property.
   *
   *  A bare `Not comparable` on the screen that decides a promotion tells a
   *  person that something is wrong and nothing about whether it is their doing.
   *  `schema_hash` in particular has a permanent class of absentees, and they
   *  cannot be repaired: see the block comment on the difference cell below. */
  const rows: ReadonlyArray<{
    label: string;
    read: (row: Record<string, unknown>) => string;
    why: string;
    absent: string;
  }> = [
    {
      label: "Schema",
      read: (row) => text(row.schema_hash, "Unavailable"),
      why: "Two identical hashes mean the shape did not move.",
      absent:
        "No schema fingerprint was recorded on one of these two publications, so the shapes cannot be compared here. Publications recorded before 2026-08-18 carry none and never will — the evidence row is immutable. Read the two runs side by side instead.",
    },
    {
      label: "Grain",
      read: (row) => (Array.isArray(row.grain_evidence) ? (row.grain_evidence as unknown[]).join(" · ") || "None declared" : "Unavailable"),
      why: "A grain change alters what one row means.",
      absent: "One of these two publications recorded no grain, so what a row means cannot be compared here.",
    },
    {
      label: "Plan version",
      read: (row) => text(row.plan_version_id, "Unavailable"),
      why: "Which execution plan produced it.",
      absent: "One of these two publications names no plan version.",
    },
    {
      label: "Mapping version",
      read: (row) => text(row.mapping_version_id, "Unavailable"),
      why: "Which source-to-canonical binding produced it.",
      absent: "One of these two publications names no mapping version.",
    },
  ];

  return (
    <Panel flush>
      <PanelHeader
        title="Candidate readiness"
        description={
          current
            ? "Candidate compared with what is currently served. `Changed` is a fact, not a verdict — the decision to promote is yours."
            : "Nothing is served yet, so there is nothing to compare against."
        }
      />
      {!candidate ? (
        <div className="p-5">
          <Status as="block" tone="warning" title="Candidate evidence unavailable">
            Execution <span className="font-mono">{candidateId}</span> is the current candidate, but no Output version
            records it. Nothing has been substituted, and it must not be promoted from this screen until it does.
          </Status>
        </div>
      ) : (
        <TableScroll label="Candidate readiness">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Property</TableHead>
                <TableHead>Current</TableHead>
                <TableHead>Candidate</TableHead>
                <TableHead>Difference</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const candidateValue = row.read(candidate);
                const currentValue = current ? row.read(current) : null;
                // TWO UNKNOWNS ARE NOT A MATCH, and the reason is NAMED.
                //
                // `read` returns the literal "Unavailable" when a field was
                // never written. Both sides then compared equal and this table
                // announced `Unchanged` in green -- "two identical hashes mean
                // the shape did not move" -- on a comparison it had not made, on
                // the screen that decides a promotion. That is what `unknown`
                // closes.
                //
                // WHY `schema_hash` IS STILL MISSING ON OLD ROWS, MEASURED
                // 2026-08-18. Two writers insert into
                // `app.datastream_output_versions`, both `ON CONFLICT
                // (output_id, execution_id) DO NOTHING`: the publication
                // (`datastream_activation.py`, which computes the hash) and the
                // run (`record_run_output_version`, called from `queue.py`,
                // which until today did not). The run of a candidate lands
                // BEFORE that candidate is published, so the run's row won the
                // conflict and the publication's -- hash included -- was
                // discarded. `trg_datastream_output_versions_immutable` refuses
                // UPDATE, so no backfill is possible: those rows read
                // "Unavailable" for ever. The run writer now carries the
                // candidate's own fingerprint, so publications from 2026-08-18
                // compare; earlier ones say so rather than pretending.
                const unknown = candidateValue === "Unavailable" || currentValue === "Unavailable";
                const changed = currentValue !== null && currentValue !== candidateValue;
                return (
                  <TableRow key={row.label}>
                    <TableCell>
                      <strong className="text-text">{row.label}</strong>
                      <span className="block text-caption text-text-secondary">{row.why}</span>
                    </TableCell>
                    <TableCell className="max-w-[18ch] truncate font-mono text-caption" title={currentValue ?? undefined}>
                      {currentValue ?? "Nothing served"}
                    </TableCell>
                    <TableCell className="max-w-[18ch] truncate font-mono text-caption" title={candidateValue}>
                      {candidateValue}
                    </TableCell>
                    <TableCell>
                      {currentValue === null
                        ? <Status tone="neutral">First publication</Status>
                        : unknown
                          ? (
                            <>
                              <Status tone="warning">Not comparable</Status>
                              <span className="mt-1 block max-w-[46ch] text-caption text-text-secondary">
                                {row.absent}
                              </span>
                            </>
                          )
                          : changed
                            ? <Status tone="warning">Changed</Status>
                            : <Status tone="success">Unchanged</Status>}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableScroll>
      )}
    </Panel>
  );
}

export default function WorkbenchOutputsPage({
  header,
  payload,
  projectId,
  datastreamId,
  onConfirmed,
  onOpenOwner,
  onOpenRun,
}: {
  header: WorkbenchHeader;
  payload: WorkbenchTabPayload;
  projectId: string;
  datastreamId: string;
  onConfirmed: () => void;
  /** Resolves a semantic owner reference into a canonical route — the ONLY way
   *  this screen may reach another workspace, because only the shell knows the
   *  Organization segment `parsePath` requires and only it validates the
   *  reference against `shell/navigation.ts` before navigating. */
  onOpenOwner?: (owner: OwnerReference) => void;
  /** Opens one run of THIS Datastream, by its execution id — the same handler
   *  the `Data` tab is given, held by the route because the selection has to
   *  survive the tab switch. */
  onOpenRun?: (executionId: string) => void;
}) {
  const outputs = records(payload.evidence.outputs);
  const usedBy = records(payload.evidence.used_by);
  return (
    <>
      {/* CHANTIER C -- what this Datastream EMITS, beside what it publishes.
          An Event Configuration is an output of the stream, and it belonged on
          the tab whose subject is "what is served, and to whom". Before this it
          belonged to no tab at all: the four operations that create one had zero
          console callers, so the object could only exist if someone knew their
          four addresses. */}
      <EventStreamPanel projectId={projectId} datastreamId={datastreamId} />
      {/* THE THREE POINTERS ARE THREE RUNS. Each of these ids is an execution,
          and naming one while offering no way to look at what it collected is
          what made this tab a list of strings. AC8 keeps them ROLES rather than
          destinations — the role is still the label, and what opens is the run
          that fills it, never a fourth "publication" object the product has
          no table for. */}
      <Panel
        flush
        role="group"
        aria-label="Publication roles"
        className="grid grid-cols-3 divide-x divide-divider-base max-lg:grid-cols-1 max-lg:divide-x-0 max-lg:divide-y"
      >
        <Metric
          label="Candidate"
          value={header.publications.candidate ? "Waiting" : "None"}
          hint={<RunLink executionId={header.publications.candidate} what="Candidate publication" onOpenRun={onOpenRun} />}
        />
        <Metric
          label="Current"
          value={header.publications.current ? "Served" : "Nothing served"}
          hint={<RunLink executionId={header.publications.current} what="Current publication" onOpenRun={onOpenRun} />}
        />
        <Metric
          label="Last-known-good"
          value={header.publications.last_known_good ? "Retained" : "None"}
          hint={<RunLink executionId={header.publications.last_known_good} what="Last-known-good publication" onOpenRun={onOpenRun} />}
        />
      </Panel>

      <CandidateReadiness header={header} outputs={outputs} />

      <Panel flush>
        <PanelHeader
          title="Physical Outputs"
          description="Stable identities, immutable versions and delivery references."
          actions={
            <>
              <DatastreamPublicationDialog
                projectId={projectId}
                datastreamId={datastreamId}
                candidateId={header.publications.candidate}
                onConfirmed={onConfirmed}
              />
              <DatastreamRollbackDialog
                projectId={projectId}
                datastreamId={datastreamId}
                targetExecutionId={header.publications.last_known_good}
                onConfirmed={onConfirmed}
              />
            </>
          }
        />
        {outputs.length === 0 ? (
          <div className="p-5">
            {/* AN EMPTY LIST SAYS WHY, AND NAMES THE GESTURE THAT FILLS IT.
                What stood here — "No published Output version evidence exists" —
                said the same thing as its own title, in the words of the table it
                read (`datastream_output_versions`, its `evidence` column), and
                named nothing a person could do. The two sentences below are the
                ones the `Candidate` block already uses for the same fact, so the
                tab says it once. */}
            <EmptyState
              title="Nothing is served from this Datastream yet"
              description="A run that reaches Published produces an Output version, and promoting it here is what serves it. Until then there is nothing downstream can read."
            />
          </div>
        ) : (
          <TableScroll label="Physical Outputs">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Output</TableHead>
                  <TableHead>Kind</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Execution</TableHead>
                  <TableHead>Delivery</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {outputs.map((output) => (
                  <TableRow key={`${text(output.id)}:${text(output.version_id)}`}>
                    <TableCell>
                      <strong>{text(output.stable_name)}</strong>
                      <span className="block font-mono text-caption text-text-secondary">
                        {text(output.id)}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge outline>{titleCase(output.output_kind)}</Badge>
                    </TableCell>
                    {/* `version_id` stays text: an Output version is not an
                        object any section of `shell/navigation.ts` declares, so
                        there is no address to build for it. The EXECUTION is the
                        one identity on this row the console can open. */}
                    <TableCell className="font-mono">{text(output.version_id)}</TableCell>
                    <TableCell>
                      <RunLink
                        executionId={text(output.execution_id) || null}
                        what={`Run that produced ${text(output.stable_name, "this Output")}`}
                        onOpenRun={onOpenRun}
                      />
                    </TableCell>
                    <TableCell className="font-mono text-caption">
                      {text(output.delivery_ref, "None")}
                    </TableCell>
                    <TableCell>{dateTime(output.created_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel flush>
        {/* `Version-bound downstream owner links` was three of this repository's
            own words in a row: `owner_href` is a stored column, `version-bound`
            is how the join is keyed, and `link` is what the console builds. The
            question the panel answers is who reads what this Datastream
            publishes. */}
        <PanelHeader
          title="Used by"
          description="Who reads what this Datastream publishes, and the exact version they are pinned to."
        />
        {usedBy.length === 0 ? (
          <div className="p-5">
            {/* THE GESTURE IS NAMED AGAIN, BECAUSE IT NOW EXISTS — re-measured
                2026-08-18.

                This empty state deliberately promised nothing, on the finding
                that `INSERT INTO app.datastream_output_used_by` appeared nowhere
                in `server/`. That is no longer true: `query_execution.py`,
                `record_output_consumption` writes the row at the moment an
                Analyze execution resolves this Datastream's `relation_ref` into
                a physical relation — which IS the act of consuming an Output.
                It writes `consumer_kind = 'result'`.

                So the sentence names that act, and only that act. It does NOT
                invite someone to "bind a consumer": there is still no screen
                that binds one, and the 2026-08-12 ruling
                (`datastream-workbench-and-wizard.md`, « Who writes a downstream
                consumer ») forbids the publication path from writing these rows
                at all — a consumer is an OBSERVATION, never a publication's
                declaration of an audience.

                `semantic_view` and `report` are two of the four allowed kinds
                and no writer records them yet, so the panel says what it can see
                rather than claiming the list is complete. */}
            <EmptyState
              title="Nothing is recorded as reading this Datastream"
              description="A row appears here when an analysis actually reads a published version of this Datastream — ask a question in Explore that this Datastream answers, and the Result records itself against the exact version it was built on. Nothing is recorded in advance: this is what was read, not who was promised access."
            />
          </div>
        ) : (
          // Named, like the two scrolling tables on this tab: a list of the
          // objects served downstream is something a screen reader must be able
          // to reach as one thing, not by walking the page.
          <ul aria-label="Used by" className="m-0 grid list-none gap-3 p-5">
            {usedBy.map((consumer) => {
              // THE ADDRESS IS BUILT, NOT READ, WHENEVER IT CAN BE.
              //
              // `owner_href` is a stored column, so this screen did not build it
              // and cannot vouch for it — the fallback used to be the literal
              // `"#"`, a live-looking link that reloaded the page onto itself.
              // `consumer_kind` and `consumer_ref`, on the other hand, are the
              // two facts the console can turn into a semantic reference the
              // shell validates against its own registry before it navigates. So
              // that comes first, the stored address stays as the fallback for a
              // kind the registry does not declare, and a consumer with neither
              // is still NAMED — it is the downstream reference, which is the
              // point of the panel — while saying why it opens nothing.
              const kind = text(consumer.consumer_kind);
              const reference = text(consumer.consumer_ref);
              const label = `${titleCase(kind)} · ${reference}`;
              const versionRef = typeof consumer.consumer_version_ref === "string"
                ? consumer.consumer_version_ref
                : null;
              const owner = onOpenOwner ? consumerOwner(kind, reference, versionRef) : null;
              const href = resolvableHref(typeof consumer.owner_href === "string" ? consumer.owner_href : null);
              return (
                <li key={`${kind}:${reference}`}>
                  {owner ? (
                    <button
                      type="button"
                      className="text-primary underline"
                      onClick={() => onOpenOwner?.(owner)}
                    >
                      {label}
                    </button>
                  ) : href ? (
                    <a className="text-primary underline" href={href}>{label}</a>
                  ) : (
                    <span
                      className="text-text-secondary"
                      title={
                        kind === "delivery"
                          ? "A delivery is not an object this console registers, so there is no screen to open for it. Its reference is on the Output row above."
                          : "No address this console resolves was recorded for this consumer, so there is nothing to open."
                      }
                    >
                      {label}
                    </span>
                  )}
                  {/* The version this consumer is PINNED to. The panel calls
                      itself version-bound and never showed one; whether the link
                      beside it opens that exact version depends on the kind, and
                      the hover says which of the two this is rather than leaving
                      a person to find out by clicking. */}
                  {versionRef && (
                    <span
                      className="ml-2 font-mono text-caption text-text-secondary"
                      title={
                        owner?.version_id
                          ? "The pinned version, and the link opens it."
                          : "The pinned version. The link opens the object; this console cannot open a pinned version of it yet."
                      }
                    >
                      {versionRef}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Panel>

      {/* WHERE THE RAW EXTRACTS LIVE — ONE OWNER, AND IT IS `Processing`.
          Amended 2026-08-18.

          The two Metrics that stood here were drawn WORD FOR WORD by
          `WorkbenchProcessingPage` as well: one fact, two panels, free to
          diverge the day either is reworded. Two tabs of one Datastream
          answering the same question is the defect amendment 3 of the
          2026-08-11 review named about the capability projection, and this was
          the same shape.

          `Processing` keeps it, because the raw zone is a property of what the
          plan DOES with what it collected — the tab whose ordered chain already
          names step by step where each landing goes. This tab's subject is what
          is SERVED and to whom, and the raw zone is neither.

          The 2026-08-05 amendment 1 put the fact "on the Outputs tab" when it
          removed the wizard's `Destination` step, and what that amendment was
          protecting is that the fact must be READABLE somewhere in the
          Workbench rather than lost with the step. It is, and this sentence is
          the road to it — the amendment is honoured, not overturned. */}
      <Panel flush>
        <PanelHeader
          title="Where the raw extracts land"
          description="Who owns the raw zone, and how long its extracts are kept, are set with the plan that writes them — on the Processing tab of this Datastream."
        />
        <div className="p-5">
          <Status as="block" tone="neutral" title="Read this on Processing">
            The raw-zone owner and the retention this Datastream declares are one
            subject with the execution plan, so they are stated once, there.
          </Status>
        </div>
      </Panel>
    </>
  );
}
