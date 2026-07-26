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
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { apiFetch } from "../lib/apiFetch";

// Story 36.8 / UX-DR27: the recommended first-report path. One source, one
// account, one recommended report, an explicit recent-period default and
// editable grain/timezone/currency, an estimated cost, an honest compatibility
// explanation, and NEVER a silent choice. WCAG 2.2 AA: keyboard/focus, an error
// summary, non-color state, a live region and a 320 px one-column reflow.

export interface RecommendedInterval {
  start: string;
  end_exclusive: string;
  window_days: number;
  label: string;
}

export interface EstimatedCost {
  read_points: number;
  unit: string;
  window_days: number;
  quota_pre_check: string;
}

export interface FirstReportCandidate {
  report_id: string;
  metrics: string[];
  dimensions: string[];
  grain: string[];
  currency: string;
  estimated_cost: EstimatedCost;
}

export interface FirstReportRecommendation {
  outcome: "recommended" | "needs_choice" | "no_safe_recommendation";
  report_id: string | null;
  metrics: string[];
  dimensions: string[];
  grain: string[];
  timezone: string;
  currency: string;
  interval: RecommendedInterval | null;
  estimated_cost: EstimatedCost | null;
  capability_contract_version: string | null;
  capability_fingerprint: string | null;
  compatibility: { reason?: string; explanation?: string };
  candidates: FirstReportCandidate[];
}

interface Props {
  projectId: string;
  datastreamId: string;
  connectionRefId: string;
  apiBase?: string;
  apiToken?: string;
  onSaved?: (draftId: string) => void;
}

const OUTCOME_LABELS: Record<string, string> = {
  recommended: "Recommandation prête",
  needs_choice: "Un choix est nécessaire",
  no_safe_recommendation: "Aucune recommandation sûre",
};

function outcomeColor(outcome: string): "success" | "warning" | "default" {
  if (outcome === "recommended") return "success";
  if (outcome === "needs_choice") return "warning";
  return "default";
}

const COMMON_TIMEZONES = ["UTC", "Europe/Paris", "Europe/London", "America/New_York"];
const COMMON_CURRENCIES = ["EUR", "USD", "GBP", "CHF", "unknown"];

function formatInterval(interval: RecommendedInterval): string {
  const fmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeZone: "UTC" });
  return `${fmt.format(new Date(interval.start))} → ${fmt.format(new Date(interval.end_exclusive))} (${interval.label})`;
}

