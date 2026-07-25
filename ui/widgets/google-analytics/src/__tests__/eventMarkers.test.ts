/**
 * Tests for eventMarkers.ts helpers (Story 31.5).
 *
 * Covers:
 *  - resolveMarkerShape: server-enriched field wins; fallback to static dict; unknown -> pin.
 *  - resolveCategory: same priority logic as resolveMarkerShape.
 *  - categoryPaletteKey: maps known categories to expected MUI palette keys.
 *  - filterEvents: category + platform filters work independently and combined.
 *  - uniqueCategories / uniquePlatforms: deduplicate correctly.
 *  - dateToX: maps dates to x positions within the domain correctly.
 *  - filterEvents with null platform maps to "manual".
 */

import { describe, it, expect } from "vitest";
import {
  resolveMarkerShape,
  resolveCategory,
  categoryPaletteKey,
  filterEvents,
  uniqueCategories,
  uniquePlatforms,
  dateToX,
} from "../eventMarkers";
import type { ContextEventMeta } from "../types";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

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

const MANUAL_MILESTONE: ContextEventMeta = {
  id: "evt_3",
  event_date: "2026-07-20",
  type: "milestone",
  label: "Q3 launch",
  platform: null,
  source: "manual",
  // no category / default_marker — legacy envelope
};

const UNKNOWN_TYPE: ContextEventMeta = {
  id: "evt_4",
  event_date: "2026-07-22",
  type: "foobar_unknown",
  label: "Mystery",
};

// ---------------------------------------------------------------------------
// resolveMarkerShape
// ---------------------------------------------------------------------------

describe("resolveMarkerShape", () => {
  it("uses server-side default_marker when present", () => {
    expect(resolveMarkerShape(YT_EVENT)).toBe("triangle");
    expect(resolveMarkerShape(GH_RELEASE)).toBe("diamond");
  });

  it("falls back to DIM_EVENT_TYPE lookup when default_marker absent", () => {
    expect(resolveMarkerShape(MANUAL_MILESTONE)).toBe("pin"); // milestone -> pin
  });

  it("returns 'pin' for unknown types not in DIM_EVENT_TYPE", () => {
    expect(resolveMarkerShape(UNKNOWN_TYPE)).toBe("pin");
  });
});

// ---------------------------------------------------------------------------
// resolveCategory
// ---------------------------------------------------------------------------

describe("resolveCategory", () => {
  it("uses server-side category when present", () => {
    expect(resolveCategory(YT_EVENT)).toBe("content");
    expect(resolveCategory(GH_RELEASE)).toBe("engineering");
  });

  it("falls back to DIM_EVENT_TYPE lookup when category absent", () => {
    expect(resolveCategory(MANUAL_MILESTONE)).toBe("business");
  });

  it("returns '' for unknown types", () => {
    expect(resolveCategory(UNKNOWN_TYPE)).toBe("");
  });
});

// ---------------------------------------------------------------------------
// categoryPaletteKey
// ---------------------------------------------------------------------------

describe("categoryPaletteKey", () => {
  it("maps operations -> error", () => expect(categoryPaletteKey("operations")).toBe("error"));
  it("maps marketing -> warning", () => expect(categoryPaletteKey("marketing")).toBe("warning"));
  it("maps commerce -> success", () => expect(categoryPaletteKey("commerce")).toBe("success"));
  it("maps engineering -> info", () => expect(categoryPaletteKey("engineering")).toBe("info"));
  it("maps content -> secondary", () => expect(categoryPaletteKey("content")).toBe("secondary"));
  it("maps business -> primary", () => expect(categoryPaletteKey("business")).toBe("primary"));
  it("maps empty string -> primary", () => expect(categoryPaletteKey("")).toBe("primary"));
});

// ---------------------------------------------------------------------------
// filterEvents
// ---------------------------------------------------------------------------

const ALL_EVENTS = [YT_EVENT, GH_RELEASE, MANUAL_MILESTONE];

describe("filterEvents", () => {
  it("returns all events when no filters applied", () => {
    expect(filterEvents(ALL_EVENTS, {})).toHaveLength(3);
  });

  it("filters by category", () => {
    const result = filterEvents(ALL_EVENTS, { categories: ["content"] });
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("video_upload");
  });

  it("filters by multiple categories", () => {
    const result = filterEvents(ALL_EVENTS, { categories: ["content", "engineering"] });
    expect(result).toHaveLength(2);
  });

  it("filters by platform", () => {
    const result = filterEvents(ALL_EVENTS, { platforms: ["youtube"] });
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("video_upload");
  });

  it("null platform maps to 'manual' in platform filter", () => {
    const result = filterEvents(ALL_EVENTS, { platforms: ["manual"] });
    expect(result).toHaveLength(1);
    expect(result[0].label).toBe("Q3 launch");
  });

  it("combines category and platform filters (AND)", () => {
    // Only content + youtube -> YT_EVENT
    const result = filterEvents(ALL_EVENTS, {
      categories: ["content"],
      platforms: ["youtube"],
    });
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("evt_1");
  });

  it("returns empty when no event matches combined filter", () => {
    const result = filterEvents(ALL_EVENTS, {
      categories: ["operations"],
      platforms: ["youtube"],
    });
    expect(result).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// uniqueCategories / uniquePlatforms
// ---------------------------------------------------------------------------

describe("uniqueCategories", () => {
  it("returns unique categories from events", () => {
    const cats = uniqueCategories(ALL_EVENTS);
    expect(cats).toContain("content");
    expect(cats).toContain("engineering");
    expect(cats).toContain("business"); // from milestone fallback
    expect(cats.length).toBe(3);
  });

  it("returns [] for empty events", () => {
    expect(uniqueCategories([])).toHaveLength(0);
  });
});

describe("uniquePlatforms", () => {
  it("returns unique platforms (null -> 'manual')", () => {
    const plats = uniquePlatforms(ALL_EVENTS);
    expect(plats).toContain("youtube");
    expect(plats).toContain("github");
    expect(plats).toContain("manual");
    expect(plats.length).toBe(3);
  });
});

// ---------------------------------------------------------------------------
// dateToX
// ---------------------------------------------------------------------------

describe("dateToX", () => {
  const domain = ["2026-07-01", "2026-07-02", "2026-07-03"];

  it("maps first date to padX", () => {
    expect(dateToX("2026-07-01", domain, 4, 100)).toBeCloseTo(4);
  });

  it("maps last date to padX + innerW", () => {
    expect(dateToX("2026-07-03", domain, 4, 100)).toBeCloseTo(104);
  });

  it("maps middle date to midpoint", () => {
    expect(dateToX("2026-07-02", domain, 4, 100)).toBeCloseTo(54);
  });

  it("returns null for date not in domain", () => {
    expect(dateToX("2026-06-01", domain, 4, 100)).toBeNull();
  });

  it("returns padX + innerW/2 for single-item domain", () => {
    expect(dateToX("2026-07-01", ["2026-07-01"], 4, 100)).toBeCloseTo(54);
  });

  it("returns null for empty domain", () => {
    expect(dateToX("2026-07-01", [], 4, 100)).toBeNull();
  });
});
