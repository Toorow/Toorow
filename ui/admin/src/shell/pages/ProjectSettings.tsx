import { type FormEvent, useEffect, useMemo, useState } from "react";
import { RunInsights } from "../../daily-insights/InsightShare";
import { apiFetch } from "../../lib/apiFetch";
import AiSettingsPanel from "../../settings/AiSettingsPanel";
import { Badge, Button, CapabilityCoverage, CapabilityImpactMatrix, capabilityLabel, EmptyState, Field, Input, ObjectId, PageFrame, PageHeader, Panel, PanelHeader, ReferenceSelect, SectionHeader, Stack, stateLabel, stateTone, Status, Tabs, TabsContent, TabsList, TabsTrigger, Textarea, type ImpactMatrixRow, Retry } from "../../ui";

export type SettingsSection = "general" | "capabilities" | "changes" | "ai";

/** A semantic owner reference. The router resolves it; no screen builds a URL. */
export type OwnerReference = {
  surface: string;
  workspace: string | null;
  section: string | null;
  global_surface: string | null;
  global_section: string | null;
  /** The collection lens inside the section, when the reference names one.
   *
   *  Story 58.9: `Add a check` opens `controls-quality/data-quality`, and a
   *  reference that stopped at the section landed on `conflicts` — the section's
   *  declared default — which is a different collection. Optional: every
   *  server-composed reference predates it and carries none. */
  lens?: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  action: string | null;
  version_id: string | null;
  evidence_id: string | null;
};

type DefaultValue = {
  active: string | null;
  pending: string | null;
  origin: string;
  confirmation_status: string;
  owner_reference: OwnerReference;
};

type Coverage = {
  applicable: number;
  complete: number;
  partial: number;
  unavailable: number;
  excluded: number;
  pending: number;
  label: string;
  percentage: number | null;
};

type Capability = {
  key: string;
  availability: "always_present" | "optional";
  dependencies: string[];
  active: { state: string; version_id: string | null };
  pending: {
    state?: string;
    change_set_id?: string;
    prepared_payload_hash?: string;
  } | null;
  coverage: Coverage;
  exceptions: Array<{
    id?: string;
    reason?: string;
    reason_code?: string;
    severity?: string;
    kind?: string;
    owner_kind?: string;
    datastream_id?: string | null;
    owner_reference?: OwnerReference;
  }>;
  blockers: Array<{
    code?: string;
    message?: string;
    datastream_id?: string;
    owner_reference?: OwnerReference;
  }>;
  owner_links: Array<{ owner: string; owner_reference: OwnerReference }>;
};

type ChangeSet = {
  id: string;
  state: string;
  summary?: string;
  prepared_payload_hash?: string;
  blockers?: Array<{
    code?: string;
    message?: string;
    datastream_id?: string;
    owner_reference?: OwnerReference;
  }>;
  /** Server-composed: the six counts per capability plus one row per proposal. */
  impact_summary?: {
    coverage?: Record<string, Coverage>;
    matrix?: ImpactMatrixRow[];
  };
  confirmation_id?: string;
};

type SettingsEnvelope = {
  project: {
    id: string;
    name: string;
    description: string | null;
    organization: { id: string; name: string };
    can_edit: boolean;
    can_manage: boolean;
    business_domains: Array<{ id: string; name: string }>;
    defaults: {
      reporting_currency: DefaultValue;
      reporting_timezone: DefaultValue;
      verification_source: DefaultValue;
    };
    /** `proactive-assertions.md` decision 2: the project-scoped capability that
     *  decides whether anything may leave the platform at all. It is not one of
     *  the six compiled capabilities — it compiles into no Datastream — so it
     *  travels beside the defaults, in the store it lives in. */
    external_sharing: {
      state: "allowed" | "forbidden";
      decided_by: string | null;
      decided_at: string | null;
      is_platform_default: boolean;
      can_change: boolean;
    };
  };
  capabilities: Capability[];
  changes: ChangeSet[];
};

