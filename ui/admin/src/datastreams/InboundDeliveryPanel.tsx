/**
 * InboundDeliveryPanel — Story 38.7
 *
 * Shown on a managed_feed datastream's detail view. Lets an operator:
 *   1. See which inbound channels the datastream accepts (from config.channels).
 *   2. Issue, rotate, and revoke the email delivery credential.
 *      - The full secret (ds_<token>@domain) is returned ONCE on issue/rotate;
 *        shown with a copy button + "shown once" warning, then cleared on unmount.
 *   3. See recent received deliveries (GET …/managed-feed/imports).
 *
 * Design tokens:
 *   - Accent:   #FF99C8 (CTAs only)
 *   - Hairline: rgba(235,235,243,0.30)  (Rule 2)
 *   - Padding:  px:3, section gap 32   (Rule 5)
 *   - No raw MUI Chip for status       (Rule 4) — inline badge <Box> only
 *
 * API seam: uses opsGet / opsPost from ops/opsApi (OpsApiCtx) for all calls
 * except the credential endpoints, which carry an Idempotency-Key header
 * (opsPost already supports opts.idempotencyKey).
 *
 * connector_name: threaded from datastream.module_name (the backend treats
 * it as an opaque path slug; for managed feeds there is no module connector
 * so we fall back to "managed_feed"). See assumption note in the report.
 */

import {
  Alert,
  Box,
  Button,
  CircularProgress,
  IconButton,
  Tooltip,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useRef, useState } from "react";
import { OpsApiCtx, OpsApiError, opsGet, opsPost } from "./ops/opsApi";
import type { ImportLedgerRow } from "./ops/opsTypes";
import type { Datastream } from "./types";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Safe read-model returned by GET …/credentials (no secret ever). */
interface CredentialReadModel {
  id: string;
  channel: string;
  state: "ACTIVE" | "ROTATING" | "REVOKED" | "EXPIRED";
  safe_suffix: string;
  version: number;
  created_at: string | null;
  expires_at: string | null;
  overlap_until: string | null;
}

interface CredentialListResponse {
  datastream_id: string;
  credentials: CredentialReadModel[];
  count: number;
}

/** Shape returned ONCE by POST …/credentials (issue) or POST …/rotate. */
interface CredentialIssueResponse extends CredentialReadModel {
  full_secret: string; // SHOWN ONCE — never re-fetchable
}

