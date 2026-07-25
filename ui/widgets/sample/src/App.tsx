import WidgetShell from "@toorow/shell";

/**
 * Story 1.2 sample widget.
 *
 * Demonstrates the full MD3 design system pipeline:
 *   - Wraps content in <WidgetShell> to get the MD3 token-derived MUI theme
 *   - Font inlining: Roboto Flex + Material Symbols (via @toorow/shell)
 *   - Transparent background (AD-11 / NFR11)
 *   - hostContext light/dark re-theming without reload (AC2 / UX-DR5)
 *
 * Passes a mock `meta` object to demonstrate the shell's stale badge, alert,
 * and reconnect affordance rendering (AC5 / AD-9).
 */

/** Mock meta envelope as specified in T7.2 */
const mockMeta = {
  freshness: {
    last_pull: "2026-07-10T00:00:00Z",
    cadence_hours: 24,
    // stale_since is absent → no stale badge shown
  },
  provenance: {
    source_system: "seeded",
    pull_id: "pull_00000000",
  },
  alerts: [] as Array<{ code: string; severity: "info" | "warn" | "error"; message: string }>,
};

export default function App() {
  return (
    <WidgetShell
      meta={mockMeta}
      // NFR4: the real deep-link arrives at runtime via tool meta — an absolute
      // URL baked into the bundle would (rightly) fail the bundle gate.
      adminConsoleUrl="/admin"
    >
      <div
        style={{
          padding: 16,
          display: "flex",
          flexDirection: "column",
          gap: 12,
          alignItems: "flex-start",
        }}
      >
        <div>Connector widget — MD3 themed via token pipeline</div>
        <div style={{ fontFamily: '"Roboto Flex", sans-serif', fontSize: 14, opacity: 0.7 }}>
          Roboto Flex + Material Symbols inlined · Light/dark via hostContext
        </div>
      </div>
    </WidgetShell>
  );
}
