/**
 * Explore, the receiving end of the Governance handoff (Story 49.3 AC12).
 *
 * This shell resolves the EXACT published Semantic View version the address
 * pins, then hands it to the narrow query door delivered by Story 50.1, which
 * creates a real Query Spec version and executes it through the server
 * capability. What is still absent is the six-lens Result workbench: that is
 * Story 50.2, and it owns the route-backed tabs and the evidence lenses.
 *
 * The two things it refuses to do:
 *
 *   - Silently switch versions. If the pinned version is superseded it says so
 *     and keeps showing the pinned one. Opening the current version instead
 *     would make a shared address show something other than what was shared.
 *   - Accept half a pin. The id and the version id are required together; the
 *     router drops either one alone, so an incomplete address arrives here as
 *     no pin at all rather than as a guess.
 */
import { useState } from "react";

import { Button, Metric, PageHeader, Panel, Stack, Status } from "../../ui";
import AnalyticsExplorer from "../../analyze/explorer/AnalyticsExplorer";
import { readHandoffPins } from "../../analyze/explorer/handoffPins";
import QueryDoor from "../../analyze/QueryDoor";
import { useGovernanceObject } from "../../governance/governanceSurface";
import { useRoute } from "../router";

function PinnedView({
  projectId,
  viewId,
  versionId,
  businessDomainId,
  onResult,
  onDropPin,
  resultRouteFailure,
}: {
  projectId: string;
  viewId: string;
  versionId: string;
  businessDomainId: string | null;
  onResult: (resultId: string) => void;
  /** THE WAY OUT OF A PIN THAT NAMES NOTHING (76-4). All three refusals below
   *  used to end the screen: a shared address that cannot be opened left the
   *  person with a sentence and no control, on a workspace that works perfectly
   *  well without the pin. Dropping the pin is the one gesture available here,
   *  and only the route owner can perform it. */
  onDropPin: () => void;
  resultRouteFailure: string | null;
}) {
  // The SAME application service the Governance workbench reads. A second
  // Explore-only endpoint would be a second answer to one question.
  const { state } = useGovernanceObject(projectId, "semantic-model", "semantic-view", viewId, versionId);

  if (state.status === "loading") {
    return (
      <p role="status" className="text-body text-text-secondary">
        Resolving the pinned Semantic View version…
      </p>
    );
  }
  if (state.status === "denied") {
    return (
      <Status
        as="block"
        tone="warning"
        title="You may not use this Semantic View"
        action={<Button variant="secondary" onClick={onDropPin}>Explore without this pin</Button>}
      >
        Your access to this Project does not include the Semantic View this address pins. Nothing
        else has been opened in its place.
      </Status>
    );
  }
  if (state.status === "unknown") {
    return (
      <Status
        as="block"
        tone="warning"
        title="This Semantic View version was not found"
        action={<Button variant="secondary" onClick={onDropPin}>Explore without this pin</Button>}
      >
        No Semantic View with this identifier and this version exists in this Project. The current
        version has NOT been opened instead: a shared address shows what it pins, or it shows nothing.
      </Status>
    );
  }
  if (state.status === "error") {
    return (
      <Status
        as="block"
        tone="error"
        title="The Semantic View could not be resolved"
        action={<Button variant="secondary" onClick={onDropPin}>Explore without this pin</Button>}
      >
        {state.message}
      </Status>
    );
  }

  const detail = state.envelope.object;
  if (!detail) {
    return (
      <Status as="block" tone="warning" title="The Semantic View owner did not answer">
        {state.envelope.unavailable_reasons[0]?.message
          ?? "The Semantic Model owner could not be read. This is not an empty Semantic View."}
      </Status>
    );
  }

  const summary = detail.summary as Record<string, unknown>;
  const stale = state.envelope.version_state === "stale";
  const queryable = Number(summary.queryable_pairs ?? 0);

  return (
    <Stack className="gap-6">
      <Status
        as="block"
        tone={stale ? "warning" : "success"}
        title={stale ? "This pinned version is no longer current" : "This pinned version is the current one"}
        data-testid="explore-version-banner"
      >
        {stale
          ? `Version ${versionId} of ${detail.object_ref.label} is authorized, immutable and superseded. It is shown exactly as pinned; the current version has not been substituted.`
          : `Version ${versionId} of ${detail.object_ref.label} is the published version of this Semantic View.`}
      </Status>

      <Panel className="grid gap-4 md:grid-cols-4">
        <Metric label="Semantic View" value={detail.object_ref.label} />
        <Metric label="Pinned version" value={versionId} />
        <Metric
          label="Queryable pairs"
          value={String(queryable)}
          hint="Metric and dimension combinations proved compatible at compilation."
        />
        <Metric label="Business Domain" value={businessDomainId ?? "All domains"} />
      </Panel>

      {resultRouteFailure ? (
        <Status
          as="block"
          tone="warning"
          title="The Result workbench address could not be built"
          data-testid="explore-result-route-failure"
        >
          {resultRouteFailure} The receipt below is the same immutable Result, read inline.
        </Status>
      ) : null}

      {/* Story 50.1 created this door; Story 50.2 extended it into the governed
          Query workbench and gave it a way out: a terminal receipt navigates to
          the exact immutable Result and its six lenses. */}
      <QueryDoor
        projectId={projectId}
        semanticViewId={viewId}
        semanticViewVersionId={versionId}
        onResult={(receipt) => onResult(receipt.result_id)}
      />
    </Stack>
  );
}

