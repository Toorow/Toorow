/** Stage 6: choose a schedule and activate the validated immutable plan. */
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
  blockingReasons: string[];
  activating: boolean;
  onActivate: () => void;
}

const CADENCE_LABELS: Record<CadenceMode, string> = {
  manual: "Manual",
  daily: "Daily",
  hourly: "Hourly",
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
        Choose the cadence and timezone. Hourly synchronization requires an
        explicit quota opt-in.
      </Typography>

      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ flexWrap: "wrap" }}>
        <TextField
          select
          label="Cadence"
          value={schedule.cadence_mode}
          onChange={(event) =>
            onScheduleChange({
              ...schedule,
              cadence_mode: event.target.value as CadenceMode,
            })
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
              {mode === "hourly" && !schedule.allow_hourly ? " (quota opt-in required)" : ""}
            </MenuItem>
          ))}
        </TextField>

        <TextField
          select
          label="Timezone"
          value={schedule.timezone}
          onChange={(event) =>
            onScheduleChange({ ...schedule, timezone: event.target.value })
          }
          data-testid="schedule-timezone"
          sx={{ minWidth: 200 }}
        >
          {COMMON_TIMEZONES.map((timezone) => (
            <MenuItem key={timezone} value={timezone}>
              {timezone}
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
            onChange={(event) =>
              onScheduleChange({
                ...schedule,
                allow_hourly: event.target.checked,
                cadence_mode:
                  !event.target.checked && schedule.cadence_mode === "hourly"
                    ? "daily"
                    : schedule.cadence_mode,
              })
            }
          />
          <Typography variant="body2">
            Allow hourly synchronization (higher quota usage)
          </Typography>
        </label>
      </Box>

      {activationBlocked && (
        <Alert severity="warning" role="alert" data-testid="activation-blocked">
          <Typography variant="subtitle2">Activation is blocked</Typography>
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

      <Box>
        <Button
          variant="contained"
          color="primary"
          onClick={onActivate}
          disabled={activationBlocked || activating}
          data-testid="activate-datastream"
        >
          {activating ? "Activating..." : "Activate Datastream"}
        </Button>
      </Box>
    </Stack>
  );
}
