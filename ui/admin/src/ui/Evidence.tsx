/**
 * Evidence rows — a governed record read field by field.
 *
 * No mockup covers this object, because it is not a screen element: it is the
 * answer to a defect that has now been found twice. Story 47.1's review found
 * the four Data workbenches rendering `JSON.stringify` inside a `<pre>`; the
 * Story 47.5 review found the same thing on the four Workbench confirmation
 * reviews — the mapping diff, the publication review, the rollback review and
 * the recovery preparation. An operator confirming an irreversible governed
 * operation was reading braces.
 *
 * The rendering lived privately in `data/DataObjectWorkbench.tsx`. It is here so
 * there is one vocabulary for "show me this record", not one per screen:
 *
 *   evidenceRows(record)  one nested level expanded into named rows
 *   displayValue(value)   a scalar reads as itself, a list as its members,
 *                         a reference-shaped object as the thing it names
 *   <EvidenceRows>        those rows as a two-column table
 *
 * Deeper structures stay on their own row, summarized — the point is that a
 * contract describing a record field by field is honoured field by field, not
 * that every leaf is reachable.
 */
import { Table, TableBody, TableCell, TableRow } from "../components/ui/table";
import { TableScroll } from "./Data";

/**
 * THE DATABASE'S WORD IS NOT THE PERSON'S WORD, and this is where the two meet.
 *
 * `label()` de-snakes a column name, which is a *typographic* repair and not a
 * vocabulary one: `content_hash` became `Content hash`, `joint_grain` became
 * `Joint grain`, and both were still the storage layer talking. Measured on the
 * Governance workbench 2026-08-16, that generic dump was the whole `Definition`
 * tab of several objects — a person read a column list.
 *
 * Two things travel per entry, and the second is the one that was missing:
 *
 *   name     the noun the product uses, when it differs from the column
 *   meaning  ONE sentence saying what the value decides
 *
 * Entries are added when a word actually reaches a screen — an exhaustive map of
 * every column would rot silently, and a word nobody sees costs nothing to leave
 * out. A key that is absent falls back to `label()`, which is the honest default
 * and not a hole.
 */
const FIELD_VOCABULARY: Record<string, { name?: string; meaning: string }> = {
  content_hash: {
    name: "Exact content",
    meaning:
      "The fingerprint of this exact version's content. Two versions with the same one hold the same thing; it is what pins a reading so a later edit cannot move it.",
  },
  dependency_fingerprint: {
    name: "Pinned dependencies",
    meaning:
      "The fingerprint of everything this version depends on. It changes when one of them is republished, which is how a stale confirmation is caught.",
  },
  joint_grain: {
    name: "Shared grain",
    meaning: "The columns several sources agree to be counted by, one row per combination.",
  },
  concept_kind: {
    name: "Kind",
    meaning: "Whether this is something measured (a metric) or something measured BY (a dimension).",
  },
  object_kind: {
    name: "Qualifies",
    meaning:
      "Which business object this field is about — the duration OF THE VIDEO rather than a free-floating duration. Empty means nobody has said.",
  },
  additivity_class: {
    name: "Can it be summed",
    meaning:
      "Whether adding this measure across rows gives a true total: additive always, semi-additive except across the dimensions named beside it, non-additive never.",
  },
  non_additive_dimensions: {
    name: "Never summed across",
    meaning: "The dimensions this measure must not be added over — a sum across them would be a wrong number, not a rounded one.",
  },
  ordinal: {
    name: "Position",
    meaning: "Where this sits in its ordered list. The order is part of the identity, not a display choice.",
  },
  scope: {
    name: "Who it belongs to",
    meaning: "Platform means every Project of this instance shares it; project means this Project alone declared it.",
  },
  aggregation: {
    meaning: "How several rows of this measure become one figure.",
  },
  evidence_hash: {
    name: "Evidence fingerprint",
    meaning: "The fingerprint of the evidence this reading was produced from.",
  },
};

/** `Attribute name` from `attribute_name`, or the product's own word for it. */
export function label(key: string): string {
  const known = FIELD_VOCABULARY[key]?.name;
  if (known) return known;
  return key.replaceAll("_", " ").replace(/^./, (first) => first.toUpperCase());
}

/** What this field DECIDES, in one sentence, or `null` when nobody wrote one. */
export function fieldMeaning(key: string): string | null {
  return FIELD_VOCABULARY[key]?.meaning ?? null;
}

/** A hash or an opaque identifier, shortened for reading but never invented.
 *
 *  A 64-character hash printed whole is a wall a person scrolls past; printed
 *  short it is something they can compare. The full value stays in the `title`
 *  so nothing is lost — truncating without keeping it would be destroying
 *  evidence to save a line. */
