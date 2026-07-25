/**
 * Story 12.13 — the two accessibility regions every stage reuses, factored out
 * of the proven Epic-36 card pattern (PremierRapportCard / RapportPretCard):
 *   - an ASSERTIVE, focusable error region (icon + text, never colour alone);
 *   - a POLITE, atomic live region that announces asynchronous results.
 *
 * WCAG 2.2 AA: focus moves to the error region on error; status changes are
 * announced without noise; state is conveyed by text, not colour.
 */
import { forwardRef } from "react";
import { Alert, Box } from "@mui/material";

/** Assertive, focusable error summary. Pass a ref to move focus to it on error. */
export const ErrorRegion = forwardRef<HTMLDivElement, { error: string | null }>(
  function ErrorRegion({ error }, ref) {
    return (
      <Box
        role="alert"
        aria-live="assertive"
        ref={ref}
        tabIndex={-1}
        sx={{ outline: "none" }}
        data-testid="wizard-error"
      >
        {error && <Alert severity="error">{error}</Alert>}
      </Box>
    );
  },
);

/** Polite, atomic live region for asynchronous result announcements. */
export function LiveRegion({ message }: { message: string }) {
  return (
    <Box
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-testid="wizard-live"
      sx={{ minHeight: 24 }}
    >
      {message}
    </Box>
  );
}
