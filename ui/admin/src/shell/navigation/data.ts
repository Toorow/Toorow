/**
 * The `data` workspace: its sections, their lenses, their object contracts.
 *
 * AD-42, 2026-08-12 -- declared here rather than inside the 313-line array it
 * shared with five other workspaces. A change to data no longer opens the file
 * the others are written in.
 *
 * ORDER IS PART OF THE CONTRACT and it stays in `navigation.ts`: the subnav
 * renders `WORKSPACES` as declared. This file owns the CONTENT of one entry,
 * never its position.
 */

import { section } from "./vocabulary";

export const data = {
  key: "data",
  slug: "data",
  label: "Data",
  question: "What data enters this project and is it current?",
  subnav: [
    section("data-overview", "Data Overview"),
    section("datastreams", "Datastreams", [
      {
        type: "datastream",
        label: "Datastream",
        // `cost` is CONDITIONAL: the contract declares it, and
        // `capabilityTabs.ts` decides whether an address may open it. The
        // amendment « Une capacité activée AJOUTE son onglet » of
        // `datastream-workbench-and-wizard.md` requires "ni onglet, ni panneau"
        // when the capability is off, and a tab absent from this list would be
        // unreachable even when it IS on.
        // `placements` was missing from this list until 2026-08-11, and the two
        // lines above are exactly why that mattered. `capabilityTabs.ts` and
        // `datastreamTabs.ts` both know the tab; this contract did not, so
        // `tabIsAddressable` refused it and `router.tsx` threw
        // `Unknown object tab` — not on the Placements tab, on EVERY tab of
        // that Datastream's Workbench, the moment `placement_mapping` reached
        // `ready` on any Project. Nothing in the code prevented that
        // activation. It never fired only because the capability is `disabled`
        // everywhere today, which is luck, not a guard. Found by the tour-3
        // review of the two conditional tabs (issue #60).
        tabs: ["overview", "mapping", "data", "cost", "placements", "processing", "runs", "outputs"],
        evidenceTabs: ["runs"],
      },
    ], ["create"]),
    section("events", "Events", [{ type: "event-configuration", label: "Event Configuration", tabs: ["overview", "source-mapping", "collection", "usage"] }]),
    section("sources", "Sources", [{ type: "source-account", label: "Source Account", tabs: ["overview", "accounts", "health", "used-by"] }]),
    section("imports", "Imports", [{ type: "import", label: "Import", tabs: ["overview", "raw-evidence", "validation", "publication"] }]),
    section("connectors", "Connectors", [{ type: "connector", label: "Connector", tabs: ["overview", "capabilities", "coverage", "versions"] }]),
  ],
} as const;
