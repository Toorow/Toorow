/**
 * McpHostsPanel — the MCP hosts holding capabilities on this organization.
 *
 * Story 36.14 / UX-DR31, built at last in 67-16. The back end has existed since
 * Epic 36 (`host_preflight.py`, four `/api/mcp-hosts/*` routes) and no console
 * surface ever reached it: the only component that named it,
 * `ConfigurationHoteCard.tsx`, was French, mounted nowhere, and was deleted.
 * `screens/orphans.md` listed the bind route as a door with no screen behind it.
 *
 * THE QUESTION THIS SCREEN ANSWERS, in one sentence: which MCP hosts can act on
 * this organization, with what, since when — and cut one.
 *
 * WHY REVOKING IS THE ONLY ACTION HERE. Binding a host is a negotiated,
 * multi-step ceremony (catalog → preflight → handoff → bind) driven from the
 * host's own install flow; a console button that pretended to do it in one click
 * would be theatre. Cutting is the opposite: one decision, one consequence,
 * immediate. That asymmetry is the screen.
 *
 * REVOCATION IS IMMEDIATE, AND THE COPY SAYS SO. `mcp_profiles` re-attests the
 * capability context against the live row on EVERY MCP call, so a cut context
 * drops the host to Insights at its next call rather than at token expiry
 * (amendment of 2026-08-17 to `mcp-tool-surface.md`). The confirmation states
 * that, because "revoke" means different things on different products and the
 * person clicking is entitled to know which one this is.
 *
 * A CUT CONTEXT STAYS IN THE LIST. It is shown revoked, with its date, its
 * author and its reason. A lifecycle list that hides what was cut cannot
 * evidence a revocation — and the first question after cutting one is always
 * "did that work".
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ObjectId,
  Spinner,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Timestamp,
} from "../ui";

// ---------------------------------------------------------------------------
// Types — mirrored on GET /api/mcp-hosts/contexts?org_id=…
// (`server/core/mcp_attestation.py`, `_ATTESTED_COLUMNS`)
// ---------------------------------------------------------------------------

export interface McpCapabilityContext {
  id: string;
  org_id: string;
  host: string | null;
  workspace_id: string | null;
  workspace_type: string | null;
  client_id: string | null;
  endpoint_binding: string;
  enabled_profiles: string[];
  workspace_evidence_hash: string | null;
  interactive_presence_evidence_hash: string | null;
  policy_version: string | null;
  catalog_version: string | null;
  created_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  revocation_reason: string | null;
  /** The dated preflight that negotiated this binding (`app.host_preflights`). */
  preflight_id: string | null;
  preflight_host_key: string | null;
  preflight_state: string | null;
  preflight_dated_at: string | null;
}

/**
 * What a person records when they cut a connection. It is fixed rather than
 * typed: `ConfirmDialog` takes no children, and a free-text field bolted onto
 * the row outside the dialog would ask for a reason before the decision is
 * made. The server records who and when regardless; the sentence names where.
 */
const REVOCATION_REASON = "Revoked from Organization Settings";

interface McpHostsPanelProps {
  orgId: string;
}

/** Insights is the floor every context keeps; the others are what a cut removes. */
const HIGH_RISK = new Set(["operations", "governance", "support"]);

function highRiskProfiles(context: McpCapabilityContext): string[] {
  return (context.enabled_profiles ?? []).filter((profile) => HIGH_RISK.has(profile));
}

/**
 * The host name is opaque data (E36-NFR03): no host is preferred over another,
 * so it is displayed as it was recorded and never mapped to a brand.
 */
function hostLabel(context: McpCapabilityContext): string {
  return context.host?.trim() || "Unnamed host";
}

