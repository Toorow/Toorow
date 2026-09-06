/**
 * The pointer path of the composition — `visualization-and-rendering.md`,
 * *Composition is direct manipulation* (2026-08-15) and *The pointer places at
 * a position, and the shelf takes a field back* (2026-08-17).
 *
 * The screen-level suites fire only clicks, on purpose: they prove the keyboard
 * keeps parity. THIS file is the other half of that bargain — it fires
 * `dragstart`, `dragover` and `drop`, so a regression in the drag layer cannot
 * hide behind a green keyboard path. jsdom implements no DataTransfer, so each
 * gesture carries a minimal literal one, which is exactly what the component
 * reads (the move itself travels through the composer, never the payload).
 *
 * 2026-08-22, story 66.7. « Les suites d'écran tirent des clics, donc le clavier
 * garde la parité » était une DÉDUCTION, et elle est fausse sur quatre points :
 * un `fireEvent.click` sur un `<button>` ne dit rien d'Échap, ni de l'étiquette
 * qui NOMME la touche à presser, ni de `aria-pressed`, ni de la région vivante
 * — le seul retour qu'un lecteur d'écran reçoit d'un déplacement. Ces quatre-là
 * étaient implémentés et testés nulle part. Ils le sont en bas de ce fichier.
 */
import { render, screen, within, fireEvent } from "@testing-library/react";
import { useState } from "react";

import {
  DraggableField,
  FieldComposer,
  FieldShelf,
  FieldWell,
  type WellBindings,
  type WellField,
  type WellSpec,
} from "../ui";

const FIELDS: WellField[] = [
  { id: "m1", label: "Spend", role: "measure" },
  { id: "m2", label: "Clicks", role: "measure" },
  { id: "m3", label: "Sessions", role: "measure" },
  { id: "d1", label: "Country", role: "dimension" },
];

const WELLS: WellSpec[] = [
  { name: "values", label: "Values", accepts: ["measure"] },
  { name: "rows", label: "Rows", accepts: ["dimension"] },
];

function Harness({ initial }: { initial: WellBindings }) {
  const [bindings, setBindings] = useState<WellBindings>(initial);
  const placed = new Set(Object.values(bindings).flat());
  return (
    <FieldComposer
      fields={FIELDS}
      wells={WELLS}
      bindings={bindings}
      onChange={(next) => setBindings(next)}
    >
      <FieldShelf testId="shelf">
        <ul>
          {FIELDS.filter((field) => !placed.has(field.id)).map((field) => (
            <li key={field.id}>
              <DraggableField field={field} />
            </li>
          ))}
        </ul>
      </FieldShelf>
      {WELLS.map((well) => (
        <FieldWell key={well.name} well={well} />
      ))}
    </FieldComposer>
  );
}

/** jsdom has no DataTransfer; the component only writes to it. */
function transfer() {
  return { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
}

function boundIn(wellTestId: string): string[] {
  return within(screen.getByTestId(wellTestId))
    .queryAllByTestId(new RegExp(`^${wellTestId}-field-`))
    .map((chip) => chip.getAttribute("data-field") ?? "");
}

it("a field dragged from the shelf lands in the well that accepts it", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  const dataTransfer = transfer();

  fireEvent.dragStart(screen.getByTestId("field-m1"), { dataTransfer });
  fireEvent.dragOver(screen.getByTestId("well-values"), { dataTransfer });
  fireEvent.drop(screen.getByTestId("well-values"), { dataTransfer });

  expect(boundIn("well-values")).toEqual(["m1"]);
  // The gesture ended: nothing is carried, the shelf lost the field.
  expect(screen.queryByTestId("field-m1")).toBeNull();
});

it("a drop on a bound field inserts before it, and the insertion point was drawn", () => {
  render(<Harness initial={{ values: ["m1", "m2"], rows: [] }} />);
  const dataTransfer = transfer();

  fireEvent.dragStart(screen.getByTestId("field-m3"), { dataTransfer });
  fireEvent.dragOver(screen.getByTestId("well-values-field-m1"), { dataTransfer });
  // The insertion point is said while the field is still in the air.
  expect(screen.getByTestId("well-values-insertion-0")).toBeInTheDocument();
  fireEvent.drop(screen.getByTestId("well-values-field-m1"), { dataTransfer });

  expect(boundIn("well-values")).toEqual(["m3", "m1", "m2"]);
});

it("a chip dragged within its well lands where it was dropped, not last", () => {
  render(<Harness initial={{ values: ["m1", "m2", "m3"], rows: [] }} />);
  const dataTransfer = transfer();

  // m3 is carried from position 3 and dropped on m1: before m1.
  fireEvent.dragStart(screen.getByTestId("well-values-field-m3"), { dataTransfer });
  fireEvent.drop(screen.getByTestId("well-values-field-m1"), { dataTransfer });
  expect(boundIn("well-values")).toEqual(["m3", "m1", "m2"]);

  // And downward: m3 (now first) dropped on m2 — its own slot no longer counts.
  fireEvent.dragStart(screen.getByTestId("well-values-field-m3"), { dataTransfer });
  fireEvent.drop(screen.getByTestId("well-values-field-m2"), { dataTransfer });
  expect(boundIn("well-values")).toEqual(["m1", "m3", "m2"]);
});

