import { useMemo } from "react";
import { useDataSurface } from "../../data/dataSurface";
import { Button, EmptyState, formatNumber, Metric, PageHeader, Panel, PanelHeader, Stack, Status, stateLabel, wireWord } from "../../ui";
import { StateValue } from "../../data/DataCollectionLayout";

const LENS_LABELS: Record<string, string> = {
  sources: "Source Accounts",
  datastreams: "Datastreams",
  imports: "Imports",
  events: "Event Configurations",
  connectors: "Connectors",
};

/**
 * The per-axis breakdown the server already computes, or null when the lens
 * carries none.
 *
 * `_compose_overview` (`server/core/data_surface.py:947`) counts every object's
 * state ON EACH AXIS and ships it as `evidence.state_counts`. This screen read
 * `object_count` and nothing else, so "12 Datastreams" was all a person got --
 * the twelve could be eleven published and one failed, or twelve never run, and
 * the page said the same word for both. `screens/expectations.json` names it:
 * "afficher la repartition des etats, pas seulement un compteur". It is the
 * README's most frequent class -- `route` without `calls`: the server computes,
 * the screen throws it away.
 *
 * The axes are NOT merged into a score. Data Overview's whole contract is that
 * independent state axes stay independent (`overview.md`: business signals,
 * operational health and trust never collapse into one compensating score), so
 * each axis is rendered as its own line.
 */
function stateBreakdown(evidence: Record<string, unknown> | undefined) {
  const raw = evidence?.state_counts;
  if (!raw || typeof raw !== "object") return null;
  const axes = Object.entries(raw as Record<string, unknown>)
    .map(([axis, counts]) => ({
      axis,
      counts:
        counts && typeof counts === "object"
          ? Object.entries(counts as Record<string, unknown>)
              .filter(([, n]) => typeof n === "number" && n > 0)
              // Highest first: what most of the objects are is the thing a
              // person is reading for.
              .sort((a, b) => Number(b[1]) - Number(a[1]))
              .map(([value, n]) => ({ value, n: Number(n) }))
          : [],
    }))
    .filter((entry) => entry.counts.length > 0);
  return axes.length > 0 ? axes : null;
}

export default function DataOverview({ projectId }: { projectId: string }) {
  const { state, reload } = useDataSurface(projectId, "overview");
  const items = state.status === "ready" ? state.envelope.items : [];
  const byLens = useMemo(() => new Map(items.map((item) => [item.lens, item])), [items]);
  const flow = ["sources", "datastreams", "imports", "events"];

  return (
    <Stack data-owner="data/data-overview">
      <PageHeader title="Data Overview" description="Evidence-backed coverage from authorized Source Accounts through Datastream configuration and publication." />
      {state.status === "loading" && <p role="status" className="text-body text-text-secondary">Loading Data posture…</p>}
      {state.status === "error" && (
        <Status as="block" tone="error" title="Data posture unavailable" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          {state.message}. No healthy posture has been inferred.
        </Status>
      )}
      {state.status === "ready" && (
        <>
          <Panel flush>
            <PanelHeader title="Project coverage" description="Counts come from the canonical owner of each Data object." />
            <div className="grid divide-x divide-divider-base sm:grid-cols-2 xl:grid-cols-5">
              {state.envelope.items.map((item) => (
                // THE CONSOLE'S WORD AND THE CONSOLE'S TONE (76-6). This tile
                // printed `{item.states.evidence}` — the stored token, lower
                // case — under a dot whose colour was decided by a ternary
                // written here. `StateValue` is the one rendering every other
                // Data screen uses, and it reads both halves from the union, so
                // `Available` cannot be spelled two ways inside one workspace.
                // A count nobody sent is NOT a zero: `??  0` claimed a
                // measurement, and `formatNumber` answers the dash instead.
                <Metric
                  key={item.lens}
                  label={LENS_LABELS[item.lens ?? ""] ?? wireWord(item.lens)}
                  value={formatNumber(item.object_count)}
                  hint={<StateValue value={item.states.evidence} />}
                />
              ))}
            </div>
          </Panel>
          <Panel flush>
            <PanelHeader title="Source-to-publication flow" description="Each stage remains independent; missing evidence is never promoted to healthy." />
            <div className="grid gap-3 p-5 md:grid-cols-4">
              {flow.map((lens, index) => {
                const item = byLens.get(lens);
                const breakdown = stateBreakdown(item?.evidence);
                return (
                  <div key={lens} className="relative rounded-control border border-divider-base p-4" data-testid={`data-overview-stage-${lens}`}>
                    <span className="text-caption font-semibold uppercase tracking-wide text-text-secondary">Stage {index + 1}</span>
                    <h3 className="mt-1 text-h3 font-h3 text-text">{LENS_LABELS[lens]}</h3>
                    <p className="mt-2 font-numeric text-metric text-text">{formatNumber(item?.object_count)}</p>
                    <StateValue value={item?.states.evidence} />
                    {breakdown ? (
                      <dl className="mt-3 flex flex-col gap-1.5" data-testid={`data-overview-breakdown-${lens}`}>
                        {breakdown.map(({ axis, counts }) => (
                          <div key={axis} className="flex flex-col gap-0.5">
                            {/* The axis is a wire NAME and the count is keyed
                                by a wire STATE, and the two take different
                                answers: `wireWord` puts a stored token's
                                spelling right, `stateLabel` replaces a state
                                word with the console's own. Both were
                                `.replaceAll("_", " ")` — the same de-snaker for
                                two different questions, and lower case for
                                both, so a stage read `12 never run` under
                                `installation`. */}
                            <dt className="text-caption uppercase tracking-wide text-text-secondary">{wireWord(axis)}</dt>
                            <dd className="flex flex-wrap gap-x-3 gap-y-0.5 text-caption text-text">
                              {counts.map(({ value, n }) => (
                                <span key={value}>
                                  <span className="font-numeric font-semibold">{formatNumber(n)}</span>{" "}
                                  {stateLabel(value)}
                                </span>
                              ))}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    ) : (
                      // Absent is NOT zero. A lens whose owner shipped no
                      // breakdown must not read as "every object is fine".
                      item?.object_count
                        ? <p className="mt-3 text-caption text-text-secondary" data-testid={`data-overview-breakdown-absent-${lens}`}>State breakdown not reported by this lens.</p>
                        : null
                    )}
                  </div>
                );
              })}
            </div>
          </Panel>
          <Panel flush>
            <PanelHeader title="Attention queue" description="Only explicit missing owner evidence is listed." />
            {state.envelope.unavailable_reasons.length === 0 ? (
              <EmptyState title="No missing collection evidence" description="This does not collapse each object's independent state axes into a global health claim." />
            ) : (
              <div className="divide-y divide-divider-base">
                {state.envelope.unavailable_reasons.map((reason) => (
                  <Status key={reason.code} as="block" tone="warning" title={wireWord(reason.code)} className="rounded-none border-0">{reason.message}</Status>
                ))}
              </div>
            )}
          </Panel>
        </>
      )}
    </Stack>
  );
}