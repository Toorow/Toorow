/**
 * The pager's three states per direction — the distinction a boolean cannot
 * make (`ui/Pager.tsx`): `undefined` draws nothing, `null` draws a disabled
 * way that exists, a handler draws the gesture. Three screens page through
 * this one control now, so this is where the rules are pinned.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import { Pager } from "../ui";

it("draws nothing for a direction the screen does not have", () => {
  render(<Pager onNext={() => {}} data-testid="pager" />);
  expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();
  // Governance pages with a cursor and cannot walk back: no dead control.
  expect(screen.queryByRole("button", { name: "Previous page" })).toBeNull();
  expect(screen.queryByRole("button", { name: "First page" })).toBeNull();
});

it("draws a disabled control for a way that exists but leads nowhere", () => {
  render(<Pager onFirst={null} onPrevious={null} onNext={() => {}} />);
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "First page" })).toBeDisabled();
});

it("spells its buttons with the screen's own noun, so two axes cannot collide", () => {
  const next = vi.fn();
  render(
    <>
      <Pager unit="rows" label="Row pages" onNext={next} />
      <Pager unit="columns" label="Column pages" onNext={() => {}} />
    </>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Next rows" }));
  expect(next).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Next columns" })).toBeInTheDocument();
});

it("disables every direction while an answer is in flight", () => {
  const next = vi.fn();
  render(<Pager busy onPrevious={() => {}} onNext={next} />);
  fireEvent.click(screen.getByRole("button", { name: "Next page" }));
  expect(next).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
});