interface ImportsListResponse {
  imports: ImportLedgerRow[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Derive the connector_name URL segment for inbound credential routes.
 *  For managed_feed datastreams, module_name is null/empty; fall back to
 *  the literal string "managed_feed" (assumed value — see report). */
function connectorNameFor(ds: Datastream): string {
  return (ds.module_name || "").trim() || "managed_feed";
}

function credentialsPath(ds: Datastream): string {
  return `/api/connectors/${encodeURIComponent(connectorNameFor(ds))}/datastreams/${encodeURIComponent(ds.id)}/credentials`;
}

function credentialActionPath(ds: Datastream, credId: string, action: "rotate" | "revoke"): string {
  return `${credentialsPath(ds)}/${encodeURIComponent(credId)}/${action}`;
}

function idempotencyKey(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// ---------------------------------------------------------------------------
// StateBadge — inline coloured dot + label (Rule 4: no MUI Chip for status)
// ---------------------------------------------------------------------------

const STATE_COLOR: Record<string, { dot: string; label: string }> = {
  ACTIVE:   { dot: "#2E7D32", label: "ACTIVE" },
  ROTATING: { dot: "#F57C00", label: "ROTATING" },
  REVOKED:  { dot: "#E53935", label: "REVOKED" },
  EXPIRED:  { dot: "#6B6A74", label: "EXPIRED" },
};

function StateBadge({ state }: { state: string }) {
  const meta = STATE_COLOR[state] ?? { dot: "#6B6A74", label: state };
  return (
    <Box
      sx={{
        display: "inline-flex",
        alignItems: "center",
        gap: 0.75,
        px: 1,
        py: 0.25,
        borderRadius: "4px",
        bgcolor: "rgba(235,235,243,0.12)",
        border: "1px solid rgba(235,235,243,0.20)",
      }}
    >
      <Box
        sx={{
          width: 7,
          height: 7,
          borderRadius: "50%",
          bgcolor: meta.dot,
          flexShrink: 0,
        }}
      />
      <Typography
        variant="caption"
        sx={{ fontSize: "0.72rem", fontWeight: 600, letterSpacing: "0.04em", color: "text.primary" }}
      >
        {meta.label}
      </Typography>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// CopyButton — clipboard helper
// ---------------------------------------------------------------------------

function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* fallback: select the text — handled by the input in ShowOnceSecret */
    }
  }, [value]);

  return (
    <Tooltip title={copied ? "Copied!" : label} placement="top">
      <IconButton
        size="small"
        onClick={handleCopy}
        aria-label={label}
        sx={{ color: copied ? "#2E7D32" : "text.secondary", p: 0.5 }}
      >
        {copied ? (
          // Checkmark
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        ) : (
          // Copy icon
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
          </svg>
        )}
      </IconButton>
    </Tooltip>
  );
}

// ---------------------------------------------------------------------------
// ShowOnceSecret — renders the full secret with copy + "shown once" warning.
// ---------------------------------------------------------------------------

function ShowOnceSecret({ secret }: { secret: string }) {
  return (
    <Box
      sx={{
        mt: 1.5,
        p: 1.5,
        borderRadius: 1.5,
        bgcolor: "rgba(255,153,200,0.08)",
        border: "1px solid rgba(255,153,200,0.30)",
      }}
      data-testid="show-once-secret"
    >
      <Alert
        severity="warning"
        icon={false}
        sx={{ mb: 1, py: 0.5, "& .MuiAlert-message": { fontSize: "0.8rem" } }}
      >
        This address is shown <strong>once only</strong>. Copy it now — it cannot be retrieved again.
      </Alert>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Box
          component="code"
          sx={{
            fontFamily: '"JetBrains Mono", monospace',
            fontSize: "0.8rem",
            color: "text.primary",
            bgcolor: "rgba(235,235,243,0.14)",
            px: 1,
            py: 0.5,
            borderRadius: "4px",
            overflowWrap: "anywhere",
            wordBreak: "break-all",
            flex: 1,
            minWidth: 0,
          }}
        >
          {secret}
        </Box>
        <CopyButton value={secret} label="Copy email address" />
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// ChannelChip — small tag for a channel kind
// ---------------------------------------------------------------------------

const CHANNEL_LABELS: Record<string, string> = {
  email: "Email",
  webhook: "Webhook",
  upload: "Upload",
};

function ChannelChip({ channel }: { channel: string }) {
  const label = CHANNEL_LABELS[channel] ?? channel;
  return (
    <Box
      sx={{
        display: "inline-flex",
        alignItems: "center",
        px: 1,
        py: 0.25,
        borderRadius: "4px",
        bgcolor: "rgba(235,235,243,0.12)",
        border: "1px solid rgba(235,235,243,0.20)",
      }}
    >
      <Typography variant="caption" sx={{ fontSize: "0.75rem", fontWeight: 500 }}>
        {label}
      </Typography>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// EmailCredentialSection — issue / rotate / revoke for the email channel
// ---------------------------------------------------------------------------

interface EmailCredentialSectionProps {
  ctx: OpsApiCtx;
  datastream: Datastream;
  cred: CredentialReadModel | null; // null = not yet issued
  onRefresh: () => void;
}

function EmailCredentialSection({
  ctx,
  datastream,
  cred,
  onRefresh,
}: EmailCredentialSectionProps) {
  const [secret, setSecret] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [domainNotReady, setDomainNotReady] = useState(false);

  // Clear the secret on unmount (AC: never persist full_secret longer than needed)
  useEffect(() => {
    return () => {
      setSecret(null);
    };
  }, []);

  const handleIssue = async () => {
    setBusy(true);
    setError(null);
    setDomainNotReady(false);
    try {
      const key = idempotencyKey();
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "Idempotency-Key": key,
      };
      if (ctx.apiToken) headers["Authorization"] = `Bearer ${ctx.apiToken}`;

      const resp = await fetch(`${ctx.apiBase}${credentialsPath(datastream)}`, {
        method: "POST",
        headers,
        body: JSON.stringify({ channel: "email" }),
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        if (body?.code === "domain_not_ready") {
          setDomainNotReady(true);
          return;
        }
        throw new Error(body?.message || `Error ${resp.status}`);
      }

      const data = (await resp.json()) as CredentialIssueResponse;
      setSecret(data.full_secret ?? null);
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleRotate = async () => {
    if (!cred) return;
    setBusy(true);
    setError(null);
    try {
      const key = idempotencyKey();
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "Idempotency-Key": key,
      };
      if (ctx.apiToken) headers["Authorization"] = `Bearer ${ctx.apiToken}`;

      const resp = await fetch(
        `${ctx.apiBase}${credentialActionPath(datastream, cred.id, "rotate")}`,
        { method: "POST", headers, body: JSON.stringify({}) }
      );

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.message || `Error ${resp.status}`);
      }

      const data = (await resp.json()) as CredentialIssueResponse;
      setSecret(data.full_secret ?? null);
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleRevoke = async () => {
    if (!cred) return;
    if (!window.confirm("Revoke this email credential? Deliveries using the current address will stop immediately.")) return;
    setBusy(true);
    setError(null);
    setSecret(null);
    try {
      const key = idempotencyKey();
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "Idempotency-Key": key,
      };
      if (ctx.apiToken) headers["Authorization"] = `Bearer ${ctx.apiToken}`;

      const resp = await fetch(
        `${ctx.apiBase}${credentialActionPath(datastream, cred.id, "revoke")}`,
        { method: "POST", headers, body: JSON.stringify({}) }
      );

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.message || `Error ${resp.status}`);
      }
      onRefresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const isTerminal = cred?.state === "REVOKED" || cred?.state === "EXPIRED";

  return (
    <Box>
      {/* Header row */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, mb: 1 }}>
        <ChannelChip channel="email" />
        {cred && <StateBadge state={cred.state} />}
      </Box>

      {/* Domain not ready notice */}
      {domainNotReady && (
        <Alert
          severity="info"
          sx={{ mb: 1.5, "& .MuiAlert-message": { fontSize: "0.82rem" } }}
          onClose={() => setDomainNotReady(false)}
        >
          The inbound domain is not yet configured or verified on this platform.
          Contact your platform administrator to complete the inbound domain setup before issuing an email credential.
        </Alert>
      )}

      {/* Error */}
      {error && (
        <Alert severity="error" sx={{ mb: 1.5 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Issued secret — shown once */}
      {secret && <ShowOnceSecret secret={secret} />}

      {/* Safe suffix when credential exists and secret is no longer shown */}
      {cred && !secret && (
        <Box sx={{ mb: 1.5 }}>
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 0.25 }}>
            Address suffix (safe to share)
          </Typography>
          <Box
            component="code"
            sx={{
              fontFamily: '"JetBrains Mono", monospace',
              fontSize: "0.78rem",
              color: "text.primary",
              bgcolor: "rgba(235,235,243,0.10)",
              px: 1,
              py: 0.5,
              borderRadius: "4px",
            }}
          >
            …{cred.safe_suffix}
          </Box>
          <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 0.5 }}>
            Version {cred.version}
            {cred.overlap_until ? ` · overlap until ${new Date(cred.overlap_until).toLocaleString()}` : ""}
          </Typography>
        </Box>
      )}

      {/* Actions */}
      <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, mt: secret ? 1.5 : 0.5 }}>
        {/* Issue — shown only when no active credential */}
        {(!cred || isTerminal) && (
          <Button
            size="small"
            variant="contained"
            disabled={busy || domainNotReady}
            onClick={handleIssue}
            aria-busy={busy}
            data-testid="issue-credential-btn"
            sx={{
              bgcolor: "#FF99C8",
              color: "text.primary",
              fontWeight: 600,
              fontSize: "0.8rem",
              "&:hover": { bgcolor: "#F77FB4" },
              "&.Mui-disabled": { opacity: 0.5 },
            }}
          >
            {busy ? <CircularProgress size={12} sx={{ mr: 0.75, color: "text.primary" }} /> : null}
            Issue email credential
          </Button>
        )}

        {/* Rotate — shown when credential is ACTIVE or ROTATING */}
        {cred && !isTerminal && (
          <Button
            size="small"
            variant="outlined"
            disabled={busy}
            onClick={handleRotate}
            aria-busy={busy}
            data-testid="rotate-credential-btn"
            sx={{ fontSize: "0.8rem", borderColor: "rgba(235,235,243,0.30)", color: "text.secondary" }}
          >
            {busy ? <CircularProgress size={12} sx={{ mr: 0.75 }} /> : null}
            Rotate
          </Button>
        )}

        {/* Revoke — shown when credential is ACTIVE or ROTATING */}
        {cred && !isTerminal && (
          <Button
            size="small"
            variant="text"
            disabled={busy}
            onClick={handleRevoke}
            data-testid="revoke-credential-btn"
            sx={{ fontSize: "0.8rem", color: "#E53935" }}
          >
            Revoke
          </Button>
        )}
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// RecentDeliveries — GET /api/datastreams/{id}/managed-feed/imports
// ---------------------------------------------------------------------------

const OUTCOME_META: Record<string, { dot: string; label: string }> = {
  published:  { dot: "#2E7D32", label: "Published" },
  rejected:   { dot: "#E53935", label: "Rejected" },
  pending:    { dot: "#F57C00", label: "Pending" },
  processing: { dot: "#FF99C8", label: "Processing" },
};

function OutcomeDot({ status }: { status?: string | null }) {
  const s = (status ?? "").toLowerCase();
  const meta = OUTCOME_META[s] ?? { dot: "#6B6A74", label: status ?? "—" };
  return (
    <Box sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      <Box sx={{ width: 7, height: 7, borderRadius: "50%", bgcolor: meta.dot, flexShrink: 0 }} />
      <Typography variant="caption" sx={{ fontSize: "0.72rem", color: "text.secondary" }}>
        {meta.label}
      </Typography>
    </Box>
  );
}

function RecentDeliveries({
  ctx,
  datastreamId,
}: {
  ctx: OpsApiCtx;
  datastreamId: string;
}) {
  const [rows, setRows] = useState<ImportLedgerRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await opsGet<ImportsListResponse>(
        ctx,
        `/api/datastreams/${encodeURIComponent(datastreamId)}/managed-feed/imports`
      );
      setRows(data.imports ?? []);
    } catch (err) {
      const msg =
        err instanceof OpsApiError ? err.message : err instanceof Error ? err.message : String(err);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [ctx, datastreamId]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return (
      <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}>
        <CircularProgress size={20} sx={{ color: "#FF99C8" }} />
      </Box>
    );
  }

  if (error) {
    return (
      <Box sx={{ py: 2 }}>
        <Typography variant="caption" sx={{ color: "error.main" }}>
          Could not load deliveries: {error}
        </Typography>
        <Button size="small" onClick={load} sx={{ ml: 1, color: "text.secondary", fontSize: "0.75rem" }}>
          Retry
        </Button>
      </Box>
    );
  }

  if (rows.length === 0) {
    return (
      <Box sx={{ py: 3, textAlign: "center" }}>
        <Typography variant="body2" sx={{ color: "text.secondary" }}>
          No deliveries yet
        </Typography>
        <Typography variant="caption" sx={{ color: "text.disabled", display: "block", mt: 0.5 }}>
          Received imports will appear here once the first file is delivered.
        </Typography>
      </Box>
    );
  }

  return (
    <Box
      sx={{
        border: "1px solid rgba(235,235,243,0.30)",
        borderRadius: 1.5,
        overflow: "hidden",
      }}
      data-testid="deliveries-table"
    >
      {/* Header */}
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "1fr auto auto",
          gap: 2,
          px: 2,
          py: 1,
          borderBottom: "1px solid rgba(235,235,243,0.30)",
          bgcolor: "rgba(235,235,243,0.08)",
        }}
      >
        {(["Received", "Rows", "Outcome"] as const).map((h) => (
          <Typography
            key={h}
            variant="caption"
            sx={{
              color: "text.secondary",
              fontWeight: 600,
              textTransform: "uppercase",
              fontSize: 10,
              letterSpacing: "0.06em",
            }}
          >
            {h}
          </Typography>
        ))}
      </Box>

