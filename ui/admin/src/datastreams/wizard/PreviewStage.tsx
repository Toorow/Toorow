/** Stage 5: render and validate the server-owned immutable plan. */
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
  drifted: boolean;
  onValidate: () => void;
  canValidate: boolean;
}

function formatInterval(
  interval: { start: string | null; end_exclusive: string | null } | null,
): string {
  if (!interval || !interval.start) return "-";
  const formatter = new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeZone: "UTC",
  });
  const start = formatter.format(new Date(interval.start));
  const end = interval.end_exclusive
    ? formatter.format(new Date(interval.end_exclusive))
    : "open";
  return `${start} to ${end}`;
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
        Save and validate the exact plan against the current server capabilities.
        Any later configuration change requires revalidation.
      </Typography>

      <Box>
        <Button
          variant="contained"
          onClick={onValidate}
          disabled={!canValidate || validating}
          data-testid="preview-validate"
        >
          {validating ? "Validating..." : plan ? "Revalidate plan" : "Validate plan"}
        </Button>
      </Box>

      {drifted && plan && (
        <Alert severity="warning" role="alert" data-testid="preview-drift">
          The configuration changed after validation. This preview is obsolete;
          revalidate before activation.
        </Alert>
      )}

      {plan && (
        <>
          <Divider />
          <Stack spacing={1} data-testid="preview-plan">
            <Typography variant="body2">
              <strong>Interval:</strong> {formatInterval(plan.interval)}{" "}
              {plan.timezone ? `(${plan.timezone})` : ""}
            </Typography>
            <Typography variant="body2">
              <strong>Full grain:</strong> {plan.full_grain.join(" / ") || "-"}
            </Typography>

            <Box>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                Server-certified KPI projection
              </Typography>
              <List dense disablePadding>
                {plan.safe_kpi_projection.map((kpi) => (
                  <ListItem key={kpi.name} disableGutters sx={{ display: "block" }}>
                    <Typography variant="body2">
                      {kpi.name}
                      {kpi.expression ? ` - ${kpi.expression}` : ""}
                    </Typography>
                  </ListItem>
                ))}
                {plan.safe_kpi_projection.length === 0 && (
                  <Typography variant="body2" color="text.secondary">
                    Not provided by this validation endpoint.
                  </Typography>
                )}
              </List>
            </Box>

            <Stack direction="row" spacing={1} sx={{ flexWrap: "wrap", gap: 1 }}>
              {plan.dq && (
                <Chip
                  size="small"
                  color={plan.dq.degraded ? "warning" : "default"}
                  label={`Server DQ: ${plan.dq.total_unresolved} unresolved`}
                />
              )}
              {plan.rejected_count != null && (
                <Chip
                  size="small"
                  variant="outlined"
                  label={`Import preview rejected rows: ${plan.rejected_count}`}
                />
              )}
            </Stack>

            <Typography
              component="code"
              variant="caption"
              data-testid="preview-intent-content-hash"
              sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
            >
              Intent content hash ...
              {(plan.intent_content_hash ?? "").slice(-12) || "not supplied"}
            </Typography>
            <Typography
              component="code"
              variant="caption"
              data-testid="preview-capability-fingerprint"
              sx={{ fontFamily: "var(--font-mono, monospace)", overflowWrap: "anywhere" }}
            >
              Capability contract: {plan.capability_contract_version ?? "not applicable"} /{" "}
              fingerprint ...
              {(plan.capability_fingerprint ?? "").slice(-12) || "not applicable"}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Plan version: {plan.plan_version_id ?? "-"} / Mapping version:{" "}
              {plan.mapping_version_id ?? "not created"}
            </Typography>

            {plan.blocking_issues.length > 0 && (
              <Alert severity="error" role="alert" data-testid="preview-blocking">
                <Typography variant="subtitle2">
                  Blocking issues ({plan.blocking_issues.length})
                </Typography>
                <List dense disablePadding>
                  {plan.blocking_issues.map((issue) => (
                    <ListItem key={issue.code} disableGutters sx={{ display: "block" }}>
                      <Typography variant="body2">
                        <strong>{issue.code}:</strong> {issue.message}
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
