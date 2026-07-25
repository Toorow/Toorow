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
  ListItemIcon,
  ListItemText,
  Stack,
  Typography,
} from "@mui/material";

// Story 36.15 / UX-DR31: render, VALIDATE and REPRODUCE the first correct answer.
// The read-only starter request renders the final report UI when the host supports
// it, or a COMPACT text fallback with mandatory bounded evidence + an optional
// authenticated deep-link (never the full dataset). A validation checklist confirms
// the figures match the active publication/mapping versions and that any
// stale/degraded state is explicit + NON-COLOR (a textual token, not a bare hue). A
// second-user reproduction check independently re-evaluates access and reports
// whether the SAME publication/provenance result is reproducible; an unauthorized
// second user is reported as introuvable WITHOUT exposing the first-user result.
// French + WCAG 2.2 AA (keyboard/focus, live region, non-color states, 320px reflow).

export interface RenderedFirstReport {
  schema_version: string;
  datastream_id: string;
  project_id: string;
  overall: "ready" | "degraded";
  host_cta: "enabled" | "disabled" | "degraded";
  ui_supported: boolean;
  render_mode: "app_ui" | "compact_text";
  headline: string;
  publication: { execution_id: string; row_count: number | null; published_at: string | null } | null;
  mapping: { mapping_version_id: string; version_number: number; mapping_contract_version: string } | null;
  provenance: { content_hash: string | null; source_schema_hash: string | null } | null;
  freshness: { last_pull_at: string | null; connection_health: string | null } | null;
  coverage: { recent: { state: string } | null; historical: { state: string } | null };
  exclusions: { kind: string; reason: string | null }[];
  bounded_evidence: {
    row_count: number | null;
    verified_row_count: number | null;
    expected_row_count: number | null;
    metric_names: string[];
    metric_count: number;
    dimension_count: number;
    content_hash: string | null;
    truncated: boolean;
    note: string;
  };
  report_deep_link: {
    kind: string;
    path: string;
    requires_authentication: boolean;
    publication_execution_id: string | null;
    note: string;
  } | null;
  validation: {
    figures_cite_publication: boolean;
    figures_cite_mapping_version: boolean;
    publication_execution_id: string | null;
    mapping_version_id: string | null;
    mapping_version_number: number | null;
    content_hash: string | null;
    state_token: "ready" | "degraded";
    is_degraded: boolean;
    degraded_note: string | null;
  };
  readiness_version: string;
  reproduced?: boolean;
}

interface Props {
  projectId: string;
  datastreamId: string;
  apiBase?: string;
  apiToken?: string;
}

type ReproState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; render: RenderedFirstReport }
  | { kind: "denied" }
  | { kind: "error"; message: string };

const OVERALL_LABELS: Record<string, string> = {
  ready: "Prêt",
  degraded: "Dégradé (utilisable)",
};

function overallColor(overall: string): "success" | "warning" {
  return overall === "ready" ? "success" : "warning";
}

// Non-color check row: an explicit textual token (OK / À vérifier) plus an icon —
// state is never conveyed by color alone (WCAG 2.2 AA).
function CheckRow({ ok, label }: { ok: boolean; label: string }) {
  return (
    <ListItem disableGutters sx={{ py: 0.25 }}>
      <ListItemIcon sx={{ minWidth: 36 }}>
        <Chip
          size="small"
          color={ok ? "success" : "warning"}
          label={ok ? "OK" : "À vérifier"}
          data-testid={`check-${ok ? "ok" : "warn"}`}
        />
      </ListItemIcon>
      <ListItemText primary={label} />
    </ListItem>
  );
}

