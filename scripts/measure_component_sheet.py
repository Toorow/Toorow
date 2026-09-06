"""Measure the rendered component sheet against the mockup contract.

Every expectation below is quoted from a validated mockup rule. A component
that disagrees with its own documented source is reported, so "adapted" is
measured rather than declared.
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parent.parent

URL = "http://localhost:5199/debug/components"

# (label, selector, {css property: expected}) — expectations from the mockups.
CHECKS = [
    ("button default", '[data-slot=button][data-size=default]',
     {"height": "40px", "borderRadius": "999px", "paddingLeft": "16px",
      "fontSize": "13px", "fontWeight": "700", "borderTopWidth": "1px"}),
    ("button sm", '[data-slot=button][data-size=sm]',
     {"height": "32px", "borderRadius": "999px", "fontSize": "13px"}),
    ("button icon", '[data-slot=button][data-size=icon]',
     {"height": "40px", "width": "40px", "borderRadius": "10px"}),
    ("badge", '[data-slot=badge]',
     {"borderRadius": "999px", "fontSize": "12px", "minHeight": "28px",
      "paddingLeft": "10px"}),
    ("table th", '[data-slot=table-head]',
     {"height": "44px", "fontSize": "12px", "fontWeight": "700",
      "textTransform": "uppercase", "letterSpacing": "0.42px"}),
    ("table td (rich row)", '[data-slot=table-row] [data-slot=table-cell]',
     {"height": "64px", "fontSize": "14px"}),
    ("panel", '[data-slot=table-container]',
     {}),
    ("input", '[data-slot=input]',
     {"height": "44px", "borderRadius": "12px", "paddingLeft": "14px", "fontSize": "14px"}),
    ("select trigger", '[data-slot=select-trigger]',
     {"height": "44px", "borderRadius": "12px", "fontSize": "14px"}),
    ("label", '[data-slot=label]', {"fontSize": "13px", "fontWeight": "700"}),
    ("tabs list", '[data-slot=tabs-list][data-variant=line]',
     {"height": "52px", "columnGap": "28px"}),
    ("tabs trigger", '[data-slot=tabs-trigger]', {"fontSize": "14px", "fontWeight": "600"}),
    ("breadcrumb list", '[data-slot=breadcrumb-list]', {"fontSize": "13px"}),
    ("avatar (36px, radius 10)", '[data-slot=avatar][data-size=default]', {"borderRadius": "10px"}),
    # Two bars: the 8px meter (health-bar) and the 6px operation bar
    # (history-progress). Both measured, both drawn in the mockups.
    ("progress · meter", '[data-slot=progress]', {"height": "8px"}),
    ("switch", '[data-slot=switch][data-size=default]', {"height": "22px", "width": "38px"}),
    # 18px, not the shadcn 16: Jean 2026-07-29 asked for a mark that reads as
    # a solid object. Square, so it cannot be mistaken for the radio.
    ("checkbox", '[data-slot=checkbox]', {"height": "18px", "borderRadius": "6px", "borderTopWidth": "2px"}),
    # Same 18px so a form of mixed controls lines up; circular, and a ring
    # rather than a filled disc, which is what makes it read as a radio.
    ("radio item", '[data-slot=radio-group-item]', {"height": "18px", "borderRadius": "999px", "borderTopWidth": "2px"}),
]

OVERLAYS = [
    ("dialog content", '[data-slot=dialog-content]',
     {"borderRadius": "16px", "padding": "24px"}),
    ("dropdown content", '[data-slot=dropdown-menu-content]',
     {"borderRadius": "14px", "padding": "10px"}),
    ("dropdown item", '[data-slot=dropdown-menu-item]',
     {"minHeight": "42px", "borderRadius": "10px", "fontSize": "14px"}),
    ("tooltip content", '[data-slot=tooltip-content]',
     {"borderRadius": "10px", "fontSize": "12px"}),
    ("select content", '[data-slot=select-content]',
     {"borderRadius": "14px"}),
    ("select item", '[data-slot=select-item]',
     {"minHeight": "42px", "borderRadius": "10px", "fontSize": "14px"}),
]

# ---------------------------------------------------------------------------
# The three refusals of the epic-58 contract, which NOTHING measured before.
#
# "A pill on two lines, a cell that folds, a column that leaves the frame -- the
# three are seen and MEASURED." Vitest can measure none of them: `jsdom` with
# `css: false` applies no stylesheet and computes no layout, so every rectangle
# is zero and the five test files that stub `offsetWidth` by hand are measuring
# their own stub. This is where the three become real numbers.
#
# THE WIDTH IS DERIVED FROM THE LIVE SHELL, NOT QUOTED. The epic says "a column
# that leaves the frame at 1232px"; that number exists nowhere in this
# repository. The probe used to hardcode 1128, read from `shell/application.css`
# (1440 viewport - 248 rail - 64 gutters) -- a sheet whose chrome wave 1 of
# `ui-css-strategy.md` deleted. The live geometry then gave 1104 (16rem rail,
# 40px gutters), and a column between 1104 and 1128 PASSED HERE WHILE LEAVING
# THE REAL FRAME: the probe was measuring a copy of a geometry that no longer
# existed. Decision taken 2026-08-25: the probe reads the rail width from
# `shell/ApplicationShell.tsx` and the gutter token from `styles/theme.css` and
# refuses to run if either moves out of reach -- the contract follows the shell,
# it does not remember it. The sheet is loaded at that width, and its own
# gutters make the grid NARROWER still: fitting here implies fitting in the
# main column.
def _main_column_px() -> int:
    shell = (REPO / "ui/admin/src/shell/ApplicationShell.tsx").read_text(encoding="utf-8")
    rail = re.search(r"lg:grid-cols-\[(\d+)rem_minmax", shell)
    theme = (REPO / "ui/admin/src/styles/theme.css").read_text(encoding="utf-8")
    gutter = re.search(r"--spacing-page-gutter:\s*(\d+)px", theme)
    if not rail or not gutter:
        sys.exit(
            "main-column geometry not found where it lives "
            "(ApplicationShell rail / --spacing-page-gutter): the probe refuses to guess"
        )
    return 1440 - int(rail.group(1)) * 16 - 2 * int(gutter.group(1))


MAIN_COLUMN_PX = _main_column_px()

GRID = "[data-testid=daily-breakdown-grid]"

GRID_CHECKS = [
    # The row of the day grid: one value per cell, so the 52px token floor and
    # not the 64px rich row. `density="compact"`, never a hand-written height.
    ("table td (compact row)", f"{GRID} [data-slot=table-cell]", {"height": "52px"}),
]

#: THE TWO PROBES, AND WHY THE OBVIOUS FORM OF EACH IS BLIND.
#:
#: OVERFLOW. `scrollWidth > clientWidth` is the right question asked of the WRONG
#: box: `Table` wraps its `<table>` in a `div[data-slot=table-container]` that
#: carries `overflow-x-auto` of its own (`components/ui/table.tsx:28-33`), and
#: that inner box absorbs the overflow before the outer `TableScroll` region ever
#: sees it. Measured: a table forced to `w-[3000px]` gave region 996 / inner 3000,
#: and the first version of this file printed "every measured value matches its
#: mockup rule". So EVERY scrollable box inside the grid is measured, innermost
#: included, and any one of them that scrolls is the failure.
#:
#: THE PILL ON TWO LINES. Comparing the pill's height to the row's cannot fail:
#: the pill is INSIDE the cell, so the row grows with it -- forced to 200px the
#: pair measured 200 and 221 and passed. The question is not a ratio, it is "did
#: this text fold?", and `getClientRects().length` answers exactly that: one
#: rectangle per line box the inline content occupies. The probe walks the pill's
#: descendants because an `inline-flex` box always reports ONE rect however its
#: contents wrap -- the wrapping shows on the inline text span within it.
GRID_JS = """(sel) => {
  const grid = document.querySelector(sel);
  if (!grid) return null;
  const name = (el) =>
    el.dataset.testid ? `[data-testid=${el.dataset.testid}]`
    : el.dataset.slot ? `[data-slot=${el.dataset.slot}]`
    : el.getAttribute('role') ? `[role=${el.getAttribute('role')}]`
    : el.tagName.toLowerCase();

  const scrollers = [grid, ...grid.querySelectorAll('*')]
    .filter((el) => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX))
    .map((el) => ({
      where: name(el), clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
    }));

  // ONE RECT PER LINE BOX OF THE TEXT ITSELF, taken with a Range.
  //
  // `element.getClientRects()` cannot answer this: a flex item -- which every
  // child of the status pill is -- is BLOCKIFIED, and a blockified box reports
  // exactly one rectangle however its contents wrap. A Range over the text
  // contents reports the line boxes the TEXT occupies, which is the question,
  // and it keeps answering it whether or not `white-space: nowrap` is in play.
  // COUNTED BY DISTINCT TOP, not by rect. Measured: a clipped run of text
  // (`overflow: hidden` + ellipsis) makes Chromium emit TWO rectangles at the
  // SAME `top` -- the full run and the visible part -- so counting rectangles
  // reported a fold on four cells that are plainly on one line. A line box is a
  // vertical position; two of them means the text folded.
  const lineBoxes = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    return new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size;
  };
  const folded = [];
  let probed = 0;
  for (const cell of grid.querySelectorAll('[data-slot=table-cell]')) {
    for (const el of [cell, ...cell.querySelectorAll('*')]) {
      if (el.childElementCount > 0) continue;      // only the leaves hold text
      if (!el.textContent.trim()) continue;
      probed += 1;
      const rects = lineBoxes(el);
      if (rects > 1) folded.push({ where: name(el), rects, text: el.textContent.trim() });
    }
  }

  return {
    scrollers,
    folded,
    // Both are vacuity guards: a probe that walked nothing, or a grid whose
    // status pill is gone, must report NOT FOUND rather than "no fold found".
    probed,
    pillPresent: Boolean(grid.querySelector('[data-testid=day-extract-status]')),
  };
}"""

#: THE TABLE A PERSON ACTUALLY WORKS IN, in its two shapes.
#:
#: `MAPPING` is the shape that SHIPS -- the readings that say the same thing on
#: every row folded into a per-row detail, which is what the review captured on
#: ten real rows. `MAPPING_WIDEST` is the same component with every reading
#: varying, so all seven of them are columns again.
#:
#: THEY DO NOT CARRY THE SAME REFUSAL, and pretending they did would make one of
#: the two a lie. Ten columns of real text do not fit 1128px; the table scrolls,
#: which is what `TableScroll` exists for, and a reading that scrolls out of view
#: is reachable by scrolling -- it is not cut off. So:
#:
#:   · the shipped shape must not scroll at all, and NOTHING in it may be
#:     clipped. Nothing scrolls there, so "outside the visible window" and "cut
#:     off" are the same statement and the probe is exact.
#:   · the widest shape may scroll, and the one thing that may never leave reach
#:     is the way to ACT on a row. The acts column is pinned to the trailing
#:     edge, and that is measured AT BOTH ENDS of the scroll -- a pin that only
#:     holds where the table happens to start is not a pin.
MAPPING = "[data-testid=mapping-bindings-sheet]"
MAPPING_WIDEST = "[data-testid=mapping-bindings-widest]"

MAPPING_CHECKS = [
    # The acts of a row are ONE menu trigger (finding D-2), not three controls
    # spread over two columns. `icon-sm`, so the 32px control step -- a bespoke
    # height here would be a fourth size nothing else in the console uses.
    ("mapping acts trigger", f"{MAPPING} [data-slot=dropdown-menu-trigger]",
     {"height": "32px", "width": "32px"}),
]

#: THE TWO REFUSALS OF THE MAPPING TABLE, AND WHY NEITHER EXISTED BEFORE.
#:
#: Finding D-2 of the 2026-08-12 visual review: `Split...` was cut off at the
#: right edge on every one of the ten rows, at 1600px. The epic contract measures
#: exactly this class -- "a column that leaves the frame" -- on the day grid and
#: ONLY on the day grid, so the table a person actually works in could overflow
#: with nothing going red. This is the same measurement, on the same instrument,
#: for the second table.
#:
#: NO COLUMN LEAVES THE FRAME. The same probe as the grid's, for the same reason
#: it is written that way: `Table` wraps its `<table>` in a
#: `div[data-slot=table-container]` carrying `overflow-x-auto` of its own
#: (`components/ui/table.tsx:28-33`), and that inner box absorbs the overflow
#: before the outer `TableScroll` region ever sees it. Every scrollable box is
#: measured, innermost included.
#:
#: NO CONTROL IS CLIPPED. Overflow alone does not say it: a cell can clip a
#: button against its own edge while the table itself scrolls not one pixel, and
#: that is what a person SEES -- half a word and no way to reach the rest. So
#: every control in the table is compared to the VISIBLE box of its nearest
#: clipping ancestor. One pixel of tolerance, because a sub-pixel layout puts a
#: right edge at `x.4` inside a container that reports `x`, which is not a clip.
MAPPING_JS = """(sel) => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const name = (el) =>
    el.dataset.testid ? `[data-testid=${el.dataset.testid}]`
    : el.dataset.slot ? `[data-slot=${el.dataset.slot}]`
    : el.getAttribute('aria-label') ? `[aria-label=${el.getAttribute('aria-label')}]`
    : el.tagName.toLowerCase();

  const scrollers = [root, ...root.querySelectorAll('*')]
    .filter((el) => ['auto', 'scroll'].includes(getComputedStyle(el).overflowX))
    .map((el) => ({
      where: name(el), clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
    }));

  // The nearest ancestor that can cut this element off: `hidden` clips in
  // silence, `auto` and `scroll` clip whatever sits past the scrolled window.
  const clipper = (el) => {
    for (let up = el.parentElement; up; up = up.parentElement) {
      const overflow = getComputedStyle(up).overflowX;
      if (['auto', 'scroll', 'hidden', 'clip'].includes(overflow)) return up;
      if (up === root) return null;
    }
    return null;
  };

  const CONTROLS = 'button, select, input, a[href], [role=checkbox]';
  const clipped = [];
  let controls = 0;
  for (const el of root.querySelectorAll(CONTROLS)) {
    const box = clipper(el);
    if (!box) continue;
    controls += 1;
    const mine = el.getBoundingClientRect();
    const frame = box.getBoundingClientRect();
    // The VISIBLE window of the clipper, not its scrollable extent: content
    // scrolled out of view is exactly what "cut off at the right edge" means.
    const left = frame.left + box.clientLeft;
    const right = left + box.clientWidth;
    if (mine.right > right + 1 || mine.left < left - 1) {
      clipped.push({
        where: name(el), text: el.textContent.trim().slice(0, 32),
        right: Math.round(mine.right), frame: Math.round(right),
      });
    }
  }
  return { scrollers, clipped, controls };
}"""

#: THE PIN, MEASURED WHERE IT IS SUPPOSED TO HOLD: at both ends of the scroll.
#:
#: `sticky right-0` on the acts cell is the answer to D-2 for a table too wide
#: for its frame -- a reading may scroll out of view, the way to act on the row
#: may not. Checking it only at `scrollLeft: 0` proves nothing on a table whose
#: last column already sits inside the window there, which is precisely the case
#: a broken pin looks identical to a working one.
PINNED_JS = """(sel) => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const box = root.querySelector('[data-slot=table-container]');
  if (!box) return null;
  const frame = box.getBoundingClientRect();
  const left = frame.left + box.clientLeft;
  const right = left + box.clientWidth;
  const acts = [...root.querySelectorAll('[data-slot=dropdown-menu-trigger]')].map((el) => {
    const mine = el.getBoundingClientRect();
    return {
      where: `[data-testid=${el.dataset.testid}]`,
      left: Math.round(mine.left), right: Math.round(mine.right),
    };
  });
  return {
    acts, frameLeft: Math.round(left), frameRight: Math.round(right),
    scrollLeft: Math.round(box.scrollLeft),
    scrollMax: Math.round(box.scrollWidth - box.clientWidth),
  };
}"""

SCROLL_JS = """([sel, to]) => {
  const box = document.querySelector(sel).querySelector('[data-slot=table-container]');
  box.scrollLeft = to === 'end' ? box.scrollWidth : 0;
}"""

JS = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const c = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const out = {};
  for (const p of ['borderRadius','paddingLeft','padding','fontSize','fontWeight',
                   'textTransform','letterSpacing','borderTopWidth','minHeight','columnGap']) out[p] = c[p];
  out.height = Math.round(r.height) + 'px';
  out.width = Math.round(r.width) + 'px';
  return out;
}"""

