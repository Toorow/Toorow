/**
 * The Project the person was last in.
 *
 * The scope surfaces — Organization Settings, User Account — carry no Project
 * in their address, by design: they do not act on one. But the rail does, and
 * removing it left those pages with no navigation at all. Jean, 2026-08-04:
 * *"en plus y a pas le menu"*.
 *
 * Picking a Project to point the rail at would be a guess, and this project
 * does not guess. Remembering the one the person came FROM is not a guess: it
 * is where they were a click ago, and it is the only Project that makes "back
 * to Data" mean anything from a page that has no Project.
 *
 * Session storage, not local: it belongs to this browsing session, and a stale
 * pointer surviving a restart would send someone into a Project they have since
 * lost access to. Absent or unreadable simply means no rail — never a fallback
 * to "the first Project", which is exactly the guess being avoided.
 */
const KEY = "toorow_last_project_scope";

export interface ProjectScope {
  organizationId: string;
  projectId: string;
}

export function rememberProjectScope(scope: ProjectScope): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(scope));
  } catch {
    // A blocked storage is not a reason to fail a navigation.
  }
}

export function lastProjectScope(): ProjectScope | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<ProjectScope>;
    return value.organizationId && value.projectId
      ? { organizationId: value.organizationId, projectId: value.projectId }
      : null;
  } catch {
    return null;
  }
}
