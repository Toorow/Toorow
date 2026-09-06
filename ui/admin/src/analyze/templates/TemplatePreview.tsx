/**
 * The preview of a Chart Template — through the shared runtime, and through nothing else.
 *
 * THIS FILE IS THE SUBJECT OF THE ONE NON-NEGOTIABLE PROHIBITION OF THIS SURFACE.
 * `visualization-and-rendering.md`, amendment 2026-08-31: "The preview goes
 * through the shared runtime and through nothing else: it mounts
 * `VisualizationRuntime` on a Visualization Spec version materialised from the
 * template and a real Result, exactly as the console mounts a Render. A preview
 * that drew any other way would be a second engine, and that is the one
 * non-negotiable prohibition of this surface."
 *
 * So this component imports exactly ONE thing that can put a pixel on a screen:
 * `VisualizationMount`, the console's single adapter for the runtime's single
 * public entry. No renderer, no chart library, no canvas, no SVG of its own, no
 * thumbnail. `ChartTemplatePreviewSubtree.test.tsx` fails on the day any file
 * under `analyze/templates/` imports one — the guard is structural rather than
 * behavioural because a second drawing path is not a bug a render test would
 * catch, it is a file that exists.
 *
 * AND WHAT IT PREVIEWS IS REAL. There is no synthetic document here: the spec
 * shown is the one the server materialised through
 * `create_visualization_spec_version`, with the identifier it was stored under.
 * A preview drawn from a document nobody saved would be exactly the fabricated
 * screen this repository has a rule against.
 */
import VisualizationMount, { type VisualizationMountProps } from "../VisualizationMount";

import type { MaterializedSpec } from "./chartTemplateClient";

export function TemplatePreview({
  projectId,
  materialized,
}: {
  projectId: string;
  materialized: MaterializedSpec;
}) {
  //  The five fields the runtime's envelope asks for, taken off what the server
  //  WROTE. `spec` is the stored document; `id` is the version it was stored
  //  under. Neither is composed here.
  const spec = {
    visualization_spec_version_id: materialized.id,
    spec_contract_version: materialized.spec.spec_contract_version,
    schema_version: materialized.spec.schema_version,
    document: materialized.spec,
  } as unknown as VisualizationMountProps["spec"];

  return (
    <VisualizationMount
      projectId={projectId}
      resultId={materialized.result_id}
      spec={spec}
      profile="console"
    />
  );
}

export default TemplatePreview;