// The capability labels live in `ui/CapabilityCoverage`, next to the component
// that draws them. This screen held a second private copy; the two disagreed on
// two of the six ("Reporting Timezone" vs "Reporting timezone"), so the same
// capability read differently depending on which card you looked at.
const DEFAULT_LABELS: Array<[keyof SettingsEnvelope["project"]["defaults"], string]> = [
  ["reporting_currency", "Reporting currency"],
  ["reporting_timezone", "Reporting timezone"],
  ["verification_source", "Verification source"],
];

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(url, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

/**
 * The governed vocabulary each default is picked from, when it has one.
 *
 * Story 48.3 AC1: the reporting currency uses "a searchable validated ISO 4217
 * selector" and the reporting timezone "a searchable validated IANA identifier
 * selector; neither is a free-text or hard-coded subset". Both were plain text
 * inputs, so the only feedback on a typo arrived much later and nothing on the
 * screen said which values were even legal.
 *
 * `verification_source` has no such vocabulary and stays a text field. Listing it
 * here as `null` is deliberate: it records that the absence was checked rather
 * than that the case was forgotten.
 */
const DEFAULT_VOCABULARY: Record<
  keyof SettingsEnvelope["project"]["defaults"],
  { endpoint: string; label: string } | null
> = {
  reporting_currency: { endpoint: "/api/reference/currencies", label: "currencies" },
  reporting_timezone: { endpoint: "/api/reference/timezones", label: "timezones" },
  verification_source: null,
};

function DefaultCard({
  label,
  value,
  canEdit,
  vocabulary,
  onPrepare,
  onOpenOwner,
}: {
  label: string;
  value: DefaultValue;
  canEdit: boolean;
  vocabulary: { endpoint: string; label: string } | null;
  onPrepare: (value: string) => Promise<void>;
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  const [draft, setDraft] = useState(value.pending ?? value.active ?? "");
  const [saving, setSaving] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft.trim()) return;
    setSaving(true);
    try {
      await onPrepare(draft.trim());
    } finally {
      setSaving(false);
    }
  };

  const confirmed = value.confirmation_status === "confirmed";
  return (
    // Same column rule as `CapabilityCard`: three of these sit side by side and
    // one of them carries an `Origin:` line the other two do not, so their
    // `Prepare change` buttons landed on three different lines.
    <Panel className="h-full">
      <form className="flex h-full flex-col gap-4" onSubmit={submit}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="m-0 text-h3 font-h3 text-text">{label}</h3>
            <p className="mt-1 mb-0 text-ui text-text-secondary">
              {/* An organization suggestion is NOT an active value. Saying
                  "Active: EUR" for a value nobody confirmed is the screen half of
                  the column default this story removed from the schema. */}
              {/* "this foundation is not in force yet" said nothing a person
                  could act on — Jean, 2026-08-05: *"c'est quoi ça ?"*.
                  "Foundation" is not in the glossary and names no object here.
                  What is true and useful is that NOTHING is set, and that the
                  field below is how it gets set. */}
              {confirmed && value.active
                ? `Active: ${value.active}`
                : "Not set — no value is in force for this Project yet"}
            </p>
          </div>
          <Badge tone={confirmed ? "success" : "warning"}>
            {confirmed ? "Confirmed" : "Unconfirmed"}
          </Badge>
        </div>
        {value.pending ? (
          <Status tone="warning">
            <span>Suggested: {value.pending}</span>
            <span className="block text-caption text-text-secondary">
              A suggestion has no effect until it is confirmed in a Project Configuration Version.
            </span>
          </Status>
        ) : null}
        {/* `app.project_preferences` holds no row until something is prepared,
            so the LEFT JOIN feeding this returns null and the line rendered as
            a bare "Origin:" with nothing after it — then the next element's
            label, which read as its value. A field with no value is not a
            field: it does not render. */}
        {value.origin ? (
          <p className="m-0 text-caption text-text-secondary">Origin: {value.origin}</p>
        ) : null}
        <Field
          label={`New ${label.toLowerCase()}`}
          hint="Preparing creates a governed Change Set; it does not activate the value."
        >
          {(props) =>
            vocabulary ? (
              <ReferenceSelect
                {...props}
                endpoint={vocabulary.endpoint}
                vocabularyLabel={vocabulary.label}
                value={draft || null}
                disabled={!canEdit || saving}
                onChange={setDraft}
              />
            ) : (
              <Input
                {...props}
                value={draft}
                disabled={!canEdit || saving}
                onChange={(event) => setDraft(event.target.value)}
              />
            )
          }
        </Field>
        <div className="mt-auto flex items-center justify-between gap-3 border-t border-divider-base pt-4">
          <button
            type="button"
            className="text-ui font-semibold text-primary hover:underline"
            onClick={() => onOpenOwner?.(value.owner_reference)}
          >
            Open owner
          </button>
          <Button type="submit" disabled={!canEdit || saving || !draft.trim()}>
            {saving ? "Preparing…" : "Prepare change"}
          </Button>
        </div>
      </form>
    </Panel>
  );
}

