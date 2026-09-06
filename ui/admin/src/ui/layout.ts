/**
 * layout.ts — the console's named widths, and the only place a panel size is
 * written down.
 *
 * `console-presentation.md` §6: « A drawer, a grid or a panel width is a token
 * or a named constant in `ui/admin/src/ui/layout.ts` (`DRAWER_WIDTH`,
 * `WIDE_DRAWER_WIDTH`, `WORKBENCH_GRID_MAX`), never a literal in a
 * `className`. »
 *
 * ## Why these are CLASS STRINGS and not numbers
 *
 * Tailwind 4 builds its stylesheet by scanning the source text for class names.
 * A width composed at runtime — `` `grid-cols-[${RAIL}_${TASK}]` `` — is a
 * string Tailwind never sees, so the rule is never emitted and the grid
 * silently collapses to one column. Moving the number out of the JSX and into a
 * constant therefore only works if the constant IS the utility, written
 * literally, in a file Tailwind scans. It scans this one.
 *
 * That is also why §6's rule is not weakened by them: the point of the rule is
 * that two screens asking for « the drawer width » get the same answer from one
 * declaration, and a caller that writes `className={DRAWER_WIDTH}` cannot
 * disagree with its neighbour.
 *
 * ## What has a caller today, and what does not
 *
 * The wizard's three zones are read by `DatastreamSetupWizard` (story 76-6).
 * The three names §6 spells out — the two drawers and the workbench grid — are
 * declared here with the widths measured on 2026-09-05 at their five literal
 * sites; migrating those sites belongs to story 76-7, which owns Analyze,
 * Governance and the Context Hub. They are written down now rather than later
 * so that pass moves five `className` literals onto an existing declaration
 * instead of inventing a sixth spelling. Stated rather than left to be
 * discovered, the way `format.ts`'s own three callerless exports are.
 */

/**
 * A drawer that carries one object's detail beside the list it came from.
 * Measured sites: 380px.
 */
export const DRAWER_WIDTH = "w-[380px]";

/** A drawer that carries a table or an editor rather than a record. 900px. */
export const WIDE_DRAWER_WIDTH = "w-[900px]";

/** The widest a workbench grid is allowed to grow before it stops being read
 *  as columns. 1400px. */
export const WORKBENCH_GRID_MAX = "max-w-[1400px]";

/**
 * THE DATASTREAM WIZARD'S THREE ZONES — `datastream-workbench-and-wizard.md:31`.
 *
 *   > At desktop width, use a **persistent left stepper**, a central task area,
 *   > and a right `Configuration summary` panel.
 *
 * 210px rail, the task area takes what is left, 290px summary. Below `xl`
 * (1280px) there is no third track to give the summary: it keeps its place in
 * the reading order and follows the task area as a block, and the rail and the
 * task area keep the widths they already had.
 */
export const WIZARD_GRID =
  "grid-cols-[210px_minmax(0,1fr)] xl:grid-cols-[210px_minmax(0,1fr)_290px]";

/**
 * WHERE THE RAIL AND THE SUMMARY SIT WHEN THE PAGE IS SCROLLED.
 *
 * Both are answers to « where am I », and a wizard step is taller than a
 * viewport at 1280px on four of its five stops — so a rail that scrolls away is
 * a rail nobody reads past the fold. `:31` calls the stepper **persistent**;
 * this is what persistent means once the content is longer than the screen.
 *
 * `self-start` is what makes `sticky` work at all inside a grid: a stretched
 * grid item is as tall as its row, and a sticky box as tall as its scroll
 * container never moves.
 */
export const WIZARD_ASIDE_STICKY = "self-start xl:sticky xl:top-6";
