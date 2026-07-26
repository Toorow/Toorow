import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  Link,
  List,
  ListItem,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { apiFetch } from "../lib/apiFetch";

// Story 36.14 / UX-DR31: host setup begins with a COMPATIBLE-HOST selector driven
// by capability / plan / role preflight -- NEVER brand-first ordering (E36-NFR03).
// Hosts are opaque entries keyed by capability; the selector sorts by
// capability/plan/role, and the display name never biases the order. Selecting a
// host records a dated preflight, generates host-specific admin handoff
// instructions, shows authorization status, and -- when app UI is unsupported --
// surfaces the standard-tool fallback path with bounded evidence (console
// deep-link as additional help only). French, WCAG 2.2 AA: keyboard/focus, an
// error region, non-color state, a live region and a 320 px one-column reflow.

export interface HostCatalogEntry {
  host_key: string;
  display_name: string;
  dated_at: string;
  capabilities: string[];
  plan_constraints: Record<string, unknown>;
  required_role: string;
  app_ui_supported: boolean;
  catalog_refresh: Record<string, unknown>;
  workspace_proof_required: boolean;
}

export interface UiSupportDecision {
  mode: "app_ui" | "standard_tool_fallback";
  bounded_evidence_required: boolean;
  console_deep_link: { kind: string; purpose: string; additional_only: boolean } | null;
  explanation: string;
}

export interface HostPreflightResult {
  preflight_id: string;
  host_key: string;
  state: string;
  capabilities: string[];
  plan_constraints: Record<string, unknown>;
  required_role: string;
  ui_support: UiSupportDecision;
  catalog_refresh: Record<string, unknown>;
  workspace_proof_required: boolean;
  dated_at: string;
  expires_at: string;
}

export interface HostInstallHandoff {
  preflight_id: string;
  state: string;
  admin_action: {
    action: string;
    required_role: string;
    return_condition: { kind: string; resource_id: string };
  };
  handoff_id: string;
  handoff_state: string;
  expires_at: string;
  delivery_url: string | null;
}

interface Props {
  taskId: string;
  orgId: string;
  projectId?: string | null;
  apiBase?: string;
  apiToken?: string;
}

// A stable, capability-driven sort so NO host is preferred by name (E36-NFR03).
// Order: app-UI capable first (richer support), then fewer plan constraints, then
// host_key as a deterministic tiebreak -- display_name is NEVER a sort key.
function capabilityOrder(a: HostCatalogEntry, b: HostCatalogEntry): number {
  if (a.app_ui_supported !== b.app_ui_supported) {
    return a.app_ui_supported ? -1 : 1;
  }
  const ac = Object.keys(a.plan_constraints ?? {}).length;
  const bc = Object.keys(b.plan_constraints ?? {}).length;
  if (ac !== bc) return ac - bc;
  return a.host_key.localeCompare(b.host_key);
}

