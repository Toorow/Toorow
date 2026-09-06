/**
 * The last addresses this person opened, so the palette can offer them back.
 *
 * `lastProjectScope.ts` remembers ONE thing — which Project the rail should
 * point at when the address carries none. This is the same idea one level up:
 * not a scope, but the exact pages that were open, in the order they were left.
 * A person who spent the morning between three Datastreams and the Semantic
 * Model should not have to walk the rail down to each of them again.
 *
 * WHAT IS STORED IS AN ADDRESS, never a route object. A serialized route would
 * be a second grammar beside `buildPath`'s, and `routeHref.ts` documents at
 * length what a second grammar costs here. The string is exactly what
 * `buildPath` produced, and it is handed back to `parsePath` to be reopened —
 * so an entry recorded before a section was renamed, a lens retired or a
 * capability closed simply stops resolving and is dropped, rather than being
 * offered as a control that lands on the unknown-route screen. That is why
 * `resolvableHref` is asked on the way IN and on the way OUT: a stored entry can
 * be made stale by a deployment it never saw.
 *
 * LOCAL storage, unlike `lastProjectScope`'s session storage, and the difference
 * is deliberate. That module POINTS the rail at a Project on a page that carries
 * none — a stale pointer there quietly puts someone inside a Project they may
 * have lost access to. This one only OFFERS a named address the person chooses
 * to click; the router still resolves it and the scope still governs it, so a
 * Project that has gone answers with the denied surface rather than with a
 * silent substitution. Surviving a browser restart is the whole value: the list
 * is a work history, and a history that forgets overnight is not one.
 *
 * The label is not derived here. It is what the top bar drew for that address —
 * the governed path, with the object NAMED when the bar had resolved its name —
 * because rebuilding it from the address would mean rebuilding the object-name
 * read that the bar already did, and would print a ULID where the bar printed
 * "Site Europe — GA4".
 */
import { resolvableHref } from "./routeHref";

const KEY = "toorow_recent_routes";

/** Ten. Long enough to cover a working session's back-and-forth, short enough
 *  that the palette's first screen is still readable without scrolling. */
const CAP = 10;

export interface RecentRoute {
  /** A canonical console address, exactly as `buildPath` produced it. */
  path: string;
  /** The governed path as the top bar drew it — what the person saw up there. */
  label: string;
}

function read(): RecentRoute[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return value.filter((entry): entry is RecentRoute =>
      Boolean(entry)
      && typeof entry === "object"
      && typeof (entry as RecentRoute).path === "string"
      && typeof (entry as RecentRoute).label === "string");
  } catch {
    // A blocked or corrupted storage means no history, never a broken palette.
    return [];
  }
}

/** Record one visited address. Deduped by address — revisiting a page moves it
 *  back to the top and refreshes its label rather than adding a second row. */
export function rememberRoute(entry: RecentRoute): void {
  const path = resolvableHref(entry.path);
  const label = entry.label.trim();
  if (!path || !label) return;
  try {
    const kept = read().filter((candidate) => candidate.path !== path);
    localStorage.setItem(KEY, JSON.stringify([{ path, label }, ...kept].slice(0, CAP)));
  } catch {
    // A blocked storage is not a reason to fail a navigation.
  }
}

/** The history, newest first, with every entry the router no longer resolves
 *  dropped rather than offered. */
export function recentRoutes(): RecentRoute[] {
  return read().filter((entry) => resolvableHref(entry.path) !== null).slice(0, CAP);
}

/** Test seam, and the honest answer to a person who wants their trail gone. */
export function forgetRecentRoutes(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // Nothing to forget if nothing could be written.
  }
}
