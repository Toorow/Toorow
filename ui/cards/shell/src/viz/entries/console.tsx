/**
 * Story 50.5 AC8 -- the Console mount entry.
 *
 * A plain React component export. The Console consumes the runtime as ordinary
 * application code (`visualization-and-rendering.md:370-372`); there is no frame,
 * no `postMessage` and no window to open. It adds exactly one thing over the
 * runtime: the `console` profile.
 *
 * NOTHING ELSE MAY DIFFER. The three entries produce the same serialized visual
 * model, the same ordered datum keys, the same datum-key -> evidence map and the
 * same disclosure strings from the same envelope -- `__tests__/parity.test.tsx`
 * asserts it by deep equality with `profile` excluded.
 */

import VisualizationRuntime, {
  serializeVisualModel,
  type VisualizationRuntimeProps,
} from "../Runtime";
import type { RenderInput } from "../contracts";

export type ConsoleVisualizationProps = Omit<VisualizationRuntimeProps, "input"> & {
  input: Omit<RenderInput, "profile"> & { profile?: RenderInput["profile"] };
};

export default function ConsoleVisualization(props: ConsoleVisualizationProps) {
  const { input, ...rest } = props;
  return <VisualizationRuntime {...rest} input={{ ...input, profile: "console" } as RenderInput} />;
}

/** The serialized model this entry produces. Used by the parity proof. */
export function serializeForConsole(input: RenderInput, scope?: Element | null) {
  return serializeVisualModel({ ...input, profile: "console" }, scope);
}
