/**
 * Tests for EventAnnotationOverlay (Story 31.5).
 *
 * Covers:
 *  - No markers rendered when events list is empty.
 *  - One marker group per unique date in the domain.
 *  - Markers are rendered inside an SVG (as <path> elements).
 *  - Tooltip <title> contains event labels and types.
 *  - Multiple events on the same date → single marker group (highest-priority shape).
 *  - Events outside the domain are silently ignored.
 *  - Cross-source demo: YouTube + GitHub events on the same chart.
 *  - aria-label on each marker group mentions label and type.
 */

import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { ThemeProvider, createTheme } from "@mui/material/styles";
import EventAnnotationOverlay from "../EventAnnotationOverlay";
import type { ContextEventMeta } from "../types";

function renderOverlay(events: ContextEventMeta[], domain: string[] = []) {
  const theme = createTheme();
  return render(
    <ThemeProvider theme={theme}>
      <svg width={320} height={68}>
        <EventAnnotationOverlay
          events={events}
          domain={domain}
          padX={4}
          innerW={312}
          axisY={62}
        />
      </svg>
    </ThemeProvider>,
  );
}

const YT_EVENT: ContextEventMeta = {
  id: "evt_1",
  event_date: "2026-07-10",
  type: "video_upload",
  label: "New video",
  platform: "youtube",
  source: "youtube-analytics",
  category: "content",
  default_marker: "triangle",
};

const GH_RELEASE: ContextEventMeta = {
  id: "evt_2",
  event_date: "2026-07-15",
  type: "release",
  label: "v2.0",
  platform: "github",
  source: "github",
  category: "engineering",
  default_marker: "diamond",
};

const DOMAIN_30 = Array.from({ length: 30 }, (_, i) => {
  const d = new Date("2026-07-01T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + i);
  return d.toISOString().slice(0, 10);
});

describe("EventAnnotationOverlay", () => {
  it("renders nothing when events list is empty", () => {
    renderOverlay([], DOMAIN_30);
    const paths = document.querySelectorAll("path");
    expect(paths.length).toBe(0);
  });

  it("renders nothing when domain is empty", () => {
    renderOverlay([YT_EVENT], []);
    const paths = document.querySelectorAll("path");
    expect(paths.length).toBe(0);
  });

  it("renders a <path> for each distinct event date in the domain", () => {
    renderOverlay([YT_EVENT, GH_RELEASE], DOMAIN_30);
    // Two distinct dates -> two <path> markers
    const groups = document.querySelectorAll("g[role='img']");
    expect(groups.length).toBe(2);
  });

  it("renders one marker group for two events on the same date", () => {
    const evt2: ContextEventMeta = {
      ...YT_EVENT,
      id: "evt_1b",
      label: "Another video",
    };
    renderOverlay([YT_EVENT, evt2], DOMAIN_30);
    const groups = document.querySelectorAll("g[role='img']");
    expect(groups.length).toBe(1);
  });

  it("marker group has aria-label containing label and type", () => {
    renderOverlay([YT_EVENT], DOMAIN_30);
    const group = document.querySelector("g[role='img']");
    expect(group).not.toBeNull();
    const ariaLabel = group!.getAttribute("aria-label") ?? "";
    expect(ariaLabel).toContain("New video");
    expect(ariaLabel).toContain("video_upload");
  });

  it("tooltip <title> contains the event label", () => {
    renderOverlay([GH_RELEASE], DOMAIN_30);
    const titles = document.querySelectorAll("g[role='img'] > title");
    expect(titles.length).toBeGreaterThan(0);
    expect(titles[0].textContent).toContain("v2.0");
  });

  it("tooltip <title> mentions source when not manual", () => {
    renderOverlay([GH_RELEASE], DOMAIN_30);
    const titles = document.querySelectorAll("g[role='img'] > title");
    expect(titles[0].textContent).toContain("github");
  });

  it("ignores events whose date is outside the domain", () => {
    const outsideEvent: ContextEventMeta = {
      id: "evt_out",
      event_date: "2025-01-01",
      type: "milestone",
      label: "Old milestone",
    };
    renderOverlay([outsideEvent, YT_EVENT], DOMAIN_30);
    // Only YT_EVENT is in domain -> 1 marker group
    const groups = document.querySelectorAll("g[role='img']");
    expect(groups.length).toBe(1);
  });

  it("cross-source demo: YouTube + GitHub markers both appear on same chart", () => {
    renderOverlay([YT_EVENT, GH_RELEASE], DOMAIN_30);
    const groups = document.querySelectorAll("g[role='img']");
    expect(groups.length).toBe(2);

    const allLabels = [...groups].map((g) => g.getAttribute("aria-label") ?? "").join(" | ");
    expect(allLabels).toContain("video_upload");
    expect(allLabels).toContain("release");
  });

  it("renders <path> elements (marker shapes) inside each group", () => {
    renderOverlay([YT_EVENT], DOMAIN_30);
    const paths = document.querySelectorAll("g[role='img'] path");
    // At least 1 path per marker (the shape + optionally the hit target rect)
    expect(paths.length).toBeGreaterThan(0);
  });
});