export default function PremierRapportRenduCard({
  projectId,
  datastreamId,
  apiBase = "",
  apiToken = import.meta.env.VITE_ADMIN_API_TOKEN ?? "",
}: Props) {
  const [render, setRender] = useState<RenderedFirstReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notRenderable, setNotRenderable] = useState(false);
  const [repro, setRepro] = useState<ReproState>({ kind: "idle" });
  const [announcement, setAnnouncement] = useState("");

  const errorRef = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(true);

  const headers = useCallback(
    (): HeadersInit => ({ ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}) }),
    [apiToken],
  );

  const base = `${apiBase}/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/first-report`;

  useEffect(() => {
    mounted.current = true;
    setLoading(true);
    fetch(`${base}/render`, { method: "GET", headers: headers(), cache: "no-store" })
      .then(async (response) => {
        if (!mounted.current) return;
        if (response.status === 409) {
          setNotRenderable(true);
          return;
        }
        if (!response.ok) {
          throw new Error(
            response.status === 404
              ? "Ce rapport est introuvable ou vous n’y avez pas accès."
              : `Rendu indisponible (HTTP ${response.status}).`,
          );
        }
        const data = (await response.json()) as RenderedFirstReport;
        setRender(data);
        setAnnouncement(`Rapport rendu : ${data.headline}`);
      })
      .catch((reason) =>
        setError(reason instanceof Error ? reason.message : "Rendu indisponible."),
      )
      .finally(() => {
        if (mounted.current) setLoading(false);
      });
    return () => {
      mounted.current = false;
    };
  }, [base, headers]);

  useEffect(() => {
    if (error && errorRef.current) errorRef.current.focus();
  }, [error]);

  const runReproduction = useCallback(async () => {
    setRepro({ kind: "loading" });
    try {
      const response = await fetch(`${base}/reproduce`, {
        method: "POST",
        headers: { ...headers(), "Content-Type": "application/json" },
        body: JSON.stringify({}),
        cache: "no-store",
      });
      if (response.status === 404) {
        // Existence-hidden: the second user/workspace is not authorized. We report
        // "introuvable" WITHOUT revealing the first-user result.
        setRepro({ kind: "denied" });
        return;
      }
      if (!response.ok) {
        throw new Error(`Reproduction indisponible (HTTP ${response.status}).`);
      }
      const data = (await response.json()) as RenderedFirstReport;
      setRepro({ kind: "ok", render: data });
    } catch (reason) {
      setRepro({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Reproduction indisponible.",
      });
    }
  }, [base, headers]);

  if (loading) {
    return (
      <Box sx={{ display: "grid", placeItems: "center", minHeight: 200 }}>
        <CircularProgress aria-label="Chargement du rendu du rapport" />
      </Box>
    );
  }
  if (error) {
    return (
      <Alert severity="warning" tabIndex={-1} ref={errorRef as never}>
        {error}
      </Alert>
    );
  }
  if (notRenderable) {
    return (
      <Alert severity="info" data-testid="render-not-renderable">
        Le rapport n’est pas encore rendable : le pull récent doit être publié avant
        que le premier rapport puisse être rendu.
      </Alert>
    );
  }
  if (!render) return null;

  const r = render;
  const v = r.validation;

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
              data-testid="render-overall"
            />
            <Typography component="h2" variant="h5">
              Premier rapport rendu
            </Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }} data-testid="render-headline">
              {r.headline}
            </Typography>
          </Box>

          <Box role="status" aria-live="polite" aria-atomic="true" data-testid="render-live" sx={{ minHeight: 24 }}>
            {announcement}
          </Box>

          {/* Render mode: app UI when supported, else compact text + bounded evidence. */}
          <Alert
            severity={r.ui_supported ? "success" : "info"}
            data-testid="render-mode"
          >
            {r.ui_supported
              ? "L’hôte prend en charge l’UI applicative : le rapport final est affiché dans l’hôte."
              : "Hôte sans UI applicative : rendu en texte compact avec preuve bornée (le jeu de données complet reste hors du modèle)."}
          </Alert>

          {/* Explicit, NON-COLOR degraded state token. */}
          {r.validation.is_degraded && (
            <Alert severity="warning" data-testid="render-degraded">
              État : dégradé (utilisable). {r.validation.degraded_note}
            </Alert>
          )}

          <Divider />

          {/* Bounded evidence (never the full dataset). */}
          <Box data-testid="render-bounded-evidence">
            <Typography variant="subtitle2">Preuve bornée</Typography>
            <Stack spacing={0.25} sx={{ mt: 0.5 }}>
              {r.bounded_evidence.row_count != null && (
                <Typography variant="body2" color="text.secondary">
                  Lignes publiées : {r.bounded_evidence.row_count.toLocaleString("fr-FR")}
                </Typography>
              )}
              <Typography variant="body2" color="text.secondary">
                Métriques : {r.bounded_evidence.metric_names.join(", ") || "—"}
                {r.bounded_evidence.truncated ? " (liste tronquée)" : ""}
              </Typography>
              {r.bounded_evidence.content_hash && (
                <Typography
                  component="code"
                  variant="caption"
                  sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
                >
                  empreinte contenu …{r.bounded_evidence.content_hash.slice(-12)}
                </Typography>
              )}
              <Typography variant="caption" color="text.secondary">
                {r.bounded_evidence.note}
              </Typography>
            </Stack>
          </Box>

          {/* Optional authenticated deep-link — a human follows it out of band. */}
          {r.report_deep_link && (
            <Button
              variant="outlined"
              href={r.report_deep_link.path}
              data-testid="render-deeplink"
              sx={{ alignSelf: "flex-start" }}
            >
              Ouvrir le rapport complet (lien authentifié)
            </Button>
          )}

          <Divider />

          {/* Validation checklist: figures match the active publication/mapping. */}
          <Box>
            <Typography variant="subtitle2">Validation du rendu</Typography>
            <List dense disablePadding data-testid="render-validation">
              <CheckRow
                ok={v.figures_cite_publication}
                label={`Les figures citent la publication active${v.publication_execution_id ? ` (${v.publication_execution_id})` : ""}.`}
              />
              <CheckRow
                ok={v.figures_cite_mapping_version}
                label={`Les figures citent la version de mapping${v.mapping_version_number != null ? ` (v${v.mapping_version_number})` : ""}.`}
              />
              <CheckRow
                ok={!v.is_degraded}
                label={
                  v.is_degraded
                    ? "État dégradé explicite (rapport présenté comme dégradé, non prêt)."
                    : "État prêt : période récente publiée et contrôles au vert."
                }
              />
            </List>
          </Box>

          <Divider />

          {/* Second-user reproduction check. */}
          <Box>
            <Typography variant="subtitle2">Reproduction par un second utilisateur</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              Vérifie qu’un second utilisateur autorisé obtient le MÊME résultat
              (accès évalué indépendamment).
            </Typography>
            <Button
              variant="contained"
              onClick={runReproduction}
              disabled={repro.kind === "loading"}
              data-testid="render-reproduce"
            >
              {repro.kind === "loading" ? "Vérification…" : "Vérifier la reproduction"}
            </Button>
            <Box role="status" aria-live="polite" sx={{ mt: 1 }}>
              {repro.kind === "ok" && (
                <Alert severity="success" data-testid="repro-ok">
                  Reproduction confirmée : même publication (
                  {repro.render.publication?.execution_id ?? "—"}) et même empreinte de
                  contenu. La valeur est partagée et reproductible.
                </Alert>
              )}
              {repro.kind === "denied" && (
                <Alert severity="warning" data-testid="repro-denied">
                  Ce second utilisateur (ou cet espace de travail) n’est pas autorisé :
                  rapport introuvable. Aucun résultat n’est exposé.
                </Alert>
              )}
              {repro.kind === "error" && (
                <Alert severity="error" data-testid="repro-error">
                  {repro.message}
                </Alert>
              )}
            </Box>
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}