export default function Explore({ projectId }: { projectId: string }) {
  const { route, navigate } = useRoute();
  const viewId = route.query?.semantic_view_id ?? null;
  const versionId = route.query?.semantic_view_version_id ?? null;
  const businessDomainId = route.query?.business_domain_id ?? null;
  const initialMatch = route.query?.left_datastream_id
    && route.query?.right_datastream_id
    && route.query?.common_key_version_id
    ? {
        leftDatastreamId: route.query.left_datastream_id,
        rightDatastreamId: route.query.right_datastream_id,
        commonKeyVersionId: route.query.common_key_version_id,
        // THE VERSIONS THAT WERE ON THE SCREEN, carried whole. Without them the
        // Explorer could only re-select the pair from its own catalog read, and
        // a handoff that re-resolves opens a different question than the one the
        // person was looking at (data.md, *Which Datastreams can usefully be
        // crossed*). `null` here means the address pinned none — a link typed by
        // hand, or one shared before the pins were carried — never that the
        // current heads were approved.
        pins: readHandoffPins(
          route.query,
          route.query.left_datastream_id,
          route.query.right_datastream_id,
        ),
      }
    : null;
  const [resultRouteFailure, setResultRouteFailure] = useState<string | null>(null);

  /** Story 50.2 AC5: an accepted execution navigates to the exact Result identity.
   *
   *  The `catch` is not defensive noise. The `result` object contract and its six
   *  lens tabs are registered in `shell/navigation.ts`, a file three sessions are
   *  writing at the time this landed; until that registration is applied the
   *  router REFUSES to build the address rather than building a broken one, and
   *  saying so beats an unexplained crash on the one click that matters. */
  const openResult = (resultId: string) => {
    try {
      navigate({ objectType: "result", objectId: resultId, tab: "view", versionId: null, evidenceId: null, action: null });
      setResultRouteFailure(null);
    } catch (error: unknown) {
      setResultRouteFailure(
        `The canonical router does not yet declare the Result workbench route (${
          error instanceof Error ? error.message : String(error)
        }).`,
      );
    }
  };

  const openVisualization = (visualizationId: string, resultId: string) => {
    navigate({
      objectType: "visualization",
      objectId: visualizationId,
      tab: "build",
      versionId: null,
      action: null,
      query: { result_id: resultId },
    });
  };

  return (
    <Stack className="gap-6" data-owner="analyze/explore">
      <PageHeader title="Explore" description="Build a governed query from published Semantic Views." />
      {viewId && versionId ? (
        <PinnedView
          projectId={projectId}
          viewId={viewId}
          versionId={versionId}
          businessDomainId={businessDomainId}
          onResult={openResult}
          onDropPin={() => navigate({ query: {} })}
          resultRouteFailure={resultRouteFailure}
        />
      ) : (
        /* With no exact single-view pin, Explore is the multi-Datastream
           workspace. A half pin is still discarded by the router, but it must
           not leave a contradictory empty-state above a governed match. */
        <AnalyticsExplorer
          projectId={projectId}
          onOpenVisualization={openVisualization}
          initialMatch={initialMatch}
        />
      )}
    </Stack>
  );
}
