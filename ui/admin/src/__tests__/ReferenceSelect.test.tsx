/**
 * ReferenceSelect — Story 48.3 AC1 and AC11.
 *
 * The component replaces two free-text inputs, so the property that matters is
 * negative: whatever an operator types, the value that leaves this component is a
 * code from the governed vocabulary or nothing at all. Everything else here is
 * the keyboard contract AC11 asks for explicitly.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReferenceSelect, type ReferenceItem } from "../ui";

const CURRENCIES: ReferenceItem[] = [
  { code: "EUR", display_name: "Euro", hint: "2" },
  { code: "EGP", display_name: "Egyptian Pound", hint: "2" },
];

function fetchItems(query: string): Promise<ReferenceItem[]> {
  const needle = query.trim().toUpperCase();
  return Promise.resolve(
    needle ? CURRENCIES.filter((item) => item.code.startsWith(needle)) : CURRENCIES,
  );
}

afterEach(() => vi.restoreAllMocks());

function setup(onChange = vi.fn()) {
  render(
    <ReferenceSelect
      endpoint="/api/reference/currencies"
      vocabularyLabel="currencies"
      value={null}
      onChange={onChange}
      fetchItems={fetchItems}
    />,
  );
  return { onChange, input: screen.getByRole("combobox") };
}

describe("ReferenceSelect", () => {
  it("never commits typed text that is not in the vocabulary", async () => {
    const user = userEvent.setup();
    const { onChange, input } = setup();

    await user.click(input);
    await user.type(input, "EURR");
    // No match, so the list is empty...
    expect(await screen.findByText(/No currencies matches/)).toBeInTheDocument();
    // ...and Enter must do nothing. This is the whole reason the component
    // exists: a free-text field would have accepted "EURR" here.
    await user.keyboard("{Enter}");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("commits only a code chosen from the ranked list", async () => {
    const user = userEvent.setup();
    const { onChange, input } = setup();

    await user.click(input);
    await user.type(input, "EU");
    const option = await screen.findByRole("option", { name: /EUR/ });
    fireEvent.mouseDown(option);
    expect(onChange).toHaveBeenCalledWith("EUR");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("is keyboard operable: arrows move, Enter commits, Escape closes", async () => {
    const user = userEvent.setup();
    const { onChange, input } = setup();

    await user.click(input);
    await screen.findAllByRole("option");
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    // Two items, active moved from index 0 (EUR) to index 1 (EGP).
    expect(onChange).toHaveBeenCalledWith("EGP");

    await user.click(input);
    await screen.findAllByRole("option");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryAllByRole("option")).toHaveLength(0));
  });

  it("announces the listbox and the active option to assistive technology", async () => {
    const user = userEvent.setup();
    const { input } = setup();

    expect(input).toHaveAttribute("aria-expanded", "false");
    await user.click(input);
    await screen.findAllByRole("option");
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(input).toHaveAttribute("aria-autocomplete", "list");
    // The active option is named, so a screen reader can read it without focus
    // ever leaving the input.
    await waitFor(() => expect(input.getAttribute("aria-activedescendant")).toBeTruthy());
    expect(screen.getByRole("listbox")).toHaveAccessibleName("currencies");
  });

  it("says it is a selector before it is touched", async () => {
    // Reported on the Create-project door: the field rendered as an `<input>`
    // with a placeholder, pixel-identical to the free-text project-name box
    // above it. Nothing on the screen said a list existed, so the answer to
    // "why is there no list of choices" was that the control never claimed to
    // have one. The list opening on focus is not an affordance; the indicator is.
    const { input } = setup();
    const indicator = document.querySelector('[data-slot="reference-select-indicator"]');
    expect(indicator).not.toBeNull();
    expect(indicator).toHaveAttribute("aria-hidden", "true");
    // And it must not eat the click that opens the list.
    expect(indicator).toHaveClass("pointer-events-none");
    expect(input).toHaveAttribute("aria-expanded", "false");
  });

  it("never commits from a list that answers an older query", async () => {
    // Found on 2026-08-04 by CreateOrgCurrency.test.tsx, but only under full-suite
    // load: loading is debounced, so between a keystroke and its response `items`
    // still holds the PREVIOUS answer. Enter then committed from it — a person
    // typed "XXX", pressed Enter, and left with "EUR". A code from the
    // vocabulary, so the free-text guard was satisfied, and not what they asked.
    let release: ((items: ReferenceItem[]) => void) | null = null;
    const onChange = vi.fn();
    render(
      <ReferenceSelect
        endpoint="/api/reference/currencies"
        vocabularyLabel="currencies"
        value={null}
        onChange={onChange}
        fetchItems={(query) =>
          query === ""
            ? Promise.resolve(CURRENCIES)
            : // The narrowed query never resolves: this IS the in-flight window.
              new Promise<ReferenceItem[]>((resolve) => {
                release = resolve;
              })
        }
      />,
    );

    const user = userEvent.setup();
    const input = screen.getByRole("combobox");
    await user.click(input);
    // The full list is on screen and EUR is active...
    await screen.findAllByRole("option");
    await user.type(input, "XXX");
    // ...and the moment the query moves on, the list must stop being committable.
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Searching/));
    await user.keyboard("{Enter}");
    expect(onChange).not.toHaveBeenCalled();

    // Once the answer for what was typed arrives, the component is usable again.
    // `release` is only assigned when the debounced load for "XXX" actually
    // starts — waiting for it is what makes this test about the in-flight window
    // rather than about the debounce timer.
    await waitFor(() => expect(release).toBeTypeOf("function"));
    release!([{ code: "XXX", display_name: "Example" }]);
    fireEvent.mouseDown(await screen.findByRole("option", { name: /XXX/ }));
    expect(onChange).toHaveBeenCalledWith("XXX");
  });

  it("says the list is unavailable rather than showing an empty one", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ReferenceSelect
        endpoint="/api/reference/currencies"
        vocabularyLabel="currencies"
        value={null}
        onChange={onChange}
        fetchItems={() => Promise.reject(new Error("network"))}
      />,
    );

    await user.click(screen.getByRole("combobox"));
    // "No matches" would read as "this value does not exist", which is a
    // different and wrong claim when the vocabulary simply could not be read.
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);
  });
});
