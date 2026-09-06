import { useEffect, useRef } from "react";
import StableSidebar from "./StableSidebar";
import TopBar from "./TopBar";
import ContentRouter from "./ContentRouter";
import RouteState from "./RouteState";
import ErrorBoundary from "../ErrorBoundary";
import { useRoute, type CanonicalRoute } from "./router";
import { lastProjectScope, rememberProjectScope, type ProjectScope } from "./lastProjectScope";

/**
 * Which Project the rail points at on this address, or null for no rail.
 *
 * `README.md:77` said *"Global scope surfaces sit outside project navigation"*
 * and the shell read it as "no navigation at all", so Project Settings, Project
 * Access, Organization Settings, User Account and Getting Started rendered with
 * nothing to click. **Jean amended it on 2026-08-04** — *"en plus y a pas le
 * menu"*: the rail stays. `page-structure.md §A.7` carries the amendment.
 *
 * A scope surface still has to name a Project for the rail to mean anything.
 * Project Settings, Project Access and Getting Started carry one in the address.
 * Organization Settings and User Account do not, and choosing one for them would
 * be a guess — so the rail follows the Project the person was last in
 * ([[lastProjectScope]]), and renders only when there is one.
 */
export function resolveRailScope(
  route: Pick<CanonicalRoute, "organizationId" | "projectId">,
  remembered: ProjectScope | null,
): ProjectScope | null {
  if (route.organizationId && route.projectId) {
    return { organizationId: route.organizationId, projectId: route.projectId };
  }
  return remembered;
}

export default function ApplicationShell() {
  const { result, route, navigate } = useRoute();
  const focusedHeadingRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const main = document.querySelector<HTMLElement>("main");
    if (!main) return;
    let timeoutId: number | undefined;
    const focusHeading = () => {
      const heading = main.querySelector<HTMLElement>("h1");
      if (!heading || heading === focusedHeadingRef.current) return;
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      focusedHeadingRef.current = heading;
    };
    const observer = new MutationObserver(() => focusHeading());
    observer.observe(main, { childList: true, subtree: true });
    focusHeading();
    timeoutId = window.setTimeout(() => observer.disconnect(), 3000);
    return () => {
      observer.disconnect();
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, [result.kind, route.workspace, route.section, route.objectType, route.objectId, route.tab, route.versionId, route.action]);

  const backToOverview = route.scope === "project"
    ? () => navigate({
        workspace: "overview",
        section: "project-overview",
        objectType: null,
        objectId: null,
        tab: null,
        versionId: null,
        action: null,
      })
    : undefined;
  const stateSurface = result.kind === "unknown" || result.kind === "denied" || result.kind === "stale"
    ? <RouteState kind={result.kind} reason={result.reason} onBackToOverview={backToOverview} />
    : result.kind === "bootstrap"
      ? <RouteState kind="unknown" reason="The entry route has not resolved an authorized project yet." />
      : null;

  // Written on every project address, read on the ones that carry no Project.
  useEffect(() => {
    if (route.organizationId && route.projectId) {
      rememberProjectScope({ organizationId: route.organizationId, projectId: route.projectId });
    }
  }, [route.organizationId, route.projectId]);
  const railScope = resolveRailScope(route, lastProjectScope());

  return (
    <div className={`grid min-h-screen min-w-0 grid-cols-1 bg-background text-foreground ${railScope ? "lg:grid-cols-[16rem_minmax(0,1fr)]" : ""}`}>
      {railScope ? <StableSidebar scope={railScope} /> : null}
      <section className="grid min-w-0 grid-rows-[4.5rem_minmax(0,1fr)]">
        {/* Below `lg` the rail above does not render, and the bar opens the same
            rows in a drawer. It is handed the scope resolved HERE so the two
            hosts can never disagree about which Project the menu belongs to
            (`page-structure.md §G.2.3`). */}
        <TopBar railScope={railScope} />
        {/* The one gutter of the console. `application-v3.css` l. 236:
            `.main { padding: 32px 40px 40px }` — 40px at the sides, which is
            also what the screen sandbox renders every design review in. The
            shell was 8px tighter than both, and four screens then added a
            second gutter of their own on top. `--spacing-page-gutter` holds it
            so a page never restates it. */}
        <main
          className="min-w-0 bg-background px-4 pt-6 pb-8 sm:px-page-gutter sm:pt-8 sm:pb-page-gutter"
          data-route-kind={result.kind}
        >
          <ErrorBoundary resetKey={result.kind === "resolved" ? `${route.workspace}/${route.section}/${route.objectId ?? ""}/${route.versionId ?? ""}` : result.kind}>
            {stateSurface ?? <ContentRouter />}
          </ErrorBoundary>
        </main>
      </section>
    </div>
  );
}