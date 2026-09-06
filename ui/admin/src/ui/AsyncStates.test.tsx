/**
 * The four things a screen says while it has no answer, and the one property
 * 76-4 added to all of them: **there is always something to press, or a named
 * place to go.**
 *
 * WHY A TEST AND NOT ONLY THE TYPE. `Failure`'s `action` is required in the
 * type, and the compiler is the instrument that keeps the nineteenth call site
 * honest — a test cannot prove that, because a file that omits the prop does not
 * build. What a test CAN prove, and what the type cannot, is that the required
 * prop actually reaches the DOM: a component may take an `action` and drop it,
 * which is how a screen ends up type-safe and still a dead end. `Status` renders
 * `action` in a `shrink-0` slot beside the body, and these assertions are what
 * holds it there.
 *
 * The other half is the WORDING. `Retry` exists so nineteen screens do not spell
 * one gesture nineteen ways, and `console-presentation.md` §5's « one fallback
 * action » is only one action if it is also one word.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Failure, Loading, NoScope, ObjectNotFound, ProjectNotFound, Retry } from "./AsyncStates";

describe("Failure", () => {
  it("renders the action it is given, beside the cause", () => {
    const noop = () => {};
    render(<Failure message="The warehouse refused the read." action={<Retry onClick={noop} />} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The warehouse refused the read.");
    // Inside the block, not floating under it: an action a person has to hunt
    // for is the defect WidgetCardsPage carried.
    expect(alert).toContainElement(screen.getByRole("button", { name: "Retry" }));
  });

  it("titles itself with the subject when it is given one", () => {
    // `This could not be loaded` is true of everything, which is why every one
    // of the nineteen sites said it. `what` is the reader's own noun.
    render(<Failure what="The media plans" message="500" action={<span />} />);
    expect(screen.getByText("The media plans could not be read")).toBeInTheDocument();
  });

  it("keeps the general title when the subject is not named", () => {
    render(<Failure message="500" action={<span />} />);
    expect(screen.getByText("This could not be loaded")).toBeInTheDocument();
  });

  it("is announced as an alert, not politely", () => {
    // An error interrupts; `Status` decides this from the tone, and `Failure`
    // is the one caller that must never be able to soften it.
    render(<Failure message="500" action={<span />} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("Retry", () => {
  it("is one word, and it is a control", () => {
    let pressed = 0;
    render(<Retry onClick={() => { pressed += 1; }} />);
    const button = screen.getByRole("button", { name: "Retry" });
    button.click();
    expect(pressed).toBe(1);
  });
});

describe("ObjectNotFound", () => {
  it("names the object, and offers the collection it belongs to", () => {
    render(<ObjectNotFound what="Report" collection="Reports" collectionHref="/analyze/reports" />);
    expect(screen.getByText("Report not found")).toBeInTheDocument();
    const back = screen.getByRole("link", { name: "Back to Reports" });
    expect(back).toHaveAttribute("href", "/analyze/reports");
  });

  it("says why, in the two answers it deliberately does not separate", () => {
    render(<ObjectNotFound what="Render" collection="Renders" collectionHref="/x" />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "does not exist in this project, or you cannot see it",
    );
  });

  it("takes a sharper sentence where the absence has one", () => {
    render(
      <ObjectNotFound
        what="Visualization Spec version"
        collection="Renders"
        collectionHref="/x"
        detail="This Render pins vsv_EXAMPLE, which this Project cannot read."
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("which this Project cannot read");
    // And the way out survives the sharper sentence.
    expect(screen.getByRole("link", { name: "Back to Renders" })).toBeInTheDocument();
  });
});

describe("ProjectNotFound", () => {
  it("names the control instead of mounting a second copy of it", () => {
    // The project switcher is in the TopBar and is on screen at this moment.
    // §5 asks that the person be able to act, not that every empty state grow
    // its own switcher.
    render(<ProjectNotFound />);
    expect(screen.getByText("Project not found")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "project switcher at the top of the screen",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("NoScope and Loading are still the other two statements", () => {
  it("NoScope is not an empty state and not a failure", () => {
    render(<NoScope what="reports" />);
    expect(screen.getByText("No project selected")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Loading keeps its accessible name while it has nothing to say", () => {
    render(<Loading label="reports" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading reports");
  });
});
