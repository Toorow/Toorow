/**
 * The identities every Datastream Workbench fixture answers about — one place.
 *
 * They were literals at the top of `DatastreamWorkbenchSandbox.tsx` while that
 * file held every fixture too. It now holds none: the fixtures grew a `/progress`
 * shape, a `/daily-breakdown` envelope, a landed-file panel and a second SOURCE
 * MODE, and a single file carrying all of them would have crossed the 1 000-line
 * bound this repository refuses.
 *
 * NO PRODUCTION IDENTIFIER, here or in any fixture that imports this: the
 * repository is shareable, so `org_EXAMPLE` / `proj_EXAMPLE` / `owner@example.com`
 * are the only names allowed to travel.
 */

export const ORG_ID = "org_EXAMPLE";
export const PROJECT_ID = "proj_EXAMPLE";

/** The Datastream that PULLS from a connector. */
export const DATASTREAM_ID = "ds_EXAMPLE";
export const PLAN_VERSION = "dplan_EXAMPLE_v1";
export const MAPPING_VERSION = "dmap_EXAMPLE_v1";

/**
 * The Datastream a file is PUSHED to — `source_kind = 'managed_feed'`.
 *
 * A second identity rather than a second `mode` on the first one: `sandboxApi`
 * answers by URL, the two Datastreams are never on screen together, and a fixture
 * that changed its own id under one address would make a capture unciteable.
 */
export const FEED_DATASTREAM_ID = "ds_EXAMPLE_FEED";
export const FEED_PLAN_VERSION = "dplan_EXAMPLE_FEED_v1";
export const FEED_MAPPING_VERSION = "dmap_EXAMPLE_FEED_v1";

/**
 * The two source modes this sandbox can be addressed in.
 *
 * `managed_feed` is 4 of the 6 live Datastreams and had NEVER been rendered:
 * every capture ever taken was a `connector_pull`, so amendment 7 — which
 * removed the connector pull axis from a file source — changed a screen nobody
 * has looked at since.
 */
export type SandboxMode = "connector_pull" | "managed_feed";

export const SANDBOX_MODES: readonly SandboxMode[] = ["connector_pull", "managed_feed"];

/** The Datastream a mode answers about. */
export function datastreamIdFor(mode: SandboxMode): string {
  return mode === "managed_feed" ? FEED_DATASTREAM_ID : DATASTREAM_ID;
}

/** The owner addresses `compose_header` really composes — `/org/{org}/project/{p}/…`. */
export const LINKS = {
  source: `/org/${ORG_ID}/project/${PROJECT_ID}/data/sources`,
  project_settings: `/org/${ORG_ID}/project/${PROJECT_ID}/settings/general`,
  governance: `/org/${ORG_ID}/project/${PROJECT_ID}/governance/master-data`,
};
