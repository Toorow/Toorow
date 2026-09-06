/**
 * The two contracts that made these primitives necessary, not their rendering.
 *
 * `Spinner` exists because fourteen files reached for MUI's `CircularProgress`
 * and one wrote `role="progressbar"` with no value — a promise of a number that
 * never arrives. So the test that matters is the ROLE, not the ring.
 *
 * `SheetInline` exists because the mockups' `.recovery-drawer` sits IN the flow
 * beside live content, and every library ships a modal Sheet instead. So the
 * test that matters is the ABSENCE of an overlay and of a focus trap — the two
 * things that would make the table it sits next to unreachable.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Sheet, SheetContent, SheetInline, SheetTrigger, Spinner } from "../ui";

describe("Spinner", () => {
  it("is a status, never a progressbar", () => {
    render(<Spinner label="Loading the run history" />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    // The defect being replaced: a determinate role with nothing to report.
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("always announces what is being waited for, visibly or not", () => {
    const { rerender } = render(<Spinner label="Loading the run history" />);
    // Present for a screen reader even when it is not drawn.
    expect(screen.getByText("Loading the run history")).toHaveClass("sr-only");
    rerender(<Spinner label="Loading the run history" showLabel />);
    expect(screen.getByText("Loading the run history")).not.toHaveClass("sr-only");
  });

  it("carries the tone as data, so it cannot disagree with the Status beside it", () => {
    render(<Spinner tone="warning" label="Retrying" />);
    expect(screen.getByRole("status")).toHaveAttribute("data-tone", "warning");
  });
});

describe("SheetInline — the in-flow panel", () => {
  it("is a labelled region, not a dialog", () => {
    render(<SheetInline title="Run diagnosis">evidence</SheetInline>);
    const panel = screen.getByRole("complementary", { name: "Run diagnosis" });
    expect(panel).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("draws NO overlay — the page beside it stays readable and operable", () => {
    const { container } = render(
      <div>
        <button type="button">a control on the page</button>
        <SheetInline title="Run diagnosis">evidence</SheetInline>
      </div>,
    );
    expect(container.querySelector('[data-slot="sheet-overlay"]')).toBeNull();
    // The whole point: what sits next to it is still reachable.
    expect(screen.getByRole("button", { name: "a control on the page" })).toBeVisible();
  });

  it("closes through the caller, and has no close control when it is permanent", async () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <SheetInline title="Run diagnosis" onClose={onClose}>evidence</SheetInline>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledOnce();

    rerender(<SheetInline title="Run diagnosis">evidence</SheetInline>);
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
  });
});

describe("SheetContent — the modal panel", () => {
  it("is a dialog and does draw the overlay the inline one refuses", async () => {
    render(
      <Sheet>
        <SheetTrigger>Open</SheetTrigger>
        <SheetContent aria-label="Field detail">panel</SheetContent>
      </Sheet>,
    );
    await userEvent.click(screen.getByText("Open"));
    expect(screen.getByRole("dialog", { name: "Field detail" })).toBeInTheDocument();
    expect(document.querySelector('[data-slot="sheet-overlay"]')).not.toBeNull();
  });
});
