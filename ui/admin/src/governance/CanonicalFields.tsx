/**
 * Canonical Fields — lot A1 of the front correction plan (issue #68).
 *
 * WHAT WAS MISSING, MEASURED. `app.mdm_canonical_fields` is the enumeration six
 * production modules validate every mdm-bound binding against — the Datastream
 * field mapping, the projection, the data model, the daily breakdown, the file
 * source producer and the registry itself. It held ZERO rows at both scopes, and
 * no screen anywhere in the console listed it: a person could be told an id was
 * not an active canonical field and had no way, anywhere, to find out which ids
 * were.
 *
 * TWO SCOPES, AND THEY ARE NOT THE SAME OBJECT WEARING A LABEL.
 *
 *   * PLATFORM — the shared vocabulary every Project of the instance aligns on.
 *     It is GOVERNED and is deliberately not minted from a Project door:
 *     `canonical_field_registry.declare_project_field` refuses a null project by
 *     name, because minting one here would let one client edit what every other
 *     client's data is compared against. So this screen shows those rows and
 *     offers no control over them at all — not a disabled button, which reads as
 *     "you lack a permission", but no control, which reads as "this is not
 *     edited from here".
 *   * PROJECT — what this client declared for their own objects. Eleven fields
 *     describe one video, of which the governed vocabulary covers zero.
 *
 * They are rendered as two separate panels rather than one table with a Scope
 * column, because the difference decides what a person may do, and a column is
 * something the eye filters out.
 *
 * THREE STATES, AND TWO OF THEM ARE DIFFERENT SENTENCES.
 *
 *   * populated — one row per field, naming its kind, its value type and its
 *     aggregation, which are the three properties that decide how it is read and
 *     whether it may be summed;
 *   * EMPTY — the important state, because zero rows is what every Project has
 *     today. It says why the list is empty and names the gesture that fills it;
 *   * BROKEN — its own sentence and NO table underneath, because a screen that
 *     could not read must never look like a screen that read and found nothing.
 *
 * IT IS A READ, AND IT SAYS SO. The writer is the file source description in
 * Data › Datastreams, which mints the fields an object needs against the
 * Datastream that feeds it. A second writer here would let a field be declared
 * without the object it qualifies.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Checkbox, EmptyState, Field, Input, ObjectId, Panel, PanelBody, PanelHeader, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, wireWord } from "../ui";
import { apiDelete, ApiError, apiGet, apiPost } from "../lib/apiFetch";
import { buildPath, type CanonicalRoute, useRoute } from "../shell/router";

export interface CanonicalField {
  id: string;
  canonical_name: string;
  concept_kind: "metric" | "dimension";
  value_type: string;
  aggregation: string | null;
  object_kind: string | null;
  non_additive: boolean;
  unit: string | null;
  description: string | null;
  scope: "platform" | "project";
}

interface CanonicalFieldsResponse {
  fields: CanonicalField[];
  scope_counts: { platform: number; project: number };
  empty_reason: { code: string; message: string } | null;
}

interface CommonKeyComponent {
  canonical_field_id: string;
  canonical_name?: string;
}

interface CommonKey {
  id: string;
  name: string;
  description: string | null;
  status: string;
  current_version: {
    id: string;
    version_number: number;
    components: CommonKeyComponent[];
  } | null;
}

interface CommonKeysResponse {
  common_keys: CommonKey[];
  empty_reason: { code: string; message: string } | null;
}

/** One identity the Project's flows share, derived across every current mapping
 *  (`governance.md`, amendment of 2026-09-04). Never stored, never pinning. */
interface SharedIdentity {
  identity: string;
  kind: "column" | "canonical";
  /** `dimension` or `metric` (2026-09-05): a measure's gesture is not a key's. */
  role?: "dimension" | "metric";
  also_known_as?: string[];
  carrier_count: number;
  carriers: { datastream_id: string; datastream_name: string; column: string; pinned_to: string | null }[];
  already_pinned: string[];
  pending_publication: string[];
  to_pin: string[];
  canonical_field_id: string | null;
  canonical_name: string | null;
  gesture: string;
}

