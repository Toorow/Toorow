/**
 * Project Access — Project grants and access handoffs (Story 46.4).
 *
 * The authority this surface holds is the whole point of it: Organization
 * membership is read-only here and links to Organization Settings, while
 * granting, changing and revoking a Project capability happens ONLY here.
 *
 * Every capability change goes through the server's immutable
 * prepare -> confirm pair, never a direct write:
 *   POST .../access/grant-changes                     freezes before/after +
 *                                                     the membership version
 *   POST .../access/grant-changes/{id}/confirmations  issues a short-lived,
 *                                                     single-use secret
 *   POST .../access/grant-changes/{id}/confirm        revalidates and commits
 * Revocation is the same contract with `after_capability: null`. The owner
 * floor cannot be revoked and the server refuses it; the UI does not offer it.
 */
import { useEffect, useRef, useState } from "react";
import { ApiError, apiGet, apiJson } from "../../lib/apiFetch";
import { Badge, Button, EmptyState, formatTimestamp, Input, Label, ObjectId, Panel, PanelHeader, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, stateLabel, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, wireWord, Retry } from "../../ui";
import GlobalScopeLayout from "../GlobalScopeLayout";
import type { ProjectAccessSection } from "../router";

type Capability = "view" | "edit" | "manage";

interface AccessPerson {
  identity: string;
  organization_role: string;
  explicit_grant: string | null;
  effective_capability: string | null;
  grant_source: string;
  membership_version?: number;
}
interface AccessHandoff {
  id: string;
  identity?: string | null;
  state: string;
  expires_at?: string | null;
  resume_ref?: string | null;
}
interface DeliveryHandoff {
  handoff_id: string;
  state: string;
  expires_at: string;
  resume_ref: string;
}
interface ProjectAccessEnvelope {
  schema_version: "project-access.v1";
  project: { id: string; name: string; organization: { id: string; name: string } };
  caller_capability: string;
  people: AccessPerson[];
  handoffs: AccessHandoff[];
}
interface PreparedChange {
  change_id: string;
  state: string;
  expires_at: string;
  identity: string;
  before: string | null;
  after: string | null;
  membership_version: number;
}

const SECTIONS = [
  { key: "people", label: "People", description: "Membership and effective Project capability" },
  { key: "handoffs", label: "Handoffs", description: "Pending Project-scoped invitations" },
] as const;

const CAPABILITIES: Capability[] = ["view", "edit", "manage"];

function failure(error: unknown): string {
  if (error instanceof ApiError) return `${error.message} (HTTP ${error.status})`;
  return error instanceof Error ? error.message : String(error);
}

export default function ProjectAccess({
  projectId, section, onSectionChange,
}: {
  projectId: string;
  section: ProjectAccessSection;
  onSectionChange: (section: ProjectAccessSection) => void;
}) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "error"; message: string } | { kind: "ready"; data: ProjectAccessEnvelope }
  >({ kind: "loading" });

  const load = () => {
    setState({ kind: "loading" });
    apiGet<ProjectAccessEnvelope>(`/api/projects/${encodeURIComponent(projectId)}/access`)
      .then((data) => setState({ kind: "ready", data }))
      .catch(() => setState({
        kind: "error",
        message: "Project Access is unavailable. No access state was inferred.",
      }));
  };
  useEffect(load, [projectId]);

  const data = state.kind === "ready" ? state.data : null;
  const canManage = data?.caller_capability === "manage";

  // NEVER THE IDENTIFIER. This fallback printed `projectId` in the eyebrow while
  // the envelope was loading or denied -- the same raw-ULID defect that
  // `GlobalScopeLayout` was reshaped to remove. "Project" is the honest word for a
  // scope whose name is not known yet.
  const scopeLabel = data ? `${data.project.organization.name} / ${data.project.name}` : "Project";
  return (
    <GlobalScopeLayout
      eyebrow="Project scope"
      title="Project Access"
      description="Project grants and access handoffs are managed here. Organization membership remains read-only."
      scopeLabel={scopeLabel}
      sections={SECTIONS}
      activeSection={section}
      onSectionChange={onSectionChange}
    >
      {state.kind === "loading" ? (
        <Status as="block" active title="Loading Project Access">
          Verifying Organization membership and explicit grants.
        </Status>
      ) : null}
      {state.kind === "error" ? (
        <Status as="block" tone="error" title="Project Access unavailable"
          action={<Retry onClick={() => void load()} />}
        >{state.message}</Status>
      ) : null}
      {data && section === "people" ? (
        <PeopleSection data={data} canManage={canManage} projectId={projectId} onChanged={load} />
      ) : null}
      {data && section === "handoffs" ? (
        <HandoffsSection data={data} canManage={canManage} projectId={projectId} onChanged={load} />
      ) : null}
    </GlobalScopeLayout>
  );
}

