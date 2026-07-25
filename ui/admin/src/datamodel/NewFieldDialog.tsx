/**
 * NewFieldDialog — dialog to create a new user-defined semantic-model field.
 *
 * v3 restyle: kept on MUI Dialog (portal/focus behavior), English throughout,
 * token-driven colors (application.css CSS variables) so it is dark-safe.
 * snake_case validation preserved.
 */

import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  FormHelperText,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import { useState } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface NewFieldData {
  name: string;
  display_name: string;
  data_type: string;
  field_kind: string;
  measure: string | null;
  description: string | null;
}

interface NewFieldDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: (field: NewFieldData) => void;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const SNAKE_CASE_RE = /^[a-z][a-z0-9_]*$/;

function validateName(name: string): string | null {
  if (!name) return "Name is required.";
  if (!SNAKE_CASE_RE.test(name)) {
    return "Name must be snake_case (lowercase letters, digits, underscores; must start with a letter).";
  }
  return null;
}

// ---------------------------------------------------------------------------
// Dialog
// ---------------------------------------------------------------------------

export default function NewFieldDialog({
  open,
  onClose,
  onCreated,
}: NewFieldDialogProps) {
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [dataType, setDataType] = useState("integer");
  const [fieldKind, setFieldKind] = useState("metric");
  const [measure, setMeasure] = useState<string>("sum");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);

  const handleClose = () => {
    // Reset on close
    setName("");
    setDisplayName("");
    setDataType("integer");
    setFieldKind("metric");
    setMeasure("sum");
    setDescription("");
    setServerError(null);
    setNameError(null);
    onClose();
  };

  const handleNameChange = (v: string) => {
    setName(v);
    setNameError(validateName(v));
  };

  const handleSubmit = async () => {
    const err = validateName(name);
    if (err) {
      setNameError(err);
      return;
    }
    if (!displayName.trim()) return;

    setSubmitting(true);
    setServerError(null);

    try {
      const resp = await fetch("/api/datamodel/fields", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("api_token") || ""}`,
        },
        body: JSON.stringify({
          name,
          display_name: displayName.trim(),
          data_type: dataType,
          field_kind: fieldKind,
          measure: fieldKind === "metric" ? (measure || null) : null,
          description: description.trim() || null,
        }),
      });

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        if (resp.status === 409) {
          setServerError(`A field named "${name}" already exists.`);
        } else {
          setServerError(body.message || `HTTP error ${resp.status}`);
        }
        return;
      }

      const created = await resp.json();
      onCreated(created);
      handleClose();
    } catch (e) {
      setServerError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const isMetric = fieldKind === "metric";

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      maxWidth="sm"
      fullWidth
      slotProps={{
        paper: {
          sx: {
            borderRadius: "12px",
            overflow: "hidden",
            backgroundColor: "var(--surface)",
            color: "var(--ink)",
          },
        },
      }}
      data-testid="new-field-dialog"
    >
      <DialogTitle
        component="div"
        sx={{
          px: 3,
          pt: "24px",
          pb: "16px",
          borderBottom: "1px solid var(--line)",
        }}
      >
        <Typography variant="h6" sx={{ fontWeight: 600, color: "var(--ink)" }}>
          New field
        </Typography>
        <Typography variant="caption" sx={{ color: "var(--muted)" }}>
          User-defined fields extend the system data dictionary.
        </Typography>
      </DialogTitle>

      <DialogContent sx={{ px: 3, py: "24px" }}>
        {serverError && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {serverError}
          </Alert>
        )}

        {/* Name */}
        <TextField
          label="Technical name (snake_case)"
          value={name}
          onChange={(e) => handleNameChange(e.target.value)}
          error={!!nameError}
          helperText={nameError || "e.g. conversion_rate, avg_session_duration"}
          fullWidth
          size="small"
          slotProps={{ htmlInput: { "data-testid": "new-field-name" } }}
          sx={{ mb: "16px" }}
          autoFocus
        />

        {/* Display name */}
        <TextField
          label="Display name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          fullWidth
          size="small"
          helperText="e.g. Conversion rate"
          sx={{ mb: "16px" }}
        />

        <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px", mb: "16px" }}>
          {/* field_kind */}
          <FormControl size="small" fullWidth>
            <InputLabel>Kind</InputLabel>
            <Select
              value={fieldKind}
              label="Kind"
              onChange={(e) => setFieldKind(e.target.value)}
              inputProps={{ "data-testid": "new-field-kind" }}
            >
              <MenuItem value="metric">Metric</MenuItem>
              <MenuItem value="dimension">Dimension</MenuItem>
            </Select>
            <FormHelperText>Metric = measurable value</FormHelperText>
          </FormControl>

          {/* data_type */}
          <FormControl size="small" fullWidth>
            <InputLabel>Data type</InputLabel>
            <Select
              value={dataType}
              label="Data type"
              onChange={(e) => setDataType(e.target.value)}
            >
              <MenuItem value="integer">Integer</MenuItem>
              <MenuItem value="decimal">Decimal</MenuItem>
              <MenuItem value="currency">Currency</MenuItem>
              <MenuItem value="string">Text</MenuItem>
              <MenuItem value="date">Date</MenuItem>
              <MenuItem value="boolean">Boolean</MenuItem>
            </Select>
          </FormControl>
        </Box>

        {/* Measure — only for metrics */}
        {isMetric && (
          <FormControl size="small" fullWidth sx={{ mb: "16px" }}>
            <InputLabel>Aggregation (measure)</InputLabel>
            <Select
              value={measure}
              label="Aggregation (measure)"
              onChange={(e) => setMeasure(e.target.value)}
              inputProps={{ "data-testid": "new-field-measure" }}
            >
              <MenuItem value="sum">Sum</MenuItem>
              <MenuItem value="average">Average</MenuItem>
              <MenuItem value="min">Minimum</MenuItem>
              <MenuItem value="max">Maximum</MenuItem>
              <MenuItem value="count">Count</MenuItem>
            </Select>
            <FormHelperText>
              Defines how this field is aggregated in rollups.
            </FormHelperText>
          </FormControl>
        )}

        <Divider sx={{ my: "16px", borderColor: "var(--line)" }} />

        {/* Description */}
        <TextField
          label="Description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          fullWidth
          size="small"
          multiline
          rows={2}
          helperText="Shown in the field detail view."
        />
      </DialogContent>

      <DialogActions
        sx={{
          px: 3,
          py: "16px",
          borderTop: "1px solid var(--line)",
          gap: "8px",
          justifyContent: "flex-end",
        }}
      >
        <Button
          onClick={handleClose}
          disabled={submitting}
          sx={{ color: "var(--muted)" }}
        >
          Cancel
        </Button>
        <Button
          variant="contained"
          onClick={handleSubmit}
          disabled={submitting || !!nameError || !name || !displayName.trim()}
          data-testid="new-field-submit"
          sx={{
            backgroundColor: "var(--rose)",
            color: "var(--ink)",
            "&:hover": { backgroundColor: "var(--rose)", filter: "brightness(0.95)" },
            "&.Mui-disabled": {
              backgroundColor: "color-mix(in srgb, var(--rose) 30%, transparent)",
              color: "text.disabled",
            },
          }}
        >
          {submitting ? "Creating…" : "Create field"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
