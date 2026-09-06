/**
 * The Datastream Workbench, rendered on its own so it can be LOOKED AT.
 *
 * Eight tabs, six of them always and two opened by a CAPABILITY — `Cost` since
 * story 58.6 and `Placements` since 61.1. It said "six-tab" while the ratified
 * amendment « Une capacité activée AJOUTE son onglet » had already made the count
 * conditional.
 *
 * The Workbench sits behind `AuthGate`, `ScopeProvider` and a live API, so the
 * only way anyone has ever seen it is with a signed-in session against a
 * Datastream that has run. That is why a build from 2026-07-30 — carrying the
 * five one-off pages `26695dcc` deleted — was still being judged as "the
 * product" three days later.
 *
 * THE ADDRESS IS THE SWITCH. Every state this file can show is addressable, the
 * way `api=refusing` already was: a query parameter costs no control on the page,
 * so a capture is of the SCREEN and not of the harness around it, and the address
 * of a state can be quoted in a review.
 *
 *     /debug/screen?name=DatastreamWorkbench
 *       &tab=overview|mapping|data|cost|placements|processing|runs|outputs
 *       &mode=connector_pull|managed_feed     which SOURCE MODE, and so which tabs
 *       &run=running|not_started|idle|never_ran|failed|stopped
 *       &file=read|no_file_yet|arrived_not_landed|arrival_refused|import_failed
 *            |relation_absent|warehouse_unavailable
 *       &days=served|empty                    the day grid, or a flux that never ran
 *       &api=refusing                         every call answers 503
 *
 * FOUR THINGS THIS FILE ANSWERED WRONGLY UNTIL 2026-08-12, each of them proved by
 * a capture that has already been reviewed:
 *
 *   1. NO `/progress` BRANCH. The call fell through to the harness's `404`, and
 *      `404` is in `STOP_STATUSES` (`lib/polledRead.ts`), so `DatastreamRunLive`
 *      — mounted above the tab band, therefore on all eight tabs — rendered « The
 *      collection state answered 404 » in every screenshot this repository holds.
 *      The band has never been seen live, idle, or in its « never ran » state;
 *   2. NO `/daily-breakdown` BRANCH, so the day grid and `Read one day` had only
 *      ever been seen through the `/debug/components` sheet, against hand-written
 *      props, and never on the tab route they live on;
 *   3. THE MODE WAS HARD-CODED to `connector_pull`, so the `managed_feed` reduced
 *      state — 4 of the 6 live Datastreams, and what amendment 7 and lot B1 both
 *      changed — had never been rendered at all;
 *   4. THE `Placements` FIXTURE SHIPPED TWO STALE KEYS from before story 61.2,
 *      and produced `Unavailable / 3` and three empty badges in the only preview
 *      tool built for that tab.
 *
 * The fixtures themselves are in `sandbox/`: they outgrew this file the moment it
 * had to answer for two source modes, and no file here goes past 1 000 lines.
 *
 * CITED BY NAME, NOT BY LINE. This once read `:684`, and story 61.1 inserted a
 * branch above it: the number then landed inside an unrelated comment, which is a
 * citation that looks exact and points at nothing.
 */
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";
import { DATASTREAM_TABS, type Tab } from "./pages/datastreamTabs";
import { RouterProvider } from "./router";
import { installSandboxApi } from "./sandboxApi";
import {
  DATASTREAM_ID,
  FEED_DATASTREAM_ID,
  SANDBOX_MODES,
  datastreamIdFor,
  type SandboxMode,
} from "./sandbox/datastreamSandboxScope";
import {
  EVIDENCE,
  EXPORT_COLUMNS,
  HEADER,
  LEDGER,
  SAMPLE,
  SCHEDULE,
} from "./sandbox/datastreamPullFixtures";
import {
  FEED_COLUMNS,
  FEED_EVIDENCE,
  FEED_HEADER,
  FILE_SHAPES,
  landedFileFixture,
  notAPushedSource,
  type FileShape,
} from "./sandbox/datastreamFeedFixtures";
import {
  DEFAULT_RUN_SHAPE,
  RUN_SHAPES,
  progressFixture,
  stopRunFixture,
  type RunShape,
} from "./sandbox/datastreamRunFixtures";
import {
  DAY_SHAPES,
  dailyBreakdownFixture,
  type DayShape,
} from "./sandbox/datastreamDayFixtures";

