/**
 * What an active capability draws ON A VALUE and ON A FIELD — lot B3, amendment 11.
 *
 * « Une capacité activée AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS L'APERÇU —
 * jamais une liste de modules. Un inventaire de modules n'est pas une
 * fonctionnalité : l'effet l'est. » These two marks ARE the effect, and they live
 * here because both readings of a day draw them: the two single blocks of
 * `DayReadingPanel` and the paired table of `PairedReading`. Holding them in
 * either file would have made one of the two import the other, and a mark that
 * differs between the single reading and the paired one is two answers about one
 * column.
 *
 * NOTHING HERE DECIDES ANYTHING. The server names the capability, the columns it
 * lands on and every sentence; these components draw what they are handed. A cell
 * that looked monetary because of its spelling, or a column marked as a country
 * because it is called `country`, would be a classification invented in a
 * browser — the exact reasoning story 48.3 removed from the server.
 */
import { Badge, TONE_TEXT } from "../../ui";
import { cn } from "../../lib/cn";
import type { MoneyRowValues } from "./breakdownTypes";

/**
 * The line under a designated amount — story 58.7, now on both readings.
 *
 * `devise · taux · date du taux`, and NOT ONE OF THE THREE IS COMPOSED HERE. When
 * the conversion did not happen the server's sentence takes the whole line: a
 * `—` there would read as "there is nothing to say about this amount", when what
 * happened is that a rate was missing and the figure is still in the currency the
 * source reported. `1.0` and a default date are the same lie with more digits.
 */
export function MoneyLine({ values }: { values: MoneyRowValues }) {
  if (values.money_gap_code) {
    return (
      <span className="mt-1 block text-caption text-text-secondary" data-testid="money-gap">
        {values.money_gap_message ?? values.money_gap_code}
      </span>
    );
  }
  return (
    <span className="mt-1 block text-caption text-text-secondary" data-testid="money-line">
      {[values.native_currency, values.fx_rate, values.fx_as_of_date]
        .filter((part): part is string => Boolean(part))
        .join(" · ")}
    </span>
  );
}

/** The columns one capability lands on, on one side, with what to say about them. */
export interface ColumnMark {
  title: string;
  message: string | null;
  columns: Set<string>;
}

/**
 * Build the mark of ONE capability for ONE side, or `null`.
 *
 * `null` when the capability is absent — it is off, and « éteinte, la capacité
 * n'apparaît nulle part » — or when it names no column of this side. An empty
 * `Set` held open would make every header pay for a mark nobody can see.
 */
export function markOf(
  capability: { title: string; message: string | null; fields: { collected: string[]; mapped: string[] } } | undefined,
  zone: "collected" | "mapped",
): ColumnMark | null {
  const columns = capability?.fields?.[zone] ?? [];
  if (!capability || columns.length === 0) return null;
  return { title: capability.title, message: capability.message, columns: new Set(columns) };
}

/** Does this capability land on this column of this side? The one predicate, so
 *  the single reading and the paired table cannot disagree about one column. */
export function isMarked(mark: ColumnMark | null | undefined, name: string): boolean {
  return Boolean(mark && mark.columns.has(name));
}

/**
 * A column name, coloured when a capability lands on it.
 *
 * THE COLOUR IS NEVER ALONE. A highlight readable by hue only is not readable at
 * all for part of the estate, so the capability's own name sits beside it as a
 * badge and its sentence as the title — the same rule the paired cell already
 * holds for its `changed` verdict. Unmarked, the header renders exactly as it did
 * before this lot: no badge, no colour, no space held.
 *
 * FOR A HEADER THAT IS ALREADY A PILL — the double pill of the paired table — use
 * `isMarked` and tone the pill itself instead. A badge inside a badge is two
 * chips saying one thing, and it is what this component must not become.
 */
export function MarkedColumnName({
  name,
  mark,
}: {
  name: string;
  mark: ColumnMark | null;
}) {
  if (!isMarked(mark, name) || !mark) return <>{name}</>;
  // AND THE BADGE IS DROPPED WHEN IT WOULD REPEAT THE COLUMN. Measured in the
  // sandbox: the mapping binds `country` to the canonical dimension called
  // `country`, and the header read « COUNTRY COUNTRY ». A chip that says the word
  // already under it teaches nothing and costs a line of width on a table that
  // must not leave the frame. The capability still reaches a reader who cannot
  // see the colour — through the title and through text nobody has to see — and
  // the badge comes back the moment the two names differ, which is the ordinary
  // case (`pays` marked `Country`).
  const repeats = mark.title.trim().toLowerCase() === name.trim().toLowerCase();
  return (
    <span
      className="inline-flex min-w-0 items-center gap-1"
      data-testid="capability-marked-field"
      title={[mark.title, mark.message].filter(Boolean).join(" · ")}
    >
      <span className={cn("truncate", TONE_TEXT.info)}>{name}</span>
      {repeats ? (
        <span className="sr-only">{mark.title}</span>
      ) : (
        <Badge tone="info" className="max-w-full">
          <span className="truncate">{mark.title}</span>
        </Badge>
      )}
    </span>
  );
}
