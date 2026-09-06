/**
 * A progress bar says how far, to EVERYBODY -- 2026-08-06.
 *
 * THE DEFECT THIS FILE CLOSES, and it was a class. `components/ui/progress.tsx`
 * destructured `value` out of its props and used it for one thing only: the
 * `translateX` of the indicator. The radix root therefore never received a
 * value, and radix derives `aria-valuenow`, `aria-valuetext` and `data-state`
 * from the value it is given -- so every progress bar in the console was an
 * INDETERMINATE progressbar. Sighted readers saw an exact fill; a screen reader
 * was told "something is in progress" and nothing else.
 *
 * It was five render sites, and the count is the point: `DatastreamRunLive`
 * (the days of a collection in flight), `OrgSettings` (datastreams against the
 * plan limit), `GettingStarted` (steps complete), `ComponentSheet`, and
 * `ui/PhaseTimeline`, which is itself mounted by every screen that draws a
 * phase. On three of those the value IS the information.
 *
 * These tests fail the moment the value stops reaching the root again.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Progress } from "../ui";
import { PhaseTimeline } from "../ui";

describe("Progress -- the value reaches the accessibility tree", () => {
  it("announces the value, its bounds and its state", () => {
    render(<Progress value={34} aria-label="Days collected" />);
    const bar = screen.getByRole("progressbar", { name: "Days collected" });
    expect(bar).toHaveAttribute("aria-valuenow", "34");
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
    // radix derives this from the value too: `indeterminate` is what every bar
    // of this console reported until the value was forwarded.
    expect(bar).toHaveAttribute("data-state", "loading");
  });

  it("announces a zero, which is a measurement and not an absence", () => {
    render(<Progress value={0} aria-label="Days collected" />);
    const bar = screen.getByRole("progressbar", { name: "Days collected" });
    expect(bar).toHaveAttribute("aria-valuenow", "0");
    expect(bar).not.toHaveAttribute("data-state", "indeterminate");
  });

  it("announces a finished bar as complete", () => {
    render(<Progress value={100} aria-label="Days collected" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("data-state", "complete");
  });

  it("stays indeterminate only when there is genuinely no value", () => {
    // The honest case: nothing was measured, so nothing is announced -- rather
    // than a `0` standing in for an absent measurement.
    render(<Progress aria-label="Days collected" />);
    const bar = screen.getByRole("progressbar");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(bar).toHaveAttribute("data-state", "indeterminate");
  });

  it("carries the value through the timeline that mounts it everywhere, WITH its subject", () => {
    // `PhaseTimeline` is the widest consumer: the Runs tab and every screen
    // that draws a phase. A repair to the primitive that did not reach it would
    // have left most of the product's bars silent.
    render(
      <PhaseTimeline
        phases={[{ label: "Collect", state: "running", percent: 42 }]}
      />,
    );
    // Named after the phase it belongs to: "42" with no subject is a worse
    // answer than the indeterminate bar it replaced.
    const bar = screen.getByRole("progressbar", { name: "Collect progress" });
    expect(bar).toHaveAttribute("aria-valuenow", "42");
  });
});

/**
 * THE GUARD, and it is the point of this file.
 *
 * Forwarding the value created the next defect immediately: three of the five
 * bars announced a number and no name -- "34", of what? Naming those three
 * fixes today and nothing else; the SIXTH bar reopens the class. So the mount
 * sites are swept, and a bar that carries a value without an accessible name
 * fails here, with its file and the tag that is missing the name.
 *
 * Sources are read through Vite's `import.meta.glob` rather than node:fs, for
 * the reason `apiSeamGuard.test.ts` gives: this workspace carries no
 * @types/node, so the fs route would not typecheck.
 */
const SOURCES = import.meta.glob("../**/*.tsx", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

/** Every `<Progress …/>` tag of a file, opening tag only. */
function progressTags(source: string): string[] {
  return source.match(/<Progress(?![A-Za-z])[\s\S]*?\/>/g) ?? [];
}

function mountSites(): Array<{ file: string; tag: string }> {
  return Object.entries(SOURCES)
    .map(([key, source]) => ({ file: key.replace(/^\.\.\//, ""), source }))
    .filter(({ file }) => !file.startsWith("__tests__/"))
    .flatMap(({ file, source }) => progressTags(source).map((tag) => ({ file, tag })));
}

describe("Progress -- every mount site names its bar", () => {
  it("finds the mount sites at all, so the sweep cannot pass on an empty list", () => {
    // A guard that matches nothing is a green light for everything. Five sites
    // were measured on 2026-08-06; the floor is what makes this test honest,
    // and it is deliberately not an exact count -- a sixth bar must be able to
    // land without touching this file, and be checked by the sweep below.
    expect(mountSites().length).toBeGreaterThanOrEqual(5);
  });

  it("refuses a bar that announces a value with no subject", () => {
    const unnamed = mountSites()
      .filter(({ tag }) => !/aria-label(ledby)?[=\s]/.test(tag))
      .map(({ file, tag }) => `${file} — ${tag.replace(/\s+/g, " ").slice(0, 120)}`);
    expect(
      unnamed,
      "A progress bar announces its value since 2026-08-06, so it must also " +
        "announce WHAT it measures. Add aria-label (or aria-labelledby) to:\n" +
        unnamed.join("\n"),
    ).toEqual([]);
  });
});