/**
 * WHAT MARKS A SENTENCE AS THE HARNESS'S OWN.
 *
 * The fallbacks below are deliberate — a screen's refusal surface is part of what
 * a visual review has to look at, and substituting a payload is what hid the
 * Placements defect for a week. But the harness's sentence is rendered by the
 * screen VERBATIM, which is the right behaviour for a server sentence and the
 * wrong one for this: on a capture of 2026-08-12 it printed ten times, under ten
 * buttons, with nothing to say it was not the product speaking.
 *
 * A reviewer must be able to tell, from the picture alone, which absences belong
 * to the product and which to the tool that is showing it.
 */
const SANDBOX_MARK = "[sandbox]";

/**
 * The project vocabulary a field row offers, so the selector renders as a
 * CONTROL. Two published concepts and two canonical fields is the smallest set
 * that exercises the grouping without turning the capture into a catalogue.
 */
const CANONICAL_FIELDS = {
  fields: [
    {
      id: "mdm_EXAMPLE_CLICKS",
      canonical_name: "clicks",
      concept_kind: "metric",
      value_type: "integer",
      aggregation: "sum",
      scope: "project",
    },
    {
      id: "mdm_EXAMPLE_IMPRESSIONS",
      canonical_name: "impressions",
      concept_kind: "metric",
      value_type: "integer",
      aggregation: "sum",
      scope: "project",
    },
    {
      id: "mdm_EXAMPLE_COUNTRY",
      canonical_name: "country",
      concept_kind: "dimension",
      value_type: "string",
      scope: "platform",
    },
  ],
};

/** What the address asked for, with a default for everything it did not name. */
interface SandboxRequest {
  mode: SandboxMode;
  tab: Tab;
  run: RunShape;
  file: FileShape;
  days: DayShape;
}

/** One reading of the address. `null` for a value this build does not know: an
 *  unknown state name falls back to the default rather than answering a `404`
 *  nobody asked for. */
function chosen<T extends string>(
  params: URLSearchParams,
  name: string,
  allowed: readonly T[],
  fallback: T,
): T {
  const value = params.get(name);
  return allowed.includes(value as T) ? (value as T) : fallback;
}

function readRequest(): SandboxRequest {
  const params = new URLSearchParams(window.location.search);
  const mode = chosen(params, "mode", SANDBOX_MODES, "connector_pull");
  const requested = chosen(params, "tab", DATASTREAM_TABS, "overview");
  // A TAB THIS MODE DOES NOT HAVE IS NOT A TAB. `Cost` and `Placements` are open
  // on the connector pull alone here, and the router refuses a conditional
  // address whose capability is off — so the sandbox refuses it too rather than
  // rendering a panel that explains the extinction, which is a panel.
  const evidence = mode === "managed_feed" ? FEED_EVIDENCE : EVIDENCE;
  return {
    mode,
    tab: requested in evidence ? requested : "overview",
    // A file source is WAITED on; a connector pull is watched while it runs. Both
    // are the ordinary state of their mode.
    run: chosen(params, "run", RUN_SHAPES, mode === "managed_feed" ? "idle" : DEFAULT_RUN_SHAPE),
    file: chosen(params, "file", FILE_SHAPES, "read"),
    days: chosen(params, "days", DAY_SHAPES, "served"),
  };
}

/** The Datastream one address is about, read from the URL that was called rather
 *  than from the request: two ids are in play, and an answer that named the other
 *  one would be refused by every reader's own scope check — correctly. */
function datastreamOf(path: string): string {
  return path.includes(FEED_DATASTREAM_ID) ? FEED_DATASTREAM_ID : DATASTREAM_ID;
}