interface SharedIdentitiesResponse {
  proposals: SharedIdentity[];
  unmapped_datastreams: { id: string; name: string }[];
  empty_reason: { code: string; message: string } | null;
}

interface PinOutcome {
  pinned: number;
  already_pinned: number;
  refused: number;
  flows: { datastream_id: string; datastream_name?: string; column: string; outcome: string; message?: string; no_candidate_reason?: string }[];
}

const ROOT = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/mdm/canonical-fields`;

const COMMON_KEYS_ROOT = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/mdm/common-keys`;

const KIND_LABEL: Record<string, string> = {
  metric: "Metric",
  dimension: "Dimension",
};

/** The value types are stored as the semantic layer spells them. Rendered in the
 *  reader's words rather than in the enum's, and unknown values pass through
 *  untouched: inventing a label for a value the server added would be worse than
 *  showing the value. */
const VALUE_TYPE_LABEL: Record<string, string> = {
  integer: "Whole number",
  decimal: "Decimal number",
  money: "Money",
  ratio: "Ratio",
  percent: "Percentage",
  duration: "Duration",
  string: "Text",
  date: "Date",
  timestamp: "Date and time",
  boolean: "Yes / no",
};

const AGGREGATION_LABEL: Record<string, string> = {
  sum: "Sum",
  average: "Average",
  min: "Minimum",
  max: "Maximum",
  count: "Count",
};

/**
 * How this field is summed, or the reason it never is.
 *
 * NEVER AN EMPTY CELL. A dimension is not a measure and carries no aggregation
 * at all; a metric with no aggregation is `non_additive`, which the database
 * itself guarantees — a metric declaring neither is refused, because "a measure
 * must never be silently non-summable". A blank here would read as a field
 * somebody forgot to finish.
 */
function aggregationLabel(field: CanonicalField): string {
  if (field.concept_kind === "dimension") return "Not a measure";
  if (field.aggregation) return AGGREGATION_LABEL[field.aggregation] ?? field.aggregation;
  return "Never summed";
}

