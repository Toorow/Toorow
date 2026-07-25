/**
 * MarkdownEditor — split-pane Markdown editor with live preview (Story 11.4).
 *
 * No heavy Markdown-renderer dependency added: the live preview renders the raw
 * Markdown text in a styled <pre> element with soft wrapping so the DoD
 * "no heavy new deps" constraint is honoured. A proper rendered view would
 * require react-markdown (~60 kB gz) which is not already in the bundle;
 * adding it can be done separately after the bundle budget check.
 *
 * Design rules: MUI/MD3 only, no ad-hoc styling beyond sx props.
 * Copy: French.
 */

import { Box, TextField, Typography } from "@mui/material";

interface MarkdownEditorProps {
  value: string;
  onChange: (v: string) => void;
  label?: string;
  placeholder?: string;
  minRows?: number;
  disabled?: boolean;
  /** data-testid placed on the editor wrapper Box. */
  "data-testid"?: string;
}

export default function MarkdownEditor({
  value,
  onChange,
  label = "Corps (Markdown)",
  placeholder = "Rédigez le contenu en Markdown…",
  minRows = 12,
  disabled = false,
  "data-testid": testId,
}: MarkdownEditorProps) {
  return (
    <Box sx={{ display: "flex", gap: 2, alignItems: "flex-start" }}>
      {/* Editor pane */}
      <Box sx={{ flex: 1, minWidth: 0 }} data-testid={testId ?? "markdown-editor-pane"}>
        <TextField
          multiline
          fullWidth
          minRows={minRows}
          label={label}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          slotProps={{
            input: {
              "aria-label": label,
              style: { fontFamily: "var(--font-mono, monospace)", fontSize: "13px" },
            },
          }}
          sx={{
            "& .MuiOutlinedInput-root": {
              borderRadius: 2,
            },
          }}
        />
      </Box>

      {/* Preview pane */}
      <Box
        sx={{
          flex: 1,
          minWidth: 0,
          border: "1px solid",
          borderColor: "divider",
          borderRadius: 2,
          p: 2,
          minHeight: minRows * 24,
          bgcolor: "background.paper",
          overflow: "auto",
        }}
        aria-label="Aperçu Markdown"
        data-testid="markdown-preview"
      >
        <Typography
          variant="caption"
          sx={{
            display: "block",
            color: "text.secondary",
            mb: 1,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: ".04em",
            fontSize: "10px",
          }}
        >
          Aperçu
        </Typography>
        {value.trim() ? (
          <Box
            component="pre"
            sx={{
              m: 0,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily: "var(--font-primary, inherit)",
              fontSize: "13px",
              color: "text.primary",
              lineHeight: 1.6,
            }}
          >
            {value}
          </Box>
        ) : (
          <Typography variant="body2" sx={{ color: "text.disabled", fontStyle: "italic" }}>
            L&apos;aperçu apparaît ici.
          </Typography>
        )}
      </Box>
    </Box>
  );
}