// ---------------------------------------------------------------------------
// People and the grant lifecycle
// ---------------------------------------------------------------------------

function PeopleSection({
  data, canManage, projectId, onChanged,
}: {
  data: ProjectAccessEnvelope;
  canManage: boolean;
  projectId: string;
  onChanged: () => void;
}) {
  const [intent, setIntent] = useState<Record<string, Capability | "none">>({});
  const [prepared, setPrepared] = useState<PreparedChange | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const prepareKeys = useRef<Record<string, string>>({});

  const base = `/api/projects/${encodeURIComponent(projectId)}/access`;

  const prepare = async (identity: string, after: Capability | null) => {
    const command = `${projectId}:${identity}:${after ?? "none"}`;
    prepareKeys.current[command] ??= crypto.randomUUID();
    setBusy(true);
    setError(null);
    setConfirmed(null);
    try {
      const change = await apiJson<PreparedChange>(`${base}/grant-changes`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": prepareKeys.current[command] },
        body: JSON.stringify({ identity, after_capability: after }),
      });
      delete prepareKeys.current[command];
      setPrepared(change);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status > 0 && cause.status < 500) {
        delete prepareKeys.current[command];
      }
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!prepared) return;
    setBusy(true);
    setError(null);
    try {
      // The secret is issued and consumed by this one gesture; it is never
      // rendered, stored or logged.
      const issued = await apiJson<{ confirmation_id: string; confirmation_secret: string }>(
        `${base}/grant-changes/${encodeURIComponent(prepared.change_id)}/confirmations`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
      );
      await apiJson(`${base}/grant-changes/${encodeURIComponent(prepared.change_id)}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirmation_id: issued.confirmation_id,
          confirmation_secret: issued.confirmation_secret,
        }),
      });
      setConfirmed(
        prepared.after
          ? `${prepared.identity} now has ${prepared.after} on this Project.`
          : `${prepared.identity} no longer has an explicit grant on this Project.`,
      );
      setPrepared(null);
      onChanged();
    } catch (cause) {
      // A 409 means the frozen change no longer matches reality. Nothing was
      // applied, and the stale change is dropped rather than retried blindly.
      setError(failure(cause));
      if (cause instanceof ApiError && cause.status === 409) setPrepared(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <Panel>
        <PanelHeader
          title="Effective Project access"
          description={`Your capability: ${data.caller_capability}`}
        />
        {!canManage ? (
          <div className="px-5 pt-5">
            <Status as="block" tone="neutral" title="View only">
              Changing a Project grant requires the manage capability. Nothing below is actionable
              for you.
            </Status>
          </div>
        ) : null}
        {error ? (
          <div className="px-5 pt-5">
            <Status as="block" tone="error" title="No grant was changed">{error}</Status>
          </div>
        ) : null}
        {confirmed ? (
          <div className="px-5 pt-5">
            <Status as="block" tone="success" title="Grant updated">{confirmed}</Status>
          </div>
        ) : null}
        <TableScroll label="Project access people matrix">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Identity</TableHead>
                <TableHead>Organization role</TableHead>
                <TableHead>Explicit grant</TableHead>
                <TableHead>Effective capability</TableHead>
                <TableHead>Source</TableHead>
                {canManage ? <TableHead>Change</TableHead> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.people.map((person) => {
                const isOwner = person.organization_role === "owner";
                const selected = intent[person.identity] ?? "none";
                return (
                  <TableRow key={person.identity}>
                    <TableCell className="font-mono text-xs">{person.identity}</TableCell>
                    <TableCell>{wireWord(person.organization_role)}</TableCell>
                    <TableCell>{person.explicit_grant ?? "None"}</TableCell>
                    <TableCell>
                      <Badge tone="neutral">{person.effective_capability ?? "No access"}</Badge>
                    </TableCell>
                    <TableCell>{person.grant_source}</TableCell>
                    {canManage ? (
                      <TableCell>
                        {isOwner ? (
                          <span className="text-caption text-text-secondary">
                            Owner floor — cannot be revoked
                          </span>
                        ) : (
                          <div className="flex flex-wrap items-center gap-2">
                            <Select
                              value={selected}
                              onValueChange={(value) =>
                                setIntent((current) => ({
                                  ...current,
                                  [person.identity]: value as Capability | "none",
                                }))
                              }
                            >
                              <SelectTrigger
                                className="w-32"
                                aria-label={`New capability for ${person.identity}`}
                              >
                                <SelectValue placeholder="Capability" />
                              </SelectTrigger>
                              <SelectContent>
                                {CAPABILITIES.map((capability) => (
                                  <SelectItem key={capability} value={capability}>
                                    {capability}
                                  </SelectItem>
                                ))}
                                <SelectItem value="none">Revoke</SelectItem>
                              </SelectContent>
                            </Select>
                            <Button
                              type="button"
                              variant="secondary"
                              disabled={busy}
                              onClick={() =>
                                void prepare(
                                  person.identity,
                                  selected === "none" ? null : selected,
                                )
                              }
                            >
                              Prepare change
                            </Button>
                          </div>
                        )}
                      </TableCell>
                    ) : null}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableScroll>
        {data.people.length === 0 ? (
          <EmptyState
            title="No eligible people"
            description="Add or reactivate Organization membership in Organization Settings first."
          />
        ) : null}
      </Panel>

      {prepared ? (
        <Panel>
          <PanelHeader
            title="Review this change before it applies"
            description="The server froze exactly what follows. Confirming revalidates it; nothing has changed yet."
          />
          <div className="space-y-4 p-5">
            <dl className="grid gap-4 sm:grid-cols-2">
              <div>
                <dt className="text-caption text-text-secondary">Identity</dt>
                <dd className="m-0 mt-1 font-mono text-ui">{prepared.identity}</dd>
              </div>
              <div>
                <dt className="text-caption text-text-secondary">Capability</dt>
                <dd className="m-0 mt-1 text-ui">
                  {prepared.before ?? "none"} → {prepared.after ?? "none (revoked)"}
                </dd>
              </div>
              <div>
                <dt className="text-caption text-text-secondary">Membership version</dt>
                <dd className="m-0 mt-1 font-numeric text-ui">{prepared.membership_version}</dd>
              </div>
              <div>
                <dt className="text-caption text-text-secondary">Expires</dt>
                <dd className="m-0 mt-1 font-numeric text-ui">{prepared.expires_at}</dd>
              </div>
            </dl>
            <div className="flex flex-wrap gap-3">
              <Button type="button" variant="secondary" onClick={() => setPrepared(null)}>
                Discard
              </Button>
              <Button type="button" disabled={busy} onClick={() => void confirm()}>
                {busy ? "Confirming…" : "Confirm change"}
              </Button>
            </div>
          </div>
        </Panel>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Handoffs
// ---------------------------------------------------------------------------

function HandoffsSection({
  data, canManage, projectId, onChanged,
}: {
  data: ProjectAccessEnvelope;
  canManage: boolean;
  projectId: string;
  onChanged: () => void;
}) {
  const [identity, setIdentity] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [delivery_handoff, setDeliveryHandoff] = useState<DeliveryHandoff | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const handoffKeys = useRef<Record<string, string>>({});

  const create = async () => {
    const resumeRef = `/org/${data.project.organization.id}/project/${data.project.id}/access/people`;
    const command = `${projectId}:${identity.trim()}:${resumeRef}`;
    handoffKeys.current[command] ??= crypto.randomUUID();
    setBusy(true);
    setError(null);
    setDeliveryHandoff(null);
    setCopyState("idle");
    try {
      const receipt = await apiJson<DeliveryHandoff>(`/api/projects/${encodeURIComponent(projectId)}/access/handoffs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": handoffKeys.current[command],
        },
        body: JSON.stringify({
          identity: identity.trim() || null,
          // A canonical route, so the handoff resumes the exact work rather
          // than a fixed onboarding page.
          resume_ref: resumeRef,
          expires_in_hours: 48,
        }),
      });
      delete handoffKeys.current[command];
      setDeliveryHandoff(receipt);
      setIdentity("");
      onChanged();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status > 0 && cause.status < 500) {
        delete handoffKeys.current[command];
      }
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel>
      <PanelHeader
        title="Access handoffs"
        description="Purpose-scoped invitations that resume this exact Project."
      />
      <div className="space-y-4 p-5">
        {error ? (
          <Status as="block" tone="error" title="No handoff was created">{error}</Status>
        ) : null}
        {delivery_handoff ? (
          <Status
            as="block"
            tone="success"
            title="Handoff accepted for delivery"
            data-testid="project-access-handoff-receipt"
            action={
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => {
                  if (!navigator.clipboard) {
                    setCopyState("failed");
                    return;
                  }
                  void navigator.clipboard.writeText(delivery_handoff.resume_ref)
                    .then(() => setCopyState("copied"))
                    .catch(() => setCopyState("failed"));
                }}
              >
                {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy resume path"}
              </Button>
            }
          >
            Server receipt {delivery_handoff.handoff_id}, accepted for delivery. Resume at{" "}
            <span className="font-mono text-caption">{delivery_handoff.resume_ref}</span>; expires{" "}
            {formatTimestamp(delivery_handoff.expires_at)}.
          </Status>
        ) : null}
        {canManage ? (
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="project-access-handoff-identity">
                Identity (leave empty for an unclaimed invitation)
              </Label>
              <Input
                id="project-access-handoff-identity"
                className="w-72"
                value={identity}
                autoComplete="off"
                onChange={(event) => setIdentity(event.target.value)}
              />
            </div>
            <Button type="button" variant="secondary" disabled={busy} onClick={() => void create()}>
              {busy ? "Creating…" : "Create handoff"}
            </Button>
          </div>
        ) : (
          <Status as="block" tone="neutral" title="View only">
            Creating a Project access handoff requires the manage capability.
          </Status>
        )}
        {data.handoffs.length === 0 ? (
          <EmptyState
            title="No pending handoffs"
            description="There are no unresolved Project access handoffs."
          />
        ) : (
          <div className="divide-y divide-divider-base">
            {data.handoffs.map((handoff) => (
              <div key={handoff.id} className="flex flex-wrap items-center justify-between gap-3 py-4">
                <div>
                  <strong className="block text-ui">
                    {handoff.identity ?? "Unclaimed invitation"}
                  </strong>
                  <ObjectId value={handoff.id} title="Handoff" />
                </div>
                <Status tone={handoff.state === "pending" ? "warning" : "neutral"}>
                  {stateLabel(handoff.state)}
                </Status>
              </div>
            ))}
          </div>
        )}
      </div>
    </Panel>
  );
}