function modeOf(datastreamId: string): SandboxMode {
  return datastreamId === FEED_DATASTREAM_ID ? "managed_feed" : "connector_pull";
}

function fixtureFor(url: string, request: SandboxRequest): { body: unknown; status: number } {
  // THE QUERY IS SPLIT OFF BEFORE ANYTHING IS MATCHED. The tab branch below tests
  // the END of the address, and `Placements` re-reads itself as
  // `…/workbench/placements?plan_id=…` when a plan is chosen — which ended in the
  // query string, matched no tab, and answered the OVERVIEW payload to the
  // placements page. Choosing a second plan showed the first one's lines.
  const [path, query] = url.split("?");
  const params = new URLSearchParams(query ?? "");
  const datastreamId = datastreamOf(path);
  const mode = modeOf(datastreamId);

  if (path.includes("/export/columns")) {
    return { status: 200, body: { ...EXPORT_COLUMNS, datastream_id: datastreamId } };
  }
  if (path.includes("/sample")) return { status: 200, body: { ...SAMPLE, datastream_id: datastreamId } };
  // Story 63.6 — the ONE write the live band offers. Before the tab branch: a
  // stop is addressed `…/runs/{id}/stop`, and `Runs` is also a tab.
  if (path.endsWith("/stop")) return { status: 200, body: stopRunFixture() };
  if (path.endsWith("/progress")) {
    return { status: 200, body: progressFixture(request.run, datastreamId) };
  }
  // Lot B1. Before the tab branch, which matches on `/workbench/…`.
  if (path.endsWith("/workbench/landed-file")) {
    return {
      status: 200,
      body:
        mode === "managed_feed"
          ? landedFileFixture(request.file, datastreamId)
          : notAPushedSource(datastreamId),
    };
  }
  if (path.endsWith("/daily-breakdown")) {
    return {
      status: 200,
      body: dailyBreakdownFixture({
        datastreamId,
        // The window the screen asked for, echoed rather than replaced: the date
        // controls of that panel move it, and a fixture that answered its own
        // window would make them look broken.
        start: params.get("start") ?? "2026-07-27",
        end: params.get("end") ?? "2026-08-02",
        // Present only when a day was asked for — the route reads one day's rows
        // uninvited nowhere.
        day: params.get("day"),
        shape: request.days,
        mode,
        columns: mode === "managed_feed" ? FEED_COLUMNS : undefined,
      }),
    };
  }
  if (path.includes("/workbench/")) {
    const evidence = mode === "managed_feed" ? FEED_EVIDENCE : EVIDENCE;
    const tab = DATASTREAM_TABS.find((entry) => path.endsWith(`/${entry}`));
    // A tab this Datastream does not have answers like an address that does not
    // exist. Substituting the Overview payload is what made the placements defect
    // above invisible for a week.
    if (!tab || !(tab in evidence)) {
      return { status: 404, body: { code: "not_found", message: `${SANDBOX_MARK} no fixture is registered for this tab.` } };
    }
    return {
      status: 200,
      body: {
        schema: `datastream_workbench.${tab}.v1`,
        tab,
        project_id: HEADER.identity.project_id,
        datastream_id: datastreamId,
        evidence: evidence[tab],
      },
    };
  }
  if (path.includes("/workbench")) {
    return { status: 200, body: mode === "managed_feed" ? FEED_HEADER : HEADER };
  }
  // THE DAYS, in the ledger's own six words. Added with story 58.9, which mounts
  // `CoverageStrip` on `Overview` as well: without a fixture the one element
  // Jean named as correct rendered its unavailable state on the screen that
  // exists to be looked at.
  if (path.includes("/ledger")) return { status: 200, body: LEDGER };
  if (path.includes("/schedule")) return { status: 200, body: SCHEDULE };
  // THE PROJECT VOCABULARY, so the field rows render their CONTROL rather than a
  // refusal. Measured on a capture of 2026-08-12: without this branch the
  // canonical-target selector of every row fell to the fallback below, and the
  // harness's own sentence printed ten times under ten `Declare or calculate a
  // concept…` buttons — indistinguishable, in a screenshot, from something the
  // product says. A preview tool whose absences look like product copy sends a
  // reviewer hunting for defects it invented itself.
  if (path.includes("/file-source-templates/canonical-fields")) {
    return { status: 200, body: CANONICAL_FIELDS };
  }
  if (path.endsWith("/analyze/matches")) {
    return {
      status: 200,
      body: {
        matches: [{
          kind: "governed",
          authority: "governed",
          observed_coverage: "exact",
          execution_safety: "ready",
          left: {
            datastream_id: datastreamId,
            name: mode === "managed_feed" ? "Weekly media plan" : "Search Console — daily",
            mapping_version_id: "dmv_SANDBOX_SEARCH",
            published_execution_id: "dse_SANDBOX_SEARCH",
            output_version_id: "dov_SANDBOX_SEARCH",
            measures: [{ canonical_field_id: "mdm_clicks", name: "Clicks", aggregation: "sum" }],
          },
          right: {
            datastream_id: "ds_SANDBOX_ADS",
            name: "Paid campaign spend",
            mapping_version_id: "dmv_SANDBOX_ADS",
            published_execution_id: "dse_SANDBOX_ADS",
            output_version_id: "dov_SANDBOX_ADS",
            measures: [{ canonical_field_id: "mdm_spend", name: "Spend", aggregation: "sum" }],
          },
          common_key: {
            id: "mck_SANDBOX_CAMPAIGN_DAY",
            name: "Campaign + day",
            version_id: "mckv_SANDBOX_CAMPAIGN_DAY_3",
            version_number: 3,
            components: [
              { canonical_field_id: "mdm_day", canonical_name: "Day" },
              { canonical_field_id: "mdm_campaign", canonical_name: "Campaign" },
            ],
          },
          key_paths: [],
          relationship: {
            relationship_name: "search_to_spend",
            cardinality: "many_to_one",
            fan_out_policy: "forbid",
            bridge_dataset: null,
            view_id: "sv_SANDBOX",
            view_version_id: "svv_SANDBOX_3",
            view_version_number: 3,
          },
          unlocked_measures: 2,
          analysis: "Compare clicks with campaign spend by Campaign and Day.",
          explore_together: {
            datastreams: [],
            common_key_version_id: "mckv_SANDBOX_CAMPAIGN_DAY_3",
            view_version_id: "svv_SANDBOX_3",
            relationship_name: "search_to_spend",
          },
        }],
        counts: { datastreams_published: 2, governed: 1, candidates: 0, returned: 1 },
        bounds: { max_datastreams_scanned: 200, max_matches: 50, truncated: false },
        observed_coverage_state: "exact",
        empty_reason: null,
      },
    };
  }
  // Anything else the tab reaches for is genuinely absent in this harness, and
  // the screen's own unavailable surface is part of what has to be looked at.
  return { status: 404, body: { code: "not_found", message: `${SANDBOX_MARK} no fixture is registered for this route.` } };
}

export default function DatastreamWorkbenchSandbox() {
  const request = readRequest();

  installSandboxApi((url) => fixtureFor(url, request));

  return (
    <RouterProvider>
      {/* NO `tabHref` HERE, and that is the honest mount (story 57.5). This
          screen lives at `/debug/screen`, which is outside the canonical
          `/org/{org}/project/{project}/…` space the router parses, so there is
          no address for a tab of it — and composing one is exactly the defect
          57.5 removed. The tabs navigate through `onNavigateTab`, which
          `NavTabs` now honours on its own: a tab with a callback and no address
          is a control, never a disabled span. */}
      <DatastreamWorkbenchRoute
        projectId={HEADER.identity.project_id}
        datastreamId={datastreamIdFor(request.mode)}
        tab={request.tab}
        onNavigateTab={(next) => {
          const url = new URL(window.location.href);
          url.searchParams.set("tab", next);
          window.location.assign(url.toString());
        }}
        onOpenAnalytics={() => {
          window.location.assign("/debug/screen?name=AnalyzeExplore");
        }}
      />
    </RouterProvider>
  );
}
