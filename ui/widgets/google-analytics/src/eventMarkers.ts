/**
 * eventMarkers.ts — event annotation helpers for the time-axis overlay.
 *
 * Story 31.5: provides the static dim_event_type lookup dictionary (marker style
 * per event_type), category/platform filter helpers, and the branding-aware color
 * resolver that respects getVizPalette / org branding.
 *
 * Design decisions:
 *  - The static DIM_EVENT_TYPE map mirrors dbt/seeds/dim_event_type.csv exactly.
 *    It is duplicated here (not fetched) because:
 *      a) the server already joins category/default_marker into each event in meta,
 *      b) this guarantees zero network calls (AD-10) and keeps the overlay purely
 *         data-driven from the envelope.
 *    A fallback lookup is provided for legacy envelopes without the server-joined fields.
 *  - AD-9: markers are ADDITIVE — they never modify any metric value.
 *  - AD-11: colors come from the MUI theme palette or org branding (getVizPalette),
 *    never from hardcoded hex literals.
 *  - AD-2: source-agnostic — no connector-specific strings in this module.
 */

import type { ContextEventMeta } from "./types";

// ---------------------------------------------------------------------------
// Static dim_event_type dictionary (mirrors dbt/seeds/dim_event_type.csv).
// Used as a fallback when the server does not yet send category/default_marker
// (legacy envelopes). The server-side join is always preferred.
// ---------------------------------------------------------------------------

export type MarkerShape =
  | "triangle"
  | "diamond"
  | "flag"
  | "star"
  | "line"
  | "pin"
  | "cross";

export type EventCategory =
  | "content"
  | "engineering"
  | "marketing"
  | "commerce"
  | "business"
  | "operations"
  | "";

export interface EventTypeDef {
  category: EventCategory;
  default_marker: MarkerShape;
}

/** Fallback dim_event_type lookup (mirrors dbt/seeds/dim_event_type.csv). */
export const DIM_EVENT_TYPE: Record<string, EventTypeDef> = {
  video_upload:    { category: "content",     default_marker: "triangle" },
  blog_post:       { category: "content",     default_marker: "triangle" },
  social_post:     { category: "content",     default_marker: "triangle" },
  release:         { category: "engineering", default_marker: "diamond"  },
  deployment:      { category: "engineering", default_marker: "diamond"  },
  campaign_launch: { category: "marketing",   default_marker: "flag"     },
  product_launch:  { category: "commerce",    default_marker: "star"     },
  price_change:    { category: "commerce",    default_marker: "line"     },
  promotion:       { category: "marketing",   default_marker: "flag"     },
  milestone:       { category: "business",    default_marker: "pin"      },
  incident:        { category: "operations",  default_marker: "cross"    },
};

/** Canonical categories (for filter UI). */
export const EVENT_CATEGORIES: EventCategory[] = [
  "content",
  "engineering",
  "marketing",
  "commerce",
  "business",
  "operations",
];

// ---------------------------------------------------------------------------
// Marker style resolution
// ---------------------------------------------------------------------------

/**
 * Resolve the marker shape for an event, using the server-enriched field when
 * present, falling back to the static DIM_EVENT_TYPE lookup, then to "pin".
 */
export function resolveMarkerShape(event: ContextEventMeta): MarkerShape {
  if (event.default_marker) return event.default_marker as MarkerShape;
  return DIM_EVENT_TYPE[event.type]?.default_marker ?? "pin";
}

/**
 * Resolve the category for an event, using the server-enriched field when
 * present, falling back to the static lookup.
 */
export function resolveCategory(event: ContextEventMeta): EventCategory {
  if (event.category) return event.category as EventCategory;
  return DIM_EVENT_TYPE[event.type]?.category ?? "";
}

// ---------------------------------------------------------------------------
// Category/platform colour mapping (for legend + filter chips).
// Colors are theme-semantic strings (returned as CSS var names or palette keys)
// so the caller resolves them via MUI useTheme() (AD-11: no hardcoded hex).
// ---------------------------------------------------------------------------

/**
 * Return a MUI palette key for a category, used to tint markers.
 * Categories map to semantic palette roles (error=red, warning=orange,
 * info=blue, success=green, secondary=purple, primary=default).
 */
export function categoryPaletteKey(
  category: EventCategory,
): "error" | "warning" | "info" | "success" | "secondary" | "primary" {
  switch (category) {
    case "operations":  return "error";
    case "marketing":   return "warning";
    case "commerce":    return "success";
    case "engineering": return "info";
    case "content":     return "secondary";
    case "business":
    default:            return "primary";
  }
}

// ---------------------------------------------------------------------------
// Filter helpers
// ---------------------------------------------------------------------------

/**
 * Filter a list of context events by category and/or platform.
 * Passing an empty/null filter means "no filter" (all pass).
 */
export function filterEvents(
  events: ContextEventMeta[],
  opts: {
    categories?: EventCategory[];
    platforms?: string[];
  },
): ContextEventMeta[] {
  const { categories, platforms } = opts;
  return events.filter((e) => {
    if (categories && categories.length > 0) {
      const cat = resolveCategory(e);
      if (!categories.includes(cat as EventCategory)) return false;
    }
    if (platforms && platforms.length > 0) {
      const plat = e.platform ?? null;
      // null platform = manual — include only if "manual" is in the filter.
      const key = plat ?? "manual";
      if (!platforms.includes(key)) return false;
    }
    return true;
  });
}

/**
 * Derive unique category values from an event list (for building filter chips).
 */
export function uniqueCategories(events: ContextEventMeta[]): EventCategory[] {
  const seen = new Set<EventCategory>();
  for (const e of events) {
    const cat = resolveCategory(e);
    if (cat) seen.add(cat as EventCategory);
  }
  return [...seen].sort();
}

/**
 * Derive unique platform values from an event list.
 * null platform → "manual".
 */
export function uniquePlatforms(events: ContextEventMeta[]): string[] {
  const seen = new Set<string>();
  for (const e of events) {
    seen.add(e.platform ?? "manual");
  }
  return [...seen].sort();
}

// ---------------------------------------------------------------------------
// Date-to-x-position helper (shared between SmallMultiples overlay and others)
// ---------------------------------------------------------------------------

/**
 * Map a date string to an x pixel position within [padX, padX+innerW].
 * Returns null when the date is outside the domain.
 */
export function dateToX(
  date: string,
  domain: string[],
  padX: number,
  innerW: number,
): number | null {
  const n = domain.length;
  if (n === 0) return null;
  const idx = domain.indexOf(date);
  if (idx === -1) return null;
  return padX + (n <= 1 ? innerW / 2 : (idx / (n - 1)) * innerW);
}