it("a refused drop changes nothing, and the refusal names the role the well takes", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  const dataTransfer = transfer();

  fireEvent.dragStart(screen.getByTestId("field-d1"), { dataTransfer });
  fireEvent.dragOver(screen.getByTestId("well-values"), { dataTransfer });
  expect(screen.getByTestId("well-values-refusal")).toHaveTextContent(
    "Values takes measure, and Country is a dimension.",
  );
  // No insertion point is offered on a refusing well's chips either.
  fireEvent.drop(screen.getByTestId("well-values"), { dataTransfer });

  expect(boundIn("well-values")).toEqual([]);
  // The field went nowhere: it is still on the shelf.
  expect(screen.getByTestId("field-d1")).toBeInTheDocument();
});

it("a bound field dropped on the shelf is unbound", () => {
  render(<Harness initial={{ values: ["m1"], rows: [] }} />);
  const dataTransfer = transfer();

  fireEvent.dragStart(screen.getByTestId("well-values-field-m1"), { dataTransfer });
  fireEvent.dragOver(screen.getByTestId("shelf"), { dataTransfer });
  expect(screen.getByTestId("shelf")).toHaveAttribute("data-over", "true");
  fireEvent.drop(screen.getByTestId("shelf"), { dataTransfer });

  expect(boundIn("well-values")).toEqual([]);
  expect(screen.getByTestId("field-m1")).toBeInTheDocument();
});

it("the shelf's return control is the same move from the keyboard", () => {
  render(<Harness initial={{ values: ["m1"], rows: [] }} />);

  fireEvent.click(screen.getByTestId("well-values-field-m1"));
  fireEvent.click(screen.getByTestId("shelf-return"));

  expect(boundIn("well-values")).toEqual([]);
  expect(screen.getByTestId("field-m1")).toBeInTheDocument();
});

it("the shelf offers nothing for a field that is already unbound", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  const dataTransfer = transfer();

  fireEvent.dragStart(screen.getByTestId("field-m1"), { dataTransfer });
  expect(screen.queryByTestId("shelf-return")).toBeNull();
  fireEvent.dragOver(screen.getByTestId("shelf"), { dataTransfer });
  expect(screen.getByTestId("shelf")).not.toHaveAttribute("data-over");
});

// ---------------------------------------------------------------------------
// Le chemin CLAVIER — story 66.7. Ce qu'un clic ne prouve pas.
// ---------------------------------------------------------------------------

it("the chip names the key to press, and names a different one once carried", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  const chip = screen.getByTestId("field-m1");

  // Sans étiquette, un utilisateur au clavier voit un bouton et ne sait pas ce
  // qu'il déclenche : « Spend » ne dit pas qu'on peut le prendre.
  expect(chip).toHaveAttribute("aria-label", "Spend, measure. Press Enter to pick it up.");
  expect(chip).toHaveAttribute("aria-pressed", "false");

  fireEvent.click(chip);

  // Et la phrase CHANGE : la même étiquette dans les deux états ferait presser
  // Entrée une seconde fois pour reprendre ce qu'on tient déjà.
  expect(screen.getByTestId("field-m1")).toHaveAttribute(
    "aria-label",
    "Spend, measure, picked up. Press Escape to put it down.",
  );
  expect(screen.getByTestId("field-m1")).toHaveAttribute("aria-pressed", "true");
});

it("Escape puts the carried field down, and the wells stop offering a place", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  fireEvent.click(screen.getByTestId("field-m1"));
  // Pendant le port, le puits qui accepte propose sa place — le MÊME élément
  // que le pointeur vise.
  expect(screen.getByTestId("well-values-place")).toBeInTheDocument();

  fireEvent.keyDown(screen.getByTestId("field-m1"), { key: "Escape" });

  // Reposer n'est pas placer : rien n'a bougé, et plus rien n'est en l'air.
  expect(screen.queryByTestId("well-values-place")).toBeNull();
  expect(screen.getByTestId("field-m1")).toHaveAttribute("aria-pressed", "false");
  expect(boundIn("well-values")).toEqual([]);
});

it("a key that is not Escape leaves the field in the air", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  fireEvent.click(screen.getByTestId("field-m1"));

  fireEvent.keyDown(screen.getByTestId("field-m1"), { key: "a" });
  fireEvent.keyDown(screen.getByTestId("field-m1"), { key: "Tab" });

  // Un handler qui reposerait sur n'importe quelle touche ferait tomber le
  // champ dès qu'on tabule vers le puits où on voulait le mettre.
  expect(screen.getByTestId("field-m1")).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByTestId("well-values-place")).toBeInTheDocument();
});

it("the whole placement is reachable by keyboard alone, and it is announced", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);

  // Prendre.
  fireEvent.click(screen.getByTestId("field-m1"));
  // Poser, sur le bouton de placement que le puits rend PENDANT le port : le
  // pointeur et le clavier visent le même élément, donc il n'y a pas deux
  // chemins de code à garder d'accord.
  fireEvent.click(screen.getByTestId("well-values-place"));

  expect(boundIn("well-values")).toEqual(["m1"]);
  // Le seul retour qu'un lecteur d'écran reçoit du déplacement. Sans lui, le
  // champ disparaît de la liste et rien ne dit où il est allé.
  expect(screen.getByTestId("composer-announce")).toHaveTextContent(/Spend/);
});

it("a well that refuses the carried field says so where the gesture is", () => {
  render(<Harness initial={{ values: [], rows: [] }} />);
  // Une mesure portée au-dessus de Rows, qui n'accepte que des dimensions.
  fireEvent.click(screen.getByTestId("field-m1"));

  // Le refus est LÀ, à côté du geste — pas après un dépôt qui a échoué, ce qu'un
  // utilisateur au clavier ne verrait jamais puisqu'il ne survole rien.
  expect(screen.queryByTestId("well-rows-place")).toBeNull();
  expect(screen.getByTestId("well-rows-refusal")).toBeInTheDocument();
});
