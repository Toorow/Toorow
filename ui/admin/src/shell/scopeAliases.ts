/**
 * Readable addresses: the slug a person sees, the id everything else uses.
 *
 * The console addressed Organizations and Projects by ULID:
 *
 *     /org/org_01KYJ0NP8VKF4VSC3MPJFTW5FW/project/proj_01KYJ0NP8VKF4VSC3MPJFTW5FY/overview
 *     /org/acme/project/site-europe/overview
 *
 * Both name the same thing. Only one can be read, typed, or dictated.
 *
 * WHY THIS IS SAFE, AND IT IS THE ONLY REASON IT IS. The usual objection to a
 * slug in an address is that renaming breaks every link already shared. Here it
 * cannot: both slugs are immutable and the server ENFORCES it —
 * `PATCH /api/organizations/{id}` refuses with `slug_immutable` because the slug
 * names the organization's warehouse datasets (epic 24, decision 6), and the
 * project handler states `id and slug are immutable`. So a slug is exactly as
 * stable as a ULID, and this repo's doctrine on shareable addresses — "a shared
 * link that follows `latest` silently changes what it shows" — is upheld rather
 * than weakened.
 *
 * WHAT THIS IS NOT. The slug is an ADDRESS, never an identity. Every route
 * object, every API call, every piece of evidence and every MCP parameter keeps
 * carrying the id. Nothing downstream of the router learns that slugs exist.
 * That containment is what makes the change small.
 *
 * WHY A MODULE-LEVEL REGISTRY RATHER THAN CONTEXT. The router parses the address
 * before React renders, and it must answer synchronously — a hook cannot be read
 * from `parse()`. `ScopeProvider` publishes here once its orgs and projects
 * land; until then every lookup misses and the router keeps using ids, which is
 * exactly the behaviour that makes the migration safe: an address never breaks,
 * it is at worst less readable than it could be.
 */

interface Aliases {
  /** slug -> id, for reading an address. */
  idOf: Map<string, string>;
  /** id -> slug, for writing one. */
  slugOf: Map<string, string>;
}

const orgs: Aliases = { idOf: new Map(), slugOf: new Map() };
const projects: Aliases = { idOf: new Map(), slugOf: new Map() };

/** Notified when the registry changes, so the shell can canonicalize the bar. */
const listeners = new Set<() => void>();

export function onScopeAliasesChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function fill(target: Aliases, entries: Iterable<{ id: string; slug?: string | null }>): boolean {
  let changed = false;
  for (const entry of entries) {
    const slug = (entry.slug ?? "").trim();
    // A slug equal to the id teaches nothing and would make `isSlug` lie.
    if (!entry.id || !slug || slug === entry.id) continue;
    if (target.slugOf.get(entry.id) === slug) continue;
    target.slugOf.set(entry.id, slug);
    target.idOf.set(slug, entry.id);
    changed = true;
  }
  return changed;
}

/** Publish what the scope loaded. Additive: a slug is never forgotten, because
 *  an address already in a person's history must keep resolving. */
export function registerScopeAliases(
  organizations: Iterable<{ id: string; slug?: string | null; projects?: Iterable<{ id: string; slug?: string | null }> }>,
): void {
  let changed = false;
  for (const organization of organizations) {
    changed = fill(orgs, [organization]) || changed;
    if (organization.projects) changed = fill(projects, organization.projects) || changed;
  }
  if (changed) for (const listener of listeners) listener();
}

/** The id an address segment denotes. Unknown segments pass through unchanged,
 *  so a ULID address keeps working and so does one for an org not yet loaded. */
export function organizationIdFor(segment: string): string {
  return orgs.idOf.get(segment) ?? segment;
}

export function projectIdFor(segment: string): string {
  return projects.idOf.get(segment) ?? segment;
}

/** The segment to write for an id — its slug when known, the id otherwise. */
export function organizationSegmentFor(id: string): string {
  return orgs.slugOf.get(id) ?? id;
}

export function projectSegmentFor(id: string): string {
  return projects.slugOf.get(id) ?? id;
}

/** True when the address could be more readable than it is. The shell uses this
 *  to decide whether replacing the bar would actually change anything. */
export function hasReadableAlias(organizationId: string | null, projectId: string | null): boolean {
  return Boolean(
    (organizationId && orgs.slugOf.has(organizationId)) ||
    (projectId && projects.slugOf.has(projectId)),
  );
}

/** Tests only: the registry is module state and would otherwise leak between them. */
export function resetScopeAliases(): void {
  for (const target of [orgs, projects]) {
    target.idOf.clear();
    target.slugOf.clear();
  }
}
