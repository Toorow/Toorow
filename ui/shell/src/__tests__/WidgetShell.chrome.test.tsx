/**
 * WidgetShell chrome footer tests — Story 8.8.
 *
 * Verifies:
 *   - The slim chrome footer bar renders when both feedbackContext and
 *     exportContext are provided.
 *   - The footer has data-testid="widget-chrome-footer".
 *   - FeedbackBar (feedback-bar testid) is rendered inside the footer.
 *   - ExportButton (export-button-container testid) is rendered inside the footer.
 *   - No footer when neither feedbackContext nor exportContext is provided.
 *   - Footer renders with only feedbackContext (no exportContext).
 *   - Footer renders with only exportContext (no feedbackContext).
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { useRef } from "react";
import WidgetShell from "../WidgetShell";
import type { WidgetMeta } from "../types";

// Mock html-to-image for ExportButton
vi.mock("html-to-image", () => ({
  toPng: vi.fn().mockResolvedValue("data:image/png;base64,fake"),
}));

function makeBaseMeta(): WidgetMeta {
  return {
    freshness: { last_pull: "2026-07-10T12:00:00Z", cadence_hours: 24 },
    provenance: { source_system: "test", pull_id: "pull_01" },
    alerts: [],
  };
}

// Wrapper that creates a ref for exportContext
function ShellWithFooter({
  withFeedback = true,
  withExport = true,
}: {
  withFeedback?: boolean;
  withExport?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  const feedbackContext = withFeedback
    ? {
        projectId: "proj_test",
        reportRef: "get_daily_report:2026-07-10",
        module: "google-analytics",
      }
    : undefined;

  const exportContext = withExport
    ? { targetRef: ref as React.RefObject<HTMLElement | null>, filename: "test-export" }
    : undefined;

  return (
    <div ref={ref}>
      <WidgetShell
        meta={makeBaseMeta()}
        feedbackContext={feedbackContext}
        exportContext={exportContext}
      >
        <div data-testid="widget-content">Widget content</div>
      </WidgetShell>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Footer renders when feedbackContext + exportContext provided
// ---------------------------------------------------------------------------

describe("WidgetShell — slim chrome footer (Story 8.8)", () => {
  it("renders the chrome footer bar when both contexts are provided", () => {
    render(<ShellWithFooter withFeedback withExport />);
    expect(screen.getByTestId("widget-chrome-footer")).toBeInTheDocument();
  });

  it("footer contains FeedbackBar", () => {
    render(<ShellWithFooter withFeedback withExport />);
    const footer = screen.getByTestId("widget-chrome-footer");
    expect(footer.contains(screen.getByTestId("feedback-bar"))).toBe(true);
  });

  it("footer contains ExportButton", () => {
    render(<ShellWithFooter withFeedback withExport />);
    const footer = screen.getByTestId("widget-chrome-footer");
    expect(footer.contains(screen.getByTestId("export-button-container"))).toBe(true);
  });

  it("does NOT render footer when neither feedbackContext nor exportContext", () => {
    render(
      <WidgetShell meta={makeBaseMeta()}>
        <div data-testid="child">child</div>
      </WidgetShell>,
    );
    expect(screen.queryByTestId("widget-chrome-footer")).not.toBeInTheDocument();
  });

  it("renders footer with only feedbackContext (no exportContext)", () => {
    render(<ShellWithFooter withFeedback withExport={false} />);
    expect(screen.getByTestId("widget-chrome-footer")).toBeInTheDocument();
    expect(screen.getByTestId("feedback-bar")).toBeInTheDocument();
    expect(screen.queryByTestId("export-button-container")).not.toBeInTheDocument();
  });

  it("renders footer with only exportContext (no feedbackContext)", () => {
    render(<ShellWithFooter withFeedback={false} withExport />);
    expect(screen.getByTestId("widget-chrome-footer")).toBeInTheDocument();
    expect(screen.getByTestId("export-button-container")).toBeInTheDocument();
    expect(screen.queryByTestId("feedback-bar")).not.toBeInTheDocument();
  });

  it("widget content is still rendered inside the shell when footer is present", () => {
    render(<ShellWithFooter withFeedback withExport />);
    expect(screen.getByTestId("widget-content")).toBeInTheDocument();
  });
});
