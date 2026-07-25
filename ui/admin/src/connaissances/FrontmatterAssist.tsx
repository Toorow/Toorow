/**
 * FrontmatterAssist — guided YAML frontmatter editor for procedures (Story 11.4).
 *
 * Helps produce valid `name` / `description` frontmatter (the Skill Pack
 * format required by 11.1). Shows a structured form that serialises to the
 * expected YAML block.
 *
 * Also surfaces the 422 `frontmatter_invalide` API error in French.
 *
 * Design rules: MUI/MD3 only, no ad-hoc styling.
 * Copy: French.
 */

import { useEffect, useState } from "react";
import { Alert, Box, TextField, Typography } from "@mui/material";

interface FrontmatterAssistProps {
  /** Current raw YAML string (passed in and out as the canonical value). */
  yaml: string;
  onChange: (yaml: string) => void;
  /** When truthy, shows the 422 error message in French. */
  frontmatterError?: string | null;
  disabled?: boolean;
}

/** Parse the two canonical fields from a minimal YAML block. */
function parseYaml(yaml: string): { name: string; description: string } {
  const nameMatch = yaml.match(/^name:\s*(.+)$/m);
  const descMatch = yaml.match(/^description:\s*(.*)$/m);
  return {
    name: nameMatch ? nameMatch[1].trim() : "",
    description: descMatch ? descMatch[1].trim() : "",
  };
}

/** Build a YAML block from name + description. */
function buildYaml(name: string, description: string): string {
  return `name: ${name}\ndescription: ${description}\n`;
}

export default function FrontmatterAssist({
  yaml,
  onChange,
  frontmatterError,
  disabled = false,
}: FrontmatterAssistProps) {
  const parsed = parseYaml(yaml);
  const [name, setName] = useState(parsed.name);
  const [description, setDescription] = useState(parsed.description);

  // Sync outward whenever the fields change.
  useEffect(() => {
    onChange(buildYaml(name, description));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, description]);

  // If parent resets yaml externally (e.g. on load), sync back in.
  useEffect(() => {
    const p = parseYaml(yaml);
    setName(p.name);
    setDescription(p.description);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [yaml]);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Typography
        variant="caption"
        sx={{
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: ".04em",
          fontSize: "10px",
          color: "text.secondary",
        }}
      >
        Métadonnées de la procédure (frontmatter)
      </Typography>

      <TextField
        label="Nom (identifiant unique)"
        placeholder="ex : analyse_conversion"
        size="small"
        value={name}
        onChange={(e) => setName(e.target.value)}
        disabled={disabled}
        required
        slotProps={{ input: { "aria-label": "Nom de la procédure" } }}
        helperText="Identifiant stable citable par l'agent via get_procedure(name)."
      />

      <TextField
        label="Description"
        placeholder="ex : Procédure d'analyse du taux de conversion…"
        size="small"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        disabled={disabled}
        multiline
        minRows={2}
        slotProps={{ input: { "aria-label": "Description de la procédure" } }}
        helperText="Une phrase ou deux décrivant l'objectif de la procédure."
      />

      {/* YAML preview */}
      <Box
        sx={{
          border: "1px solid",
          borderColor: "divider",
          borderRadius: 1.5,
          p: 1.5,
          bgcolor: "action.hover",
        }}
        aria-label="Aperçu du frontmatter YAML"
      >
        <Typography
          variant="caption"
          sx={{ fontWeight: 600, color: "text.secondary", fontSize: "10px", display: "block", mb: 0.5 }}
        >
          YAML généré
        </Typography>
        <Box
          component="pre"
          sx={{
            m: 0,
            fontSize: "12px",
            fontFamily: "var(--font-mono, monospace)",
            color: "text.primary",
            whiteSpace: "pre-wrap",
          }}
          data-testid="frontmatter-yaml-preview"
        >
          {buildYaml(name, description)}
        </Box>
      </Box>

      {/* 422 frontmatter error surfaced in French */}
      {frontmatterError && (
        <Alert
          severity="error"
          data-testid="frontmatter-error-alert"
        >
          Frontmatter invalide — {frontmatterError}
        </Alert>
      )}
    </Box>
  );
}
