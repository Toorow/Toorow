/**
 * The centre: ten typed binding wells, named by SEMANTIC ROLE (AC7).
 *
 * The labels are exactly the words `visualization-and-rendering.md:98-99` uses:
 *
 *     Measure · Dimension · Time · Series · Breakdown · Facet · Color · Size ·
 *     Label · Detail
 *
 * No well is ever labelled `xAxis`, `yAxis`, `encode`, `dataset` or `dataKey`. A
 * renderer-library word in a well label is how a person learns to think in the
 * renderer's model, and then asks for a renderer option -- which the grammar
 * cannot store.
 *
 * A MEMBER IS MOVED INTO A WELL, and the keyboard moves it just as far
 * (`visualization-and-rendering.md`, *Composition is direct manipulation, and
 * the keyboard keeps parity*, ratified 2026-08-15). This replaces decision D7
 * of story 50.4, which read the WCAG obligation -- 2.5.7 wants a single-pointer
 * alternative to dragging, 2.1.1 wants the whole operation from the keyboard --
 * as a reason to ship the `<select>` path ALONE. The obligation is on parity,
 * not on abstinence, and `ui/FieldWells.tsx` meets it with one component: the
 * button a person clicks to place a carried member is the same element the
 * pointer drops onto. The Explorer's Composition region uses that component
 * too, so the console has one way to compose and not two.
 *
 * A WELL WHOSE ROLE HAS NO SOURCE IS DISABLED, NOT HIDDEN. The vocabulary does
 * not shrink because a source is late. The Time well -- and any well a family
 * declares as classification-only -- renders with its reason and its owner and
 * cannot be operated. An ENABLED well that silently accepted any member,
 * because the validator cannot tell the roles apart, is the invented role AC4
 * clause 2 forbids.
 *
 * NO COMPATIBILITY LOGIC LIVES HERE. The offer is `member.role` (declared by
 * the server, per member) tested against `well.accepts` (declared by the
 * server, per family). That is rendering a server declaration, not deriving a
 * rule: the browser never decides which roles a well takes, and it says the
 * refusal in the words of the roles the well does take.
 */
import { FieldWell, Panel, PanelHeader, Status, type WellSpec } from "../../ui";
import type {
  VisualizationFamily,
  VisualizationMember,
  VisualizationRefusal,
} from "../visualizationClient";

export type Bindings = Record<string, string[]>;

/** One well as it is rendered: the family's declaration when it has one, or the
 *  vocabulary entry marked unavailable on this family when it does not. The list
 *  of ten NEVER shrinks -- a hidden well reads as a vocabulary that does not
 *  exist, and the next person asks for `xAxis` instead. */
export function renderableWells(
  family: VisualizationFamily,
  vocabulary: { name: string; label: string; accepts: string[] }[],
): WellSpec[] {
  const declared = new Map(family.wells.map((w) => [w.name, w]));
  return vocabulary.map((entry) => {
    const well = declared.get(entry.name);
    if (well) {
      return {
        name: well.name,
        label: well.label,
        accepts: well.accepts,
        required: well.required,
        max: well.max_members || null,
        available: well.available,
        unavailableReason: well.unavailable_reason ?? undefined,
        unavailableOwner: well.unavailable_owner ?? undefined,
      };
    }
    return {
      name: entry.name,
      label: entry.label,
      accepts: entry.accepts,
      required: false,
      max: 0,
      available: false,
      unavailableReason: `The ${family.label} family declares no ${entry.label} well.`,
      unavailableOwner: "This family's registry entry",
    };
  });
}

/** Every member the pinned Query Spec selected, as fields a well can hold. The
 *  role is the SERVER's, carried through untouched. */
export function membersAsFields(members: VisualizationMember[]) {
  return members.map((member) => ({
    id: member.id,
    label: member.label,
    role: member.role,
    hint: member.metadata?.grain?.value ?? undefined,
  }));
}

export default function BindingWells({
  family,
  vocabulary,
  refusalsByWellName,
}: {
  family: VisualizationFamily;
  /** The ten wells, from the server registry. Rendered in full, always. */
  vocabulary: { name: string; label: string; accepts: string[] }[];
  refusalsByWellName: Record<string, VisualizationRefusal[]>;
}) {
  return (
    <Panel flush>
      <PanelHeader
        title="Binding wells"
        description="The ten wells, named by semantic role. Disabled ones say why."
      />
      <ul className="m-0 flex list-none flex-col gap-4 p-5">
        {renderableWells(family, vocabulary).map((well) => {
          const refusals = refusalsByWellName[well.name] ?? [];
          return (
            <li key={well.name}>
              <FieldWell
                well={well}
                emptyHint={`Drag a ${well.accepts.join(" or ")} here, or pick one up in the field catalog and place it.`}
                refusals={refusals.map((refusal, index) => (
                  <Status
                    key={`${refusal.code}-${index}`}
                    as="block"
                    tone="error"
                    title={refusal.code.replace(/_/g, " ")}
                    data-testid={`well-${well.name}-server-refusal`}
                  >
                    {refusal.message}
                    {refusal.remedy ? ` ${refusal.remedy}` : ""}
                  </Status>
                ))}
              />
            </li>
          );
        })}
      </ul>
    </Panel>
  );
}
