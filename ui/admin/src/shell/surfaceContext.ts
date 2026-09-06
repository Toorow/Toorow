/**
 * What a surface table needs to render, named once -- AD-42, 2026-08-12.
 *
 * WHY THIS TYPE EXISTS. `ContentRouter` was ONE function of 613 lines that 35
 * distinct subjects had edited 44 times since June: every screen ever added to
 * the console was added inside it. The two dispatch tables it held now live in
 * their own files, and this is the contract between them -- the five (and ten)
 * values they used to reach by closure, made explicit so the move required no
 * edit to a single branch body.
 */
import type { ReactNode } from "react";
import type { ProjectGlobalRoute, ProjectWorkspaceRoute } from "./router";
import type { OwnerReference } from "./pages/ProjectOverview";

export type SurfaceContext = {
  /** LA PREUVE DE LA GARDE, PORTEE PAR LE TYPE. Les deux tables ne tournent
   *  qu'apres `if (route.scope !== "project") return ...` dans ContentRouter :
   *  avant l'extraction, TypeScript retrecissait l'union tout seul et
   *  `route.organizationId` s'y lisait `string`. Nommer ici le membre projet
   *  de l'union rend cette preuve explicite -- et c'est ce qui a permis de
   *  deplacer les branches SANS EN TOUCHER UNE SEULE. */
  readonly route: ProjectWorkspaceRoute | ProjectGlobalRoute;
  readonly projectId: string;
  readonly navigate: (patch: Record<string, unknown>) => void;
  readonly openOwner: (owner: OwnerReference) => void;
  readonly backToOverview: () => void;
  readonly onOpenDatastream: (objectId: string, tab?: string) => void;
  readonly onAddDatastream: () => void;
  readonly openAnalyzeResult: (id: string) => void;
  readonly openTestObject: (section: string, objectType: string) => (objectId: string) => void;
  readonly openDataObject: (section: string, objectType: string, objectId: string) => void;
};

export type SurfaceRenderer = (context: SurfaceContext) => ReactNode;
