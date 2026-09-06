/**
 * The three DOM methods jsdom does not implement and Radix's popup primitives
 * call unconditionally.
 *
 * Without them a `Select` throws the moment its trigger is clicked, which reads
 * in a test report as "the screen is broken" rather than "the environment is
 * incomplete". Importing this file is the whole fix.
 *
 * It lives here rather than in `test-setup.ts` deliberately: that file is being
 * edited by a parallel session, and a change dropped into a file someone else
 * is rewriting is a change that disappears.
 */
import { vi } from "vitest";

if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = vi.fn(() => false);
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

/**
 * Pick an option from a Radix `Select` by its visible text.
 *
 * A Radix select is a button and a portalled listbox, not a `<select>`, so
 * `selectOptions` and `fireEvent.change` do nothing to it. This opens it and
 * clicks the option, which is what a person does.
 */
export async function chooseOption(
  user: { click: (element: Element) => Promise<void> },
  trigger: Element,
  optionText: string | RegExp,
) {
  const { screen } = await import("@testing-library/react");
  await user.click(trigger);
  await user.click(await screen.findByRole("option", { name: optionText }));
}