export default function PremierRapportCard({
  projectId,
  datastreamId,
  connectionRefId,
  apiBase = "",
  apiToken = "",
  onSaved,
}: Props) {
  const [reco, setReco] = useState<FirstReportRecommendation | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  // Editable choices (grain / timezone / currency + which candidate report).
  const [reportId, setReportId] = useState<string>("");
  const [grainKey, setGrainKey] = useState<string>("");
  const [timezone, setTimezone] = useState<string>("UTC");
  const [currency, setCurrency] = useState<string>("unknown");
  const errorRef = useRef<HTMLDivElement | null>(null);
  const mounted = useRef(true);

  const headers = useCallback((): HeadersInit => ({
    ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
  }), [apiToken]);

  const applyRecommendation = useCallback((data: FirstReportRecommendation) => {
    setReco(data);
    setTimezone(data.timezone || "UTC");
    if (data.report_id) {
      // A single safe recommendation: pre-fill the exact recommended choices.
      setReportId(data.report_id);
      setGrainKey(data.grain.join(","));
      setCurrency(data.currency || "unknown");
      return;
    }
    // needs_choice: nothing is picked silently, but the first candidate seeds the
    // editable controls so the operator can review before choosing.
    const first: FirstReportCandidate | undefined = data.candidates[0];
    if (first) {
      setReportId(first.report_id);
      setGrainKey(first.grain.join(","));
      setCurrency(first.currency || "unknown");
    }
  }, []);

  const load = useCallback(async () => {
    const response = await apiFetch(
      `${apiBase}/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/first-report/recommend`,
      { method: "POST", headers: headers(), cache: "no-store" },
    );
    if (!response.ok) {
      throw new Error(response.status === 404
        ? "Aucune source autorisée n’est disponible pour ce flux."
        : `Recommandation indisponible (HTTP ${response.status}).`);
    }
    const data = await response.json() as FirstReportRecommendation;
    if (mounted.current) applyRecommendation(data);
  }, [apiBase, headers, projectId, datastreamId, applyRecommendation]);

  useEffect(() => {
    mounted.current = true;
    setLoading(true);
    load()
      .catch((reason) => setError(reason instanceof Error ? reason.message : "Recommandation indisponible."))
      .finally(() => setLoading(false));
    return () => { mounted.current = false; };
  }, [load]);

  useEffect(() => {
    if (error && errorRef.current) errorRef.current.focus();
  }, [error]);

  const activeCandidate: FirstReportCandidate | null = reco
    ? (reco.candidates.find((c) => c.report_id === reportId)
      ?? (reco.report_id === reportId
        ? {
          report_id: reco.report_id,
          metrics: reco.metrics,
          dimensions: reco.dimensions,
          grain: reco.grain,
          currency: reco.currency,
          estimated_cost: reco.estimated_cost as EstimatedCost,
        }
        : null))
    : null;

  async function save() {
    if (!reco || !reco.interval || !reportId || !grainKey) {
      setError("Choisissez un rapport, une granularité, un fuseau et une devise avant d’enregistrer.");
      return;
    }
    setSaving(true);
    setError(null);
    setAnnouncement("");
    try {
      const grain = grainKey.split(",").filter(Boolean);
      const response = await apiFetch(
        `${apiBase}/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/first-report/draft`,
        {
          method: "POST",
          headers: {
            ...headers(),
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({
            connection_ref_id: connectionRefId,
            report_id: reportId,
            metrics: activeCandidate?.metrics ?? reco.metrics,
            dimensions: activeCandidate?.dimensions ?? reco.dimensions,
            grain,
            timezone,
            currency,
            interval: { start: reco.interval.start, end_exclusive: reco.interval.end_exclusive },
          }),
        },
      );
      if (!response.ok) {
        throw new Error(response.status === 409
          ? "Ce brouillon a déjà été enregistré."
          : `Enregistrement impossible (HTTP ${response.status}).`);
      }
      const result = await response.json() as { draft_id: string };
      setAnnouncement("Brouillon enregistré. Les versions de plan et de mapping sont figées ; l’aperçu ne publie rien.");
      onSaved?.(result.draft_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Enregistrement impossible.");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: "grid", placeItems: "center", minHeight: 200 }}>
        <CircularProgress aria-label="Chargement de la recommandation" />
      </Box>
    );
  }
  if (error && !reco) {
    return (
      <Alert severity="warning" tabIndex={-1} ref={errorRef as never}>{error}</Alert>
    );
  }
  if (!reco) return null;

  const grainOptions = activeCandidate
    ? [activeCandidate.grain]
    : reco.candidates.map((c) => c.grain);

  return (
    <Card component="section" variant="outlined" sx={{ borderRadius: 3, maxWidth: 760 }}>
      <CardContent>
        <Stack spacing={2}>
          <Box>
            <Chip
              size="small"
              color={outcomeColor(reco.outcome)}
              label={OUTCOME_LABELS[reco.outcome] ?? reco.outcome}
              sx={{ mb: 1 }}
            />
            <Typography component="h2" variant="h5">Premier rapport recommandé</Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>
              {reco.compatibility.explanation
                ?? "Une source, un compte, un rapport et une période récente bornée."}
            </Typography>
          </Box>

          {/* Error summary: focusable, non-color (icon + text), announced. */}
          <Box role="alert" aria-live="assertive" ref={errorRef} tabIndex={-1} sx={{ outline: "none" }}>
            {error && <Alert severity="error">{error}</Alert>}
          </Box>
          <Box role="status" aria-live="polite" aria-atomic="true" sx={{ minHeight: 24 }}>
            {announcement}
          </Box>

          {reco.outcome === "no_safe_recommendation" ? (
            <Alert severity="info">
              {reco.compatibility.explanation
                ?? "Aucune combinaison sûre n’est possible pour l’instant. Vérifiez l’exposition du compte et la source."}
            </Alert>
          ) : (
            <>
              {reco.outcome === "needs_choice" && (
                <Alert severity="warning">
                  {reco.compatibility.explanation
                    ?? "Plusieurs options sont compatibles. Aucun choix n’est fait automatiquement."}
                </Alert>
              )}

              <Stack
                data-testid="first-report-fields"
                direction={{ xs: "column", md: "row" }}
                spacing={2}
                sx={{ flexWrap: "wrap" }}
              >
                <TextField
                  select
                  fullWidth
                  label="Rapport"
                  value={reportId}
                  onChange={(e) => {
                    const next = e.target.value;
                    setReportId(next);
                    const cand = reco.candidates.find((c) => c.report_id === next);
                    if (cand) {
                      setGrainKey(cand.grain.join(","));
                      setCurrency(cand.currency || "unknown");
                    }
                  }}
                  helperText="Choisi explicitement, jamais imposé."
                  sx={{ minWidth: 200 }}
                >
                  {(reco.candidates.length
                    ? reco.candidates
                    : reco.report_id
                      ? [{ report_id: reco.report_id } as FirstReportCandidate]
                      : []
                  ).map((c) => (
                    <MenuItem key={c.report_id} value={c.report_id}>{c.report_id}</MenuItem>
                  ))}
                </TextField>

                <TextField
                  select
                  fullWidth
                  label="Granularité"
                  value={grainKey}
                  onChange={(e) => setGrainKey(e.target.value)}
                  helperText="Uniquement des granularités supportées par le rapport."
                  sx={{ minWidth: 200 }}
                >
                  {grainOptions.map((g) => (
                    <MenuItem key={g.join(",")} value={g.join(",")}>{g.join(" · ") || "—"}</MenuItem>
                  ))}
                </TextField>

                <TextField
                  select
                  fullWidth
                  label="Fuseau horaire"
                  value={timezone}
                  onChange={(e) => setTimezone(e.target.value)}
                  sx={{ minWidth: 200 }}
                >
                  {COMMON_TIMEZONES.map((tz) => (
                    <MenuItem key={tz} value={tz}>{tz}</MenuItem>
                  ))}
                </TextField>

                <TextField
                  select
                  fullWidth
                  label="Devise"
                  value={currency}
                  onChange={(e) => setCurrency(e.target.value)}
                  helperText={currency === "unknown"
                    ? "Devise à confirmer : elle n’est pas déduite automatiquement."
                    : "Devise explicite du rapport."}
                  sx={{ minWidth: 200 }}
                >
                  {[...new Set([currency, ...COMMON_CURRENCIES])].map((c) => (
                    <MenuItem key={c} value={c}>{c}</MenuItem>
                  ))}
                </TextField>
              </Stack>

              <Divider />

              <Stack spacing={0.5}>
                {reco.interval && (
                  <Typography variant="body2">
                    <strong>Période récente :</strong> {formatInterval(reco.interval)}
                  </Typography>
                )}
                {activeCandidate?.estimated_cost && (
                  <Typography variant="body2">
                    <strong>Coût estimé :</strong>{" "}
                    {activeCandidate.estimated_cost.read_points} {activeCandidate.estimated_cost.unit}
                    {" · "}quota {activeCandidate.estimated_cost.quota_pre_check}
                  </Typography>
                )}
                {reco.capability_fingerprint && (
                  <Typography
                    component="code"
                    variant="caption"
                    sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
                  >
                    catalogue {reco.capability_contract_version ?? "?"} · empreinte …{reco.capability_fingerprint.slice(-12)}
                  </Typography>
                )}
              </Stack>

              <Box>
                <Button
                  variant="contained"
                  disabled={saving || !reportId}
                  onClick={save}
                >
                  {saving ? "Enregistrement…" : "Enregistrer le brouillon"}
                </Button>
              </Box>
            </>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}
