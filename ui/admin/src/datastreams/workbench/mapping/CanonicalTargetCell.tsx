/**
 * The CANONICAL FIELD a column is bound to: named, reachable, and pickable.
 *
 * THE WORD, AND WHY IT IS NOT `CONCEPT` HERE (glossary, arbitration 2026-08-14).
 * `mdm_target` names a row of `app.mdm_canonical_fields` — the vocabulary a
 * binding is checked against, carrying no formula and no version history. A
 * **Semantic Concept** is the other object: versioned, expression-carrying,
 * published through a change set. This cell binds the first and never the
 * second, so its copy says *canonical field*. The stored token `concept_kind`
 * and the `data-testid`s below keep their spelling: a column name and a test
 * selector are not product words, and renaming them would buy nothing.
 *
 * TWO AMENDMENTS OF 2026-08-11 MEET IN THIS CELL.
 *
 * 5 — « Relier une colonne à son concept est un geste de cet écran. » The cell
 * was `text(...)` twice over, and no control anywhere in the console wrote
 * `mdm_target`. It now carries the selector and the door that declares a
 * project-scoped canonical field from the field's own row.
 *
 * 10 — « Un champ nomme son propriétaire et y mène. » A bound field led nowhere:
 * the tab stated its governed reach as `22 / 22` and offered not one address.
 * The canonical field is now a control that opens it — WHEN the router admits
 * the address, and inert text naming the reason when it does not. Never a
 * control whose address the router refuses: that is `Add a check`, which did
 * nothing on every Datastream of every Project until somebody measured it.
 */
import { Button, NativeSelect } from "../../../ui";
import type { OwnerReference } from "../../../shell/pages/ProjectSettings";
import {
  canonicalFieldOwner,
  NO_CANONICAL_ADDRESS,
  type CanonicalCatalog,
} from "./canonicalFields";

/** The identity shape itself, straight off the CHECK of migration 032
 *  (`ck_mdm_canonical_fields_id`). It is what tells an MDM IDENTITY apart from a
 *  canonical NAME, and both arrive under the same keys of the payload. */
const MDM_IDENTITY = /^mdm_[0-9A-HJKMNP-TV-Z]{26}$/;

export default function CanonicalTargetCell({
  fieldId,
  target,
  editable,
  catalog,
  claimed,
  onSelect,
  onDeclare,
  onOpenOwner,
  pending,
}: {
  fieldId: string;
  /** The canonical field in force on this field, or the one picked and not yet
   *  confirmed. Empty string means the column reaches no governed target. */
  target: string;
  editable: boolean;
  catalog: CanonicalCatalog;
  /** Canonical fields another binding or treatment already produces. Offering
   *  one twice composes a mapping `dispatch_mapping_collision` refuses while
   *  rows land. */
  claimed: Set<string>;
  onSelect: (value: string | null) => void;
  onDeclare: () => void;
  onOpenOwner?: (owner: OwnerReference) => void;
  /** True when this row carries a pick nobody has confirmed yet. */
  pending: boolean;
}) {
  const entry = catalog.fields.find((field) => field.id === target) ?? null;
  const label = entry?.canonical_name || target;
  const owner = MDM_IDENTITY.test(target) ? canonicalFieldOwner(target) : null;

  return (
    <div className="grid gap-1">
      {target === "" ? (
        <span className="text-caption text-text-secondary">None</span>
      ) : owner && onOpenOwner ? (
        <Button
          variant="link"
          size="xs"
          className="px-0"
          data-testid={`open-concept-${fieldId}`}
          onClick={() => onOpenOwner(owner)}
        >
          {label}
        </Button>
      ) : (
        <>
          <span className="font-mono text-caption">{label}</span>
          {MDM_IDENTITY.test(target) && (
            <span
              className="text-caption text-text-secondary"
              data-testid={`concept-inert-${fieldId}`}
            >
              {NO_CANONICAL_ADDRESS}
            </span>
          )}
        </>
      )}

      {editable && (
        <div className="grid gap-1">
          <NativeSelect
            aria-label={`Canonical field for ${fieldId}`}
            data-testid={`bind-concept-${fieldId}`}
            value={target}
            disabled={catalog.loading}
            onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
              onSelect(event.target.value === "" ? null : event.target.value)
            }
          >
            <option value="">No governed target</option>
            {/* A canonical field already in force on this field stays selectable
                even when the catalog does not carry it — a project whose
                vocabulary cannot be read must not silently drop the binding a
                person is looking at. */}
            {target !== "" && !entry && <option value={target}>{target}</option>}
            {catalog.fields.map((field) => (
              <option
                key={field.id}
                value={field.id}
                disabled={field.id !== target && claimed.has(field.id)}
              >
                {field.canonical_name} · {field.concept_kind}
                {field.id !== target && claimed.has(field.id) ? " (already claimed)" : ""}
              </option>
            ))}
          </NativeSelect>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="xs"
              // The label names both doors and cannot shorten (glossary rule
              // below) — but at the main-column width its nowrap floor was the
              // one thing pushing the whole table out of the frame (measured:
              // 826px table in a 790px container, this button the 307px floor).
              // A pill that folds is honest; a column that leaves the frame is
              // the epic-58 refusal.
              className="h-auto min-h-6 whitespace-normal text-left"
              data-testid={`declare-concept-${fieldId}`}
              onClick={onDeclare}
            >
              {/* Both doors, named on the one control that opens them — the
                  panel's first question is which one applies. The glossary rule
                  is explicit: a control that mints NAMES the object it mints,
                  and « Declare a concept… » is refused wherever the gesture can
                  produce either one. A person needing `CPA = cost / conversions`
                  had no word for it on this tab and left the Datastream to find
                  one. */}
              Declare a canonical field or a Semantic Concept…
            </Button>
            {pending && (
              <span
                className="text-caption text-text-secondary"
                data-testid={`bind-pending-${fieldId}`}
              >
                Not written yet
              </span>
            )}
          </div>
          {catalog.error && (
            <span
              className="text-caption text-text-secondary"
              data-testid={`concept-catalog-error-${fieldId}`}
            >
              {catalog.error}
            </span>
          )}
          {!catalog.error && !catalog.loading && catalog.fields.length === 0 && (
            <span className="text-caption text-text-secondary">
              This project has declared no canonical field yet. Declare the one this column means.
            </span>
          )}
        </div>
      )}
    </div>
  );
}