export function shortenOpaque(value: string): string {
  if (/^[0-9a-f]{32,}$/i.test(value)) return `${value.slice(0, 12)}…`;
  if (/^[a-z]{2,8}_[0-9A-HJKMNP-TV-Z]{26}$/.test(value)) {
    const [prefix, ulid] = value.split("_");
    return `${prefix}_${ulid.slice(0, 6)}…`;
  }
  return value;
}

/** A reference-shaped object read as the thing it names, not as its braces. */
export function summarizeObject(value: Record<string, unknown>): string {
  for (const key of ["label", "name", "display_name", "id", "object", "ref"]) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate) return candidate;
  }
  const entries = Object.entries(value).filter(
    ([, entry]) => entry !== null && entry !== undefined && entry !== "",
  );
  if (entries.length === 0) return "Unavailable";
  return entries
    .map(([key, entry]) => `${label(key)}: ${typeof entry === "object" ? "…" : String(entry)}`)
    .join(" · ");
}

/**
 * A scalar reads as itself; a list reads as its members; anything absent says
 * so. Objects reaching here are summarized rather than dumped — `evidenceRows`
 * has already turned the first nested level into named rows.
 */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  if (Array.isArray(value)) {
    if (value.length === 0) return "None";
    return value
      .map((entry) =>
        entry && typeof entry === "object"
          ? summarizeObject(entry as Record<string, unknown>)
          : String(entry),
      )
      .join(", ");
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return summarizeObject(value as Record<string, unknown>);
  //  Un hash de 64 caracteres ou un `ckv_01J…` complet est un mur qu on saute ;
  //  rogne, c est quelque chose qu on compare. La valeur ENTIERE reste dans le
  //  `title` de la cellule : rogner sans la garder detruirait de la preuve pour
  //  gagner une ligne.
  return shortenOpaque(String(value));
}

/** One row of a read record: the stored key, the word a person reads, the value.
 *
 *  THE KEY TRAVELS WITH THE LABEL. A label is a translation, and a translation
 *  that destroys its original leaves whoever has to fix the record — or read it
 *  beside a payload — with no way back to the column. It is kept on the row's
 *  `title`, which is the same disclosure `FieldCatalogRail` gives its facets. */
export interface EvidenceRow {
  key: string;
  label: string;
  value: unknown;
}

/**
 * Expand one nested level into named rows so a record shows fields, not a blob.
 *
 * `labels` is the CALLER's vocabulary for its own record — the pattern
 * `FieldCatalogRail` uses for the field catalogue's facets, and the reason it
 * is not in `FIELD_VOCABULARY`: `lifecycle` on an AI Path means "this walk can
 * still grow steps", and the same column on another object would not mean that.
 * A word that is true of one record only belongs to that record's screen; a
 * word that is true of every record belongs to `FIELD_VOCABULARY` above.
 */
export function evidenceRows(
  source: Record<string, unknown>,
  labels?: Record<string, string>,
): EvidenceRow[] {
  const rows: EvidenceRow[] = [];
  const named = (key: string) => labels?.[key] ?? label(key);
  for (const [key, value] of Object.entries(source)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const nested = Object.entries(value as Record<string, unknown>);
      if (nested.length > 0 && nested.length <= 12) {
        for (const [childKey, childValue] of nested) {
          rows.push({
            key: `${key}.${childKey}`,
            label: `${named(key)} · ${named(childKey)}`,
            value: childValue,
          });
        }
        continue;
      }
    }
    rows.push({ key, label: named(key), value });
  }
  return rows;
}

/**
 * The rows as a table. `label` names the scroll region, so a caller that shows
 * two records on one screen (a review beside its consequence) stays navigable
 * by keyboard.
 */
export function EvidenceRows({
  source,
  label: regionLabel,
  className,
  labels,
}: {
  source: Record<string, unknown>;
  label: string;
  className?: string;
  /** This record's own words, keyed by the stored key. See `evidenceRows`. */
  labels?: Record<string, string>;
}) {
  const rows = evidenceRows(source, labels);
  if (rows.length === 0) return null;
  return (
    <TableScroll label={regionLabel} className={className}>
      <Table>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.key}>
              {/* The stored key on `title`: the human word is what is read, and
                  the column it renames is one hover away. */}
              <TableCell className="w-64 font-semibold text-text" title={row.key}>
                {row.label}
              </TableCell>
              <TableCell className="text-ui text-text-secondary">{displayValue(row.value)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}
