/**
 * Chantier C -- arming this Datastream's events, from the screen, in one gesture.
 *
 * WHAT IT REPLACES. A governed Event Configuration reaches `active` through four
 * operations, and until this panel the console called none of them: measured
 * 2026-08-14, `event-configurations` appeared three times in `ui/admin/src` and
 * every occurrence was a read. The three configurations that exist in production
 * were written through the API by hand.
 *
 * IT ASKS NOTHING, BECAUSE THERE IS NOTHING LEFT TO ASK. Every field of the
 * configuration is already on the Datastream (`module_name`, `report_profile_id`,
 * its schedule) or in the Connector manifest it was built from. The server
 * derives it; this panel SHOWS what would be created, then performs it. A gesture
 * whose consequence cannot be read first is a gesture people click to find out
 * what it does.
 *
 * AND NOTHING HERE IS ABOUT VIDEO. Five Connectors of this repository declare an
 * event profile -- a social post, a campaign launch, a board change, a product
 * launch, a video upload -- and the panel branches on none of them. A Datastream
 * whose report declares no event says so, in its own words, and offers nothing:
 * an absent gesture beats a button that refuses.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError, apiGet, apiPost } from "../../lib/apiFetch";
import { Button, EvidenceRows, Panel, PanelHeader, Stack, Status } from "../../ui";

interface EventStreamPlan {
  datastream_name: string;
  configuration_name: string;
  source_mapping: { module: string; report_id: string; events: string[] };
  collection_policy: { cadence: string; timezone: string };
}

interface ArmedConfiguration {
  event_configuration_id: string;
  name: string;
  lifecycle_state: string;
  version_number: number | null;
  review_state: string | null;
}

interface EventStreamState {
  armed: ArmedConfiguration | null;
  plan: EventStreamPlan | null;
  blocked_reason: string | null;
}

type Loaded =
  | { status: "loading" }
  | { status: "ready"; body: EventStreamState }
  | { status: "error"; message: string };

/** THE TWO STORED WORDS, SAID IN THE USER'S WORDS -- like `RUN_STATE` in
 *  `SchedulePanel`, and for the same reason: the panel used to print
 *  `lifecycle_state` and `review_state` straight into a sentence, so an operator
 *  read "Kardinal video events is draft" and "Version 3 is superseded". Those
 *  are column values, and neither says what to do about it.
 *
 *  The vocabularies are closed and measured against their single writers:
 *  `event_configurations.py` sets `lifecycle_state` to `draft` on creation and
 *  `active` on arming, and excludes `archived` from every read; it sets
 *  `review_state` to `draft`, then `confirmed`, then `active`, superseding the
 *  previous `active` as `superseded`.
 *
 *  An unknown word is never printed raw: a value neither writer produces means
 *  the console is behind the server, and showing the token would tell a person
 *  something only a developer can read. */
const CONFIGURATION_STATE: Record<string, string> = {
  draft: "started but never armed",
  active: "armed",
  archived: "archived",
};

const VERSION_STATE: Record<string, string> = {
  draft: "still being written",
  confirmed: "confirmed and waiting to be armed",
  active: "the one in force",
  superseded: "replaced by a newer one",
};

function configurationState(value: string | null | undefined): string {
  return CONFIGURATION_STATE[value ?? ""] ?? "in a state this console does not recognize";
}

function versionState(value: string | null | undefined): string | null {
  if (!value) return null;
  return VERSION_STATE[value] ?? "in a state this console does not recognize";
}

export default function EventStreamPanel({
  projectId,
  datastreamId,
}: {
  projectId: string;
  datastreamId: string;
}) {
  const [state, setState] = useState<Loaded>({ status: "loading" });
  const [arming, setArming] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);

  const address = `/api/projects/${encodeURIComponent(projectId)}/datastreams/${encodeURIComponent(datastreamId)}/event-stream`;

  const load = useCallback(
    (signal?: AbortSignal) =>
      apiGet<EventStreamState>(address, signal ? { signal } : {})
        .then((body) => {
          if (signal?.aborted) return;
          setState({ status: "ready", body });
        })
        .catch((error: unknown) => {
          if (signal?.aborted) return;
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "The request failed.",
          });
        }),
    [address],
  );

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const arm = async () => {
    setArming(true);
    setRefused(null);
    try {
      await apiPost(address, {});
      await load();
    } catch (error) {
      // The server's own sentence. It names the gesture; rewriting it here is
      // how two surfaces end up refusing the same thing differently.
      setRefused(
        error instanceof ApiError
          ? error.message
          : "This event stream could not be armed. Try again.",
      );
    } finally {
      setArming(false);
    }
  };

  if (state.status === "loading") {
    return (
      <Panel>
        <PanelHeader title="Events" />
        <p className="mb-0 text-body text-text-secondary">Reading what this Datastream emits…</p>
      </Panel>
    );
  }

  if (state.status === "error") {
    return (
      <Panel>
        <PanelHeader title="Events" />
        <Status
          as="block"
          tone="error"
          title="What this Datastream emits could not be read"
          action={
            <Button variant="secondary" onClick={() => void load()}>
              Retry
            </Button>
          }
        >
          {state.message} Nothing was armed, and nothing is claimed about what is.
        </Status>
      </Panel>
    );
  }

  const { armed, plan, blocked_reason: blocked } = state.body;

  // A Datastream that collects measurements has no gesture here, and says why.
  // A disabled button would ask a person to discover a refusal by clicking it.
  if (!plan && !armed) {
    return (
      <Panel>
        <PanelHeader title="Events" />
        <Status as="block" tone="info" title="This Datastream emits no event">
          {blocked ?? "This Datastream declares no event profile."}
        </Status>
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader
        title="Events"
        description={
          armed?.lifecycle_state === "active"
            ? "What this Datastream emits, and under which governed configuration."
            : "What arming would create. Nothing is written until you ask for it."
        }
      />
      <Stack className="gap-4">
        {armed?.lifecycle_state === "active" ? (
          <Status as="block" tone="success" title={`Armed as ${armed.name}`}>
            Version {armed.version_number ?? "—"}
            {versionState(armed.review_state) ? ` is ${versionState(armed.review_state)}` : " is the one in force"}.
            Every run of this Datastream now lands its events under this configuration.
          </Status>
        ) : null}
        {armed && armed.lifecycle_state !== "active" ? (
          <Status as="block" tone="warning" title={`An unfinished configuration exists`}>
            {armed.name} was {configurationState(armed.lifecycle_state)}
            {versionState(armed.review_state) ? `, and its latest version is ${versionState(armed.review_state)}` : ""}. Arming
            now finishes it rather than creating a second one.
          </Status>
        ) : null}
        {plan ? (
          <EvidenceRows
            label="What this configuration declares"
            source={{
              events: plan.source_mapping.events.join(", "),
              connector: plan.source_mapping.module,
              report: plan.source_mapping.report_id,
              cadence: plan.collection_policy.cadence,
              timezone: plan.collection_policy.timezone,
            }}
          />
        ) : null}
        {refused ? (
          <Status as="block" tone="error" title="This event stream was not armed">
            {refused}
          </Status>
        ) : null}
        {plan && armed?.lifecycle_state !== "active" ? (
          <div>
            <Button type="button" onClick={() => void arm()} disabled={arming}>
              {arming ? "Arming…" : "Arm this event stream"}
            </Button>
          </div>
        ) : null}
      </Stack>
    </Panel>
  );
}
