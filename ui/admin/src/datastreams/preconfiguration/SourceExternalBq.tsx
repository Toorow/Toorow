/** Step 1's external_bq panel (57.12, T2b) — extracted from
 *  `DatastreamSetupWizard.tsx`.
 *
 *  THE ORDER IS THE CONTRACT (ratified document, `:77-83`): the exposed
 *  BigQuery access, then the object, then the declared writer, then the
 *  read-only acknowledgement — which is the consequence of the writer and is
 *  asked after it, never before.
 *
 *  THE OBJECT IS BROWSED LIVE (57.1 D2 / 57.11): choosing an access walks
 *  `GET /api/connections/{connection_ref.id}/accounts`, the same door the
 *  Sources page uses, and renders the project → dataset → table tree it
 *  returns. Two things are pinned by measurement, not taste:
 *
 *   - `?connector=bigquery` IS sent, and this line used to say the exact
 *     opposite for a reason that expired. BigQuery reads through the person's
 *     Google consent since the 2026-08-11 amendment, and AI-285 wired
 *     `bigquery.readonly` into `GOOGLE_SCOPE_CONNECTORS`. A consent carrying
 *     several Google scopes — which is every consent this console asks for —
 *     leaves `resolve_connection_connector` with nothing to guess from, so the
 *     unnamed call discovered against provider `google`, which declares no
 *     topology, and answered 409. Measured 2026-08-16; see `wizardApi.ts`.
 *   - `truncated` on a dataset node, or a listing that failed to load, keeps
 *     the free-text three-part reference as the stated fallback — a bounded
 *     list that presented itself as complete would be a fabrication.
 *
 *  The walk is component state, not wizard state: it is a READ, it owns no
 *  answer, and re-running it changes nothing the draft persists. Only picking
 *  a TABLE writes — navigating project and dataset writes nothing, because a
 *  grouping is not an answer and a navigation is not an edit. */
import { useEffect, useState } from "react";
import { Checkbox, Field, Input, NativeSelect, Status } from "../../ui";
import ConnectGoogleButton from "../../authorizations/ConnectGoogleButton";
import {
  listConnectionAccounts,
  type ConnectionAccountNode,
  type DatastreamSetupSourceOptions,
  type WizardApiConfig,
} from "../wizard/wizardApi";
import {
  EXTERNAL_BQ_COPY,
  sourceAccountId,
  truncatedListingSentence,
  type ExternalInput,
} from "./sourceStepShared";

/** What the live listing returned, and the states that are NOT the same
 *  absence: `failed` is a read that did not answer (the message names what
 *  failed), `loaded` with an empty tree is an authorization that can read
 *  nothing, and a loaded tree is browsable. `idle` means the chosen access
 *  carries no `connection_ref` — a deployment that predates 57.11 — and the
 *  free-text reference is then the only control, as it always was. */
type Browse =
  | { status: "idle" | "loading" }
  | { status: "failed"; message: string }
  | { status: "loaded"; tree: ConnectionAccountNode[] };

export interface SourceExternalBqProps {
  input: ExternalInput;
  options: DatastreamSetupSourceOptions | null;
  /** Boolean(observation || proposal) in the wizard — what makes an upstream
   *  change worth a confirmation. */
  hasDependentEvidence: boolean;
  cfg: WizardApiConfig;
  onChooseAccess: (accessRef: string) => void;
  onEditUpstream: (value: ExternalInput, changed: boolean, message: string) => void;
  onEdit: (value: ExternalInput) => void;
}