function CommonKeys({ projectId, fields }: { projectId: string; fields: CanonicalField[] }) {
  const [keys, setKeys] = useState<CommonKey[]>([]);
  const [name, setName] = useState("");
  const [components, setComponents] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The key a person asked to retire, held until they confirm it. Archiving is
   *  not a delete — the versions stay, and a relationship that pins one keeps
   *  working — but it is still a decision, so it is never one click. */
  const [retiring, setRetiring] = useState<CommonKey | null>(null);
  const [retireError, setRetireError] = useState<string | null>(null);
  /** What the Project's flows share, read across every current mapping in one
   *  call. It is the sentence a person needs BEFORE declaring a key: which
   *  identities exist, on which flows, and which flows still have to pin them.
   *  Until 2026-09-04 the only place that said so was the pairwise match
   *  catalogue -- thirty-six entries on a ten-flow project. */
  const [shared, setShared] = useState<SharedIdentitiesResponse | null>(null);
  /** The identity being pinned from the panel, and what the last pin answered
   *  per flow -- the panel performs the gesture it names (2026-09-05). */
  const [pinning, setPinning] = useState<string | null>(null);
  const [pinOutcomes, setPinOutcomes] = useState<Record<string, PinOutcome | { error: string }>>({});
  const dimensions = fields.filter((field) => field.concept_kind === "dimension");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiGet<CommonKeysResponse>(COMMON_KEYS_ROOT(projectId));
      setKeys(body.common_keys);
      setError(null);
    } catch (reason) {
      setKeys([]);
      setError(reason instanceof ApiError ? reason.message : "The common keys could not be read.");
    } finally {
      setLoading(false);
    }
    try {
      const proposed = await apiGet<SharedIdentitiesResponse>(`${COMMON_KEYS_ROOT(projectId)}/proposals`);
      setShared({
        proposals: Array.isArray(proposed?.proposals) ? proposed.proposals : [],
        unmapped_datastreams: Array.isArray(proposed?.unmapped_datastreams) ? proposed.unmapped_datastreams : [],
        empty_reason: proposed?.empty_reason ?? null,
      });
    } catch {
      // An unreadable proposal is not a missing one: the section says it could not
      // be read rather than showing an empty state that would mean "nothing shared".
      setShared(null);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * THE PANEL SAID « PIN `views` ON THE MAPPING OF 7 FLOW(S) » AND OFFERED NO
   * CONTROL: a person had to open seven Mapping tabs. This posts the flows still
   * to pin to the console door onto the same function the MCP tool calls, then
   * reads the proposal again -- the server says what changed.
   */
  const pin = async (row: SharedIdentity) => {
    if (!row.canonical_field_id) return;
    const toPin = new Set(row.to_pin);
    const carriers = row.carriers
      .filter((carrier) => toPin.has(carrier.datastream_id))
      .map((carrier) => ({ datastream_id: carrier.datastream_id, column: carrier.column }));
    if (carriers.length === 0) return;
    setPinning(row.identity);
    try {
      const outcome = await apiPost<PinOutcome>(`${COMMON_KEYS_ROOT(projectId)}/proposals/pin`, {
        canonical_field_id: row.canonical_field_id,
        carriers,
      });
      setPinOutcomes((current) => ({ ...current, [row.identity]: outcome }));
      await load();
    } catch (reason) {
      setPinOutcomes((current) => ({
        ...current,
        [row.identity]: { error: reason instanceof ApiError ? reason.message : "The Datastreams were not pinned." },
      }));
    } finally {
      setPinning(null);
    }
  };

  const declare = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim() || components.length === 0) {
      setError("Name the key and select at least one canonical dimension.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const body = await apiPost<{ common_key: CommonKey }>(COMMON_KEYS_ROOT(projectId), {
        name: name.trim(),
        components,
      });
      setKeys((current) => [...current, body.common_key]);
      setName("");
      setComponents([]);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "The common key was not declared.");
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * THE ROUTE WAS SERVED AND NO CONTROL REACHED IT. `DELETE .../mdm/common-keys/{id}`
   * has existed since the store did, with its own refusal for a key a Semantic
   * View relationship pins — a 409 whose sentence already names the repair. This
   * table listed keys and offered no way to retire one, so a name typed by
   * mistake stayed in the vocabulary for good.
   */
  const retire = async (key: CommonKey) => {
    setSubmitting(true);
    setRetireError(null);
    try {
      await apiDelete(`${COMMON_KEYS_ROOT(projectId)}/${encodeURIComponent(key.id)}`);
      setRetiring(null);
      // Read the list again rather than editing it here: the server decides what
      // a retired key looks like, and guessing it would be a second opinion.
      await load();
    } catch (reason) {
      // The 409 sentence names the relationships to retire first. Rendered whole,
      // and on the confirmation rather than behind it, so the key stays selected.
      setRetireError(
        reason instanceof ApiError ? reason.message : "The common key was not retired.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Panel flush data-testid="mdm-common-keys">
      <PanelHeader
        title="Common keys"
        description="Declare the canonical dimensions that mean the same thing in several Datastreams. Analytics uses an exact version of this key to explain and govern each proposed match."
      />
      <PanelBody className="flex flex-col gap-5">
        {loading && <p role="status" className="m-0 text-body text-text-secondary">Loading common keys…</p>}
        {!loading && keys.length === 0 && !error && (
          <p className="m-0 text-body text-text-secondary">
            No common key is declared yet. Select the shared business dimensions below; a key is
            useful only after Datastream mappings implement those dimensions.
          </p>
        )}
        {/* WHAT THE FLOWS SHARE, BEFORE ANY KEY IS DECLARED. Derived across every
            current mapping of the Project in one read; never stored, never
            pinning. Each row names the identity, the flows that carry it, the
            ones already pinned, the ones whose pin waits in an unpublished
            mapping version, the ones still to pin, and the gesture. */}
        <section className="grid gap-2" data-testid="mdm-shared-identities">
          <h3 className="m-0 text-body font-semibold text-text">Shared identities</h3>
          {shared === null && !loading && (
            <p className="m-0 text-caption text-text-secondary">
              What the Datastreams share could not be read. Reload the page; if it stays unreadable, the
              Datastream mappings of this Project are the place to look.
            </p>
          )}
          {shared && shared.proposals.length === 0 && (
            <p className="m-0 text-body text-text-secondary">
              {shared.empty_reason?.message ??
                "No published Datastream carries a dimension another Datastream shares, nor a measure to govern, yet. Publish a Datastream with a mapping, then come back."}
            </p>
          )}
          {shared && shared.proposals.length > 0 && (
            <TableScroll label="Shared identities">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Identity</TableHead>
                    <TableHead>Role</TableHead>
                    <TableHead>Carried by</TableHead>
                    <TableHead>Pinned</TableHead>
                    <TableHead>Canonical field</TableHead>
                    <TableHead>What to do</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {shared.proposals.map((row) => (
                    <TableRow key={`${row.kind}:${row.identity}`} data-testid={`shared-identity-${row.identity}`}>
                      <TableCell className="font-semibold text-text">
                        {row.identity}
                        {/* Across sources one identity travels under several column
                            names (Search Console `page`, GA4 `pagePath`): said, so a
                            reader recognises their own column in the shared word. */}
                        {row.also_known_as && row.also_known_as.length > 0 && (
                          <span className="block text-caption font-normal text-text-secondary">
                            also seen as {row.also_known_as.join(", ")}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-text-secondary">{row.role === "metric" ? "Measure" : "Dimension"}</TableCell>
                      <TableCell className="text-text-secondary">
                        {row.carrier_count} Datastream{row.carrier_count > 1 ? "s" : ""}
                        <span className="block text-caption">
                          {row.carriers.map((carrier) => carrier.datastream_name).join(", ")}
                        </span>
                      </TableCell>
                      <TableCell className="text-text-secondary">
                        {row.already_pinned.length} of {row.carrier_count}
                        {row.pending_publication.length > 0 && (
                          <span className="block text-caption">
                            {row.pending_publication.length} waiting in an unpublished mapping version
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-text-secondary">{row.canonical_name ?? "None yet"}</TableCell>
                      <TableCell className="text-caption text-text-secondary">
                        <span className="block">{row.gesture}</span>
                        {row.canonical_field_id && row.to_pin.length > 0 && (
                          <Button
                            type="button"
                            size="sm"
                            variant="secondary"
                            className="mt-1"
                            disabled={pinning !== null}
                            onClick={() => void pin(row)}
                            data-testid={`pin-shared-identity-${row.identity}`}
                          >
                            {pinning === row.identity
                              ? "Pinning…"
                              : `Pin ${row.to_pin.length} Datastream${row.to_pin.length > 1 ? "s" : ""}`}
                          </Button>
                        )}
                        {(() => {
                          const outcome = pinOutcomes[row.identity];
                          if (!outcome) return null;
                          if ("error" in outcome) {
                            return <span className="block text-danger" role="alert">{outcome.error}</span>;
                          }
                          return (
                            <span className="block" data-testid={`pin-outcome-${row.identity}`}>
                              {outcome.pinned} pinned, {outcome.already_pinned} already pinned, {outcome.refused} refused.
                              {outcome.flows
                                .filter((flow) => flow.outcome === "refused")
                                .map((flow) => (
                                  <span key={`${flow.datastream_id}:${flow.column}`} className="block text-danger">
                                    {flow.datastream_name ?? flow.datastream_id}: {flow.message}
                                  </span>
                                ))}
                            </span>
                          );
                        })()}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
          {shared && shared.unmapped_datastreams.length > 0 && (
            <p className="m-0 text-caption text-text-secondary">
              Not counted, because they publish no mapping yet:{" "}
              {shared.unmapped_datastreams.map((datastream) => datastream.name).join(", ")}.
            </p>
          )}
        </section>

        {keys.length > 0 && (
          <TableScroll label="Declared common keys">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Key</TableHead>
                  <TableHead>Exact version</TableHead>
                  <TableHead>Canonical dimensions</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>
                    <span className="sr-only">Retire</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((key) => (
                  <TableRow key={key.id}>
                    <TableCell className="font-semibold text-text">{key.name}</TableCell>
                    <TableCell className="font-mono text-caption text-text-secondary">
                      {key.current_version
                        ? `v${key.current_version.version_number} · ${key.current_version.id}`
                        : "Unavailable"}
                    </TableCell>
                    {/* THE FROZEN WORD, NEVER THE FROZEN ID. A key version pins
                        `canonical_name` beside every component
                        (`mdm_common_keys.Component.as_dict`), read off
                        `app.mdm_canonical_fields.canonical_name`, which is
                        `NOT NULL` since migration 032. `?? canonical_field_id`
                        could therefore only fire on a payload written before the
                        name was frozen -- and would then have spelt the key as
                        `mdm_<ULID> + mdm_<ULID>`, which names no identity at all.
                        A component with no frozen word says so instead. */}
                    <TableCell className="text-text-secondary">
                      {key.current_version?.components
                        .map((component) => component.canonical_name || "Unnamed field")
                        .join(" + ") || "Unavailable"}
                    </TableCell>
                    {/* `Active`, like every other state in the console. The wire
                        word is lowercase and printing it raw made this the only
                        table where a status reads in the database's voice. */}
                    <TableCell>
                      <Status tone={key.status === "active" ? "success" : "neutral"}>
                        {key.status ? key.status[0].toUpperCase() + key.status.slice(1) : "Unknown"}
                      </Status>
                    </TableCell>
                    <TableCell>
                      {/* Only an active key can be retired; an archived one is
                          already retired and a control that repeats a state is
                          the read-only theatre this lens just left. */}
                      {key.status === "active" && (
                        <Button
                          variant="ghost"
                          size="sm"
                          data-testid={`retire-common-key-${key.id}`}
                          onClick={() => {
                            setRetireError(null);
                            setRetiring(key);
                          }}
                        >
                          Retire
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}

        {/* THE CONFIRMATION SAYS WHAT ARCHIVING IS AND WHAT IT IS NOT. A person
            reading "Retire" has no way to know that the versions survive and
            that a pinned relationship keeps working — and that is exactly the
            difference between this and a delete. */}
        {retiring && (
          <div
            className="grid gap-3 rounded-lg border border-border p-4"
            data-testid="retire-common-key-confirm"
          >
            <p className="m-0 text-body text-text">
              Retire <strong>{retiring.name}</strong> from this Project&apos;s vocabulary?
            </p>
            <p className="m-0 text-caption text-text-secondary">
              Nothing is deleted: its versions stay, and any Semantic View relationship that pins
              one keeps working. What changes is that no new relationship can be built on it. A key
              a relationship still pins is refused, and the refusal names which ones to retire
              first.
            </p>
            {retireError && (
              <Status as="block" tone="error" title="The key was not retired">
                {retireError}
              </Status>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                disabled={submitting}
                data-testid="retire-common-key-confirmed"
                onClick={() => void retire(retiring)}
              >
                {submitting ? "Retiring…" : `Retire ${retiring.name}`}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={submitting}
                data-testid="retire-common-key-cancel"
                onClick={() => {
                  setRetiring(null);
                  setRetireError(null);
                }}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        <form className="grid gap-4 rounded-lg border border-border p-4" onSubmit={declare}>
          <div>
            <h3 className="m-0 text-ui font-semibold text-text">Declare a common key</h3>
            <p className="m-0 text-caption text-text-secondary">
              Examples: Day, Campaign, Country, or Campaign + Day. Only canonical dimensions are
              offered; raw connector columns never become matching authority.
            </p>
          </div>
          <Field label="Business name" required>
            {(props) => (
              <Input
                {...props}
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Campaign + day"
              />
            )}
          </Field>
          <fieldset className="grid gap-2 border-0 p-0">
            <legend className="text-label font-label text-text">Canonical dimensions</legend>
            {dimensions.length === 0 ? (
              <p className="m-0 text-caption text-text-secondary">
                Declare or map a canonical dimension first; metrics cannot be matching keys.
              </p>
            ) : dimensions.map((field) => (
              <label key={field.id} className="flex items-center gap-2 text-body text-text">
                <Checkbox
                  checked={components.includes(field.id)}
                  onCheckedChange={(checked) => setComponents((current) => (
                    checked ? [...current, field.id] : current.filter((id) => id !== field.id)
                  ))}
                />
                <span>{field.canonical_name}</span>
                <ObjectId value={field.id} title="Canonical Field" />
              </label>
            ))}
          </fieldset>
          {error && <Status as="block" tone="error">{error}</Status>}
          <div>
            <Button type="submit" disabled={submitting || dimensions.length === 0}>
              {submitting ? "Declaring…" : "Declare common key"}
            </Button>
          </div>
        </form>
      </PanelBody>
    </Panel>
  );
}

export default function CanonicalFields({ projectId }: { projectId: string }) {
  const { route, navigate } = useRoute();
  const [payload, setPayload] = useState<CanonicalFieldsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** The field a person asked to retire, held until they confirm it. Archiving
   *  is not a delete -- a published mapping that pins the id keeps resolving --
   *  but it frees the NAME, so it is never one click. */
  const [retiring, setRetiring] = useState<CanonicalField | null>(null);
  const [retireError, setRetireError] = useState<string | null>(null);
  const [retireSubmitting, setRetireSubmitting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiGet<CanonicalFieldsResponse>(ROOT(projectId));
      setPayload(body);
      setError(null);
    } catch (err) {
      // BROKEN is not EMPTY. The payload is dropped so no table can be drawn
      // from a read that did not happen.
      setPayload(null);
      setError(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const fields = payload?.fields ?? [];
  const platform = useMemo(() => fields.filter((f) => f.scope === "platform"), [fields]);
  const project = useMemo(() => fields.filter((f) => f.scope === "project"), [fields]);

  /**
   * Retire a field of this Project (AI-304).
   *
   * THE CONTROL HAD NOWHERE TO REACH. This table listed fields and the route
   * served POST and GET only, so a name typed by mistake stayed in the
   * vocabulary for good — the mirror image of the Common Keys table below,
   * where the DELETE was served and no control reached it.
   */
  const retire = async (field: CanonicalField) => {
    setRetireSubmitting(true);
    setRetireError(null);
    try {
      await apiDelete(`${ROOT(projectId)}/${encodeURIComponent(field.id)}`);
      setRetiring(null);
      // Read the list again rather than editing it here: the server decides what
      // a retired field looks like, and guessing it would be a second opinion.
      await load();
    } catch (reason) {
      setRetireError(
        reason instanceof ApiError ? reason.message : "The field was not retired.",
      );
    } finally {
      setRetireSubmitting(false);
    }
  };

  /** The object address of one field. It resolves because `canonical-field` is a
   *  registered object type of this section with a `definition` tab — before lot
   *  A1 no link could point at a canonical field at all. */
  const fieldHref = (field: CanonicalField) =>
    buildPath({
      ...route,
      workspace: "governance",
      section: "semantic-model",
      lens: null,
      objectType: "canonical-field",
      objectId: field.id,
      tab: "definition",
      versionId: null,
      action: null,
      query: {},
    } as CanonicalRoute);

  const openField = (field: CanonicalField) =>
    navigate({
      workspace: "governance",
      section: "semantic-model",
      lens: null,
      objectType: "canonical-field",
      objectId: field.id,
      tab: "definition",
      versionId: null,
      action: null,
      query: {},
    });

  const datastreamsHref = buildPath({
    ...route,
    workspace: "data",
    section: "datastreams",
    lens: null,
    objectType: null,
    objectId: null,
    tab: null,
    versionId: null,
    action: null,
    query: {},
  } as CanonicalRoute);

  const openDatastreams = () =>
    navigate({
      workspace: "data",
      section: "datastreams",
      lens: null,
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      action: null,
      query: {},
    });

  const table = (rows: CanonicalField[], label: string, retirable = false) => (
    <TableScroll label={label}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Field</TableHead>
            <TableHead>Kind</TableHead>
            <TableHead>Value type</TableHead>
            <TableHead>How it is summed</TableHead>
            <TableHead>Describes</TableHead>
            {/* The column exists only where the gesture does. A shared row
                carries no control at all — not a disabled one, which reads as a
                missing permission rather than as "this is not yours to change". */}
            {retirable && (
              <TableHead>
                <span className="sr-only">Retire</span>
              </TableHead>
            )}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((field) => (
            <TableRow key={field.id}>
              <TableCell>
                <a
                  className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                  href={fieldHref(field)}
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
                    openField(field);
                  }}
                >
                  {field.canonical_name}
                </a>
                {field.description && (
                  <p className="m-0 text-caption text-text-secondary">{field.description}</p>
                )}
              </TableCell>
              <TableCell className="text-text-secondary">
                {KIND_LABEL[field.concept_kind] ?? wireWord(field.concept_kind)}
              </TableCell>
              <TableCell className="text-text-secondary">
                {VALUE_TYPE_LABEL[field.value_type] ?? field.value_type}
                {field.unit ? ` (${field.unit})` : ""}
              </TableCell>
              <TableCell className="text-text-secondary">{aggregationLabel(field)}</TableCell>
              {/* A platform field is about the fact itself and about no object —
                  the database refuses an object kind on a platform row. "Any
                  object" would be a claim; the em dash is the absence. */}
              <TableCell className="text-text-secondary">{field.object_kind ?? "—"}</TableCell>
              {retirable && (
                <TableCell>
                  <Button
                    variant="secondary"
                    size="sm"
                    data-testid={`retire-canonical-field-${field.id}`}
                    onClick={() => {
                      setRetiring(field);
                      setRetireError(null);
                    }}
                  >
                    Retire
                  </Button>
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );

  return (
    <div className="flex flex-col gap-4" data-testid="canonical-fields">
      <Panel flush>
        <PanelHeader
          title="Canonical fields"
          description="The vocabulary every mapping is checked against. A field says what it is — a measure or a way of slicing — what kind of value it holds, and how it may be added up."
        />
        <PanelBody className="flex flex-col gap-6">
          {loading && (
            <p role="status" className="m-0 text-body text-text-secondary">
              Loading canonical fields…
            </p>
          )}

          {/* BROKEN — its own sentence, and no table of any kind underneath. */}
          {!loading && error && (
            <Status
              as="block"
              tone="error"
              title="The canonical vocabulary could not be read."
              data-testid="canonical-fields-broken"
              action={
                <Button variant="secondary" onClick={() => void load()}>
                  Retry
                </Button>
              }
            >
              {error} No field is listed, because none was read — this is not a Project without a
              vocabulary.
            </Status>
          )}

          {/* EMPTY — the state every Project is in today. It says WHY, and it
              names the two gestures, one per scope. Neither is a deployment
              state and neither is a table name. */}
          {!loading && payload && fields.length === 0 && (
            <EmptyState
              title="No field has been defined yet, at either level."
              description={
                <span data-testid="canonical-fields-empty">
                  {payload.empty_reason?.message ??
                    "No field has been declared yet, at either scope."}{" "}
                  Describe a file in{" "}
                  <a
                    className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                    href={datastreamsHref}
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
                      openDatastreams();
                    }}
                  >
                    Data › Datastreams
                  </a>{" "}
                  and name the fields it carries: each one is declared against the object that file
                  feeds. Nothing has been hidden or substituted.
                </span>
              }
            />
          )}

          {!loading && payload && fields.length > 0 && (
            <>
              {/* SHARED — listed first, because it is what a Project's own
                  vocabulary extends. No control of any kind: it is not edited
                  from a Project, and a disabled button would say "you lack a
                  permission" instead of "this is not yours to change". */}
              <section className="flex flex-col gap-2" data-testid="canonical-fields-platform">
                <div>
                  <h3 className="m-0 text-ui font-semibold text-text">Shared vocabulary</h3>
                  <p className="m-0 text-caption text-text-secondary">
                    Defined once for every Project on this installation, so the same word means the
                    same thing everywhere. It is set up with your provider and is not changed from
                    here.
                  </p>
                </div>
                {platform.length === 0 ? (
                  <p className="m-0 text-caption text-text-secondary">
                    No shared field is defined on this installation yet. Every field below belongs
                    to this Project alone.
                  </p>
                ) : (
                  table(platform, "Shared canonical fields")
                )}
              </section>

              {/* THIS PROJECT'S OWN — the client's half of the arbitration. */}
              <section className="flex flex-col gap-2" data-testid="canonical-fields-project">
                <div>
                  <h3 className="m-0 text-ui font-semibold text-text">This Project's own fields</h3>
                  <p className="m-0 text-caption text-text-secondary">
                    What this Project needed and the shared vocabulary did not cover. A field is
                    declared while describing the file that carries it, so it is always attached to
                    the object it belongs to.
                  </p>
                </div>
                {project.length === 0 && (
                  <p className="m-0 text-caption text-text-secondary">
                    This Project has declared no field of its own. Describe a file in{" "}
                    <a
                      className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                      href={datastreamsHref}
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
                        openDatastreams();
                      }}
                    >
                      Data › Datastreams
                    </a>{" "}
                    to add one.
                  </p>
                )}
                {project.length > 0 && table(project, "Canonical fields of this Project", true)}

                {/* THE CONFIRMATION SAYS WHAT RETIRING IS AND WHAT IT IS NOT. A
                    person reading "Retire" has no way to know that a published
                    mapping pinning this id keeps resolving, nor that the NAME is
                    freed the moment the field is retired — and those two are
                    exactly the difference between this and a delete. */}
                {retiring && (
                  <div
                    className="grid gap-3 rounded-lg border border-border p-4"
                    data-testid="retire-canonical-field-confirm"
                  >
                    <p className="m-0 text-body text-text">
                      Retire <strong>{retiring.canonical_name}</strong> from this Project&apos;s
                      vocabulary?
                    </p>
                    <p className="m-0 text-caption text-text-secondary">
                      Nothing is deleted: a mapping already published against this field keeps
                      working. What changes is that it leaves this list and no new mapping can be
                      built on it — and the name <strong>{retiring.canonical_name}</strong> becomes
                      free again, so a later field may take it.
                    </p>
                    {retireError && (
                      <Status as="block" tone="error" title="The field was not retired">
                        {retireError}
                      </Status>
                    )}
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="sm"
                        disabled={retireSubmitting}
                        data-testid="retire-canonical-field-confirmed"
                        onClick={() => void retire(retiring)}
                      >
                        {retireSubmitting ? "Retiring…" : `Retire ${retiring.canonical_name}`}
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={retireSubmitting}
                        data-testid="retire-canonical-field-cancel"
                        onClick={() => {
                          setRetiring(null);
                          setRetireError(null);
                        }}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
              </section>
            </>
          )}
        </PanelBody>
      </Panel>
      {!loading && payload && fields.length > 0 && (
        <CommonKeys projectId={projectId} fields={fields} />
      )}
    </div>
  );
}