export default function ConfigurationHoteCard({
  taskId,
  orgId,
  projectId = null,
  apiBase = "",
  apiToken = "",
}: Props) {
  const [catalog, setCatalog] = useState<HostCatalogEntry[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [preflight, setPreflight] = useState<HostPreflightResult | null>(null);
  const [handoff, setHandoff] = useState<HostInstallHandoff | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const errorRef = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(true);

  const headers = useCallback((): HeadersInit => ({
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  }), [apiToken]);

  const loadCatalog = useCallback(async () => {
    const response = await apiFetch(`${apiBase}/api/mcp-hosts/catalog`, {
      method: "GET",
      headers: headers(),
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(response.status === 404
        ? "La configuration d’hôte n’est pas disponible pour cette organisation."
        : `Catalogue des hôtes indisponible (HTTP ${response.status}).`);
    }
    const data = await response.json() as { hosts: Record<string, HostCatalogEntry> };
    if (mounted.current) setCatalog(Object.values(data.hosts ?? {}));
  }, [apiBase, headers]);

  useEffect(() => {
    mounted.current = true;
    setLoading(true);
    loadCatalog()
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Catalogue indisponible."))
      .finally(() => { if (mounted.current) setLoading(false); });
    return () => { mounted.current = false; };
  }, [loadCatalog]);

  useEffect(() => {
    if (error && errorRef.current) errorRef.current.focus();
  }, [error]);

  // Capability-negotiated ordering -- names never bias the list (E36-NFR03).
  const orderedHosts = useMemo(() => [...catalog].sort(capabilityOrder), [catalog]);

  async function runPreflight() {
    if (!selected) {
      setError("Choisissez un hôte compatible avant de lancer le contrôle de compatibilité.");
      return;
    }
    setBusy(true);
    setError(null);
    setAnnouncement("");
    setHandoff(null);
    try {
      const response = await apiFetch(`${apiBase}/api/mcp-hosts/preflight`, {
        method: "POST",
        headers: {
          ...headers(),
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          host_key: selected,
          task_id: taskId,
          org_id: orgId,
          project_id: projectId,
        }),
      });
      if (!response.ok) {
        throw new Error(response.status === 409
          ? "Ce contrôle de compatibilité a déjà été effectué ou l’étape a changé d’état."
          : `Contrôle de compatibilité impossible (HTTP ${response.status}).`);
      }
      const data = await response.json() as HostPreflightResult;
      if (!mounted.current) return;
      setPreflight(data);
      setAnnouncement(
        data.ui_support.mode === "app_ui"
          ? "Hôte compatible : interface applicative prise en charge."
          : "Hôte compatible : bascule sur les outils standard avec preuve bornée.",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Contrôle de compatibilité impossible.");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function handOff() {
    if (!preflight) return;
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch(
        `${apiBase}/api/mcp-hosts/preflight/${encodeURIComponent(preflight.preflight_id)}/handoff`,
        {
          method: "POST",
          headers: {
            ...headers(),
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({}),
        },
      );
      if (!response.ok) {
        throw new Error(response.status === 409
          ? "L’installation a déjà été confiée ou l’étape a changé d’état."
          : `Transmission de l’installation impossible (HTTP ${response.status}).`);
      }
      const data = await response.json() as HostInstallHandoff;
      if (!mounted.current) return;
      setHandoff(data);
      setAnnouncement("Installation confiée à l’administrateur ou l’administratrice de l’hôte.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Transmission impossible.");
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: "grid", placeItems: "center", minHeight: 200 }}>
        <CircularProgress aria-label="Chargement du catalogue des hôtes" />
      </Box>
    );
  }
  if (error && catalog.length === 0) {
    return <Alert severity="warning" tabIndex={-1} ref={errorRef as never}>{error}</Alert>;
  }

  const fallback = preflight?.ui_support.mode === "standard_tool_fallback";

  return (
    <Card component="section" variant="outlined" sx={{ borderRadius: 3, maxWidth: 760 }}>
      <CardContent>
        <Stack spacing={2}>
          <Box>
            <Typography component="h2" variant="h5">Configuration de l’hôte MCP</Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>
              Choisissez un hôte compatible. Le classement dépend uniquement des
              capacités, du plan et du rôle requis — jamais de la marque.
            </Typography>
          </Box>

          {/* Error region: assertive, focusable, non-color (icon + text). */}
          <Box role="alert" aria-live="assertive" ref={errorRef} tabIndex={-1} sx={{ outline: "none" }}>
            {error && <Alert severity="error">{error}</Alert>}
          </Box>
          {/* Live region: polite status announcements. */}
          <Box role="status" aria-live="polite" aria-atomic="true" sx={{ minHeight: 24 }}>
            {announcement}
          </Box>

          {/* Compatible-host selector (capability-negotiated ordering). */}
          <TextField
            select
            label="Hôte compatible"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            data-testid="host-selector"
            helperText="Hôtes ordonnés par capacité, plan et rôle requis."
            fullWidth
          >
            {orderedHosts.map((h) => (
              <MenuItem key={h.host_key} value={h.host_key} data-testid={`host-option-${h.host_key}`}>
                {h.display_name} — {h.app_ui_supported ? "interface applicative" : "outils standard"}
                {" · rôle "}{h.required_role}
              </MenuItem>
            ))}
          </TextField>

          <Box>
            <Button
              variant="contained"
              onClick={runPreflight}
              disabled={busy || !selected}
              data-testid="host-preflight-run"
            >
              {busy && !preflight ? "Contrôle en cours…" : "Contrôler la compatibilité"}
            </Button>
          </Box>

          {preflight && (
            <>
              <Divider />
              <Stack spacing={1} data-testid="host-preflight-result">
                <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", alignItems: "center" }}>
                  <Chip
                    size="small"
                    color={fallback ? "warning" : "success"}
                    label={fallback ? "Outils standard (repli)" : "Interface applicative"}
                    data-testid="host-ui-support"
                  />
                  <Chip size="small" variant="outlined" label={`Daté du ${preflight.dated_at}`} />
                  <Chip
                    size="small"
                    variant="outlined"
                    label={`Rôle requis : ${preflight.required_role}`}
                    data-testid="host-required-role"
                  />
                </Stack>
                <Typography variant="body2" color="text.secondary">
                  {preflight.ui_support.explanation}
                </Typography>

                {/* Capabilities + plan constraints (capability preflight, UX-DR31). */}
                <Typography variant="body2">
                  <strong>Capacités :</strong> {preflight.capabilities.join(", ")}
                </Typography>
                {Object.keys(preflight.plan_constraints ?? {}).length > 0 && (
                  <Typography variant="body2">
                    <strong>Contraintes de plan :</strong>{" "}
                    {Object.entries(preflight.plan_constraints)
                      .map(([k, v]) => `${k}=${String(v)}`)
                      .join(", ")}
                  </Typography>
                )}
                {preflight.workspace_proof_required && (
                  <Alert severity="info" data-testid="host-proof-required">
                    Une preuve d’espace de travail vérifiable est requise avant
                    d’activer les profils sensibles. Sans preuve, seul le profil
                    lecture (Insights) est lié.
                  </Alert>
                )}

                {/* Standard-tool fallback path: bounded evidence + additional-only link. */}
                {fallback && (
                  <Alert severity="warning" data-testid="host-fallback-path">
                    Cet hôte ne prend pas en charge l’interface applicative. Toorow
                    bascule sur les outils standard avec une preuve bornée
                    obligatoire.
                    {preflight.ui_support.console_deep_link && (
                      <>
                        {" "}
                        <Link href="#console" data-testid="host-console-deeplink">
                          Ouvrir la console (aide supplémentaire)
                        </Link>
                        .
                      </>
                    )}
                  </Alert>
                )}

                <Divider />
                {/* Generated host-specific admin handoff instructions (UX-DR31). */}
                <Typography component="h3" variant="subtitle1">
                  Confier l’installation à l’administration de l’hôte
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Si vous n’avez pas l’autorité « {preflight.required_role} » sur
                  l’hôte, transmettez une consigne minimale et à visée unique : elle
                  nomme l’action, l’expiration et la condition de retour, sans donner
                  aucune donnée Toorow.
                </Typography>
                <Box>
                  <Button
                    variant="outlined"
                    onClick={handOff}
                    disabled={busy}
                    data-testid="host-handoff-run"
                  >
                    Confier l’installation
                  </Button>
                </Box>
              </Stack>
            </>
          )}

          {handoff && (
            <>
              <Divider />
              {/* Authorization status + host-specific handoff instructions. */}
              <Stack spacing={1} data-testid="host-handoff-result">
                <Chip
                  size="small"
                  color="info"
                  label={`Installation confiée (${handoff.handoff_state})`}
                  data-testid="host-handoff-state"
                />
                <List dense disablePadding>
                  <ListItem disableGutters sx={{ display: "block" }}>
                    <Typography variant="body2">
                      <strong>Action :</strong> installer l’application MCP dans l’hôte.
                    </Typography>
                  </ListItem>
                  <ListItem disableGutters sx={{ display: "block" }}>
                    <Typography variant="body2">
                      <strong>Rôle requis :</strong> {handoff.admin_action.required_role}.
                    </Typography>
                  </ListItem>
                  <ListItem disableGutters sx={{ display: "block" }}>
                    <Typography variant="body2">
                      <strong>Expire le :</strong>{" "}
                      {new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeStyle: "short" })
                        .format(new Date(handoff.expires_at))}
                      .
                    </Typography>
                  </ListItem>
                  <ListItem disableGutters sx={{ display: "block" }}>
                    <Typography variant="body2">
                      <strong>Retour après installation :</strong> l’étape se
                      débloque automatiquement une fois l’hôte connecté (condition
                      « {handoff.admin_action.return_condition.kind} »).
                    </Typography>
                  </ListItem>
                </List>
                {handoff.delivery_url && (
                  <Typography
                    component="code"
                    variant="caption"
                    data-testid="host-handoff-link"
                    sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
                  >
                    Lien de transmission à usage unique : {handoff.delivery_url}
                  </Typography>
                )}
              </Stack>
            </>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}
