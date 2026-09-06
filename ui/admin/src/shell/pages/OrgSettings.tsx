/**
 * Organization Settings — the Organization-scoped global surface (Story 46.4).
 *
 * Eight URL-backed sections: General, Members, Credentials, Account exposure,
 * Data access, MCP hosts, Plan and Actions. Project grants are deliberately
 * absent: they belong to Project Access.
 *
 * MCP hosts (67-16) sits here rather than on a surface of its own because a
 * capability context is org-scoped: `app.mcp_capability_contexts.org_id` is what
 * it is bound to, and `manage` on the organization is what its API asks for.
 *
 * `OrgDangerZone` (exported, and mounted by the `actions` section) is the
 * irreversible one. Its contract is not cosmetic and is pinned by
 * `__tests__/DangerZone.test.tsx`:
 *   - nothing is fetched until the zone is OPENED — reading what would be
 *     destroyed is itself the first step of the destructive flow;
 *   - the manifest names the projects, the record counts and the warehouse
 *     datasets that get dropped, before any confirmation is possible;
 *   - the delete button stays disabled until the organization name is typed
 *     EXACTLY;
 *   - a failed preview is reported as a failure and offers no deletion — an
 *     empty list would read as "there is nothing to destroy" (finding F-010);
 *   - a blocked preview offers no destructive control at all.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, apiGet, apiJson } from "../../lib/apiFetch";
import { wireWord,
  ObjectId,
  Badge, Button, Collapsible, CollapsibleContent, CollapsibleTrigger, ConfirmDialog,
  EmptyState, Input, Label, Metric, NativeSelect, Panel, PanelHeader, Progress, Status,
  Retry,
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Textarea,
  formatTimestamp,
  stateLabel,
  stateTone,
} from "../../ui";
import AuthorizationsPanel from "../../authorizations/AuthorizationsPanel";
import CredentialGrantsPanel from "../../orgs/CredentialGrantsPanel";
import DataAccessGrantsPanel from "../../orgs/DataAccessGrantsPanel";
import McpHostsPanel from "../../orgs/McpHostsPanel";
import AiSettingsPanel from "../../settings/AiSettingsPanel";
import GlobalScopeLayout from "../GlobalScopeLayout";
import type { OrganizationSettingsSection } from "../router";

interface OrgSettingsRecord {
  id: string;
  name: string;
  slug: string;
  status?: string;
  billing_ref?: string | null;
  brand_primary?: string | null;
  brand_secondary?: string | null;
  brand_accent?: string | null;
  logo_url?: string | null;
}
interface OrgMember {
  id?: string;
  identity: string;
  role: string;
  status: string;
  joined_at?: string | null;
  /** L'e-mail vérifié résolu par le read-model (`admin_api.py`, GET .../members).
   *  Nul pour une identité héritée d'avant la migration 111, qui EST déjà un
   *  e-mail : `memberLabel` retombe alors sur `identity`. */
  verified_email?: string | null;
}

/** Le nom qu'un humain reconnaît. `identity` reste l'identifiant que l'API mute ;
 *  il ne disparaît pas de l'écran, il cesse d'en être le seul contenu. */
const memberLabel = (member: OrgMember) => member.verified_email || member.identity;
/** `GET /api/organizations/{id}/plan` (AI-176). A limit of `null` is "no cap"
 *  (full/internal) and MUST NOT be rendered as a number — see PlanSection. */
interface OrgPlanRecord {
  org_id: string;
  plan: string;
  limits: { max_datastreams: number | null; max_backfill_days: number | null };
  usage: { active_datastreams: number };
  granted_at?: string | null;
}
/**
 * One row of `GET /api/organizations/{org}/authorizations`, as the server
 * actually serializes it (`me_api.py#_serialize_authorization`).
 *
 * `provided` and `owner_label` used to be declared here and NEITHER is sent.
 * The direction is carried by `exposure`, whose three values that serializer
 * fixes; the owning organization is `owner_org_name`. Read against the wire on
 * 2026-08-17: with `provided` undefined on every row, `!item.provided` was true
 * everywhere, so the exposure chooser offered inbound connections this
 * organization cannot grant on, and the inbound panel was permanently empty.
 */
interface Authorization {
  id: string;
  provider?: string;
  /** `provided_by_org` = another organization exposes it TO us (inbound). */
  exposure?: "provided_by_org" | "shared_with_org" | "owned";
  owner_org_id?: string;
  owner_org_name?: string | null;
  account_label?: string | null;
}

/** The one place the inbound/outbound question is answered. */
function isInbound(item: Authorization): boolean {
  return item.exposure === "provided_by_org";
}

export interface DeletionBlocker { kind: string; detail: string }
export interface OrgDeletionPreview {
  org_id: string;
  org_name?: string;
  name?: string;
  slug?: string;
  counts?: Record<string, number>;
  warehouse_datasets?: string[];
  projects?: Array<{ id: string; name: string; status?: string; datastream_count?: number }>;
  members?: number | OrgMember[];
  active_datastreams?: number;
  warehouse_schemas?: string[];
  blockers?: DeletionBlocker[];
}
export interface OrgDangerZoneProps {
  orgId: string;
  /** What to do once the organization no longer exists. Default: leave it, so
   *  no scope keeps pointing at a deleted organization. */
  onDeleted?: () => void;
}
export interface OrgSettingsProps {
  orgId?: string;
  section?: OrganizationSettingsSection;
  onSectionChange?: (section: OrganizationSettingsSection) => void;
  initialOrg?: OrgSettingsRecord;
}

const SECTIONS = [
  { key: "general", label: "General", description: "Identity, defaults, billing and branding" },
  { key: "members", label: "Members", description: "Membership policy, roles and invitations" },
  { key: "credentials", label: "Authorizations", description: "Organization-visible grants to read a Source" },
  { key: "account-exposure", label: "Account exposure", description: "Provider accounts shared to beneficiary scopes" },
  { key: "data-access", label: "Data access", description: "External principals allowed to read this organization's warehouse" },
  { key: "mcp-hosts", label: "MCP hosts", description: "Agent hosts bound to this organization, and what they are allowed to do" },
  { key: "plan", label: "Plan", description: "The plan, the limits it enforces, and what is used against them" },
  { key: "ai", label: "AI settings", description: "What an agent obeys across this organization, and what a project may override" },
  { key: "actions", label: "Actions", description: "Deletion preview and guarded deletion" },
] as const;

