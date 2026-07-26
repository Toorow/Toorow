import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  List,
  ListItem,
  Stack,
  Typography,
} from "@mui/material";
import { apiFetch } from "../lib/apiFetch";

// Story 36.10 / UX-DR28 + UX-DR30: ONE honest first-pull progress + report
// readiness view. Named phases with interval / rows / attempts / current
// publication / next action; an EXPLICIT degraded state (non-color, explicit
// text, never "prêt" when degraded); and a host-connection CTA whose
// enabled/disabled/degraded state comes STRAIGHT from the server readiness result
// (overall/host_cta) -- the UI NEVER re-infers it. Bounded polling with backoff
// that stops on a terminal overall; an aria-live region that announces state
// changes without noise (keyed on readiness_version); stable focus; 320 px
// one-column reflow. WCAG 2.2 AA.

export type PhaseStateValue =
  | "waiting"
  | "running"
  | "succeeded"
  | "degraded"
  | "failed"
  | "blocked";

export interface ReadinessPhase {
  phase: string;
  state: PhaseStateValue;
  interval: { start?: string; end_exclusive?: string } | null;
  rows: number | null;
  attempts: number;
  live_publication_execution_id: string | null;
  next_action: { label: string; owner: string } | null;
  detail?: Record<string, unknown>;
}

export interface FirstReportReadiness {
  schema_version: string;
  readiness_version: string;
  datastream_id: string;
  project_id: string;
  overall: "ready" | "degraded" | "blocked";
  host_cta: "enabled" | "disabled" | "degraded";
  headline: string;
  current_publication: { execution_id: string; row_count: number | null; published_at: string | null } | null;
  selected_objects: {
    report_id: string | null;
    metrics: string[];
    dimensions: string[];
    grain: string[];
    timezone: string | null;
    currency: string | null;
  };
  recent_coverage: { state: string } | null;
  historical_coverage: { state: string } | null;
  freshness: { last_pull_at: string | null; connection_health: string | null } | null;
  verification: { verdict: string | null; row_count: number | null } | null;
  dq: {
    total_unresolved: number;
    monitors_unavailable: boolean;
    degraded: boolean;
    evaluated_days_30d?: number;
    issue_count?: number;
  } | null;
  mapping: { mapping_version_id: string; version_number: number; mapping_contract_version: string } | null;
  provenance: { content_hash: string | null; source_schema_hash: string | null } | null;
  exclusions: { kind: string; reason: string | null }[];
  last_successful_pull: { execution_id: string | null; row_count: number | null; at: string | null } | null;
  phases: ReadinessPhase[];
}

interface Props {
  projectId: string;
  datastreamId: string;
  apiBase?: string;
  apiToken?: string;
  onConnectHost?: () => void;
}

const PHASE_LABELS: Record<string, string> = {
  selection: "Sélection du rapport",
  recent_pull: "Pull récent",
  verification: "Vérification",
  publication: "Publication",
  data_quality: "Qualité des données",
  history: "Historique (arrière-plan)",
};

// Non-color labels: every state carries an explicit textual token (WCAG: state is
// never conveyed by color alone).
const PHASE_STATE_LABELS: Record<PhaseStateValue, string> = {
  waiting: "En attente",
  running: "En cours",
  succeeded: "Terminé",
  degraded: "Dégradé",
  failed: "Échec",
  blocked: "Bloqué",
};

function phaseColor(state: PhaseStateValue): "default" | "info" | "success" | "warning" | "error" {
  if (state === "succeeded") return "success";
  if (state === "running") return "info";
  if (state === "degraded") return "warning";
  if (state === "failed" || state === "blocked") return "error";
  return "default";
}

const OVERALL_LABELS: Record<string, string> = {
  ready: "Prêt",
  degraded: "Dégradé (utilisable)",
  blocked: "Indisponible",
};

function overallColor(overall: string): "success" | "warning" | "error" {
  if (overall === "ready") return "success";
  if (overall === "degraded") return "warning";
  return "error";
}

// Bounded polling: keep polling while the overall is non-terminal (still loading),
// with backoff, and STOP once the readiness reaches a terminal disposition.
const POLL_MIN_MS = 4000;
const POLL_MAX_MS = 30000;
const POLL_MAX_ATTEMPTS = 40; // hard ceiling so we never poll forever.

function isTerminalOverall(r: FirstReportReadiness): boolean {
  // Ready/blocked are terminal. Degraded is terminal too (recent is published;
  // history/DQ may still improve but the CTA is already decided) UNLESS a phase is
  // still running (recent pull / verification loading).
  const running = r.phases.some((p) => p.state === "running" || p.state === "waiting");
  if (r.overall === "blocked" && !running) return true;
  if (r.overall === "ready") return true;
  if (r.overall === "degraded" && !running) return true;
  return false;
}

