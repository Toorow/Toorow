/**
 * CardFeedbackBar — 👍/👎 + commentaire optionnel → submit_feedback via callServerTool
 * (Story 9.2b). Variante compacte pour le footer de CardShell.
 *
 * callServerTool (Story 9.10) : helper partagé importé de @toorow/shell —
 * la copie locale du pattern raw-postMessage est supprimée (centralisation).
 *   - Chemin SDK (@modelcontextprotocol/ext-apps) : RPC awaitable réel — un
 *     rejet (échec transport OU résultat isError) affiche l'état d'erreur
 *     designé ci-dessous (F-11, enfin atteignable).
 *   - Fallback legacy : raw postMessage fire-and-forget, graceful no-op hors
 *     host MCP Apps (Vitest, Storybook…) — confirmation optimiste inchangée.
 *
 * UX :
 *   - Compact : thumbs 👍/👎 sur une ligne, pas de TextField multi-ligne
 *     (on garde le commentaire mais en single line pour la compacité footer).
 *   - Click thumb → sélection visuelle (filled/outlined).
 *   - Bouton « Envoyer » disabled jusqu'à sélection d'un rating.
 *   - Confirmation : « Merci pour votre retour ! »
 *
 * Copie française (UX-DR10) : « Utile » / « Pas utile » / « Merci pour votre retour ! »
 */

import { useState } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import Stack from "@mui/material/Stack";
import { callServerTool } from "@toorow/shell";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CardFeedbackBarProps {
  /** Identifiant du projet (depuis meta.project_id). */
  projectId: string;
  /** OTel trace_id depuis meta.trace_id. */
  traceId: string | null;
  /** Référence du rapport, ex : "card:kpi:2026-07-13". */
  reportRef: string;
  /** Module, ex : "card-kpi". */
  module: string;
}

// ---------------------------------------------------------------------------
// Composant
// ---------------------------------------------------------------------------

export default function CardFeedbackBar({
  projectId,
  traceId,
  reportRef,
  module,
}: CardFeedbackBarProps) {
  const [selectedRating, setSelectedRating] = useState<1 | -1 | null>(null);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [submitError, setSubmitError] = useState(false);

  async function handleSubmit() {
    if (submitting || submitted || selectedRating === null) return;
    setSubmitting(true);
    setSubmitError(false);
    try {
      // Story 9.10: callServerTool est le helper partagé (@toorow/shell).
      // Chemin SDK : RPC awaitable — un rejet (échec transport ou résultat
      // isError) signifie un échec réel et affiche l'état d'erreur (F-11,
      // enfin atteignable). Chemin legacy : postMessage fire-and-forget qui
      // résout immédiatement — confirmation optimiste inchangée.
      await callServerTool("submit_feedback", {
        project_id: projectId,
        rating: selectedRating,
        trace_id: traceId ?? null,
        comment: comment.trim() || "",
        report_ref: reportRef,
        module,
      });
      setSubmitted(true);
    } catch {
      // Designed failure state (F-11) : atteint sur le chemin SDK quand le
      // host/serveur rejette la soumission.
      setSubmitError(true);
    } finally {
      setSubmitting(false);
    }
  }

  if (submitted) {
    return (
      <Box data-testid="card-feedback-confirmation">
        <Typography variant="caption" color="text.secondary">
          Merci pour votre retour !
        </Typography>
      </Box>
    );
  }

  if (submitError) {
    return (
      <Box data-testid="card-feedback-error">
        <Typography variant="caption" color="error.main">
          Erreur lors de l'envoi. Veuillez réessayer.
        </Typography>
        <Button
          size="small"
          variant="text"
          onClick={() => setSubmitError(false)}
          sx={{ ml: 1, fontSize: "0.7rem", py: 0.25, px: 0.75 }}
          data-testid="card-feedback-retry"
        >
          Réessayer
        </Button>
      </Box>
    );
  }

  return (
    <Box data-testid="card-feedback-bar">
      <Stack
        direction="row"
        spacing={0.75}
        sx={{ alignItems: "center", flexWrap: "wrap" }}
        useFlexGap
      >
        <Typography variant="caption" color="text.secondary" sx={{ flexShrink: 0 }}>
          Cette carte vous a été utile ?
        </Typography>
        <Button
          size="small"
          variant={selectedRating === 1 ? "contained" : "outlined"}
          disabled={submitting}
          onClick={() => setSelectedRating(1)}
          data-testid="card-feedback-thumbs-up"
          aria-label="Utile"
          aria-pressed={selectedRating === 1}
          sx={{ minWidth: 0, px: 1, py: 0.25, fontSize: "0.7rem", lineHeight: 1.4 }}
        >
          👍 Utile
        </Button>
        <Button
          size="small"
          variant={selectedRating === -1 ? "contained" : "outlined"}
          disabled={submitting}
          onClick={() => setSelectedRating(-1)}
          data-testid="card-feedback-thumbs-down"
          aria-label="Pas utile"
          aria-pressed={selectedRating === -1}
          sx={{ minWidth: 0, px: 1, py: 0.25, fontSize: "0.7rem", lineHeight: 1.4 }}
        >
          👎 Pas utile
        </Button>
        <TextField
          size="small"
          placeholder="Commentaire…"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          disabled={submitting}
          slotProps={{
            input: {
              // @ts-expect-error -- data-testid forwarded via slotProps in MUI v9
              "data-testid": "card-feedback-comment",
              "aria-label": "Commentaire optionnel",
            },
          }}
          sx={{ flex: "1 1 120px", minWidth: 80, "& .MuiInputBase-input": { py: 0.4, fontSize: "0.7rem" } }}
        />
        <Button
          size="small"
          variant="contained"
          disabled={submitting || selectedRating === null}
          onClick={() => { void handleSubmit(); }}
          data-testid="card-feedback-submit"
          sx={{ minWidth: 0, px: 1, py: 0.25, fontSize: "0.7rem", lineHeight: 1.4, flexShrink: 0 }}
        >
          Envoyer
        </Button>
      </Stack>
    </Box>
  );
}