function CapabilityCard({
  capability,
  projectId,
  canEdit,
  onPrepare,
  onOpenOwner,
}: {
  capability: Capability;
  projectId: string;
  canEdit: boolean;
  onPrepare: (enabled: boolean) => Promise<void>;
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  const label = capabilityLabel(capability.key);
  const enabled = capability.active.state !== "disabled";
  const nextEnabled = !enabled;
  const isCountry = capability.key === "country";
  const isTaxFees = capability.key === "tax_fees";
  const action = nextEnabled
    ? isCountry ? "Activate" : "Enable"
    : isCountry ? "Deactivate" : "Disable";
  // The long note is NOT part of the header row. Inside it, a wrapping flex put
  // the action under the paragraph on Country and at the top right on Tax &
  // Fees — one control, three positions in one grid, decided by how much prose
  // the capability happens to carry. The row now holds the title, its posture
  // and its action, and nothing that can grow; the note sits under the row, at
  // the same place on every card.
  const note = isTaxFees && enabled
    ? /* `Active: enabled` on this card does NOT mean the ladder is live.
         Migration 148 makes `tax_fees_active` true only when all four hold:
         the capability is not disabled, its pinned configuration version is
         the Project's active one, a tax_fee Rule Set version is published,
         AND a Money Policy version is published. This envelope proves the
         first and can prove the second; it carries neither published
         version, so the card names what it cannot see instead of showing a
         green state it has not measured. */
      <>
        Enabled here is not the same as effective. The ladder also requires a published
        Tax &amp; Fee Rule Set version and a published Money Policy version, and this
        page cannot see either — open the Governance owner below to read the published
        ladder, its version and whether any rule leaves Rest of world or Unknown
        undecided. Rules are never authored from this card.
      </>
    : isCountry
      ? <>
          Every applicable compatible Datastream is compiled automatically. Country hierarchy
          editing stays with its exact Governance owner.
        </>
      : null;
  return (
    // A column, not a stack of blocks: the owner links are a FOOTER and are
    // pushed to the bottom edge, so two cards side by side end on the same line
    // however much prose sits above them.
    <Panel data-testid={`capability-${capability.key}`} className="flex h-full flex-col gap-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="m-0 text-h3 font-h3 text-text">{label}</h3>
            <Badge tone={capability.availability === "always_present" ? "info" : "neutral"}>
              {capability.availability === "always_present" ? "Always present" : "Optional"}
            </Badge>
          </div>
          {/* Two versions exist and they are not the same object. What this
              envelope carries is the PROJECT CONFIGURATION VERSION
              (`project_settings.py:376-386`, `app.project_capabilities`) — not
              the version of the rule set that decides the capability's content.
              Labelling it "Version" let a reader conclude they were looking at
              the live rule version, which the card has never known (41.7, AC8). */}
          <p className="mt-1 mb-0 text-ui text-text-secondary">
            {enabled ? (
              <>
                Active: {capability.active.state} · Project configuration version:{" "}
                {capability.active.version_id ?? "Unversioned"}
              </>
            ) : "Not active"}
          </p>
          {capability.pending ? (
            <p className="mt-1 mb-0 font-mono text-caption text-text-secondary">
              Pending: {capability.pending.state ?? "change"} · {capability.pending.prepared_payload_hash ?? capability.pending.change_set_id}
            </p>
          ) : null}
        </div>
        {capability.availability === "optional" ? (
          <div className="shrink-0">
            <Button
              type="button"
              variant={nextEnabled ? "default" : "secondary"}
              disabled={!canEdit}
              onClick={() => void onPrepare(nextEnabled)}
            >
              {action} {label}
            </Button>
          </div>
        ) : null}
      </div>
      {note ? <p className="m-0 max-w-[64ch] text-ui text-text-secondary">{note}</p> : null}
      <CapabilityCoverage coverage={capability.coverage} />
      <CapabilityDatastreams projectId={projectId} capabilityKey={capability.key} />
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <p className="m-0 text-caption text-text-secondary">Exceptions</p>
          {/* `text-lg` is Tailwind's own 18px — the only step on this card that
              did not come from the token scale, and it landed between h3 and
              metric so the two counts read at a size nothing else in the console
              uses. A count IS a metric. */}
          <p className="mt-1 mb-0 font-numeric text-metric font-metric text-text">
            {capability.exceptions.length}
          </p>
        </div>
        <div>
          <p className="m-0 text-caption text-text-secondary">Blockers</p>
          <p className="mt-1 mb-0 font-numeric text-metric font-metric text-text">
            {capability.blockers.length}
          </p>
        </div>
      </div>
      {/* The footer. `mt-auto` is what keeps the owner links on the bottom edge
          of every card in the row instead of wherever the prose above happened
          to stop — Country's sat 100px below Currency & FX's for no reason a
          reader could name. The dependency line belongs with them: it names the
          other capability, they open it. */}
      <div className="mt-auto flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-divider-base pt-4">
        {capability.dependencies.length ? (
          <p className="m-0 text-caption text-text-secondary">
            Depends on {capability.dependencies.map((key) => capabilityLabel(key)).join(", ")}
          </p>
        ) : null}
        {capability.owner_links.map((link) => (
          <button
            key={`${link.owner}:${link.owner_reference.workspace ?? link.owner_reference.global_surface}:${link.owner_reference.section ?? ""}`}
            type="button"
            className="text-ui font-semibold text-primary hover:underline"
            onClick={() => onOpenOwner?.(link.owner_reference)}
          >
            Open {link.owner}
          </button>
        ))}
      </div>
    </Panel>
  );
}

export default function ProjectSettings({
  projectId,
  section = "general",
  onSectionChange,
  onOpenOwner,
}: {
  projectId: string;
  section?: SettingsSection;
  onSectionChange?: (section: SettingsSection) => void;
  /** Resolves a semantic owner reference into a canonical route. */
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  const [model, setModel] = useState<SettingsEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedChangeId, setSelectedChangeId] = useState<string | null>(null);

  const load = async () => {
    try {
      setError(null);
      setModel(await request<SettingsEnvelope>(`/api/projects/${projectId}/settings`));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load project settings.");
    }
  };

  useEffect(() => {
    void load();
  }, [projectId]);

  const selectedChange = useMemo(
    () => model?.changes.find((change) => change.id === selectedChangeId) ?? null,
    [model, selectedChangeId],
  );

  const prepare = async (intent: object) => {
    try {
      setError(null);
      const created = await request<ChangeSet>(`/api/projects/${projectId}/settings/change-sets`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ intent }),
      });
      await request(`/api/projects/${projectId}/settings/change-sets/${created.id}/prepare`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      setSelectedChangeId(created.id);
      setNotice("Change prepared for review.");
      await load();
      onSectionChange?.("changes");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to prepare the change.");
    }
  };

  const saveProfile = async (name: string, description: string) => {
    try {
      setError(null);
      await request(`/api/projects/${projectId}/settings/profile`, {
        method: "PATCH",
        body: JSON.stringify({ name, description }),
      });
      setNotice("Project profile updated.");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to update the profile.");
    }
  };

  if (!model && !error) {
    return <div className="p-8 text-sm text-text-secondary" role="status">Loading project settings…</div>;
  }
  if (!model) {
    return (
      <div className="p-8">
        <Status as="block" tone="error" title="Project settings are unavailable"
          action={<Retry onClick={() => void load()} />}
        >
          {error}
        </Status>
      </div>
    );
  }

  const tabs: Array<[SettingsSection, string]> = [
    ["general", "General"],
    ["capabilities", "Capabilities"],
    ["changes", "Changes"],
    ["ai", "AI"],
  ];

  return (
    // `PageFrame`, not a second `main`: `ApplicationShell` already opens the one
    // this page renders inside, and two of them make the landmark ambiguous.
    <PageFrame>
      <PageHeader
        eyebrow={
          // Same contract as the Business Domains below: the association carries
          // a link to its owner. `README.md:94` puts the Organization under
          // Organization Settings, so that is where the name opens \u2014 and the
          // name stays plain text when this surface is mounted without an
          // owner-opener rather than rendering a control that does nothing.
          <>
            {onOpenOwner ? (
              <button
                type="button"
                className="cursor-pointer border-0 bg-transparent p-0 text-caption text-primary underline"
                onClick={() => onOpenOwner({
                  surface: "global",
                  workspace: null,
                  section: null,
                  global_surface: "organization-settings",
                  global_section: "general",
                  object_type: null,
                  object_id: null,
                  tab: null,
                  action: null,
                  version_id: null,
                  evidence_id: null,
                })}
              >
                {model.project.organization.name}
              </button>
            ) : (
              model.project.organization.name
            )}
            {` \u00B7 ${model.project.name}`}
          </>
        }
        title="Project settings"
        description="Governed defaults and capability posture for this project."
      />
      <Tabs
        value={section}
        onValueChange={(value) => onSectionChange?.(value as SettingsSection)}
      >
        <TabsList className="mb-6" aria-label="Project settings sections">
          {tabs.map(([key, label]) => (
            <TabsTrigger key={key} value={key}>
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
        {notice ? <Status as="block" tone="success" className="mb-6">{notice}</Status> : null}
        {error ? <Status as="block" tone="error" className="mb-6">{error}</Status> : null}
        {!model.project.can_edit ? (
          <Status as="block" tone="info" title="View-only access" className="mb-6">
            You can inspect confirmed and pending settings but cannot prepare or confirm changes.
          </Status>
        ) : null}

        <TabsContent value="general">
          <GeneralSection
            model={model}
            onSave={saveProfile}
            onPrepare={prepare}
            onOpenOwner={onOpenOwner}
            onReload={() => void load()}
          />
        </TabsContent>
        <TabsContent value="capabilities">
          <Stack>
            {/* `SectionHeader`, not `PanelHeader`. `PanelHeader` is the BAND at
                the top of a `Panel` and brings that panel's `px-5` and its
                bottom rule with it; used bare on the page it indented "Project
                capabilities" 20px past the cards under it and past the `h1`
                above it, and drew a rule across a section that has no panel. */}
            <SectionHeader
              level={2}
              title="Project capabilities"
              // NO COUNT IN THIS SENTENCE. It used to spell the number of
              // capabilities out, and kept spelling the old one above SIX cards
              // once story 61.5 landed — the count is the server's, it arrives in
              // the envelope, and a screen that writes it down contradicts the
              // rows it draws the day the count moves.
              description="The governed capabilities of this Project, with active and pending posture kept distinct."
            />
            <div className="grid gap-4 xl:grid-cols-2">
              {model.capabilities.map((capability) => (
                <CapabilityCard
                  key={capability.key}
                  capability={capability}
                  projectId={model.project.id}
                  canEdit={model.project.can_edit}
                  onOpenOwner={onOpenOwner}
                  onPrepare={(enabled) =>
                    prepare({ capabilities: { [capability.key]: enabled ? "enabled" : "disabled" } })
                  }
                />
              ))}
            </div>
          </Stack>
        </TabsContent>
        <TabsContent value="changes">
          <ChangesSection
            changes={model.changes}
            selected={selectedChange}
            canEdit={model.project.can_edit}
            projectId={projectId}
            onOpenOwner={onOpenOwner}
            onSelect={setSelectedChangeId}
            onNotice={setNotice}
            onError={setError}
            onReload={load}
          />
        </TabsContent>
        <TabsContent value="ai">
          <AiSettingsPanel projectId={projectId} canEdit={model.project.can_edit} />
        </TabsContent>
      </Tabs>
    </PageFrame>
  );
}

/**
 * The scheduled-task recipe, where the person schedules the task (AI-294).
 *
 * WHY IT LIVES HERE AND NOWHERE NEW. Jean's arbitration of 2026-08-16 was
 * explicit: attach to the existing, no seventh place. The daily insight runs
 * from the operator's OWN LLM host, on a schedule toorow neither sets nor sees --
 * so what toorow owes is not a scheduler, it is the exact text to paste into one.
 *
 * DERIVED, NEVER STORED. `GET /api/daily-insights/recipe` builds it as a pure
 * function of the project, its timezone and the hour. A stored copy would be a
 * second answer to "what should the task say", and it would go stale the day the
 * contract version moves -- which the recipe carries precisely so a reader can
 * tell.
 *
 * It carries no model secret and schedules nothing server-side. Saying so on the
 * screen is not decoration: a person about to paste a prompt into their own host
 * is entitled to know what leaves this product.
 */
/** WHICH Datastreams a capability covers, not how many (AI screens, 2026-08-17).
 *
 *  The card showed `coverage.applicable` — a NUMBER — and the server has served
 *  the LIST at `/capabilities/{key}/datastreams` all along. A count tells a
 *  person that something is uncovered; only the list tells them WHICH, and a
 *  person cannot act on a number.
 *
 *  ON DEMAND, and that is deliberate: five capabilities on one screen would mean
 *  five reads on mount for a list most visits never open. The button says what
 *  it will do, and the panel says what came back.
 *
 *  A LIST THAT FAILED TO LOAD IS NOT AN EMPTY ONE. Rendering nothing on an error
 *  would say "this capability covers no Datastream" — a claim about the Project
 *  that a failed read does not support.
 */
function CapabilityDatastreams({
  projectId,
  capabilityKey,
}: {
  projectId: string;
  capabilityKey: string;
}) {
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "loading" }
    | { status: "ready"; rows: Array<Record<string, unknown>> }
    | { status: "failed"; message: string }
  >({ status: "idle" });

  if (state.status === "idle") {
    return (
      <Button
        type="button"
        variant="ghost"
        onClick={() => {
          setState({ status: "loading" });
          request<{ datastreams: Array<Record<string, unknown>> }>(
            `/api/projects/${encodeURIComponent(projectId)}/capabilities/${encodeURIComponent(capabilityKey)}/datastreams`,
          )
            .then((body) => setState({ status: "ready", rows: body.datastreams ?? [] }))
            .catch((reason) =>
              setState({
                status: "failed",
                message:
                  reason instanceof Error ? reason.message : "The Datastream list could not be read",
              }),
            );
        }}
      >
        Show which Datastreams
      </Button>
    );
  }
  if (state.status === "loading") {
    return <p className="m-0 text-caption text-text-secondary">Reading the Datastreams…</p>;
  }
  if (state.status === "failed") {
    return (
      <Status tone="warning" data-testid={`capability-datastreams-error-${capabilityKey}`}>
        {state.message}
      </Status>
    );
  }
  if (state.rows.length === 0) {
    return (
      <EmptyState
        title="No Datastream is compatible with this capability yet"
        description="A Datastream appears here once it carries the fields this capability reads. One is added in Data."
      />
    );
  }
  return (
    <ul
      className="m-0 grid list-none gap-1 p-0"
      data-testid={`capability-datastreams-${capabilityKey}`}
    >
      {state.rows.map((row, index) => (
        <li key={String(row.datastream_id ?? index)} className="text-caption text-text-secondary">
          {/* `read_project_capability` joins `app.datastreams` and orders on
              `d.name`, which is `NOT NULL` (migration 023), so the word is
              always served here and `?? row.datastream_id` could only ever
              print a `ds_<ULID>` where the name already stood. */}
          <span className="text-text">{String(row.datastream_name ?? "Unnamed")}</span>
          {" — "}
          {String(row.coverage_state ?? "unknown")}
          {row.applicability ? ` · ${String(row.applicability)}` : ""}
        </li>
      ))}
    </ul>
  );
}


function TaskRecipePanel({ projectId }: { projectId: string }) {
  const [state, setState] = useState<
    | { status: "idle" | "loading" }
    | { status: "ready"; text: string; version: string | null }
    | { status: "failed"; message: string }
  >({ status: "idle" });

  useEffect(() => {
    let disposed = false;
    setState({ status: "loading" });
    request<{ recipe: Record<string, unknown>; text: string }>(
      `/api/daily-insights/recipe?project_id=${encodeURIComponent(projectId)}`,
    )
      .then((body) => {
        if (disposed) return;
        setState({
          status: "ready",
          text: body.text,
          version: (body.recipe?.recipeVersion as string) ?? null,
        });
      })
      .catch((reason) => {
        if (disposed) return;
        setState({
          status: "failed",
          message: reason instanceof Error ? reason.message : "The recipe could not be built",
        });
      });
    return () => {
      disposed = true;
    };
  }, [projectId]);

  return (
    <Panel>
      <div>
        <h2 className="m-0 text-h2 font-h2 text-text">Daily insight — scheduled task</h2>
        <p className="mt-1 mb-0 text-ui text-text-secondary">
          Paste this into your own LLM host&apos;s scheduled task. toorow does not run it: the
          hour, the tokens and the cost stay with you, and this text carries no secret.
        </p>
      </div>
      {state.status === "loading" ? (
        <p className="mt-4 mb-0 text-ui text-text-secondary">Building the recipe…</p>
      ) : null}
      {state.status === "failed" ? (
        /* Named, not swallowed: a recipe that cannot be built is not an empty
           recipe, and pasting nothing into a host schedules nothing. */
        <Status tone="warning" className="mt-4">
          {state.message}
        </Status>
      ) : null}
      {state.status === "ready" ? (
        <>
          <pre className="mt-4 max-h-96 overflow-auto whitespace-pre-wrap rounded border border-border bg-surface-2 p-3 text-technical">
            {state.text}
          </pre>
          {state.version ? (
            <p className="mt-2 mb-0 text-ui text-text-secondary">
              Recipe version {state.version}. Re-copy it after a contract change — an older
              paste keeps running against the older contract.
            </p>
          ) : null}
        </>
      ) : null}
    </Panel>
  );
}


/** The five things a scheduled run can be, and none of them collapses (AI-294).
 *
 *  `execution-substrate.md` fixes the vocabulary and says why it is five and not
 *  two: `published`, `no_insight` (nothing was worth saying), `blocked` (the data
 *  was not ready), `failed`, and an ABSENT row -- the task did not run at all.
 *  Collapsing any pair of those IS the defect.
 *
 *  The tones follow the meaning, not the mood. `no_insight` is INFO: a day with
 *  nothing worth saying is a healthy day, and colouring it as a warning would
 *  teach a reader to ignore the colour. `absent` is a warning because toorow was
 *  told nothing -- and that is the one state it must never present as health.
 */
/*
 * THE PRIVATE RUN-STATE MAP IS GONE (76-2), and this one AGREED with the union
 * on every word -- which is the reason it had to go rather than a reason to keep
 * it. A second definition that agrees today is the one that drifts tomorrow. Its
 * two original words are declared instead: `no_insight` is `info` and
 * `absent` is a warning, with the sentences this file had already written.
 */

const RUN_STATE_SENTENCE: Record<string, string> = {
  published: "The task ran and published its insights.",
  no_insight: "The task ran and found nothing worth saying. That is a healthy day.",
  blocked: "The task ran and stopped: the data it needed was not ready.",
  failed: "The task ran and failed.",
  absent: "No run was recorded for this day. toorow was told nothing — this is NOT a day without insights.",
};

/**
 * The run history of work toorow does not schedule.
 *
 * WHY THE ABSENT STATE IS THE POINT. toorow neither sets nor sees the operator's
 * schedule, so a missing row means "the task did not run" -- an ABSENCE to
 * display, never an inference to make. The document is explicit: no back-filled
 * "presumed ran", and no health derived from silence.
 *
 * So this panel never fills a gap and never counts a missing day as anything. It
 * shows the rows the server returned, each under its own state.
 *
 * AND IT IS WHERE AN INSIGHT BECOMES SHAREABLE (AI-294, last remnant). A day the
 * server says carries insights can be opened, and each insight then says one of
 * three things: it names a Result and the door to the one Share mechanism opens
 * at that Result; it names the link that was missing, with the gesture; or it
 * predates migration 281 and says so rather than inventing a verdict. The three
 * states and the sentences live in `daily-insights/InsightShare.tsx`.
 */
function RunHistoryPanel({
  projectId,
  onOpenOwner,
}: {
  projectId: string;
  onOpenOwner?: (owner: OwnerReference) => void;
}) {
  const [state, setState] = useState<
    | { status: "idle" | "loading" }
    | { status: "ready"; runs: Array<Record<string, unknown>> }
    | { status: "failed"; message: string }
  >({ status: "idle" });

  useEffect(() => {
    let disposed = false;
    setState({ status: "loading" });
    request<{ runs: Array<Record<string, unknown>> }>(
      `/api/daily-insights/runs?project_id=${encodeURIComponent(projectId)}&limit=14`,
    )
      .then((body) => {
        if (!disposed) setState({ status: "ready", runs: body.runs ?? [] });
      })
      .catch((reason) => {
        if (disposed) return;
        setState({
          status: "failed",
          message: reason instanceof Error ? reason.message : "The run history could not be read",
        });
      });
    return () => {
      disposed = true;
    };
  }, [projectId]);

  return (
    <Panel data-testid="daily-insight-runs">
      <div>
        <h2 className="m-0 text-h2 font-h2 text-text">Daily insight — run history</h2>
        <p className="mt-1 mb-0 text-ui text-text-secondary">
          What each day&apos;s scheduled task actually did. toorow records what it was told; a
          day it was told nothing about is shown as such, never as a day without insights.
        </p>
      </div>
      {state.status === "loading" ? (
        <p className="mt-4 mb-0 text-ui text-text-secondary">Reading the journal…</p>
      ) : null}
      {state.status === "failed" ? (
        /* A journal that cannot be READ is not a journal that is EMPTY. Showing
           an empty list here would say "no task ever ran", which is a claim
           about the operator's host that this failure does not support. */
        <Status tone="warning" className="mt-4">
          {state.message}
        </Status>
      ) : null}
      {state.status === "ready" && state.runs.length === 0 ? (
        <EmptyState
          title="No run has been recorded for this Project yet"
          description="The first run appears here once the recipe above is running in your LLM host's scheduled task."
        />
      ) : null}
      {state.status === "ready" && state.runs.length > 0 ? (
        <ul className="mt-4 grid gap-2">
          {state.runs.map((run, index) => {
            const runState = String(run.state ?? "absent");
            const date = String(run.insightDate ?? "");
            const itemCount = Number(run.itemCount ?? 0);
            const retractedCount = Number(run.retractedCount ?? 0);
            return (
              <li key={`${date}-${index}`} className="text-body">
                <Status tone={stateTone(runState)}>
                  {date || "Unknown date"} — {stateLabel(runState)}
                </Status>
                <span className="ml-2 text-text-secondary">
                  {RUN_STATE_SENTENCE[runState] ?? "This state is not one the contract declares."}
                </span>
                {/* A day whose single claim was withdrawn must not read exactly
                    like a day whose claim still stands (review of ae60c22a, R2):
                    the server counts retractions for precisely this row, and a
                    journal that dropped the count would erase a withdrawal from
                    the one surface that reports days. */}
                {retractedCount > 0 ? (
                  <span
                    className="ml-2 text-text-secondary"
                    data-testid={`daily-insight-retracted-count-${date}`}
                  >
                    {retractedCount === 1
                      ? "1 claim was withdrawn."
                      : `${retractedCount} claims were withdrawn.`}
                  </span>
                ) : null}
                {/* AI-294, last remnant: from an insight to a share. The day is
                    opened only when the SERVER said it carries insights — a
                    disclosure over an empty day would promise a list nobody
                    published. `openResult` builds a SEMANTIC reference and hands
                    it to the shell, exactly as the Organization link above does;
                    no screen here builds a URL, and no screen here mints a
                    share. */}
                {date && itemCount > 0 ? (
                  <RunInsights
                    projectId={projectId}
                    insightDate={date}
                    onOpenResult={
                      onOpenOwner
                        ? (resultId) =>
                            onOpenOwner({
                              surface: "project",
                              workspace: "analyze",
                              section: "explore",
                              global_surface: null,
                              global_section: null,
                              object_type: "result",
                              object_id: resultId,
                              tab: "view",
                              action: null,
                              version_id: null,
                              evidence_id: null,
                            })
                        : undefined
                    }
                  />
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </Panel>
  );
}


/**
 * External sharing — the project-scoped capability of
 * `docs/product-architecture/proactive-assertions.md` decision 2.
 *
 * WHY IT IS HERE AND NOT IN **Capabilities**. That tab holds the six capabilities
 * that COMPILE — each one has a per-Datastream coverage projection, a dependency
 * graph and an impact review, because each changes what a Datastream produces.
 * This one changes no figure and compiles into nothing; its card would carry six
 * empty counts forever. It belongs with the Project's explicit defaults, which
 * is **General**, and that is also the store it lives in.
 *
 * IT SAYS WHO DECIDED, OR THAT NOBODY DID. A project that forbids sharing
 * because nobody has ever allowed it is not the same fact as one somebody turned
 * off, and a screen that showed them identically would have the reader hunting
 * for a decision that was never taken.
 */
function ExternalSharingPanel({
  projectId,
  posture,
  onChanged,
}: {
  projectId: string;
  posture: SettingsEnvelope["project"]["external_sharing"];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allowed = posture.state === "allowed";

  const move = async (next: "allowed" | "forbidden") => {
    setBusy(true);
    setError(null);
    try {
      await request(`/api/projects/${encodeURIComponent(projectId)}/settings/external-sharing`, {
        method: "PUT",
        body: JSON.stringify({ external_sharing: next }),
      });
      onChanged();
    } catch (reason: unknown) {
      setError((reason as Error).message || "The change was refused.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel data-testid="external-sharing">
      <PanelHeader
        title="External sharing"
        description="Whether anything in this project may be published outside the platform through a share link. Inside the platform, publication is unaffected."
      />
      <Stack>
        <p className="m-0 text-ui text-text-secondary">
          {allowed
            ? "A share link can be requested here, and it only becomes a link once a second person with the Edit role confirms it."
            : "No share link can be requested. Nothing in this project can be opened by someone without an account."}
        </p>
        <p className="m-0 text-caption text-text-secondary">
          {posture.is_platform_default
            ? "Nobody has decided this yet: a new project forbids external sharing until someone allows it."
            : `Set to ${allowed ? "allowed" : "forbidden"} by ${posture.decided_by}.`}
        </p>
        {posture.can_change ? (
          <Button
            disabled={busy}
            variant={allowed ? "ghost" : "default"}
            onClick={() => void move(allowed ? "forbidden" : "allowed")}
          >
            {allowed ? "Forbid external sharing" : "Allow external sharing"}
          </Button>
        ) : (
          <p className="m-0 text-caption text-text-secondary">
            A person holding the Manage role on this project can change this.
          </p>
        )}
        {error ? (
          <Status as="block" tone="error" title="The change was not applied">
            {error}
          </Status>
        ) : null}
      </Stack>
    </Panel>
  );
}

function GeneralSection({
  model,
  onSave,
  onPrepare,
  onOpenOwner,
  onReload,
}: {
  model: SettingsEnvelope;
  onSave: (name: string, description: string) => Promise<void>;
  onPrepare: (intent: object) => Promise<void>;
  onOpenOwner?: (owner: OwnerReference) => void;
  onReload: () => void;
}) {
  const [name, setName] = useState(model.project.name);
  const [description, setDescription] = useState(model.project.description ?? "");
  return (
    <Stack>
      <TaskRecipePanel projectId={model.project.id} />
      <RunHistoryPanel projectId={model.project.id} onOpenOwner={onOpenOwner} />
      <Panel>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void onSave(name.trim(), description.trim());
          }}
        >
          <div>
            <h2 className="m-0 text-h2 font-h2 text-text">Project profile</h2>
            <p className="mt-1 mb-0 text-ui text-text-secondary">Identity can be edited directly; governed defaults cannot.</p>
          </div>
          <Field label="Project name">
            {(props) => <Input {...props} value={name} disabled={!model.project.can_edit} onChange={(event) => setName(event.target.value)} />}
          </Field>
          <Field label="Description">
            {(props) => <Textarea {...props} value={description} disabled={!model.project.can_edit} onChange={(event) => setDescription(event.target.value)} />}
          </Field>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="m-0 text-caption text-text-secondary">
              {/* `project-settings.md:43` contracts these as "read-only
                  organization and Business Domain associations WITH LINKS to
                  their owners". They were a `.join(", ")` — the name of the
                  owner without the way to it, which is the one thing the line
                  asks for. Governance > Master Data owns the Business Domain
                  (`README.md:97`), so that is where each one opens. */}
              Business domains:{" "}
              {model.project.business_domains.length === 0
                ? "None"
                : model.project.business_domains.map((domain, index) => (
                    <span key={domain.id}>
                      {index > 0 ? ", " : ""}
                      {onOpenOwner ? (
                        <button
                          type="button"
                          className="cursor-pointer border-0 bg-transparent p-0 text-caption text-primary underline"
                          onClick={() => onOpenOwner({
                            surface: "project",
                            workspace: "governance",
                            section: "master-data",
                            global_surface: null,
                            global_section: null,
                            object_type: "business-domain",
                            object_id: domain.id,
                            tab: "overview",
                            action: null,
                            version_id: null,
                            evidence_id: null,
                          })}
                        >
                          {domain.name}
                        </button>
                      ) : (
                        domain.name
                      )}
                    </span>
                  ))}
            </p>
            <Button type="submit" disabled={!model.project.can_edit || !name.trim()}>Save profile</Button>
          </div>
        </form>
      </Panel>
      <ExternalSharingPanel
        projectId={model.project.id}
        posture={model.project.external_sharing}
        onChanged={onReload}
      />
      <div className="grid gap-4 xl:grid-cols-3">
        {DEFAULT_LABELS.map(([key, label]) => (
          <DefaultCard
            key={key}
            label={label}
            value={model.project.defaults[key]}
            canEdit={model.project.can_edit}
            vocabulary={DEFAULT_VOCABULARY[key]}
            onOpenOwner={onOpenOwner}
            onPrepare={(value) => onPrepare({ defaults: { [key]: value } })}
          />
        ))}
      </div>
    </Stack>
  );
}

function ChangesSection({
  changes,
  selected,
  canEdit,
  projectId,
  onOpenOwner,
  onSelect,
  onNotice,
  onError,
  onReload,
}: {
  changes: ChangeSet[];
  selected: ChangeSet | null;
  canEdit: boolean;
  projectId: string;
  onOpenOwner?: (owner: OwnerReference) => void;
  onSelect: (id: string) => void;
  onNotice: (message: string) => void;
  onError: (message: string | null) => void;
  onReload: () => Promise<void>;
}) {
  const confirm = async () => {
    if (!selected) return;
    try {
      onError(null);
      const confirmationKey = crypto.randomUUID();
      const confirmation = await request<{
        confirmation_id: string;
        confirmation_secret: string;
      }>(
        `/api/projects/${projectId}/settings/change-sets/${selected.id}/confirmations`,
        {
          method: "POST",
          headers: { "Idempotency-Key": confirmationKey },
          body: JSON.stringify({}),
        },
      );
      await request(`/api/projects/${projectId}/settings/change-sets/${selected.id}/confirm`, {
        method: "POST",
        headers: { "Idempotency-Key": confirmationKey },
        body: JSON.stringify({
          confirmation_id: confirmation.confirmation_id,
          confirmation_secret: confirmation.confirmation_secret,
          prepared_payload_hash: selected.prepared_payload_hash,
        }),
      });
      onNotice("Change activated.");
      await onReload();
    } catch (cause) {
      onError(cause instanceof Error ? cause.message : "Unable to confirm the change.");
    }
  };

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(18rem,0.8fr)_minmax(24rem,1.2fr)]">
      <Panel flush>
        <PanelHeader title="Change Sets" description="Prepared, blocked, active and historical project changes." />
        {changes.length ? (
          <div className="divide-y divide-divider-base">
            {changes.map((change) => (
              <button
                key={change.id}
                type="button"
                className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left hover:bg-surface-muted"
                onClick={() => onSelect(change.id)}
              >
                <span>
                  <strong className="block text-ui text-text">{change.summary ?? change.id}</strong>
                  <span className="text-caption text-text-secondary"><ObjectId value={change.id} title="Change set" /></span>
                </span>
                <Badge tone={change.state === "activated" ? "success" : change.state === "blocked" ? "error" : "neutral"}>
                  {stateLabel(change.state)}
                </Badge>
              </button>
            ))}
          </div>
        ) : (
          <EmptyState title="No Change Sets" description="Prepare a default or capability change to start a governed review." />
        )}
      </Panel>
      <Panel>
        {selected ? (
          <div className="space-y-4">
            <div>
              <h2 className="m-0 text-h2 font-h2 text-text">Change details</h2>
              <p className="mt-1 mb-0 font-mono text-caption text-text-secondary"><ObjectId value={selected.id} title="Change set" /></p>
            </div>
            <Status tone={selected.state === "blocked" ? "error" : "info"}>State: {selected.state}</Status>
            {selected.prepared_payload_hash ? (
              <p className="break-all font-mono text-caption text-text-secondary">
                Prepared payload: {selected.prepared_payload_hash}
              </p>
            ) : null}
            {selected.impact_summary ? (
              <div className="space-y-3">
                <h3 className="m-0 text-h3 font-h3 text-text">Frozen impact</h3>
                {selected.impact_summary.coverage ? (
                  <div className="grid gap-4 xl:grid-cols-2">
                    {Object.entries(selected.impact_summary.coverage)
                      // Only the capabilities this change actually touches: a
                      // wall of "Not applicable" cards — one per capability the
                      // change never named — would bury the one that moved.
                      .filter(([, coverage]) => coverage.applicable > 0)
                      .map(([capabilityKey, coverage]) => (
                        <div key={capabilityKey} className="space-y-1">
                          {/* The capability names its coverage, so it is the
                              heading — the same `text-h3` a capability card
                              uses, not a step under the value it introduces. */}
                          <p className="m-0 text-h3 font-h3 text-text">
                            {capabilityLabel(capabilityKey)}
                          </p>
                          <CapabilityCoverage coverage={coverage} />
                        </div>
                      ))}
                  </div>
                ) : null}
                <CapabilityImpactMatrix rows={selected.impact_summary.matrix ?? []} />
              </div>
            ) : null}
            {selected.blockers?.map((blocker, index) => (
              <Status
                key={`${blocker.code ?? "blocker"}:${index}`}
                as="block"
                tone="error"
                action={
                  blocker.owner_reference ? (
                    <button type="button" onClick={() => onOpenOwner?.(blocker.owner_reference!)}>
                      Open owner evidence
                    </button>
                  ) : undefined
                }
              >
                {blocker.message ?? blocker.code ?? "Blocked"}
                {blocker.datastream_id ? ` (${blocker.datastream_id})` : ""}
              </Status>
            ))}
            <Button
              type="button"
              disabled={!canEdit || selected.state !== "prepared" || !selected.prepared_payload_hash}
              onClick={() => void confirm()}
            >
              Confirm exact prepared change
            </Button>
          </div>
        ) : (
          <EmptyState title="Select a Change Set" description="Its frozen payload, blockers and confirmation action will appear here." />
        )}
      </Panel>
    </div>
  );
}