function formatInterval(interval: { start?: string; end_exclusive?: string } | null): string | null {
  if (!interval || !interval.start) return null;
  const fmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeZone: "UTC" });
  const start = fmt.format(new Date(interval.start));
  const end = interval.end_exclusive ? fmt.format(new Date(interval.end_exclusive)) : "…";
  return `${start} → ${end}`;
}

export default function RapportPretCard({
  projectId,
  datastreamId,
  apiBase = "",
  apiToken = "",
  onConnectHost,
}: Props) {
  const [readiness, setReadiness] = useState<FirstReportReadiness | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");

  const errorRef = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attempts = useRef(0);
  const lastVersion = useRef<string | null>(null);

  const headers = useCallback((): HeadersInit => ({
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  }), [apiToken]);

  const fetchOnce = useCallback(async (): Promise<FirstReportReadiness> => {
    const response = await apiFetch(
      `${apiBase}/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/first-report/readiness`,
      { method: "GET", headers: headers(), cache: "no-store" },
    );
    if (!response.ok) {
      throw new Error(response.status === 404
        ? "Ce flux est introuvable ou vous n’y avez pas accès."
        : `État de préparation indisponible (HTTP ${response.status}).`);
    }
    return await response.json() as FirstReportReadiness;
  }, [apiBase, headers, projectId, datastreamId]);

  // Bounded, backoff polling loop. Stops on terminal overall or the ceiling.
  const scheduleNext = useCallback((delay: number) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void poll(delay), delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const poll = useCallback(async (prevDelay: number) => {
    if (!mounted.current) return;
    try {
      const data = await fetchOnce();
      if (!mounted.current) return;
      setReadiness(data);
      setError(null);
      // Announce ONLY when the versioned truth actually changed (avoids noise).
      if (lastVersion.current && lastVersion.current !== data.readiness_version) {
        setAnnouncement(`Mise à jour : ${data.headline}`);
      }
      lastVersion.current = data.readiness_version;
      attempts.current += 1;
      if (isTerminalOverall(data) || attempts.current >= POLL_MAX_ATTEMPTS) {
        return; // terminal -> stop polling (bounded).
      }
      const next = Math.min(POLL_MAX_MS, Math.max(POLL_MIN_MS, Math.floor(prevDelay * 1.5)));
      scheduleNext(next);
    } catch (reason) {
      if (!mounted.current) return;
      setError(reason instanceof Error ? reason.message : "État de préparation indisponible.");
    }
  }, [fetchOnce, scheduleNext]);

  useEffect(() => {
    mounted.current = true;
    attempts.current = 0;
    setLoading(true);
    fetchOnce()
      .then((data) => {
        if (!mounted.current) return;
        setReadiness(data);
        lastVersion.current = data.readiness_version;
        attempts.current = 1;
        if (!isTerminalOverall(data)) scheduleNext(POLL_MIN_MS);
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "État de préparation indisponible."))
      .finally(() => { if (mounted.current) setLoading(false); });
    return () => {
      mounted.current = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [fetchOnce, scheduleNext]);

  useEffect(() => {
    if (error && errorRef.current) errorRef.current.focus();
  }, [error]);

  if (loading && !readiness) {
    return (
      <Box sx={{ display: "grid", placeItems: "center", minHeight: 200 }}>
        <CircularProgress aria-label="Chargement de l’état de préparation" />
      </Box>
    );
  }
  if (error && !readiness) {
    return (
      <Alert severity="warning" tabIndex={-1} ref={errorRef as never}>{error}</Alert>
    );
  }
  if (!readiness) return null;

  const r = readiness;
  // The CTA state is SERVER-derived (host_cta). The UI NEVER re-infers it from
  // colours or its own logic -- it renders exactly what the server decided.
  const ctaDisabled = r.host_cta === "disabled";
  const ctaDegraded = r.host_cta === "degraded";

  return (
    <Card component="section" variant="outlined" sx={{ borderRadius: 3, maxWidth: 760 }}>
      <CardContent>
        <Stack spacing={2}>
          <Box>
            <Chip
              size="small"
              color={overallColor(r.overall)}
              label={OVERALL_LABELS[r.overall] ?? r.overall}
              sx={{ mb: 1 }}
              data-testid="readiness-overall"
            />
            <Typography component="h2" variant="h5">État du premier rapport</Typography>
            {/* Honest headline: never says "prêt" unless overall === ready. */}
            <Typography color="text.secondary" sx={{ mt: 0.5 }} data-testid="readiness-headline">
              {r.headline}
            </Typography>
          </Box>

          {/* Live region: polite, atomic, announces only real version changes. */}
          <Box
            role="status"
            aria-live="polite"
            aria-atomic="true"
            data-testid="readiness-live"
            sx={{ minHeight: 24 }}
          >
            {announcement}
          </Box>
          {/* Error region: assertive, focusable, non-color (icon + text). */}
          <Box role="alert" aria-live="assertive" ref={errorRef} tabIndex={-1} sx={{ outline: "none" }}>
            {error && <Alert severity="error">{error}</Alert>}
          </Box>

          {/* Explicit degraded banner -- honest, textual, never a bare colour. */}
          {r.overall === "degraded" && (
            <Alert severity="warning" data-testid="readiness-degraded-banner">
              Rapport dégradé mais utilisable : le résultat récent est publié.
              L’historique ou la qualité des données reste partiel — ce rapport
              n’est donc pas présenté comme entièrement disponible.
            </Alert>
          )}
          {r.overall === "blocked" && (
            <Alert severity="error" data-testid="readiness-blocked-banner">
              Aucun résultat récent n’est publié : la connexion de l’hôte reste
              indisponible tant que le pull récent n’a pas réussi.
            </Alert>
          )}

          <Divider />

          {/* Named phases (UX-DR28): interval / rows / attempts / next action. */}
          <List dense disablePadding data-testid="readiness-phases">
            {r.phases.map((p) => {
              const interval = formatInterval(p.interval);
              return (
                <ListItem
                  key={p.phase}
                  disableGutters
                  sx={{ display: "block", py: 1, borderBottom: "1px solid", borderColor: "divider" }}
                >
                  <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ alignItems: { sm: "center" } }}>
                    <Chip
                      size="small"
                      color={phaseColor(p.state)}
                      label={PHASE_STATE_LABELS[p.state]}
                      sx={{ minWidth: 92 }}
                    />
                    <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
                      {PHASE_LABELS[p.phase] ?? p.phase}
                    </Typography>
                  </Stack>
                  <Stack spacing={0.25} sx={{ mt: 0.5, pl: { sm: "100px" } }}>
                    {interval && (
                      <Typography variant="body2" color="text.secondary">
                        Intervalle : {interval}
                      </Typography>
                    )}
                    {p.rows != null && (
                      <Typography variant="body2" color="text.secondary">
                        Lignes : {p.rows.toLocaleString("fr-FR")}
                      </Typography>
                    )}
                    <Typography variant="body2" color="text.secondary">
                      Tentatives : {p.attempts}
                    </Typography>
                    {p.live_publication_execution_id && (
                      <Typography
                        component="code"
                        variant="caption"
                        sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
                      >
                        publication en cours : {p.live_publication_execution_id}
                      </Typography>
                    )}
                    {p.next_action && (
                      <Typography variant="body2">
                        <strong>Action :</strong> {p.next_action.label}
                      </Typography>
                    )}
                  </Stack>
                </ListItem>
              );
            })}
          </List>

          {/* Provenance + coverage + exclusions (UX-DR30). */}
          <Stack spacing={0.5}>
            {r.freshness?.last_pull_at && (
              <Typography variant="body2">
                <strong>Fraîcheur :</strong> dernier pull {formatInterval({ start: r.freshness.last_pull_at }) ?? r.freshness.last_pull_at}
              </Typography>
            )}
            {r.mapping && (
              <Typography variant="body2">
                <strong>Mapping :</strong> version {r.mapping.version_number} (contrat {r.mapping.mapping_contract_version})
              </Typography>
            )}
            {r.provenance?.content_hash && (
              <Typography
                component="code"
                variant="caption"
                sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
              >
                empreinte contenu …{r.provenance.content_hash.slice(-12)}
              </Typography>
            )}
            {r.exclusions.length > 0 && (
              <Typography variant="body2" color="text.secondary">
                <strong>Exclusions connues :</strong>{" "}
                {r.exclusions.map((e) => e.kind).join(", ")}
              </Typography>
            )}
          </Stack>

          <Divider />

          {/* Host CTA -- state comes STRAIGHT from the server (host_cta). */}
          <Box>
            <Button
              variant={ctaDegraded ? "outlined" : "contained"}
              color={ctaDegraded ? "warning" : "primary"}
              disabled={ctaDisabled}
              onClick={onConnectHost}
              data-testid="readiness-connect-host"
            >
              {ctaDisabled
                ? "Connexion de l’hôte indisponible"
                : ctaDegraded
                  ? "Connecter un hôte (rapport dégradé)"
                  : "Connecter un hôte MCP"}
            </Button>
            {ctaDegraded && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                Vous pouvez connecter un hôte, mais le rapport est présenté comme dégradé.
              </Typography>
            )}
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}