      {/* Rows */}
      {rows.slice(0, 20).map((row, i) => (
        <Box
          key={row.id ?? row.execution_id ?? i}
          sx={{
            display: "grid",
            gridTemplateColumns: "1fr auto auto",
            gap: 2,
            px: 2,
            py: 1.25,
            alignItems: "center",
            borderBottom: "1px solid rgba(235,235,243,0.18)",
            "&:last-of-type": { borderBottom: 0 },
          }}
        >
          <Typography variant="caption" sx={{ color: "text.secondary", fontSize: "0.78rem" }}>
            {row.loaded_at
              ? new Date(row.loaded_at).toLocaleString(undefined, {
                  dateStyle: "short",
                  timeStyle: "short",
                })
              : row.date ?? "—"}
          </Typography>
          <Typography
            variant="caption"
            sx={{
              color: "text.primary",
              fontVariantNumeric: "tabular-nums lining-nums",
              fontSize: "0.78rem",
              textAlign: "right",
            }}
          >
            {row.row_count != null ? row.row_count.toLocaleString() : "—"}
          </Typography>
          <OutcomeDot status={row.status} />
        </Box>
      ))}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// InboundDeliveryPanel — top-level exported component
// ---------------------------------------------------------------------------

export interface InboundDeliveryPanelProps {
  datastream: Datastream;
  projectId: string;
  apiBase: string;
  apiToken: string;
}

export default function InboundDeliveryPanel({
  datastream,
  projectId,
  apiBase,
  apiToken,
}: InboundDeliveryPanelProps) {
  const ctx: OpsApiCtx = { apiBase, apiToken, projectId };

  // Declared channels from datastream config (array of strings or absent = all)
  const rawChannels = (datastream.config?.channels ?? []) as string[];
  const channels: string[] = Array.isArray(rawChannels) && rawChannels.length > 0
    ? rawChannels
    : ["email", "upload"]; // default for managed_feed when not declared

  const hasEmail = channels.includes("email");

  // Credential list (safe read-model, no secret)
  const [credentials, setCredentials] = useState<CredentialReadModel[]>([]);
  const [credsLoading, setCredsLoading] = useState(true);
  const [credsError, setCredsError] = useState<string | null>(null);

  const loadCredentials = useCallback(async () => {
    setCredsLoading(true);
    setCredsError(null);
    try {
      // GET uses apiToken auth; no project_id needed for credential list
      const headers: HeadersInit = apiToken ? { Authorization: `Bearer ${apiToken}` } : {};
      const resp = await fetch(`${apiBase}${credentialsPath(datastream)}`, {
        headers,
        cache: "no-store",
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body?.message || `Error ${resp.status}`);
      }
      const data = (await resp.json()) as CredentialListResponse;
      setCredentials(data.credentials ?? []);
    } catch (err) {
      setCredsError(err instanceof Error ? err.message : String(err));
    } finally {
      setCredsLoading(false);
    }
  }, [apiBase, apiToken, datastream]);

  useEffect(() => {
    if (hasEmail) {
      loadCredentials();
    } else {
      setCredsLoading(false);
    }
  }, [hasEmail, loadCredentials]);

  // Active or rotating credential for the email channel
  const emailCred =
    credentials.find(
      (c) => c.channel === "email" && (c.state === "ACTIVE" || c.state === "ROTATING")
    ) ??
    credentials.find((c) => c.channel === "email") ??
    null;

  // Section divider style
  const sectionSx = {
    pt: 3,
    pb: 3,
    borderBottom: "1px solid rgba(235,235,243,0.18)",
    "&:last-of-type": { borderBottom: 0 },
  };

  return (
    <Box sx={{ px: 3, pt: 3 }} data-testid="inbound-delivery-panel">
      {/* ------------------------------------------------------------------ */}
      {/* Section header */}
      {/* ------------------------------------------------------------------ */}
      <Typography
        variant="subtitle2"
        sx={{
          mb: 2.5,
          color: "text.secondary",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          fontSize: 11,
          fontWeight: 600,
        }}
      >
        Inbound delivery
      </Typography>

      {/* ------------------------------------------------------------------ */}
      {/* Channels accepted */}
      {/* ------------------------------------------------------------------ */}
      <Box sx={sectionSx}>
        <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mb: 1 }}>
          Accepted channels
        </Typography>
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1 }}>
          {channels.map((ch) => (
            <ChannelChip key={ch} channel={ch} />
          ))}
        </Box>
      </Box>

      {/* ------------------------------------------------------------------ */}
      {/* Email credential */}
      {/* ------------------------------------------------------------------ */}
      {hasEmail && (
        <Box sx={sectionSx}>
          <Typography
            variant="caption"
            sx={{
              color: "text.secondary",
              display: "block",
              mb: 1.5,
              fontWeight: 600,
              textTransform: "uppercase",
              fontSize: 10,
              letterSpacing: "0.05em",
            }}
          >
            Email delivery address
          </Typography>

          {credsLoading ? (
            <Box sx={{ py: 2, display: "flex", alignItems: "center", gap: 1 }}>
              <CircularProgress size={16} sx={{ color: "#FF99C8" }} />
              <Typography variant="caption" sx={{ color: "text.secondary" }}>
                Loading credential…
              </Typography>
            </Box>
          ) : credsError ? (
            <Box>
              <Typography variant="caption" sx={{ color: "error.main" }}>
                Could not load credentials: {credsError}
              </Typography>
              <Button size="small" onClick={loadCredentials} sx={{ ml: 1, color: "text.secondary", fontSize: "0.75rem" }}>
                Retry
              </Button>
            </Box>
          ) : (
            <EmailCredentialSection
              ctx={ctx}
              datastream={datastream}
              cred={emailCred}
              onRefresh={loadCredentials}
            />
          )}
        </Box>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Recent deliveries */}
      {/* ------------------------------------------------------------------ */}
      <Box sx={{ pt: 3 }}>
        <Typography
          variant="caption"
          sx={{
            color: "text.secondary",
            display: "block",
            mb: 1.5,
            fontWeight: 600,
            textTransform: "uppercase",
            fontSize: 10,
            letterSpacing: "0.05em",
          }}
        >
          Recent deliveries
        </Typography>
        <RecentDeliveries ctx={ctx} datastreamId={datastream.id} />
      </Box>

      <Box sx={{ pb: 6 }} />
    </Box>
  );
}