fails, missing = [], []

#: WHAT WAS REALLY MEASURED, and the count is derived from it.
#:
#: This list replaces a hard-coded total. The first version printed
#: `len(CHECKS) + len(OVERLAYS) + len(GRID_CHECKS) + 2` and then "every measured
#: value matches its mockup rule" -- so renaming one selector produced a
#: `NOT FOUND` line, the SAME total, the same reassuring sentence and exit 0. An
#: absent selector is a failed measurement, never a silent one.
measured = []


def run(page, checks):
    for label, sel, expect in checks:
        got = page.evaluate(JS, sel)
        if got is None:
            missing.append(f"{label}  ({sel})")
            continue
        measured.append(label)
        for prop, want in expect.items():
            have = got.get(prop, "?")
            if have != want:
                fails.append(f"{label:24s} {prop:16s} want {want:12s} got {have}")


with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={"width": 1440, "height": 900})
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(800)
    run(page, CHECKS)

    page.get_by_role("button", name="Open dialog", exact=True).click(); page.wait_for_timeout(400)
    run(page, [OVERLAYS[0]])
    page.keyboard.press("Escape"); page.wait_for_timeout(300)

    page.get_by_role("button", name="Actions", exact=True).click(); page.wait_for_timeout(400)
    run(page, OVERLAYS[1:3])
    page.keyboard.press("Escape"); page.wait_for_timeout(300)

    page.get_by_role("button", name="Hover me", exact=True).hover(); page.wait_for_timeout(800)
    run(page, [OVERLAYS[3]])

    # BY ITS SLOT, not by "the first combobox on the page": the mapping table
    # mounted below puts a NATIVE `<select>` on this sheet, which also answers to
    # `combobox`, and a position-dependent probe silently started clicking that
    # one instead — two `NOT FOUND` lines for components that had not moved.
    page.locator("[data-slot=select-trigger]").first.click(); page.wait_for_timeout(400)
    run(page, OVERLAYS[4:6])
    page.keyboard.press("Escape"); page.wait_for_timeout(300)

    # The day grid, at the width of the main column and not at the sheet's.
    page.set_viewport_size({"width": MAIN_COLUMN_PX, "height": 900})
    page.wait_for_timeout(400)
    run(page, GRID_CHECKS)
    grid = page.evaluate(GRID_JS, GRID)
    if grid is None:
        missing.append(f"day-by-day grid  ({GRID})")
    elif not grid["scrollers"]:
        # No scrollable box at all means the probe found nothing to measure, not
        # that nothing overflows.
        missing.append(f"day grid scroll container  ({GRID} [overflow-x])")
    else:
        measured.append("day grid · no column leaves the frame")
        for box in grid["scrollers"]:
            if box["scrollWidth"] > box["clientWidth"]:
                fails.append(
                    f"{'day grid overflow':24s} {box['where']:16s} "
                    f"want <= {box['clientWidth']:<5d} got {box['scrollWidth']}"
                )
        if not grid["pillPresent"] or not grid["probed"]:
            missing.append(
                f"day grid status pill  ({GRID} [data-testid=day-extract-status]) — "
                f"{grid['probed']} text leaves probed"
            )
        else:
            measured.append("day grid · no cell folds onto a second line")
            for folded in grid["folded"]:
                fails.append(
                    f"{'day grid folded cell':24s} {folded['where']:16s} "
                    f"want 1 line box  got {folded['rects']}   ({folded['text'][:40]!r})"
                )

    # The mapping table, at the SAME width as the day grid. Two tables of one
    # console judged at two widths would be two contracts.
    run(page, MAPPING_CHECKS)

    shipped = page.evaluate(MAPPING_JS, MAPPING)
    if shipped is None:
        missing.append(f"mapping bindings table · as it ships  ({MAPPING})")
    elif not shipped["scrollers"]:
        missing.append(f"mapping scroll container · as it ships  ({MAPPING} [overflow-x])")
    else:
        measured.append("mapping table · as it ships · no column leaves the frame")
        for box in shipped["scrollers"]:
            if box["scrollWidth"] > box["clientWidth"]:
                fails.append(
                    f"{'mapping overflow':24s} {box['where']:16s} "
                    f"want <= {box['clientWidth']:<5d} got {box['scrollWidth']}"
                )
        # A probe that walked no control reports NOT FOUND, never "nothing is
        # clipped": the acts of a row are the very thing D-2 found cut off.
        if not shipped["controls"]:
            missing.append(f"mapping controls · as it ships  ({MAPPING} button|select|input)")
        else:
            measured.append("mapping table · as it ships · no control is cut off")
            for cut in shipped["clipped"]:
                fails.append(
                    f"{'mapping clipped':24s} {cut['where']:16s} "
                    f"want <= {cut['frame']:<5d} got {cut['right']}   ({cut['text']!r})"
                )

    # The widest shape: it MAY scroll, and the acts may not leave the frame while
    # it does. Measured at both ends, and refused outright if it does not scroll
    # — a fixture that fits would make this whole reading vacuous.
    pinned = page.evaluate(PINNED_JS, MAPPING_WIDEST)
    if pinned is None:
        missing.append(f"mapping bindings table · every reading varies  ({MAPPING_WIDEST})")
    elif pinned["scrollMax"] <= 0 or not pinned["acts"]:
        missing.append(
            f"mapping acts pinned · every reading varies  ({MAPPING_WIDEST}) — "
            f"{pinned['scrollMax']}px of scroll, {len(pinned['acts'])} acts found"
        )
    else:
        measured.append("mapping table · every reading varies · the acts stay in the frame")
        for edge in ("start", "end"):
            page.evaluate(SCROLL_JS, [MAPPING_WIDEST, edge])
            page.wait_for_timeout(200)
            at = page.evaluate(PINNED_JS, MAPPING_WIDEST)
            for act in at["acts"]:
                if act["right"] > at["frameRight"] + 1 or act["left"] < at["frameLeft"] - 1:
                    fails.append(
                        f"{'mapping acts off frame':24s} {act['where']:16s} "
                        f"want {at['frameLeft']}..{at['frameRight']} "
                        f"got {act['left']}..{act['right']}  (scrolled to the {edge})"
                    )
    b.close()

#: +2 for the day grid's two probes, +3 for the mapping table's (the frame on
#: the shipped shape, the clipping on both).
expected = len(CHECKS) + len(OVERLAYS) + len(GRID_CHECKS) + len(MAPPING_CHECKS) + 5
print(f"{len(measured)} of {expected} measurements taken")
if missing:
    print("\nNOT FOUND — a selector that resolves to nothing is a FAILED measurement,")
    print("not a silent one. Either the component moved or its hook was renamed:")
    for m in missing:
        print("  ", m)
if fails:
    print("\nOFF CONTRACT:")
    for f in fails:
        print("  ", f)
if missing or fails:
    # AND IT EXITS NON-ZERO. This script printed its findings and returned 0
    # whatever it found, so nothing that ran it could tell a pass from a report.
    sys.exit(1)
print("every measured value matches its mockup rule")
