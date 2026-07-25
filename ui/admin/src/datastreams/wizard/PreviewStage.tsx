/**
 * Story 12.13 — Stage 5: Preview & validate (AC4, second half).
 *
 * Renders the versioned validation plan: interval / timezone, full grain, safe
 * KPI projection (12.4), DQ + rejections, the schema hash, and the frozen plan /
 * mapping versions. A "Valider le plan" action runs validation (produces the
 * plan + freezes the schema hash used for drift detection).
 *
 * DRIFT (AC4): when the source or schema hash observed now differs from the one
 * captured at validation time, the preview is marked OBSOLETE and revalidation
 * is required — this state disables activation until the operator revalidates.
 * Blocking issues also disable activation.
 *
 * WCAG: the obsolescence banner is textual + assertive-announced; the validate
 * result is announced by the parent live region; no colour-only status.
 */
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  List,
  ListItem,
  Stack,
  Typography,
} from "@mui/material";
import type { ValidationPlan } from "./wizardTypes";

interface Props {
  plan: ValidationPlan | null;
  validating: boolean;
  /** True when the source/schema drifted since validation — preview is obsolete. */
  drifted: boolean;
  onValidate: () => void;
  canValidate: boolean;
}

function formatInterval(
  interval: { start: string | null; end_exclusive: string | null } | null,
): string {
  if (!interval || !interval.start) return "—";
  const fmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeZone: "UTC" });
  const start = fmt.format(new Date(interval.start));
  const end = interval.end_exclusive ? fmt.format(new Date(interval.end_exclusive)) : "…";
  return `${start} → ${end}`;
}

export default function PreviewStage({
  plan,
  validating,
  drifted,
  onValidate,
  canValidate,
}: Props) {
  return (
    <Stack spacing={2} data-testid="stage-preview">
      <Typography color="text.secondary" variant="body2">
        Validez le plan pour figer les versions, l’empreinte de schéma et la
        projection sûre. Un changement de source ou de schéma rend l’aperçu
        obsolète et impose une nouvelle validation.
      </Typography>

      {/* M1 — honest disclosure: this validation plan is a LOCAL estimate. */}
      <Alert severity="info" data-testid="preview-local-estimate">
        Estimation locale : cet aperçu est assemblé dans le navigateur à partir
        des données déjà collectées. La classification et la qualité serveur
        (Stories 12.3 / 12.4), ainsi que les identifiants de version de plan et de
        mapping, seront calculés et figés côté serveur à la validation puis à
        l’activation.
      </Alert>

      <Box>
        <Button
          variant="contained"
          onClick={onValidate}
          disabled={!canValidate || validating}
          data-testid="preview-validate"
        >
          {validating
            ? "Validation en cours…"
            : plan
              ? "Revalider le plan"
              : "Valider le plan"}
        </Button>
      </Box>

      {/* Drift makes the preview obsolete -> revalidation required (AC4). */}
      {drifted && plan && (
        <Alert severity="warning" role="alert" data-testid="preview-drift">
          La source ou le schéma a changé depuis la dernière validation : cet
          aperçu est <strong>obsolète</strong>. Revalidez le plan avant de pouvoir
          activer.
        </Alert>
      )}

      {plan && (
        <>
          <Divider />
          <Stack spacing={1} data-testid="preview-plan">
            <Typography variant="body2">
              <strong>Intervalle :</strong> {formatInterval(plan.interval)}{" "}
              {plan.timezone ? `(${plan.timezone})` : ""}
            </Typography>
            <Typography variant="body2">
              <strong>Grain complet :</strong> {plan.full_grain.join(" · ") || "—"}
            </Typography>

            <Box>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                Projection KPI sûre
              </Typography>
              <List dense disablePadding>
                {plan.safe_kpi_projection.map((k) => (
                  <ListItem key={k.name} disableGutters sx={{ display: "block" }}>
                    <Typography variant="body2">
                      {k.name}
                      {k.expression ? ` — ${k.expression}` : ""}
                    </Typography>
                  </ListItem>
                ))}
                {plan.safe_kpi_projection.length === 0 && (
                  <Typography variant="body2" color="text.secondary">
                    Aucune projection.
                  </Typography>
                )}
              </List>
            </Box>

            <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
              <Chip
                size="small"
                color={plan.dq?.degraded ? "warning" : "success"}
                label={
                  plan.dq
                    ? plan.dq.degraded
                      ? `Qualité dégradée · ${plan.dq.total_unresolved} non résolu(s)`
                      : `Qualité OK · ${plan.dq.total_unresolved} non résolu(s)`
                    : "Qualité : indisponible"
                }
              />
              <Chip
                size="small"
                variant="outlined"
                label={`Rejets : ${plan.rejected_count ?? 0}`}
              />
            </Stack>

            <Typography
              component="code"
              variant="caption"
              data-testid="preview-schema-hash"
              sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
            >
              empreinte schéma …{(plan.schema_hash ?? "").slice(-12) || "?"}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Version de plan : {plan.plan_version_id ?? "—"} · Version de mapping :{" "}
              {plan.mapping_version_id ?? "—"}
            </Typography>

            {/* Blocking issues disable activation (AC1). */}
            {plan.blocking_issues.length > 0 && (
              <Alert severity="error" role="alert" data-testid="preview-blocking">
                <Typography variant="subtitle2">
                  Problèmes bloquants ({plan.blocking_issues.length})
                </Typography>
                <List dense disablePadding>
                  {plan.blocking_issues.map((issue) => (
                    <ListItem key={issue.code} disableGutters sx={{ display: "block" }}>
                      <Typography variant="body2">
                        <strong>{issue.code} :</strong> {issue.message}
                      </Typography>
                    </ListItem>
                  ))}
                </List>
              </Alert>
            )}
          </Stack>
        </>
      )}
    </Stack>
  );
}
