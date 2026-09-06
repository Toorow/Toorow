/**
 * WHICH entry surface an address names, and where a completed entry goes next.
 *
 * AD-42, 2026-08-12. `App.tsx` held three jobs: these URL decisions, the entry
 * surfaces that render before a scope exists, and the composition that wires
 * them. Measured on 44 edits since June, **50 of the 141 diff hunks landed in
 * this one** -- a change to entry routing had to open the file that composes the
 * whole shell.
 *
 * These are PURE: they read `window.location` and nothing of React. That is what
 * makes them testable on their own, and it is the reason they were worth moving
 * rather than left where they were written.
 *
 * The remaining pile in `App.tsx` -- 42 hunks on its import block -- is NOT a
 * defect and is deliberately left alone: a file that composes the entry has to
 * import what it composes. Splitting an import list for its own sake moves a
 * crossroads instead of removing it (module-boundaries, criterion 9).
 */

/** An invitation link — decided from the URL alone, ahead of any scope fetch. */
export function isInvitePath(p: string): boolean {
  return p === "/invite" || p === "/invite/";
}

let inviteFragmentCapture: { locationKey: string; bearer: string } | null = null;

export function takeInviteBearer(): string {
  const hash = window.location.hash || "";
  const locationKey = window.location.pathname + window.location.search;
  const bearer = hash.startsWith("#invite=") ? hash.slice("#invite=".length) : "";
  if (hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  if (bearer) {
    inviteFragmentCapture = { locationKey, bearer };
    return bearer;
  }
  if (inviteFragmentCapture?.locationKey === locationKey) {
    return inviteFragmentCapture.bearer;
  }
  return bearer;
}

/** The explicit organization-creation routes (typed or linked deliberately). */
export function isCreateOrgPath(p: string): boolean {
  return p === "/onboarding" || p === "/onboarding/" || p === "/create-org" || p === "/create-org/";
}

export function safeServerPath(value?: string): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return "/";
  }
  const parsed = new URL(value, window.location.origin);
  return parsed.origin === window.location.origin ? parsed.pathname + parsed.search : "/";
}

export function goToCreatedProject(_projectId: string): void {
  window.location.assign("/");
}

export function goToCreatedScope(_orgId: string, nextUrl?: string): void {
  window.location.assign(safeServerPath(nextUrl));
}
export function goToInvitationScope(nextUrl: string): void {
  window.location.assign(safeServerPath(nextUrl));
}
export function goHome(): void {
  // A full navigation, so the scope is refetched with the new membership.
  window.location.assign("/");
}

/**
 * Release the one-shot capture, once the bearer it held has been consumed.
 *
 * The capture is module-level and MUTABLE on purpose: `takeInviteBearer` runs in
 * a `useState` initializer, before `AuthGate` mounts, so the token never sits in
 * the URL where GIS could see it. Releasing it is therefore a second gesture,
 * and it is named here rather than reaching into the variable from outside --
 * a mutable that two files write is a mutable nobody owns.
 */
export function releaseInviteBearer(bearer: string): void {
  if (inviteFragmentCapture?.bearer === bearer) {
    inviteFragmentCapture = null;
  }
}
