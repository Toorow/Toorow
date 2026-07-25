/**
 * Story 12.13 — Stage 6: Schedule & activate (AC1).
 *
 * Product-facing cadence modes only: manual / daily / hourly (12.2 AC5). Hourly
 * requires an explicit allow_hourly opt-in (12.10 quota gate). The IANA timezone
 * is chosen here. The final action is DISABLED whenever blocking validation
 * exists or the preview is obsolete (drift) — the disabled reason is stated
 * plainly.
 *
 * HONEST PHASE B (C2): there is no versioned enable/activate route yet — the
 * server persists the flow disabled (enabled:FALSE) and PATCH enabled:true
 * returns `activation_not_available` (Story 12.6). So the final action does NOT
 * claim "Flux activé"; it saves the flow READY to activate and states that
 * scheduled mise-en-service is Phase B. The parent shell owns the actual call.
 */
import {
  Alert,
  Box,
  Button,
  MenuItem,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import {
  type CadenceMode,
  COMMON_TIMEZONES,
  type ScheduleConfig,
} from "./wizardTypes";

interface Props {
  schedule: ScheduleConfig;
  onScheduleChange: (schedule: ScheduleConfig) => void;
  /** Reasons that block activation (empty => activation allowed). */
  blockingReasons: string[];
  activating: boolean;
  onActivate: () => void;
}

const CADENCE_LABELS: Record<CadenceMode, string> = {
  manual: "Manuel",
  daily: "Quotidien",
  hourly: "Horaire",
};

export default function ScheduleStage({
  schedule,
  onScheduleChange,
  blockingReasons,
  activating,
  onActivate,
}: Props) {
  const activationBlocked = blockingReasons.length > 0;

  return (
    <Stack spacing={2} data-testid="stage-schedule">
      <Typography color="text.secondary" variant="body2">
        Choisissez la cadence et le fuseau. Les modes disponibles sont manuel,
        quotidien et horaire ; l’horaire exige un accord explicite (quota).
      </Typography>

      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ flexWrap: "wrap" }}>
        <TextField
          select
          label="Cadence"
          value={schedule.cadence_mode}
          onChange={(e) =>
            onScheduleChange({ ...schedule, cadence_mode: e.target.value as CadenceMode })
          }
          data-testid="schedule-cadence"
          sx={{ minWidth: 200 }}
        >
          {(["manual", "daily", "hourly"] as CadenceMode[]).map((mode) => (
            <MenuItem
              key={mode}
              value={mode}
              disabled={mode === "hourly" && !schedule.allow_hourly}
            >
              {CADENCE_LABELS[mode]}
              {mode === "hourly" && !schedule.allow_hourly ? " (quota requis)" : ""}
            </MenuItem>
          ))}
        </TextField>

        <TextField
          select
          label="Fuseau horaire"
          value={schedule.timezone}
          onChange={(e) => onScheduleChange({ ...schedule, timezone: e.target.value })}
          data-testid="schedule-timezone"
          sx={{ minWidth: 200 }}
        >
          {COMMON_TIMEZONES.map((tz) => (
            <MenuItem key={tz} value={tz}>
              {tz}
            </MenuItem>
          ))}
        </TextField>
      </Stack>

      <Box>
        <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={schedule.allow_hourly}
            data-testid="schedule-allow-hourly"
            onChange={(e) =>
              onScheduleChange({
                ...schedule,
                allow_hourly: e.target.checked,
                cadence_mode:
                  !e.target.checked && schedule.cadence_mode === "hourly"
                    ? "daily"
                    : schedule.cadence_mode,
              })
            }
          />
          <Typography variant="body2">
            Autoriser la cadence horaire (consommation de quota accrue)
          </Typography>
        </label>
      </Box>

      {/* Blocking validation disables the final action, with a plain reason (AC1). */}
      {activationBlocked && (
        <Alert severity="warning" role="alert" data-testid="activation-blocked">
          <Typography variant="subtitle2">
            Enregistrement du flux prêt à activer impossible pour l’instant
          </Typography>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {blockingReasons.map((reason) => (
              <li key={reason}>
                <Typography variant="body2" component="span">
                  {reason}
                </Typography>
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {/* HONEST PHASE B: no versioned enable seam yet (Story 12.6). */}
      <Alert severity="info" data-testid="activation-phaseb">
        La mise en service planifiée (activation automatique du flux) arrive en
        Phase B (Story 12.6). Pour l’instant, cet assistant enregistre le flux{" "}
        <strong>prêt à activer</strong> : il reste désactivé côté serveur jusqu’à
        l’ouverture de l’activation planifiée.
      </Alert>

      <Box>
        <Button
          variant="contained"
          color="primary"
          onClick={onActivate}
          disabled={activationBlocked || activating}
          data-testid="activate-datastream"
        >
          {activating
            ? "Enregistrement en cours…"
            : "Enregistrer le flux prêt à activer"}
        </Button>
      </Box>
    </Stack>
  );
}
