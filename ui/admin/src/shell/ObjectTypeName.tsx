/**
 * An object type, read as a word — and, when nothing declares one, in double.
 *
 * `console-presentation.md` §4: *"A type declared nowhere does not print its
 * token: it renders `Unknown object type` with the token in monospace beside
 * it, so the missing declaration is visible and attributable."*
 *
 * WHY A COMPONENT AND NOT A STRING. `objectLabel()`
 * (`governance/contracts.ts`) answers the places that can only produce a
 * string — a sort value, a `title` attribute, a sentence being composed — and
 * for an undeclared type it can only say `Unknown object type`. That is honest
 * but it loses the one piece of information a person could act on: WHICH type.
 * Rendering is where both fit, and `ObjectId` is the console's only rendering of
 * an identifier (`ui/Data.tsx`), so the token goes through it rather than into a
 * second monospace span.
 *
 * WHY IT LIVES IN `shell/` AND NOT IN `ui/`. It reads the navigation registry.
 * `ui/` is the primitive layer and knows nothing about routes; `shell/` already
 * reads both — `TopBar.tsx` imports `./navigation` and `../ui` on the same
 * screen — so this is the layer where the two meet without inverting anything.
 */
import { objectTypeLabel } from "./navigation";
import { UNKNOWN_OBJECT_TYPE } from "../governance/contracts";
import { ObjectId } from "../ui";

export function ObjectTypeName({ objectType }: { objectType: string }) {
  const declared = objectTypeLabel(objectType);
  if (declared) return <>{declared}</>;
  return (
    <>
      {UNKNOWN_OBJECT_TYPE} <ObjectId value={objectType} title="Object type" />
    </>
  );
}