export default function McpHostsPanel({ orgId }: McpHostsPanelProps) {
  const [contexts, setContexts] = useState<McpCapabilityContext[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [pendingRevoke, setPendingRevoke] = useState<McpCapabilityContext | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [opInfo, setOpInfo] = useState<string | null>(null);
  // One key per context, so a retry after a 5xx is the SAME command and not a
  // second revocation attributed to a second click.
  const revokeKeys = useRef<Record<string, string>>({});

  const load = useCallback(async () => {
    if (!orgId) return;
    setContexts(null);
    setLoadError(null);
    setDenied(false);
    try {
      const resp = await apiFetch(
        `/api/mcp-hosts/contexts?org_id=${encodeURIComponent(orgId)}`
      );
      if (resp.status === 403) {
        setDenied(true);
        return;
      }
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const body = (await resp.json()) as { contexts?: McpCapabilityContext[] };
      setContexts(body.contexts ?? []);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRevoke(context: McpCapabilityContext) {
    setRevoking(true);
    setOpError(null);
    setOpInfo(null);
    revokeKeys.current[context.id] ??= crypto.randomUUID();
    try {
      const resp = await apiFetch(
        `/api/mcp-hosts/contexts/${encodeURIComponent(context.id)}/revoke`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": revokeKeys.current[context.id],
          },
          body: JSON.stringify({ reason: REVOCATION_REASON }),
        }
      );
      if (!resp.ok) {
        // A refusal below 500 is an answer, not a hiccup: the next attempt is a
        // new decision and must not reuse the key of the one that was refused.
        if (resp.status < 500) delete revokeKeys.current[context.id];
        const body = (await resp.json().catch(() => null)) as { message?: string } | null;
        if (resp.status === 403) {
          setOpError("Only an owner or admin of this organization can revoke a host's capabilities.");
        } else if (resp.status === 404) {
          setOpInfo("This capability context no longer exists. The list has been reloaded.");
          await load();
        } else {
          setOpError(body?.message ?? `The revocation failed (HTTP ${resp.status}).`);
        }
        return;
      }
      delete revokeKeys.current[context.id];
      setPendingRevoke(null);
      await load();
    } catch (err) {
      setOpError(err instanceof Error ? err.message : String(err));
    } finally {
      setRevoking(false);
    }
  }

  const loading = contexts === null && loadError === null && !denied;

  return (
    <div className="flex flex-col gap-4" data-testid="mcp-hosts-panel">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold">MCP hosts</h2>
        <p className="text-sm text-muted-foreground">
          Every MCP host connection bound to this organization, what it is allowed to do, and
          since when. Revoking a connection takes effect at its next call.
        </p>
      </div>

      {opError ? (
        <Status
          as="block"
          tone="error"
          title="The revocation did not go through"
          data-testid="mcp-hosts-op-error"
          action={<Button variant="ghost" size="sm" onClick={() => setOpError(null)}>Dismiss</Button>}
        >
          {opError}
        </Status>
      ) : null}
      {opInfo ? (
        <Status
          as="block"
          tone="info"
          title="Nothing to revoke"
          data-testid="mcp-hosts-op-info"
          action={<Button variant="ghost" size="sm" onClick={() => setOpInfo(null)}>Dismiss</Button>}
        >
          {opInfo}
        </Status>
      ) : null}

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="mcp-hosts-loading">
          <Spinner /> Reading the bound host connections…
        </div>
      ) : null}

      {denied ? (
        <Status as="block" tone="error" title="Not yours to read" data-testid="mcp-hosts-denied">
          Reading the MCP host connections of this organization is restricted to its owners and
          admins. Ask an owner to add you, or open an organization you administer.
        </Status>
      ) : null}

      {loadError ? (
        <Status
          as="block"
          tone="error"
          title="The host connections could not be read"
          data-testid="mcp-hosts-load-error"
          action={<Button size="sm" onClick={() => void load()}>Try again</Button>}
        >
          {loadError} — nothing is shown rather than an empty list, which would read as
          &ldquo;no host is connected&rdquo;.
        </Status>
      ) : null}

      {contexts !== null && contexts.length === 0 ? (
        <div data-testid="mcp-hosts-empty">
          <EmptyState
            title="No MCP host is connected to this organization"
            description={
              "A host appears here once it completes its install: it reads the host catalog, "
              + "records a preflight, and binds the connection from its own setup flow. "
              + "Nothing on this screen can bind one, and nothing needs to."
            }
          />
        </div>
      ) : null}

      {contexts !== null && contexts.length > 0 ? (
        <TableScroll label="MCP host connections">
          <Table data-testid="mcp-hosts-table">
            <TableHeader>
              <TableRow>
                <TableHead>Host</TableHead>
                <TableHead>Endpoint</TableHead>
                <TableHead>Can do</TableHead>
                <TableHead>Preflight</TableHead>
                <TableHead>Bound</TableHead>
                <TableHead>State</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {contexts.map((context) => {
                const risky = highRiskProfiles(context);
                const revoked = context.revoked_at !== null;
                return (
                  <TableRow key={context.id} data-testid={`mcp-host-row-${context.id}`}>
                    <TableCell>
                      <div className="flex flex-col gap-0.5">
                        <span className="font-medium">{hostLabel(context)}</span>
                        {context.workspace_id ? (
                          <span className="text-xs text-muted-foreground">
                            Workspace <ObjectId value={context.workspace_id} />
                          </span>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell className="text-xs">{context.endpoint_binding}</TableCell>
                    <TableCell>
                      {revoked ? (
                        <span className="text-xs text-muted-foreground">Reads only</span>
                      ) : risky.length === 0 ? (
                        <span className="text-xs text-muted-foreground">Reads only</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {risky.map((profile) => (
                            <Badge key={profile} outline>{profile}</Badge>
                          ))}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      {context.preflight_dated_at ? (
                        <div className="flex flex-col gap-0.5">
                          <Timestamp value={context.preflight_dated_at} />
                          <span className="text-xs text-muted-foreground">
                            {context.preflight_state ?? "unknown"}
                          </span>
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          Not recorded
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      {context.created_at ? <Timestamp value={context.created_at} /> : "—"}
                    </TableCell>
                    <TableCell>
                      {revoked ? (
                        <div className="flex flex-col gap-0.5" data-testid={`mcp-host-revoked-${context.id}`}>
                          <span className="text-xs font-medium">
                            Revoked {context.revoked_at ? <Timestamp value={context.revoked_at} /> : null}
                          </span>
                          <span className="text-xs text-muted-foreground">
                            by {context.revoked_by ?? "—"}
                            {context.revocation_reason ? ` — ${context.revocation_reason}` : ""}
                          </span>
                        </div>
                      ) : (
                        <Badge tone="success">Active</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {revoked ? null : (
                        <Button
                          variant="destructive"
                          size="sm"
                          data-testid={`mcp-host-revoke-${context.id}`}
                          onClick={() => {
                            setOpError(null);
                            setPendingRevoke(context);
                          }}
                        >
                          Revoke
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableScroll>
      ) : null}

      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRevoke(null);
        }}
        title="Revoke this host's capabilities?"
        description={
          "The host drops to read-only at its very next call — not when its token expires. "
          + "Anything it was allowed to change here, it can no longer change. "
          + "This cannot be undone: reconnecting the host binds a new connection."
        }
        evidence={{
          Host: pendingRevoke ? hostLabel(pendingRevoke) : null,
          Endpoint: pendingRevoke?.endpoint_binding ?? null,
          "Loses the right to": pendingRevoke
            ? highRiskProfiles(pendingRevoke).join(", ") || "nothing beyond reads"
            : null,
        }}
        evidenceLabel="Connection that will be cut"
        confirmLabel="Revoke capabilities"
        confirmTestId="mcp-hosts-confirm-revoke"
        cancelTestId="mcp-hosts-cancel-revoke"
        destructive
        busy={revoking}
        error={opError}
        onConfirm={() => {
          if (pendingRevoke) void handleRevoke(pendingRevoke);
        }}
      />
    </div>
  );
}
