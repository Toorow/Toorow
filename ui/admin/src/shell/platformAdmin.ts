/**
 * Whether this person may act on the platform itself — read from the server.
 *
 * WHY THIS EXISTS. `/platform/clocks` is a complete screen: it edits a declared
 * cadence, pushes it into Cloud Scheduler and fires a clock on demand. Nothing in
 * the console navigated to it — `grep -rln 'globalSurface: "platform"'` returned
 * `router.tsx` and nothing else — so the only way in was to type the address. An
 * editing screen nobody can reach edits nothing.
 *
 * The link could not simply be added, because the route is gated server-side by
 * `_enforce_platform_admin` / `TOOROW_SUPER_ADMINS`, and a refusal there is a 404:
 * showing the entry to everyone would put a dead link in every sidebar.
 *
 * The answer already existed and no client read it. `GET /api/me/profile` returns
 * `is_super_admin`, computed by `admin_api.py` from the SAME `is_super_admin` the
 * gate calls, on the same identity — added for organization creation with a
 * comment saying the console knew nothing about it. This module is the second
 * reader.
 *
 * IT STAYS ADVISORY. The gate is the authority; this only decides what the shell
 * is allowed to OFFER. Unknown is not "no": it is unknown, and it renders nothing
 * — a sidebar that flickered an admin entry in and out on every reload would be
 * worse than one that waits.
 */
import { useEffect, useState } from "react";
import { apiFetch } from "../lib/apiFetch";

const PROFILE_ENDPOINT = "/api/me/profile";

/** `null` while unknown — never rendered as a refusal. */
export type PlatformAdmin = boolean | null;

/**
 * Read the flag once. Never throws: a profile that cannot be read leaves the
 * answer unknown, which hides the entry, which is the safe direction.
 */
export async function fetchIsPlatformAdmin(): Promise<PlatformAdmin> {
  try {
    const response = await apiFetch(PROFILE_ENDPOINT, { cache: "no-store" });
    if (!response.ok) return null;
    const profile = (await response.json()) as { is_super_admin?: unknown };
    // Only a literal `true` counts. A server that stops sending the field must
    // not be read as a promotion.
    return profile?.is_super_admin === true;
  } catch {
    return null;
  }
}

export function usePlatformAdmin(): PlatformAdmin {
  const [isAdmin, setIsAdmin] = useState<PlatformAdmin>(null);

  useEffect(() => {
    let cancelled = false;
    void fetchIsPlatformAdmin().then((answer) => {
      if (!cancelled) setIsAdmin(answer);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return isAdmin;
}
