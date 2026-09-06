/**
 * NoEnvelope — what a card shows when the host delivered nothing (AI-271).
 *
 * WHY THIS FILE EXISTS. All nine cards ended their entrypoint with the same line:
 *
 *     render(readInjectedEnvelope<CardEnvelope>() ?? FIXTURE_ENVELOPE);
 *
 * so a card opened with no envelope rendered its FIXTURE — a full card, numbers
 * and all, that no server ever founded. Measured on `conversions`: the card kept
 * showing a 50 EUR target in green after story 53.5 had removed that target from
 * the server. Nothing on screen said the figures were invented, which is the one
 * thing the product forbids outright ("no demonstration content").
 *
 * WHAT REPLACES IT, and why it is not simply a blank. An empty state must say why
 * it is empty and name the gesture that fills it. A card is a rendering of ONE
 * answer: the host — the assistant — injects the envelope when it decides to show
 * it. So "no envelope" means exactly one thing, and it is nobody's mistake: the
 * card was opened outside the answer it belongs to.
 *
 * THE FIXTURES ARE NOT DELETED. They are the tests' subject matter and the
 * Storybook-less way to look at a template while designing it; what changes is
 * that the RENDER PATH can no longer reach them. `test_cards_never_render_a_fixture`
 * is the guard that keeps it that way.
 */

import { Box, Typography, useTheme } from "@toorow/shell";

export interface NoEnvelopeProps {
  /** The card's own title, so the frame still says which card this is. */
  title: string;
}

export default function NoEnvelope({ title }: NoEnvelopeProps) {
  const theme = useTheme();
  return (
    <Box
      data-testid="card-no-envelope"
      role="status"
      sx={{
        display: "flex",
        flexDirection: "column",
        gap: 1,
        px: 3,
        py: 6,
        textAlign: "center",
        color: theme.palette.text.secondary,
      }}
    >
      <Typography variant="subtitle2" sx={{ color: theme.palette.text.primary }}>
        {title}
      </Typography>
      <Typography variant="body2">This card has no answer to show yet.</Typography>
      <Typography variant="caption">
        A card is rendered with the answer it belongs to. Ask the assistant the question
        again, and it will open this card with its figures.
      </Typography>
    </Box>
  );
}