export default function SourceExternalBq({
  input, options, hasDependentEvidence, cfg, onChooseAccess, onEditUpstream, onEdit,
}: SourceExternalBqProps) {
  /** The access the operator picked, and what it can honestly say about the
   *  object. For this Connector a Source Account IS a table — only a leaf of the
   *  discovery walk carries an id — so the reference the step used to ask for a
   *  second time is already in hand. */
  const externalAccess = options?.external_access.find(
    (account) => sourceAccountId(account) === input.source.access_ref,
  );
  /** A prefilled READ, but only when the list it came from is whole. A capped
   *  listing that presents itself as a closed choice hides objects, and the
   *  operator reads that absence as "my table does not exist". */
  const externalObjectIsRead = Boolean(externalAccess?.external_object_ref) && externalAccess?.truncated !== true;
  // `objectIsFree` names a BOUNDED listing, so it needs a chosen access whose
  // listing was walked: with no access chosen at all, nothing was listed and
  // the "bounded" sentence is false (live finding, walk of 2026-08-10).
  const externalObjectHint = externalObjectIsRead
    ? EXTERNAL_BQ_COPY.objectIsRead
    : externalAccess && !externalAccess.external_object_ref
      ? EXTERNAL_BQ_COPY.objectIsTyped
      : externalAccess
        ? EXTERNAL_BQ_COPY.objectIsFree
        : EXTERNAL_BQ_COPY.objectIsNoAccess;

  /** The live walk, keyed on the AUTHORIZATION the access belongs to —
   *  `connection_ref.id`, the reference 57.11 put on every external access.
   *  Re-chosen access, new walk; same access, the walk is not re-run. */
  const connectionId = externalAccess?.connection_ref?.id ?? null;
  const [browse, setBrowse] = useState<Browse>({ status: "idle" });
  useEffect(() => {
    if (!connectionId) {
      setBrowse({ status: "idle" });
      return;
    }
    let disposed = false;
    setBrowse({ status: "loading" });
    listConnectionAccounts(cfg, connectionId, "bigquery")
      .then((result) => {
        if (!disposed) setBrowse({ status: "loaded", tree: result.accounts ?? [] });
      })
      .catch((reason) => {
        if (disposed) return;
        setBrowse({
          status: "failed",
          message: reason instanceof Error ? reason.message : "The listing route did not answer",
        });
      });
    return () => { disposed = true; };
  }, [cfg, connectionId]);

  /** Where the drill-down stands, as LOCAL navigation: `null` reads the
   *  default, which is the answer already given — an `object_ref` that names a
   *  project and a dataset of the tree preselects both, so a resumed draft
   *  opens the picker where its own answer lives rather than at the top of
   *  the walk. */
  const [navigated, setNavigated] = useState<{ project: string; dataset: string } | null>(null);
  const objectParts = input.source.object_ref.split(".");
  const tree = browse.status === "loaded" ? browse.tree : [];
  const pickedProject = tree.find((node) => node.label === navigated?.project)
    ?? tree.find((node) => node.label === objectParts[0])
    ?? tree[0]
    ?? null;
  const datasets = pickedProject?.children ?? [];
  const pickedDataset = datasets.find((node) => node.label === navigated?.dataset)
    ?? (navigated === null ? datasets.find((node) => node.label === objectParts[1]) : undefined)
    ?? datasets[0]
    ?? null;
  const tables = pickedDataset?.children ?? [];
  const pickObject = (objectRef: string) => {
    if (!objectRef) return;
    onEditUpstream(
      { ...input, source: { ...input.source, object_ref: objectRef } },
      hasDependentEvidence,
      "Changing the external object invalidates dependent evidence. Continue?",
    );
  };

  return <div className="grid gap-4">
    {/* THE "NOT WIRED" WARNING IS GONE, and it had to go with the client:
        `observe_external_bigquery` is now injected as a closure over the
        typed reader (57.1), so `Discover source` reads a real schema. A
        warning that outlives what it warned about is the defect this file
        has already been corrected for twice. */}
    {options && options.external_access.length === 0 && (
      <Status as="block" tone="neutral" title="No BigQuery access is exposed to this Project">
        <div className="grid gap-3 justify-items-start">
          <span>{EXTERNAL_BQ_COPY.noAccess}</span>
          <ConnectGoogleButton projectId={cfg.projectId} label="Connect Google" />
        </div>
      </Status>
    )}
    <Field label="BigQuery access" required>
      {({ id }) => (
        <NativeSelect
          id={id}
          value={input.source.access_ref}
          onChange={(event) => onChooseAccess(event.target.value)}
        >
          <option value="">Select exposed BigQuery access</option>
          {input.source.access_ref && !options?.external_access.some(
            (item) => sourceAccountId(item) === input.source.access_ref,
          ) && (
            <option value={input.source.access_ref}>Saved BigQuery access — options unavailable</option>
          )}
          {options?.external_access.map((account) => (
            <option key={sourceAccountId(account)} value={sourceAccountId(account)}>
              {account.external_object_ref ?? account.label}
            </option>
          ))}
        </NativeSelect>
      )}
    </Field>
    {/* QUESTION 2 — THE OBJECT, BROWSED LIVE. The picker and the free-text
        reference are TWO DOORS TO ONE ANSWER: picking a leaf writes the same
        `project.dataset.table` the operator would have typed, and the input
        stays because a bounded walk cannot promise completeness.

        ASKED ONCE AN ACCESS IS CHOSEN (2026-08-10, UX pass): before that,
        neither door can answer — the walk has nothing to walk and the
        reference nothing to be read against. */}
    {input.source.access_ref && <>
    {connectionId && browse.status === "loading" && (
      <Status as="block" tone="neutral" active title={EXTERNAL_BQ_COPY.browseLoading}>
        Project, then dataset, then table — the same walk the Sources page offers.
      </Status>
    )}
    {browse.status === "failed" && (
      <Status as="block" tone="error" title={EXTERNAL_BQ_COPY.browseFailedTitle}>
        {browse.message} {EXTERNAL_BQ_COPY.browseFailedBody}
      </Status>
    )}
    {browse.status === "loaded" && tree.length === 0 && (
      <Status as="block" tone="neutral" title="This authorization listed nothing it can read">
        {EXTERNAL_BQ_COPY.browseEmpty}
      </Status>
    )}
    {browse.status === "loaded" && tree.length > 0 && pickedProject && (
      <>
        <Field label="Project">
          {({ id }) => (
            <NativeSelect
              id={id}
              value={pickedProject.label}
              onChange={(event) => setNavigated({ project: event.target.value, dataset: "" })}
            >
              {tree.map((node) => (
                <option key={node.label} value={node.label}>{node.label}</option>
              ))}
            </NativeSelect>
          )}
        </Field>
        {pickedDataset && (
          <Field label="Dataset">
            {({ id }) => (
              <NativeSelect
                id={id}
                value={pickedDataset.label}
                onChange={(event) => setNavigated({
                  project: pickedProject.label,
                  dataset: event.target.value,
                })}
              >
                {datasets.map((node) => (
                  <option key={node.label} value={node.label}>
                    {node.label}{node.truncated ? " — listing bounded" : ""}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
        )}
        {/* A BOUNDED DATASET SAYS SO AT ITS OWN LEVEL. The bound lives on the
            dataset node (500 tables, `connector.py:_MAX_DISCOVERY_TABLES`), so
            the sentence appears when the operator is standing on it — and the
            free-text reference below is the door it names. */}
        {pickedDataset?.truncated && (
          <Status as="block" tone="warning" title="This dataset reached the listing bound">
            {EXTERNAL_BQ_COPY.browseTruncated(`${pickedProject.label}.${pickedDataset.label}`)}
          </Status>
        )}
        {pickedDataset && tables.length > 0 && (
          <Field label="Table or view">
            {({ id }) => (
              <NativeSelect
                id={id}
                value={tables.some((node) => node.id === input.source.object_ref) ? input.source.object_ref : ""}
                onChange={(event) => pickObject(event.target.value)}
              >
                <option value="">Select a table or view</option>
                {tables.map((node) => (
                  <option key={node.id} value={node.id}>{node.label}</option>
                ))}
              </NativeSelect>
            )}
          </Field>
        )}
      </>
    )}
    <Field label="Table or view reference" required hint={externalObjectHint}>
      {(props) => (
        <Input
          {...props}
          readOnly={externalObjectIsRead}
          value={input.source.object_ref}
          onChange={(event) => onEditUpstream(
            { ...input, source: { ...input.source, object_ref: event.target.value } },
            hasDependentEvidence,
            "Changing the external object invalidates dependent evidence. Continue?",
          )}
        />
      )}
    </Field>
    {/* A BOUNDED LIST THAT DOES NOT SAY SO IS A FABRICATED COMPLETENESS.
        Discovery lists at most `listing_bound` objects per dataset; when
        that bound was reached, a read-only prefill would hide the rest and
        the operator would conclude their table does not exist. */}
    {externalAccess?.truncated && externalAccess.external_object_ref && (
      <Status as="block" tone="warning" title="This dataset reached the discovery listing bound">
        {truncatedListingSentence(externalAccess)}
      </Status>
    )}
    </>}
    {/* QUESTION 3 — THE DECLARED WRITER. It stays authoritative: toorow never
        writes here, which is exactly what question 4 acknowledges. Asked once
        the object answers (2026-08-10, UX pass): who writes a table nobody
        has named is not a question yet. */}
    {input.source.object_ref && (
    <Field label="Declared writer" required>
      {(props) => (
        <Input
          {...props}
          value={input.source.declared_writer}
          onChange={(event) => onEditUpstream(
            { ...input, source: { ...input.source, declared_writer: event.target.value } },
            hasDependentEvidence,
            "Changing the declared writer invalidates dependent evidence. Continue?",
          )}
        />
      )}
    </Field>
    )}
    {/* QUESTION 4 — THE ACKNOWLEDGEMENT, a consequence of the writer, asked
        after it and never before. */}
    {input.source.object_ref && input.source.declared_writer && (
    <label className="flex items-center gap-2 text-body text-text">
      <Checkbox
        checked={input.source.readonly_acknowledged}
        onCheckedChange={(checked) => onEdit({
          ...input,
          source: { ...input.source, readonly_acknowledged: checked === true },
        })}
        aria-label="I acknowledge read-only access"
      />
      I acknowledge read-only access
    </label>
    )}
  </div>;
}