export default function OrgSettings({
  orgId = "",
  section = "general",
  onSectionChange = () => undefined,
  initialOrg,
}: OrgSettingsProps) {
  const [org, setOrg] = useState<OrgSettingsRecord | null>(initialOrg ?? null);
  const [members, setMembers] = useState<OrgMember[] | null>(null);
  const [authorizations, setAuthorizations] = useState<Authorization[] | null>(null);
  const [plan, setPlan] = useState<OrgPlanRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a successful save so the record is RE-READ from its owner
  // rather than patched locally: the server is what decides what was stored.
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!orgId) return;
    let active = true;
    setError(null);
    const request =
      section === "general"
        ? apiGet<OrgSettingsRecord>(`/api/organizations/${encodeURIComponent(orgId)}`)
        : section === "members"
          ? apiGet<{ members: OrgMember[] }>(`/api/organizations/${encodeURIComponent(orgId)}/members`)
          : section === "credentials" || section === "account-exposure"
            ? apiGet<{ authorizations: Authorization[] }>(`/api/organizations/${encodeURIComponent(orgId)}/authorizations`)
            : section === "plan"
              ? apiGet<OrgPlanRecord>(`/api/organizations/${encodeURIComponent(orgId)}/plan`)
              : Promise.resolve(null);
    request
      .then((value) => {
        if (!active || value === null) return;
        if (section === "general") setOrg(value as OrgSettingsRecord);
        else if (section === "members") setMembers((value as { members: OrgMember[] }).members);
        else if (section === "plan") setPlan(value as OrgPlanRecord);
        else setAuthorizations((value as { authorizations: Authorization[] }).authorizations);
      })
      .catch(() => {
        if (active) setError("Organization Settings are unavailable. No empty state was inferred.");
      });
    return () => { active = false; };
  }, [reloadKey, orgId, section]);

  return (
    <GlobalScopeLayout
      eyebrow="Organization scope"
      title="Organization Settings"
      description="Organization identity, membership, Authorizations and provider-account exposure have one owner."
      scopeLabel={org?.name ?? "Organization"}
      sections={SECTIONS}
      activeSection={section}
      onSectionChange={onSectionChange}
    >
      {error ? (
        <Status
          as="block"
          tone="error"
          title="Organization Settings unavailable"
          action={<Retry onClick={() => setReloadKey((k) => k + 1)} />}
        >
          {error}
        </Status>
      ) : null}
      {section === "general" ? <GeneralSection org={org} orgId={orgId} onSaved={() => setReloadKey((k) => k + 1)} /> : null}
      {section === "members" ? (
        <MembersSection
          orgId={orgId}
          members={members}
          onMembersChanged={() => setReloadKey((k) => k + 1)}
        />
      ) : null}
      {section === "credentials" ? <AuthorizationsPanel scope={{ kind: "org", orgId }} /> : null}
      {section === "account-exposure" ? (
        <AccountExposureSection orgId={orgId} authorizations={authorizations} />
      ) : null}
      {section === "data-access" ? <DataAccessSection orgId={orgId} /> : null}
      {section === "mcp-hosts" ? <McpHostsPanel orgId={orgId} /> : null}
      {/* The ORG scope of the cascade the Project tab shows one level down.
          The SAME panel: one screen decides what "Set here" means, and the two
          tabs cannot drift into two vocabularies for one object. */}
      {section === "ai" ? <AiSettingsPanel orgId={orgId} /> : null}
      {section === "plan" ? <PlanSection plan={plan} failed={error !== null} /> : null}
      {section === "actions" ? <OrgDangerZone orgId={orgId} /> : null}
    </GlobalScopeLayout>
  );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-caption text-text-secondary">{label}</dt>
      <dd className={`mt-1 text-ui ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}

/**
 * Plan & Entitlements (AI-176) — the ceiling the org is already enforced against.
 *
 * Epic 34 shipped this as enforcement with no surface: `check_datastream_limit`
 * refuses the 4th datastream with a typed 409 `trial_datastream_limit`, and until
 * 2026-08-04 the console contained the string "entitlement" exactly zero times.
 * The person met the cap by hitting it.
 *
 * What this section deliberately does NOT render:
 *
 *  - **a trial countdown.** `max_backfill_days` is the depth of history an import
 *    may reach back to, NOT how long the organization lives. The QA roadmap
 *    (G5-T05) reads "30 jours et 3 datastreams", and epic-34 §7 leaves
 *    "30 days = backfill window OR org expiry?" explicitly undecided. Rendering a
 *    countdown would be inventing the arbitration, so the field is labelled for
 *    what the server actually holds.
 *  - **an upgrade control.** The plan moves through the super-admin POST only
 *    (`/api/admin/org-plan`); there is no self-serve billing surface anywhere, and
 *    a button that led nowhere would be worse than the sentence that says so.
 */
function PlanSection({ plan, failed }: { plan: OrgPlanRecord | null; failed: boolean }) {
  // The read fails CLOSED server-side (503). Rendering "0 of 3 used" over a dead
  // read is the counter lying at the exact moment it matters, so nothing numeric
  // is shown until a record arrives.
  if (failed) return null;
  if (!plan) {
    return (
      <Panel>
        <PanelHeader title="Plan" description="Reading the organization's plan and limits." />
        <EmptyState title="Loading the plan" description="No limit is displayed until the server has stated one." />
      </Panel>
    );
  }

  const capped = plan.limits.max_datastreams;
  const used = plan.usage.active_datastreams;
  const atLimit = capped !== null && used >= capped;
  const tone = plan.plan === "trial" ? (atLimit ? "error" : "warning") : "success";

  return (
    <Panel>
      <PanelHeader
        title="Plan"
        description="What this organization is entitled to, and what it has used against that entitlement."
      />
      <div className="p-5">
        <div className="flex items-center gap-3">
          <Badge tone={tone}>{plan.plan}</Badge>
          <span className="text-caption text-text-secondary">
            {plan.plan === "trial"
              ? "Trial limits are enforced whenever a datastream goes live, not merely displayed."
              : "No entitlement cap applies to this organization."}
          </span>
        </div>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <div>
            <Metric
              label="Active datastreams"
              value={capped === null ? String(used) : `${used} / ${capped}`}
              hint={
                capped === null
                  ? "No cap on this plan"
                  : "Datastreams that are on — exactly what the guard counts before letting another one go live"
              }
            />
            {capped !== null ? (
              <Progress
                value={Math.min(100, capped === 0 ? 100 : (used / capped) * 100)}
                tone={atLimit ? "error" : "accent"}
                aria-label="Active datastreams against the plan limit"
              />
            ) : null}
          </div>
          <Metric
            label="Backfill window"
            value={
              plan.limits.max_backfill_days === null
                ? "Unlimited"
                : `${plan.limits.max_backfill_days} days`
            }
            // Named for what it bounds. It is NOT a trial duration — see the
            // component header.
            hint="How far back an import may reach. Requests reaching further are raised to this floor, not refused."
          />
        </div>

        {atLimit ? (
          <Status as="block" tone="error" title="The datastream limit is reached">
            {/* THE REACH A PERSON READS IS THE REACH THAT IS ENFORCED
                (`organization-settings.md`, "the ceiling a person reads is not the
                ceiling that is enforced"). This said "the cap bounds creation
                only", and it was false in four ways: `check_datastream_limit` is
                called by FIVE writers of `enabled = TRUE` — create, publish,
                re-arm a schedule, roll a dataset back, and the maintenance
                backfill — not by the create path alone. A person reading "creation
                only" and then being refused when they turn a paused datastream
                back on has been told the wrong ceiling by the very screen that
                exists to state it. The five doors are proved on a real database by
                `server/tests/integration/test_datastream_cap_bounds_all_five_doors_pg.py`.

                FOUR ARE NAMED HERE AND THE FIFTH IS NOT, deliberately: the
                maintenance backfill is not a gesture anybody makes, and naming a
                door a person cannot walk through would be the vocabulary of the
                machine, not theirs. It is bounded all the same, and the test above
                is where that is said. */}
            Anything that would put another datastream on the air is refused with{" "}
            <code className="font-mono text-caption">trial_datastream_limit</code> — creating one,
            publishing one, turning a paused one back on, or restoring one from a rollback.
            Datastreams already on keep running, and turning one off frees an allowance
            immediately. Ask toorow to lift the plan; it takes effect on the very next call.
          </Status>
        ) : null}

        <p className="mt-5 text-caption text-text-secondary">
          The plan is changed by toorow, not from this screen: there is no self-serve billing
          surface, and this section would be lying if it offered one.
        </p>
      </div>
    </Panel>
  );
}

/** #RRGGBB, the same rule `_extract_brand_fields` enforces server-side
 *  (`organizations_api.py#_HEX_COLOUR_RE`). Checked here so the person is told
 *  at the field rather
 *  than by a 422 that names the column — never INSTEAD of the server. */
const HEX = /^#[0-9A-Fa-f]{6}$/;

/** The three brand colours and the logo, exactly the four the PATCH accepts. */
const BRAND_FIELDS = [
  { key: "brand_primary", label: "Brand primary", colour: true },
  { key: "brand_secondary", label: "Brand secondary", colour: true },
  { key: "brand_accent", label: "Brand accent", colour: true },
  { key: "logo_url", label: "Logo URL", colour: false },
] as const;

/** Organization identity and branding — READABLE and now WRITABLE.
 *
 *  This section rendered six `Fact` rows and nothing else. The write path has
 *  existed since Story 21.2: `PATCH /api/organizations/{org_id}`
 *  (`organizations_api.py#_patch_org`) takes `name`, `billing_ref` and the four branding
 *  fields, and validates each colour against `#RRGGBB`. No screen ever called
 *  it — served, and not wired.
 *
 *  Two of the three colours were not even DISPLAYED: `brand_secondary` and
 *  `brand_accent` are typed on the record and appeared nowhere, so the one place
 *  an organization is supposed to choose its colours showed a third of them.
 *
 *  The slug stays a `Fact` on purpose. The server refuses it with
 *  `slug_immutable` because it names the warehouse datasets; rendering an input
 *  that always fails would be worse than rendering none.
 */
function GeneralSection({ org, orgId, onSaved }: {
  org: OrgSettingsRecord | null;
  orgId: string;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  if (!org) {
    return (
      <Panel>
        <PanelHeader title="Organization identity" description="The slug is immutable because it anchors governed warehouse scope." />
        <Status as="block" active title="Loading Organization">Verifying Organization identity.</Status>
      </Panel>
    );
  }

  const current = (key: string): string =>
    draft[key] ?? String((org as unknown as Record<string, unknown>)[key] ?? "");
  const set = (key: string, value: string) => {
    setSaved(false);
    setDraft((d) => ({ ...d, [key]: value }));
  };

  const badColour = BRAND_FIELDS.filter(
    (f) => f.colour && current(f.key) !== "" && !HEX.test(current(f.key)),
  );
  const dirty = Object.keys(draft).length > 0;

  async function save() {
    setBusy(true);
    setError(null);
    try {
      // Only what changed. The PATCH treats an absent key as unchanged, so
      // sending the whole record would rewrite fields nobody touched.
      await apiJson(`/api/organizations/${encodeURIComponent(orgId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, v.trim() === "" ? null : v.trim()])),
        ),
      });
      setDraft({});
      setSaved(true);
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel>
      <PanelHeader
        title="Organization identity"
        description="The slug is immutable because it anchors governed warehouse scope."
      />
      <div className="grid gap-4 p-5 sm:grid-cols-2">
        <Fact label="Immutable slug" value={org.slug} mono />
        <Fact label="Status" value={org.status ?? "Unknown"} />
        <div className="space-y-1.5">
          <Label htmlFor="org-name">Name</Label>
          <Input id="org-name" value={current("name")} onChange={(e) => set("name", e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="org-billing">Billing reference</Label>
          <Input id="org-billing" value={current("billing_ref")} onChange={(e) => set("billing_ref", e.target.value)} />
        </div>
        {BRAND_FIELDS.map((field) => {
          const value = current(field.key);
          const invalid = field.colour && value !== "" && !HEX.test(value);
          return (
            <div key={field.key} className="space-y-1.5">
              <Label htmlFor={`org-${field.key}`}>{field.label}</Label>
              <div className="flex items-center gap-2">
                <Input
                  id={`org-${field.key}`}
                  value={value}
                  placeholder={field.colour ? "#RRGGBB" : "https://…"}
                  aria-invalid={invalid || undefined}
                  onChange={(e) => set(field.key, e.target.value)}
                  data-testid={`org-${field.key}`}
                />
                {field.colour && HEX.test(value) && (
                  // The swatch is the point of a colour field: a hex string is
                  // not a colour until you can see it.
                  <span
                    aria-hidden
                    className="size-8 shrink-0 rounded-md border border-divider-base"
                    style={{ background: value }}
                  />
                )}
              </div>
              {invalid && (
                <Status tone="error" className="text-caption">
                  {field.label} must be #RRGGBB.
                </Status>
              )}
            </div>
          );
        })}
      </div>
      <div className="flex items-center gap-3 border-t border-divider-base p-5">
        <Button onClick={save} disabled={busy || !dirty || badColour.length > 0} data-testid="org-save-identity">
          {busy ? "Saving…" : "Save"}
        </Button>
        {saved && !dirty && <Status tone="success">Saved.</Status>}
        {error && <Status tone="error" data-testid="org-save-error">{error}</Status>}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Members and invitations
// ---------------------------------------------------------------------------

/** The roles `_ORG_ROLES` accepts on PATCH (`server/core/org_members_api.py#_update_org_member`). */
const ORG_ROLES = ["owner", "admin", "member", "viewer"] as const;
type OrgRole = (typeof ORG_ROLES)[number];

const ROLE_LABELS: Record<string, string> = {
  owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer",
};

/**
 * The role, in the DECLARED word, and never the stored token.
 *
 * `ROLE_LABELS[role] ?? role` is a map with a RAW fallback: the four words are
 * declared, and the fifth role the server ever adds prints its wire value into
 * a table cell. `console-presentation.md` §4 refuses the token alone and
 * ratifies the shape for a thing this console cannot name — the words, then the
 * token in monospace beside them, so the gap is visible and attributable
 * instead of being read as a role. `wireWord` is NOT the answer here: a
 * prettified `Superadmin` would claim the console understands a permission it
 * has never been told about.
 */
function roleWord(role: string): ReactNode {
  const declared = ROLE_LABELS[role];
  if (declared) return declared;
  return (
    <span className="inline-flex flex-wrap items-baseline gap-1">
      Unknown role
      <ObjectId value={role} title="Role token" />
    </span>
  );
}
/*
 * TWO PRIVATE MAPS ARE GONE (76-2). The disagreement neither declared: `revoked`
 * GREY here, where the union draws it red everywhere else -- a revoked
 * invitation is refused, not retired, and grey said the opposite on the one
 * screen where somebody acts on it. `expired` stays neutral (a life that ended
 * on schedule); `invited`, `suspended`, `pending`, `delivered` and
 * `delivery_failed` are declared words.
 */

interface OrgInvitation {
  invitation_id: string;
  role: string;
  state: string;
  expires_at: string;
  issuer: string;
  available_actions: Array<"resend" | "revoke">;
}
interface InvitationMutation {
  invitation_id: string;
  state: string;
  delivery_handoff?: { url?: string; single_return?: boolean };
}
interface InvitationGrant { scope_id: string; capability: "view" | "edit" | "manage" }

/** `POST` on this family REFUSES a request with no `Idempotency-Key`
 *  (422 `missing_idempotency_key`, `server/core/invitations_api.py#_issue_invitation`). Held per command so a
 *  retry after a 5xx re-sends the SAME key and cannot double-issue. */
function newIdempotencyKey(): string {
  return typeof crypto?.randomUUID === "function"
    ? crypto.randomUUID()
    : `idem-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

/** One line per scope, `scope-id:view|edit|manage` — the shape `_parse_invitation_grants`
 *  expects. Parsed here so a typo is named at the field instead of returning a 422
 *  that names a server-side key. */
function parseGrants(value: string, label: string): InvitationGrant[] {
  const seen = new Set<string>();
  return value
    .split(/\r?\n/)
    .map((row) => row.trim())
    .filter(Boolean)
    .map((row) => {
      const cut = row.lastIndexOf(":");
      const scope_id = row.slice(0, cut).trim();
      const capability = row.slice(cut + 1).trim();
      if (cut <= 0 || !scope_id || scope_id.length > 256 || !["view", "edit", "manage"].includes(capability)) {
        throw new Error(`${label} grants must use one line per scope: scope-id:view|edit|manage.`);
      }
      if (seen.has(scope_id)) throw new Error(`${label} grants contain a duplicate scope.`);
      seen.add(scope_id);
      return { scope_id, capability: capability as InvitationGrant["capability"] };
    });
}

function formatMoment(value: string): string {
  return formatTimestamp(value);
}

/**
 * Membership, roles and invitations — READABLE and now OPERABLE.
 *
 * This section rendered three columns and nothing else, while the whole write
 * side had existed since Story 21.5/21.8 and was served, routed and guarded:
 * `PATCH`/`DELETE /api/organizations/{id}/members/{identity}` and the four
 * invitation routes (`invitations_api.py#_issue_invitation`,
 * `org_members_api.py#_update_org_member`). A complete MUI panel against
 * those routes — `orgs/OrgDetailPanel.tsx` — was written for Story 21.8 and
 * mounted by nothing, so the capability existed three times over and reached
 * nobody. That file was deleted on 2026-08-17; this section is where issuing,
 * listing, resending and revoking an invitation lives, and the only place.
 * It was ported here rather than mounted: the console's vocabulary is
 * Tailwind + shadcn (CLAUDE.md §5), and mounting a 46th MUI surface to close a
 * gap would trade one debt for a larger one.
 *
 * What the server decides, and this screen only reports:
 *
 *  - **the last-owner guard.** `_would_orphan_last_owner` answers 409 on the
 *    removal, the downgrade AND the suspension of an org's last active owner.
 *    It is shown as a warning, never swallowed (AD-9) — the operation did not
 *    happen and the reason is not a failure of the person.
 *  - **who may write.** `_enforce_org_manage` gates every mutation, and
 *    `_enforce_role_assignment` additionally refuses assigning a role above the
 *    actor's own rank or self-promotion. No access rule is re-implemented here;
 *    the controls are disabled from the server's OWN verdict on the invitation
 *    read, which is manage-gated and so answers 403 to exactly the people whose
 *    writes would be refused.
 *  - **delivery.** Issuing an invitation sends nothing. The server hands back a
 *    single-use link ONCE; if this screen does not show it, it is lost. Hence
 *    the copy affordance and the sentence that refuses to claim an email went
 *    out.
 *
 * Direct enrolment is deliberately absent: `POST /members` answers 409
 * `invitation_required` under canonical identity (`server/core/org_members_api.py#_add_org_member`). A form
 * that adds a member outright would be a control that cannot work.
 */
function MembersSection({ orgId, members, onMembersChanged }: {
  orgId: string;
  members: OrgMember[] | null;
  onMembersChanged: () => void;
}) {
  const [invitations, setInvitations] = useState<OrgInvitation[] | null>(null);
  const [invitationsError, setInvitationsError] = useState<string | null>(null);
  // The server's own answer to "may this person manage the org", taken from the
  // manage-gated invitation read rather than guessed client-side.
  const [manages, setManages] = useState<boolean | null>(null);

  const [warning, setWarning] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pendingRemoval, setPendingRemoval] = useState<OrgMember | null>(null);

  const [identity, setIdentity] = useState("");
  const [role, setRole] = useState<OrgRole>("member");
  const [expiryHours, setExpiryHours] = useState("48");
  const [projectGrants, setProjectGrants] = useState("");
  const [datastreamGrants, setDatastreamGrants] = useState("");
  const [issueError, setIssueError] = useState<string | null>(null);
  const [handoff, setHandoff] = useState<{ url: string | null; message: string } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  // Same fingerprint -> same key, so a resubmit after a 5xx is the SAME command.
  const issueKey = useRef<{ fingerprint: string; key: string } | null>(null);
  const actionKeys = useRef<Record<string, string>>({});

  const loadInvitations = useCallback(async () => {
    if (!orgId) return;
    setInvitationsError(null);
    try {
      const data = await apiGet<{ items?: OrgInvitation[] }>(
        `/api/organizations/${encodeURIComponent(orgId)}/invitations`,
      );
      setInvitations(Array.isArray(data.items) ? data.items : []);
      setManages(true);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
        // Not a manager. Not an error to report as a failure: the read is
        // gated, and the write controls are hidden for the same reason.
        setManages(false);
        setInvitations([]);
        return;
      }
      setManages(null);
      setInvitations([]);
      setInvitationsError(err instanceof Error ? err.message : "Invitations could not be read.");
    }
  }, [orgId]);

  useEffect(() => { void loadInvitations(); }, [loadInvitations]);

  /** Maps a refused mutation onto what the server actually said. A 409 here is
   *  ALWAYS the last-owner guard — it is the only conflict these routes raise. */
  function report(err: unknown, fallback: string) {
    if (err instanceof ApiError) {
      if (err.status === 409) { setWarning(err.message); return; }
      setFailure(err.status === 403 ? `${err.message} (you do not have manage on this organization)` : err.message);
      return;
    }
    setFailure(err instanceof Error ? err.message : fallback);
  }

  async function changeRole(member: OrgMember, next: string) {
    setWarning(null); setFailure(null); setBusy(`role:${member.identity}`);
    try {
      await apiJson(`/api/organizations/${encodeURIComponent(orgId)}/members/${encodeURIComponent(member.identity)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: next }),
      });
      onMembersChanged();
    } catch (err) {
      report(err, "The role was not changed.");
    } finally {
      setBusy(null);
    }
  }

  async function removeMember(member: OrgMember) {
    setWarning(null); setFailure(null); setBusy(`remove:${member.identity}`);
    try {
      await apiJson(`/api/organizations/${encodeURIComponent(orgId)}/members/${encodeURIComponent(member.identity)}`, {
        method: "DELETE",
      });
      setPendingRemoval(null);
      onMembersChanged();
    } catch (err) {
      report(err, "The member was not removed.");
      setPendingRemoval(null);
    } finally {
      setBusy(null);
    }
  }

  async function issueInvitation() {
    let project: InvitationGrant[];
    let datastream: InvitationGrant[];
    const hours = Number(expiryHours);
    try {
      project = parseGrants(projectGrants, "Project");
      datastream = parseGrants(datastreamGrants, "Datastream");
      if (project.length + datastream.length > 100) throw new Error("An invitation carries at most 100 grants.");
      if (!Number.isInteger(hours) || hours < 1 || hours > 168) {
        throw new Error("Expiry must be a whole number of hours between 1 and 168.");
      }
    } catch (err) {
      setIssueError(err instanceof Error ? err.message : "The invitation scope is invalid.");
      return;
    }

    const fingerprint = JSON.stringify([orgId, identity.trim(), role, project, datastream, hours]);
    if (issueKey.current?.fingerprint !== fingerprint) {
      issueKey.current = { fingerprint, key: newIdempotencyKey() };
    }
    setBusy("issue"); setIssueError(null); setHandoff(null); setCopied(null);
    try {
      const result = await apiJson<InvitationMutation>(
        `/api/organizations/${encodeURIComponent(orgId)}/invitations`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": issueKey.current.key },
          body: JSON.stringify({
            invited_identity: identity.trim(),
            role,
            project_grants: project,
            datastream_grants: datastream,
            expires_in_hours: hours,
          }),
        },
      );
      issueKey.current = null;
      const url = result.delivery_handoff?.url?.trim() || null;
      setIdentity(""); setRole("member"); setExpiryHours("48");
      setProjectGrants(""); setDatastreamGrants("");
      setHandoff({
        url,
        message: url
          ? "Invitation created. Nothing was sent: copy this single-use link and hand it over through a channel you trust. It is shown once."
          : "Invitation created, but the server returned no link. Nothing was sent. Use Resend below to mint a replacement link.",
      });
      await loadInvitations();
    } catch (err) {
      // A 4xx is a rejected command, not a lost one — drop the key so a corrected
      // resubmit is a new command. A 5xx keeps it: the first one may have landed.
      if (err instanceof ApiError && err.status < 500) issueKey.current = null;
      setIssueError(err instanceof ApiError ? err.message : err instanceof Error ? err.message : "The invitation was not created.");
    } finally {
      setBusy(null);
    }
  }

  async function actOnInvitation(invitation: OrgInvitation, action: "resend" | "revoke") {
    const command = `${invitation.invitation_id}:${action}`;
    actionKeys.current[command] ??= newIdempotencyKey();
    setBusy(command); setFailure(null); setHandoff(null); setCopied(null);
    try {
      const result = await apiJson<InvitationMutation>(
        `/api/organizations/${encodeURIComponent(orgId)}/invitations/${encodeURIComponent(invitation.invitation_id)}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": actionKeys.current[command] },
          ...(action === "resend" ? { body: JSON.stringify({ expires_in_hours: 48 }) } : {}),
        },
      );
      delete actionKeys.current[command];
      if (action === "resend") {
        const url = result.delivery_handoff?.url?.trim() || null;
        setHandoff({
          url,
          message: url
            ? "Replacement link created and the previous one revoked. Nothing was sent: copy it now, it is shown once."
            : "The resend completed but returned no link. Nothing was sent.",
        });
      }
      await loadInvitations();
    } catch (err) {
      if (err instanceof ApiError && err.status < 500) delete actionKeys.current[command];
      report(err, `The invitation was not ${action === "resend" ? "resent" : "revoked"}.`);
    } finally {
      setBusy(null);
    }
  }

  async function copyLink(url: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(url);
      setCopied("Link copied. Give it only to the person it names.");
    } catch {
      setCopied("Copy failed. Select the link and copy it by hand — it is not shown again.");
    }
  }

  const canWrite = manages === true;

  return (
    <div className="space-y-5">
      {warning ? (
        <Status as="block" tone="warning" title="The change was refused" data-testid="members-warning">
          {warning} Nothing was modified.
        </Status>
      ) : null}
      {failure ? (
        <Status as="block" tone="error" title="The change did not go through" data-testid="members-error">
          {failure}
        </Status>
      ) : null}

      <Panel>
        <PanelHeader
          title="Members"
          description="Roles and removal are governed here. Project grants are intentionally managed in Project Access."
        />
        {members === null ? (
          <Status as="block" active title="Loading members">Verifying active membership.</Status>
        ) : members.length === 0 ? (
          <EmptyState
            title="Nobody has joined this organization yet"
            description="A person becomes a member by accepting an invitation — there is no other way in. Create one in Invitations below and hand the link over."
          />
        ) : (
          <TableScroll label="Organization members">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Person</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Joined</TableHead>
                  {canWrite ? <TableHead className="text-right">Actions</TableHead> : null}
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.map((member) => (
                  <TableRow key={member.identity} data-testid={`member-row-${member.identity}`}>
                    <TableCell>
                      <span className="block text-ui text-text">{memberLabel(member)}</span>
                      {member.verified_email ? (
                        <span className="block font-mono text-caption text-text-secondary">
                          {member.identity}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      {canWrite ? (
                        <>
                          <Label htmlFor={`role-${member.identity}`} className="sr-only">
                            Role of {memberLabel(member)}
                          </Label>
                          <NativeSelect
                            id={`role-${member.identity}`}
                            className="w-36"
                            value={member.role}
                            disabled={busy !== null}
                            aria-label={`Role of ${memberLabel(member)}`}
                            data-testid={`role-select-${member.identity}`}
                            onChange={(event) => void changeRole(member, event.target.value)}
                          >
                            {ORG_ROLES.map((value) => (
                              <option key={value} value={value}>{ROLE_LABELS[value]}</option>
                            ))}
                          </NativeSelect>
                        </>
                      ) : (
                        roleWord(member.role)
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge tone={stateTone(member.status)}>{stateLabel(member.status)}</Badge>
                    </TableCell>
                    <TableCell className="text-text-secondary">
                      {member.joined_at ? formatMoment(member.joined_at) : "—"}
                    </TableCell>
                    {canWrite ? (
                      <TableCell className="text-right">
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={busy !== null}
                          data-testid={`remove-member-${member.identity}`}
                          onClick={() => { setWarning(null); setFailure(null); setPendingRemoval(member); }}
                        >
                          Remove…
                        </Button>
                      </TableCell>
                    ) : null}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
        {manages === false ? (
          <p className="border-t border-divider-base p-5 text-caption text-text-secondary">
            You can read this membership but not change it. Roles, removal and invitations are
            reserved to the organization's owners and admins.
          </p>
        ) : null}
      </Panel>

      {/* Removal is irreversible for the person's access, so it is never one
          click away from a table row. The dialog names who is being removed and
          what that does NOT do — their toorow account survives. */}
      <ConfirmDialog
        open={pendingRemoval !== null}
        onOpenChange={(open) => { if (!open) setPendingRemoval(null); }}
        title="Remove this member?"
        description={
          <>
            <strong>{pendingRemoval ? memberLabel(pendingRemoval) : ""}</strong> loses access to this organization and every
            project inside it, immediately. Any window they left signed in is signed out at their
            next action — they do not keep reading until their session expires. Their toorow account
            is not deleted, and re-adding them needs a fresh invitation they must accept.
          </>
        }
        confirmLabel="Remove member"
        destructive
        busy={busy?.startsWith("remove:") ?? false}
        onConfirm={() => { if (pendingRemoval) void removeMember(pendingRemoval); }}
      />

      <Panel>
        <PanelHeader
          title="Invitations"
          description="A person becomes a member by accepting an invitation — never by being added outright."
        />
        {manages === false ? (
          <EmptyState
            title="Invitations are managed by owners and admins"
            description="This list is not readable with your role, and no invitation state is inferred from that."
          />
        ) : (
          <div className="space-y-5 p-5">
            {canWrite ? (
              <div className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-[2fr_1fr_1fr]">
                  <div className="space-y-1.5">
                    <Label htmlFor="invite-identity">Verified email</Label>
                    <Input
                      id="invite-identity"
                      value={identity}
                      placeholder="person@example.com"
                      data-testid="invite-identity"
                      onChange={(event) => setIdentity(event.target.value)}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="invite-role">Role</Label>
                    <NativeSelect
                      id="invite-role"
                      value={role}
                      data-testid="invite-role"
                      onChange={(event) => setRole(event.target.value as OrgRole)}
                    >
                      {ORG_ROLES.map((value) => (
                        <option key={value} value={value}>{ROLE_LABELS[value]}</option>
                      ))}
                    </NativeSelect>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="invite-expiry">Expires in (hours)</Label>
                    <Input
                      id="invite-expiry"
                      type="number"
                      min={1}
                      max={168}
                      step={1}
                      value={expiryHours}
                      data-testid="invite-expiry"
                      onChange={(event) => setExpiryHours(event.target.value)}
                    />
                  </div>
                </div>

                {/* Scope grants ride ON the invitation: they are applied at the
                    moment it is accepted, which is why they cannot be added
                    afterwards from here. Folded away because the common case is
                    a role and nothing else. */}
                <Collapsible>
                  <CollapsibleTrigger className="text-caption text-text-secondary underline underline-offset-4">
                    Grant project or datastream scope with this invitation
                  </CollapsibleTrigger>
                  <CollapsibleContent className="mt-3 grid gap-4 sm:grid-cols-2">
                    <div className="space-y-1.5">
                      <Label htmlFor="invite-project-grants">Project grants</Label>
                      <Textarea
                        id="invite-project-grants"
                        value={projectGrants}
                        placeholder={"project-id:view\nproject-id:manage"}
                        onChange={(event) => setProjectGrants(event.target.value)}
                      />
                      <p className="text-caption text-text-secondary">One per line: <code className="font-mono">project-id:view|edit|manage</code></p>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="invite-datastream-grants">Datastream grants</Label>
                      <Textarea
                        id="invite-datastream-grants"
                        value={datastreamGrants}
                        placeholder={"datastream-id:view"}
                        onChange={(event) => setDatastreamGrants(event.target.value)}
                      />
                      <p className="text-caption text-text-secondary">One per line: <code className="font-mono">datastream-id:view|edit|manage</code></p>
                    </div>
                  </CollapsibleContent>
                </Collapsible>

                <div className="flex items-center gap-3">
                  <Button
                    type="button"
                    disabled={busy !== null || identity.trim() === ""}
                    data-testid="invite-submit"
                    onClick={() => void issueInvitation()}
                  >
                    {busy === "issue" ? "Creating…" : "Create invitation"}
                  </Button>
                  <span className="text-caption text-text-secondary">
                    toorow sends no email. You hand the link over yourself.
                  </span>
                </div>

                {issueError ? (
                  <Status as="block" tone="error" title="The invitation was not created" data-testid="invite-error">
                    {issueError}
                  </Status>
                ) : null}

                {handoff ? (
                  <Status
                    as="block"
                    tone={handoff.url ? "warning" : "info"}
                    title={handoff.url ? "Copy this link now" : "No link was returned"}
                    data-testid="invite-handoff"
                  >
                    <p className="m-0">{handoff.message}</p>
                    {handoff.url ? (
                      <div className="mt-3 flex items-start gap-2">
                        <Input
                          readOnly
                          value={handoff.url}
                          spellCheck={false}
                          autoComplete="off"
                          aria-label="Single-use invitation link"
                          data-testid="invite-link"
                        />
                        <Button type="button" variant="secondary" onClick={() => void copyLink(handoff.url!)}>
                          Copy
                        </Button>
                      </div>
                    ) : null}
                    {copied ? <p className="mt-2 mb-0 text-caption" role="status">{copied}</p> : null}
                    <Button
                      className="mt-3"
                      type="button"
                      variant="secondary"
                      onClick={() => { setHandoff(null); setCopied(null); }}
                    >
                      Dismiss
                    </Button>
                  </Status>
                ) : null}
              </div>
            ) : null}

            {invitationsError ? (
              <Status
                as="block"
                tone="error"
                title="Invitations could not be read"
                action={<Retry onClick={() => void loadInvitations()} />}
              >
                <p className="m-0">{invitationsError}. No invitation state is shown — an empty list here would read as “nobody is pending”.</p>
              </Status>
            ) : invitations === null ? (
              <Status as="block" active title="Loading invitations">Reading the invitation lifecycle.</Status>
            ) : invitations.length === 0 ? (
              <EmptyState
                title="Nobody is waiting to join"
                description="No invitation is outstanding. Create one with the form above; toorow sends no email, so you hand the link over yourself."
              />
            ) : (
              <TableScroll label="Organization invitations">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Role</TableHead>
                      <TableHead>State</TableHead>
                      <TableHead>Expires</TableHead>
                      <TableHead>Issued by</TableHead>
                      {canWrite ? <TableHead className="text-right">Actions</TableHead> : null}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {invitations.map((invitation) => (
                      <TableRow
                        key={invitation.invitation_id}
                        data-testid={`invitation-row-${invitation.invitation_id}`}
                      >
                        <TableCell>{roleWord(invitation.role)}</TableCell>
                        <TableCell>
                          <Badge tone={stateTone(invitation.state)}>{stateLabel(invitation.state)}</Badge>
                        </TableCell>
                        <TableCell className="text-text-secondary">{formatMoment(invitation.expires_at)}</TableCell>
                        <TableCell className="font-mono text-xs">{invitation.issuer}</TableCell>
                        {canWrite ? (
                          <TableCell className="text-right">
                            {/* The server states which actions this invitation still
                                admits. Rendering the other one would offer a control
                                that is already spent. */}
                            <div className="flex justify-end gap-2">
                              {invitation.available_actions.includes("resend") ? (
                                <Button
                                  type="button"
                                  variant="secondary"
                                  disabled={busy !== null}
                                  data-testid={`resend-invitation-${invitation.invitation_id}`}
                                  onClick={() => void actOnInvitation(invitation, "resend")}
                                >
                                  {busy === `${invitation.invitation_id}:resend` ? "Resending…" : "Resend"}
                                </Button>
                              ) : null}
                              {invitation.available_actions.includes("revoke") ? (
                                <Button
                                  type="button"
                                  variant="destructive"
                                  disabled={busy !== null}
                                  data-testid={`revoke-invitation-${invitation.invitation_id}`}
                                  onClick={() => void actOnInvitation(invitation, "revoke")}
                                >
                                  {busy === `${invitation.invitation_id}:revoke` ? "Revoking…" : "Revoke"}
                                </Button>
                              ) : null}
                            </div>
                          </TableCell>
                        ) : null}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}

/**
 * Account exposure — the OUTBOUND side, which had no surface at all.
 *
 * This section rendered `AuthorizationList` filtered on the inbound direction —
 * `owner_org_id != org_id` server-side
 * (`org_members_api.py#_list_org_authorizations`), which means *another owner
 *  exposes this to us*. So the
 * section showed the INBOUND direction, and offered no action on it, which is
 * correct: the server says of those rows "the person who plugged it in is not
 * disclosed, and no action is offered".
 *
 * The section's own ratified purpose is the other direction — `README.md:83`
 * *"provider-account exposure"*, and its `SECTIONS` entry reads "Provider
 * accounts shared TO beneficiary scopes". `glossary.md:93-101` names the owner
 * of that act: *"Owner. Organization Settings (exposure)"*, over
 * `app.credential_account_grants` keyed `(credential_id, external_account_id,
 * grantee_org_id)`. Exposing an account is what makes it selectable by the
 * Projects of the grantee organization — without it a connection is plugged in
 * and unusable.
 *
 * That act had no surface anywhere. `CredentialGrantsPanel` implements it in
 * full — and, since 2026-08-02, in the console's own vocabulary — but the only
 * thing that ever mounted it was `orgs/OrgDetailPanel.tsx`, which nothing
 * mounted. It is mounted here rather than rewritten — and with all four of its
 * capabilities landed here, that panel was deleted on 2026-08-17.
 *
 * Two things are deliberately NOT built:
 *
 *  - **a grantee picker.** The panel grants to the organization it is given, so
 *    here the grantee is this organization: an owner exposing its own accounts
 *    to its own Projects, which is the flow the glossary describes. Granting
 *    ACROSS organizations is story 21.10, which Jean deferred ("pas urgent a
 *    date"). Inventing a cross-org picker would be deciding that story.
 *  - **the typed credential id.** Asking a person to type `cred_…` from memory
 *    is not a control. The organization's own authorizations are already read
 *    by this screen, so the identifier is chosen, never recalled. The panel used
 *    to render the field ANYWAY, underneath this chooser — the sentence and the
 *    screen disagreed until 2026-08-17. It now renders the field only on a mount
 *    that supplies no `credentialId`, so the statement above is what the screen
 *    does rather than what it intended.
 */
function AccountExposureSection({ orgId, authorizations }: {
  orgId: string;
  authorizations: Authorization[] | null;
}) {
  const [selected, setSelected] = useState<string | null>(null);

  if (authorizations === null) {
    return (
      <Panel>
        <PanelHeader title="Provider-account exposure" description="Reading this organization's authorizations." />
        <Status as="block" active title="Loading authorizations">Verifying authorization ownership.</Status>
      </Panel>
    );
  }

  // Only what this organization OWNS can be exposed by it. The rest is inbound
  // and is shown below, without controls, because it is not ours to grant.
  const owned = authorizations.filter((item) => !isInbound(item));
  const inbound = authorizations.filter(isInbound);
  const current = owned.find((item) => item.id === selected) ?? null;

  return (
    <div className="space-y-5">
      <Panel>
        <PanelHeader
          title="Provider-account exposure"
          description="Exposing an account is what makes it selectable by this organization's Projects. Exposure never reveals token material."
        />
        {owned.length === 0 ? (
          <EmptyState
            title="No Authorization to expose"
            description="This organization owns no Authorization yet. Exposure is a decision about an existing Authorization, not a way to create one: an Authorization appears here once a Source has been connected, in Data."
          />
        ) : (
          <div className="space-y-4 p-5">
            <div className="space-y-1.5">
              <Label htmlFor="exposure-credential">Authorization</Label>
              <NativeSelect
                id="exposure-credential"
                className="max-w-md"
                value={selected ?? ""}
                data-testid="exposure-credential"
                onChange={(event) => setSelected(event.target.value || null)}
              >
                <option value="">Choose an Authorization…</option>
                {owned.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.account_label ?? item.provider ?? item.id}
                  </option>
                ))}
              </NativeSelect>
              <p className="text-caption text-text-secondary">
                Its accounts are listed once chosen. Each one is exposed or revoked on its own row.
              </p>
            </div>

            {current ? (
              <div className="border-t border-divider-base pt-4" data-testid="exposure-panel">
                <CredentialGrantsPanel orgId={orgId} credentialId={current.id} />
              </div>
            ) : null}
          </div>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="Exposed to this organization"
          description="Accounts another owner has shared with you. Revoking them belongs to that owner, not here."
        />
        {inbound.length === 0 ? (
          <EmptyState
            title="Nothing is shared with this organization"
            description="No other organization exposes a provider account to this one."
          />
        ) : (
          <div className="divide-y divide-divider-base">
            {/* An inert row is only honest if it says who to go to. The owning
                organization travels with the row (`owner_org_name`), so the
                party to ask is named rather than left as "that owner" in a
                header the reader has already scrolled past. No gesture is
                offered here because the server offers none on this direction. */}
            {inbound.map((item) => {
              // The NAME or nothing. `?? item.owner_org_id` sat between the
              // served word and a sentence written for its absence, so a row
              // whose owner could not be named said "ask an owner or admin of
              // org_01K… to revoke it" instead of the sentence below, which
              // names the same gesture without an address nobody can act on.
              const owner = item.owner_org_name ?? null;
              return (
                <div key={item.id} className="flex items-center justify-between gap-4 p-4">
                  <div>
                    <strong className="block text-ui">{item.account_label ?? item.provider ?? "Authorization"}</strong>
                    <ObjectId value={item.id} title="Object" />
                    <span className="mt-1 block text-caption text-text-secondary">
                      {owner
                        ? `Exposed by ${owner}. To end it, ask an owner or admin of ${owner} to revoke it.`
                        : "To end it, ask an owner or admin of the organization that owns this Authorization to revoke it."}
                    </span>
                  </div>
                  <Badge tone="neutral">Exposed to you</Badge>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}

/**
 * Data access — who, outside toorow, may read this organization's warehouse.
 *
 * The last of the four capabilities that lived in the unmounted
 * `orgs/OrgDetailPanel.tsx` (deleted 2026-08-17, once all four had landed
 * here), and the only one that had NO ratified target: the
 * twenty documents of `docs/product-architecture/` name `marts` as a dbt layer
 * and never as something one grants. Measured twice, with two vocabularies:
 *
 *     grep -rniE "bigquery grant|dataset access|mart|IAM|principal" \
 *       docs/product-architecture/*.md docs/product-architecture/capabilities/*.md
 *
 * It is mounted here on Jean's instruction of 2026-08-04 ("applique"), and the
 * placement is READ from the object rather than invented — the rule
 * `glossary.md:93-101` already applies to the neighbouring grant: an
 * organization-scoped grant belongs to Organization Settings.
 * `app.dataset_access_grants` is keyed by `org_id` alone, `_grant_dataset_access`
 * gates on `_enforce_org_manage`, and the dataset it opens is `org_<slug>_marts`
 * — provisioned when the organization was created. Every one of those says
 * organization, so nothing here decides a question the code left open.
 *
 * What this is NOT: it does not publish, choose or shape what the warehouse
 * contains — that is Data > Publication and its Output (`glossary.md:159`,
 * *"What can downstream consumers read?"*). This section answers the adjacent
 * question that document does not: **who** is allowed to read it at all.
 *
 * The contract is written down in `docs/product-architecture/data.md` in the
 * same commit, per CLAUDE.md §3 — a surface delivered without the line that
 * says so is how the next session fails to find the target.
 */
function DataAccessSection({ orgId }: { orgId: string }) {
  return (
    <Panel>
      <PanelHeader
        title="Data access"
        description="Requested, effective, failed and revoked IAM readers for this organization's marts dataset."
      />
      <div className="space-y-4 p-5">
        <p className="m-0 text-body text-text-secondary">
          A request here asks Google Cloud to let a service account, user or group read this organization's
          <span className="font-mono text-caption"> marts </span>
          dataset directly — from a BI tool, a notebook or a scheduled query of your own. Access is
          effective only after Google confirms it. It grants nothing inside toorow and
          never exposes a provider credential: it opens the warehouse that toorow builds,
          not the sources it reads.
        </p>
        <DataAccessGrantsPanel orgId={orgId} />
      </div>
    </Panel>
  );
}

// `AuthorizationList` lived here and rendered both the owned and the inbound
// list from one `exposure` flag. Its two callers are gone: `credentials` has
// used `AuthorizationsPanel` for a while, and `account-exposure` now operates
// the grant instead of listing it. Its inbound half is kept verbatim inside
// `AccountExposureSection` — nothing it displayed stopped being displayed.

// ---------------------------------------------------------------------------
// The irreversible surface
// ---------------------------------------------------------------------------

/** The counts, in the order they are shown, with what each one actually means. */
const ORG_COUNT_ROWS: Array<{ key: string; label: string; note: string }> = [
  { key: "datastreams", label: "Datastreams", note: "Their schedules and their ingested history." },
  { key: "connections", label: "Authorizations", note: "Stored grants to read the source platforms." },
  { key: "invitations", label: "Invitations", note: "Pending and accepted invitations to this organization." },
  { key: "members", label: "Memberships", note: "People lose access here. Their toorow account is not deleted." },
  { key: "operations", label: "Operations", note: "Recorded runs and their outcome history." },
];

/** The confirmation phrase the server requires on the DELETE. */
const ORG_CONFIRM_HEADER = "drop-warehouse-data";

type PreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; preview: OrgDeletionPreview };

type DeleteState =
  | { status: "idle" }
  | { status: "deleting" }
  | { status: "error"; message: string }
  | { status: "deleted" };

function failureMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.message} (HTTP ${err.status})`;
  return err instanceof Error ? err.message : String(err);
}

export function OrgDangerZone({ orgId, onDeleted }: OrgDangerZoneProps) {
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [preview, setPreview] = useState<PreviewState>({ status: "idle" });
  const [confirmText, setConfirmText] = useState("");
  const [del, setDel] = useState<DeleteState>({ status: "idle" });
  const panelRef = useRef<HTMLDivElement>(null);

  // Fetched WHEN THE ZONE IS OPENED (and on each retry), never on mount.
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setPreview({ status: "loading" });
    void (async () => {
      try {
        const data = await apiGet<OrgDeletionPreview>(
          `/api/organizations/${encodeURIComponent(orgId)}/deletion-preview`,
        );
        if (alive) setPreview({ status: "ready", preview: data });
      } catch (err) {
        if (alive) setPreview({ status: "error", message: err instanceof ApiError ? err.message : failureMessage(err) });
      }
    })();
    return () => { alive = false; };
  }, [open, orgId, attempt]);

  // The destructive form must not appear behind the cursor without the keyboard
  // following it.
  useEffect(() => { if (open) panelRef.current?.focus(); }, [open]);

  function closeZone() {
    setOpen(false);
    setConfirmText("");
    setPreview({ status: "idle" });
    setDel({ status: "idle" });
  }

  async function runDelete() {
    setDel({ status: "deleting" });
    try {
      await apiJson<{ deleted: boolean; org_id: string; removed?: unknown }>(
        `/api/organizations/${encodeURIComponent(orgId)}`,
        { method: "DELETE", headers: { "X-Confirm-Delete": ORG_CONFIRM_HEADER } },
      );
      setDel({ status: "deleted" });
      (onDeleted ?? (() => window.location.assign("/")))();
    } catch (err) {
      setDel({ status: "error", message: failureMessage(err) });
    }
  }

  const ready = preview.status === "ready" ? preview.preview : null;
  // THE NAME A PERSON HAS TO TYPE, OR NOTHING TO TYPE AT ALL.
  // `org_lifecycle.org_deletion_preview` serves `name` off
  // `app.organizations.name` (`NOT NULL`, migration 035) and the whole block
  // below renders only when that preview is `ready`, so `?? orgId` was
  // unreachable — and had it fired it would have asked a person to type
  // `org_01K…` to confirm the destruction of their organization. A missing name
  // now makes the confirmation impossible rather than turning an address into
  // the password for an irreversible act.
  const orgName = ready?.name ?? ready?.org_name ?? null;
  const projects = ready?.projects ?? [];
  const datasets = ready?.warehouse_datasets ?? [];
  const blockers = ready?.blockers ?? [];
  const blocked = blockers.length > 0;
  const nameMatches = ready != null && orgName != null && confirmText === orgName;

  return (
    <Panel>
      <PanelHeader
        title="Organization actions"
        description="Deleting this organization is permanent and cannot be undone."
      />
      <div className="space-y-4 p-5">
        <p className="text-body text-text-secondary">
          Deleting an organization removes its projects, datastreams, connections and members, and{" "}
          <strong className="text-text">drops the warehouse datasets</strong> that were provisioned when it was
          created. There is no restore and no export step.
        </p>

        {!open ? (
          <Button
            type="button"
            variant="secondary"
            aria-expanded={false}
            aria-controls="orgsettings-danger-panel"
            onClick={() => setOpen(true)}
          >
            Delete this organization…
          </Button>
        ) : (
          <div
            id="orgsettings-danger-panel"
            ref={panelRef}
            tabIndex={-1}
            aria-labelledby="orgsettings-danger-panel-title"
            className="space-y-4 rounded-control border border-divider-base p-5 focus-visible:outline-3 focus-visible:outline-focus"
          >
            <h3 id="orgsettings-danger-panel-title" className="m-0 text-ui font-semibold text-text">
              Delete this organization
            </h3>

            {preview.status === "loading" ? (
              <Status as="block" active title="Checking">
                Checking exactly what deleting this organization would destroy…
              </Status>
            ) : null}

            {/* `Status` already carries role="alert" for the error tone — wrapping
                it in a second one makes findByRole("alert") ambiguous. */}
            {preview.status === "error" ? (
              <Status as="block" tone="error" title="We could not check what would be deleted">
                <p className="m-0">
                  {preview.message}. Nothing has been deleted. We will not offer a deletion we cannot
                  describe — this is not a sign that the organization is empty. Try again, and if it keeps
                  failing, leave the organization in place and report it.
                </p>
                <Button className="mt-3" type="button" variant="secondary" onClick={() => setAttempt((n) => n + 1)}>
                  Try again
                </Button>
              </Status>
            ) : null}

            {ready && blocked ? (
              <Status as="block" tone="warning" title="This organization cannot be deleted yet">
                <p className="m-0">Deletion is unavailable until the following is resolved. Nothing has been deleted.</p>
                <ul className="mt-3 mb-0 grid list-none gap-2 p-0">
                  {blockers.map((blocker, index) => (
                    <li key={`${blocker.kind}-${index}`} className="flex flex-wrap items-baseline gap-2">
                      <span className="text-caption text-text-secondary">{wireWord(blocker.kind)}</span>
                      <span className="text-ui">{blocker.detail}</span>
                    </li>
                  ))}
                </ul>
              </Status>
            ) : null}

            {ready && !blocked ? (
              <>
                <Status as="block" tone="error" title="This is permanent">
                  Everything listed below is destroyed when you confirm. It cannot be undone and it cannot be
                  recovered by support.
                </Status>

                <section className="space-y-5">
                  <div>
                    <h4 className="m-0 text-ui font-semibold text-text">Projects deleted ({projects.length})</h4>
                    {projects.length === 0 ? (
                      <p className="mt-2 mb-0 text-body text-text-secondary">This organization has no project.</p>
                    ) : (
                      <ul className="mt-2 mb-0 flex list-none flex-wrap gap-2 p-0" aria-label="Projects that will be deleted">
                        {projects.map((project) => (
                          <li key={project.id} className="flex items-baseline gap-2 rounded-control border border-divider-base px-3 py-1.5">
                            <span className="text-ui">{project.name}</span>
                            {project.status ? (
                              <span className="text-caption text-text-secondary">{stateLabel(project.status)}</span>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  <div>
                    <h4 className="m-0 text-ui font-semibold text-text">Records deleted</h4>
                    <dl className="mt-2 mb-0 grid gap-3 sm:grid-cols-2">
                      {ORG_COUNT_ROWS.map((row) => {
                        const value = ready.counts?.[row.key];
                        return (
                          <div key={row.key}>
                            <dt className="text-caption text-text-secondary">{row.label}</dt>
                            <dd className="m-0 font-numeric text-ui text-text">
                              {typeof value === "number" ? value : "—"}
                            </dd>
                            <p className="m-0 text-caption text-text-secondary">{row.note}</p>
                          </div>
                        );
                      })}
                    </dl>
                  </div>

                  <div>
                    <h4 className="m-0 text-ui font-semibold text-text">
                      Warehouse datasets dropped ({datasets.length})
                    </h4>
                    <p className="mt-2 mb-0 text-body text-text-secondary">
                      Creating this organization provisioned its warehouse. Deleting it drops these datasets and
                      every table they contain.
                    </p>
                    {datasets.length === 0 ? (
                      <p className="mt-2 mb-0 text-body text-text-secondary">
                        The preview reports no warehouse dataset for this organization.
                      </p>
                    ) : (
                      <ul
                        className="mt-2 mb-0 grid list-none gap-1 p-0"
                        aria-label="Warehouse datasets that will be dropped"
                      >
                        {datasets.map((dataset) => (
                          <li key={dataset} className="font-mono text-caption text-text">{dataset}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </section>

                {del.status === "error" ? (
                  <Status as="block" tone="error" title="Organization not deleted">
                    <p className="m-0">{del.message}</p>
                  </Status>
                ) : null}

                {del.status === "deleted" ? (
                  <Status as="block" tone="success" title="Organization deleted">
                    {orgName ?? "This organization"} and everything listed above are gone. Leaving this organization…
                  </Status>
                ) : null}

                <div className="space-y-2">
                  <Label htmlFor="orgsettings-danger-confirm">Type the organization name to confirm</Label>
                  <Input
                    id="orgsettings-danger-confirm"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    value={confirmText}
                    placeholder={orgName ?? ""}
                    aria-describedby="orgsettings-danger-confirm-hint"
                    disabled={orgName === null || del.status === "deleting" || del.status === "deleted"}
                    onChange={(event) => {
                      setConfirmText(event.target.value);
                      if (del.status === "error") setDel({ status: "idle" });
                    }}
                  />
                  <p id="orgsettings-danger-confirm-hint" className="m-0 text-caption text-text-secondary">
                    {orgName === null
                      ? "This organization's name could not be read, so the confirmation cannot be typed. Reload the preview."
                      : (<>Enter <span className="font-mono">{orgName}</span> exactly. The delete button stays disabled until it matches.</>)}
                  </p>
                </div>

                <div className="flex flex-wrap gap-3">
                  <Button type="button" variant="secondary" onClick={closeZone}>Cancel</Button>
                  <Button
                    type="button"
                    variant="destructive"
                    disabled={!nameMatches || del.status === "deleting" || del.status === "deleted"}
                    onClick={() => void runDelete()}
                  >
                    {del.status === "deleting" ? "Deleting…" : "Delete organization permanently"}
                  </Button>
                </div>
              </>
            ) : null}

            {preview.status === "error" || blocked ? (
              <Button type="button" variant="secondary" onClick={closeZone}>Close</Button>
            ) : null}
          </div>
        )}
      </div>
    </Panel>
  );
}
