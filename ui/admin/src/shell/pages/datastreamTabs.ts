/**
 * The Datastream workbench tab contract, in one place.
 *
 * `docs/product-architecture/datastream-workbench-and-wizard.md` contracts six
 * tabs. Every entry is a navigable owner route; no tab aliases another screen.
 */
import type { NavTab } from "../../ui";
import { CONDITIONAL_OBJECT_TABS } from "../capabilityTabs";

/**
 * The eight tabs, IN THE RATIFIED ORDER — corrected 2026-09-02.
 *
 * A union has no order at runtime, which is why this line kept spelling `data`
 * before `mapping` while the comment ten lines below said the opposite and
 * `DATASTREAM_TABS` held the amendment. A reader who opens this file to learn
 * the order reads the union first; a declaration that contradicts its own file
 * is the same defect as one that contradicts another file, and cheaper to fix.
 */
export type Tab =
  | "overview" | "mapping" | "data" | "cost" | "placements"
  | "processing" | "runs" | "outputs";

/**
 * `Mapping` COMES BEFORE `Data` — amendment 3 of the ratified target,
 * « `Mapping` vient avant `Data` » in `datastream-workbench-and-wizard.md`,
 * applied by story 58.2. Cited by its name: the line numbers of that document
 * moved in the very commit that applied it.
 *
 * "One understands a flux by reading first what each field becomes — the list of
 * dimensions and metrics — and only then the rows." The amendment was ratified
 * on 2026-08-06 and this line still declared the old order, so the console
 * contradicted its own contract.
 *
 * `Cost` SITS WHERE THE AMENDMENT PUTS IT — `Overview`, `Mapping`, `Data`,
 * [`Cost`], [`Placements`], `Processing`, `Runs`, `Outputs` — and it is here in
 * the ORDER, not in what is rendered: the list below is the contract, and
 * `datastreamTabs` is what drops a tab whose capability is off (story 58.6).
 *
 * `Placements` TOOK ITS PLACE IN THAT ORDER IN STORY 61.1. It was named in the
 * comment above and absent from the list below, so the contract this file exists
 * to freeze was written down and not held — the same shape story 58.2 found for
 * `Mapping` before `Data`. Both conditional tabs are now in the list, and both
 * are dropped by `datastreamTabs` when their capability is off.
 */
export const DATASTREAM_TABS: readonly Tab[] = [
  "overview", "mapping", "data", "cost", "placements", "processing", "runs", "outputs",
];

/** The tabs a capability opens, for this object type. */
export const CAPABILITY_DATASTREAM_TABS = CONDITIONAL_OBJECT_TABS.filter(
  (entry) => entry.objectType === "datastream",
);

/**
 * Is this tab conditional on a capability?
 *
 * Declared once and read by both the band and the router, so "which tabs exist"
 * has one answer. A second list is how a tab ends up hidden in one place and
 * reachable in the other.
 */
export function isCapabilityTab(tab: Tab): boolean {
  return CAPABILITY_DATASTREAM_TABS.some((entry) => entry.tab === tab);
}

/**
 * The address of a tab is BUILT BY THE ROUTER, never composed here — story 57.5.
 *
 * This file used to write `/p/{projectId}/data/datastreams/o/datastream/{id}/{tab}`,
 * and `parsePath` (`shell/router.tsx:130`) refuses every path whose first segment
 * is not `/org/`: six tabs, six addresses that resolve to nothing. It could not be
 * fixed here either, because a canonical address carries the ORGANIZATION and this
 * module is never told which one. So the caller that has the current route hands
 * `buildPath` down, exactly as the Analyze surfaces already do. A tab with no
 * builder renders visible and disabled rather than carrying a false address.
 */
export function datastreamTabs(
  tabHref?: (tab: Tab) => string,
  /**
   * The conditional tabs that are OPEN, read from the Workbench header. Absent
   * means none is: a capability tab is drawn only from a measured `open`, never
   * from a default, because a band that draws it while the answer is unknown has
   * shown a tab that must not exist.
   */
  openCapabilityTabs: readonly string[] = [],
): NavTab[] {
  return DATASTREAM_TABS.filter(
    (tab) => !isCapabilityTab(tab) || openCapabilityTabs.includes(tab),
  ).map((tab) => ({
    key: tab,
    label: tab[0].toUpperCase() + tab.slice(1),
    href: tabHref?.(tab),
  }));
}
