/**
 * The narrow Explore door (Story 50.1 AC11).
 *
 * It proves ONE path end to end: pick governed members the server offered,
 * create a Query Spec version, execute it, and show the real receipt with a
 * bounded evidence preview. It is deliberately NOT the Query/Result workbench —
 * that is Story 50.2, with its six lenses and its route-backed tabs.
 *
 * What it refuses to do, and why each refusal matters:
 *
 *   - No fixtures. Every member comes from `query-options`, which the server
 *     composes from the same compiled artifact its validator reads.
 *   - No local compatibility. A pair the compiler did not prove is disabled with
 *     its reason shown, rather than offered and refused after the round trip.
 *   - No collapsing of outcomes. `empty` means we asked and nothing matched;
 *     `unavailable` means we could not ask. Showing "no data" for both is the
 *     single most misleading thing this screen could do, so they render
 *     differently and `unavailable` shows which link is missing.
 *
 * STORY 50.2 EXTENDED IT, and this is why it was extended rather than replaced.
 * The door already held every refusal above; what it lacked was the rest of the
 * governed request — time window, grain, comparison, sort, limit, classification
 * facets — and a way out to the Result workbench. Building a SECOND builder
 * beside it would have meant two screens creating Query Specs, which is exactly
 * the duplicated authority the refusals above exist to prevent. So:
 *
 *   - options now come from `query-facets`, the superset of `query-options`
 *     composed by the same server from the same compiled artifact;
 *   - the governed controls are rendered from that response, never from a list
 *     kept here — an absent grain vocabulary shows its reason, not `day/week`;
 *   - a terminal receipt calls `onResult`, and Explore navigates to the exact
 *     immutable Result address. The inline receipt stays for the case where no
 *     navigation handler is wired.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import {
  ObjectId, Button, Checkbox, Field, Input, NativeSelect, Panel, PanelHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
  stateLabel,
  stateTone,
} from "../ui";
import {
  createQuerySpec,
  executeQuerySpecVersion,
  fetchResultEvidence,
  type ExecutionReceipt,
  type ResultEvidence,
} from "./queryClient";
import { EnvelopeMismatch, fetchQueryFacets, type QueryFacets } from "./workbenchClient";

type Phase =
  | { status: "loading" }
  | { status: "ready"; options: QueryFacets }
  | { status: "denied" }
  | { status: "error"; message: string };

/** The governed controls, held as the request the server will be sent. */
interface Controls {
  timeMemberId: string;
  start: string;
  end: string;
  asOf: string;
  grain: string;
  comparison: string;
  sortMemberId: string;
  sortDirection: string;
  rowLimit: string;
  facetMemberId: string;
  facetValue: string;
}

/*
 * THE PRIVATE OUTCOME MAP IS GONE (76-2). It disagreed with the union about
 * three words at once and said so nowhere: `unavailable` amber where the union
 * says neutral (the server could not ask -- a silence, not a fault the reader
 * can fix), `refused` amber where the union says error (a refusal produced no
 * answer), and `empty` cyan where the union says warning (`info` is a statement
 * about the deployment, never a verdict on a run). `stateTone` answers all five.
 */

const EMPTY_CONTROLS: Controls = {
  timeMemberId: "",
  start: "",
  end: "",
  asOf: "",
  grain: "",
  comparison: "none",
  sortMemberId: "",
  sortDirection: "desc",
  rowLimit: "",
  facetMemberId: "",
  facetValue: "",
};

export default function QueryDoor({
  projectId,
  semanticViewId,
  semanticViewVersionId,
  onResult,
}: {
  projectId: string;
  semanticViewId: string;
  semanticViewVersionId: string;
  /** Story 50.2 AC5: a terminal receipt navigates to the exact Result address. */
  onResult?: (receipt: ExecutionReceipt, querySpec: { query_spec_id: string; id: string }) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [measures, setMeasures] = useState<string[]>([]);
  const [dimensions, setDimensions] = useState<string[]>([]);
  const [controls, setControls] = useState<Controls>(EMPTY_CONTROLS);
  const [running, setRunning] = useState(false);
  const [receipt, setReceipt] = useState<ExecutionReceipt | null>(null);
  const [evidence, setEvidence] = useState<ResultEvidence | null>(null);
  const [failure, setFailure] = useState<{ message: string; detail?: unknown } | null>(null);
  const runButton = useRef<HTMLButtonElement>(null);

  // Route-scoped fetch with symmetric cleanup: a slow answer for a PREVIOUS pin
  // must never overwrite the current one.
  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    setMeasures([]);
    setDimensions([]);
    setControls(EMPTY_CONTROLS);
    setReceipt(null);
    setEvidence(null);
    fetchQueryFacets(projectId, semanticViewId, semanticViewVersionId, {
      signal: controller.signal,
    })
      .then((options) => {
        if (live) setPhase({ status: "ready", options });
      })
      .catch((err: unknown) => {
        if (!live || controller.signal.aborted) return;
        if (err instanceof EnvelopeMismatch) {
          // The response does not describe what this address asked for. Refusing
          // beats rendering: the alternative is one pin's members offered under
          // another pin's name.
          setPhase({ status: "error", message: err.message });
          return;
        }
        if (err instanceof ApiError && (err.status === 404 || err.unauthenticated)) {
          setPhase({ status: "denied" });
          return;
        }
        setPhase({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, semanticViewId, semanticViewVersionId]);

  const options = phase.status === "ready" ? phase.options : null;

  // Compatibility is READ from the server's pair list, never computed here.
  const blockedDimensions = useMemo(() => {
    if (!options || measures.length === 0) return new Set<string>();
    const blocked = new Set<string>();
    for (const dimension of options.dimensions) {
      const legal = measures.every((m) =>
        options.pairs.some(
          (p) => p.measure_id === m && p.dimension_id === dimension.concept_id && p.queryable,
        ),
      );
      if (!legal) blocked.add(dimension.concept_id);
    }
    return blocked;
  }, [options, measures]);

  const toggle = (list: string[], setList: (v: string[]) => void, id: string) =>
    setList(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);

  const set = (key: keyof Controls, value: string) =>
    setControls((current) => ({ ...current, [key]: value }));

  /** A member's governed label. Never a slug prettified in the browser: the
   *  label belongs to the member version, and inventing one here would put a
   *  second name on a governed object. */
  const memberLabel = (id: string) =>
    [...(options?.measures ?? []), ...(options?.dimensions ?? [])].find(
      (member) => member.concept_id === id,
    )?.label ?? id;

  /**
   * The canonical request. Every field is either something the person chose from
   * a server-composed option, or omitted entirely — there is no default invented
   * here, because a default invented in a browser is a semantic decision nobody
   * ratified. The server remains the validator and the hash authority.
   */
  /**
   * Story 67.9 — WHY THIS QUESTION IS NOT ASKED YET.
   *
   * `query_specs._compile_comparison` refuses a comparison on THREE conditions,
   * and the screen ignored all three: it offered the choice, then the server
   * refused at submit. "Every question narrows the next one: answering one
   * narrows the next, or the next is not asked" — here it was asked and narrowed
   * nothing, and the person learned their mistake after filling everything in.
   *
   * The sentences are the server's, not translations of them: two wordings of
   * one refusal become two refusals in the reader's head.
   *
   * DECLARED BEFORE `buildSpec`, and that is the repair of 2026-08-22. It used
   * to be computed sixty lines BELOW, so the request builder could not consult
   * it — the control was disabled going forward and the VALUE survived its
   * precondition going backward. Measured by probe: choose `previous_period`
   * with the three conditions met, then clear the To date, and `buildSpec` still
   * sent `comparison: previous_period` with a `time` carrying no `end` — exactly
   * what `query_specs.py` refuses as `comparison_window_required`. A question
   * that stops being askable must stop being answered.
   *
   * AND THE SENTENCES ARE NOW READ, NOT RETYPED — the rest of 67.9. The claim
   * above ("the sentences are the server's") was written while three adapted
   * COPIES of them sat right here, tails changed. Two wordings of one refusal is
   * exactly what the claim forbids, and nothing would have caught the day the
   * validator changed its words. The screen still decides for itself WHICH
   * condition is unmet — that decision gates `buildSpec` and must not fail open
   * on an older payload — but the words come from
   * `query_specs.COMPARISON_PRECONDITIONS`, served in the facets response.
   */
  const unmetComparisonCondition: string | null = !controls.timeMemberId
    ? "time_member_required"
    : !dimensions.includes(controls.timeMemberId)
      ? "time_member_not_selected"
      : !controls.start || !controls.end
        ? "window_required"
        : null;

  const comparisonBlocker: string | null =
    (unmetComparisonCondition &&
      options?.time.comparison_preconditions?.find(
        (precondition) => precondition.condition === unmetComparisonCondition,
      )?.message) ||
    null;

  const buildSpec = useCallback(() => {
    const spec: Record<string, unknown> = { measures, dimensions };
    if (controls.rowLimit.trim()) spec.row_limit = Number(controls.rowLimit);
    if (controls.grain) spec.grain = controls.grain;
    // A comparison whose precondition has gone is not sent. The disabled control
    // stops it being CHOSEN; this stops it being KEPT. It reads the CONDITION,
    // not the sentence: a payload that carried no sentence would otherwise
    // reopen the gate, which is the fail-open this screen exists to refuse.
    if (controls.comparison && !unmetComparisonCondition) spec.comparison = controls.comparison;
    if (controls.sortMemberId) {
      spec.sort = [{ member_id: controls.sortMemberId, direction: controls.sortDirection }];
    }
    if (controls.timeMemberId) {
      spec.time = {
        member_id: controls.timeMemberId,
        start: controls.start || undefined,
        end: controls.end || undefined,
        as_of: controls.asOf || undefined,
      };
    }
    if (controls.facetMemberId && controls.facetValue) {
      const facet = options?.classification_facets.find(
        (candidate) => candidate.dimension_id === controls.facetMemberId,
      );
      const member = facet?.members.find(
        (candidate) => candidate.classification_object_id === controls.facetValue,
      );
      spec.filters = [
        {
          member_id: controls.facetMemberId,
          operator: "eq",
          value: member?.slug ?? controls.facetValue,
          // The pins travel WITH the filter: a classification value only means
          // something under the hierarchy version that defined it.
          classification_object_id: member?.classification_object_id,
          hierarchy_id: member?.hierarchy_id ?? undefined,
          hierarchy_version_id: member?.hierarchy_version_id ?? undefined,
        },
      ];
    }
    return spec;
  }, [measures, dimensions, controls, options, unmetComparisonCondition]);

  const run = useCallback(async () => {
    if (!options) return;
    setRunning(true);
    setFailure(null);
    setReceipt(null);
    setEvidence(null);
    try {
      const version = await createQuerySpec(projectId, {
        semantic_view_id: semanticViewId,
        semantic_view_version_id: semanticViewVersionId,
        spec: buildSpec(),
      });
      const result = await executeQuerySpecVersion(projectId, version.id);
      setReceipt(result);
      setEvidence(await fetchResultEvidence(projectId, result.result_id));
      // Every terminal outcome navigates — including `unavailable`, which is an
      // answer with inspectable evidence, not a failure to hide.
      onResult?.(result, { query_spec_id: version.query_spec_id, id: version.id });
    } catch (err: unknown) {
      // The server's structured refusal is shown as-is. Rewriting it here would
      // hide which member was rejected, and a caller who cannot see that guesses.
      if (err instanceof ApiError) {
        setFailure({ message: err.message, detail: err.body });
      } else {
        setFailure({ message: err instanceof Error ? err.message : String(err) });
      }
    } finally {
      setRunning(false);
      runButton.current?.focus();
    }
  }, [options, projectId, semanticViewId, semanticViewVersionId, buildSpec, onResult, reloadToken]);

  if (phase.status === "loading") {
    return (
      <p role="status" className="text-body text-text-secondary">
        Loading the governed query options…
      </p>
    );
  }
  if (phase.status === "denied") {
    return (
      <Status as="block" tone="warning" title="These query options are not available to you">
        Nothing has been opened in their place.
      </Status>
    );
  }
  if (phase.status === "error") {
    return (
      <Status as="block" tone="error" title="The query options could not be read"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >
        {phase.message}
      </Status>
    );
  }

  const ready = phase.options;
  if (!ready.executable) {
    return (
      <Status as="block" tone="warning" title="This version cannot run a query">
        Its status is {stateLabel(ready.status)}. Only a published, compiled version executes, and
        no other version has been substituted for it.
      </Status>
    );
  }

  return (
    <Stack className="gap-6" data-testid="explore-query-door">
      <Panel>
        <PanelHeader
          title="Build a governed query"
          description="Members come from the compiled Semantic View version. Nothing here is a sample."
        />
        <div className="grid gap-6 md:grid-cols-2">
          <fieldset className="grid gap-2">
            <legend className="text-ui font-medium">Measures</legend>
            {ready.measures.map((m) => (
              <label key={m.concept_id} className="flex items-center gap-2 text-body">
                <Checkbox
                  checked={measures.includes(m.concept_id)}
                  onCheckedChange={() => toggle(measures, setMeasures, m.concept_id)}
                />
                {m.label}
              </label>
            ))}
          </fieldset>
          <fieldset className="grid gap-2">
            <legend className="text-ui font-medium">Dimensions</legend>
            {ready.dimensions.map((d) => {
              const blocked = blockedDimensions.has(d.concept_id);
              return (
                <label
                  key={d.concept_id}
                  className={`flex items-center gap-2 text-body ${blocked ? "opacity-60" : ""}`}
                >
                  <Checkbox
                    checked={dimensions.includes(d.concept_id)}
                    disabled={blocked}
                    onCheckedChange={() => toggle(dimensions, setDimensions, d.concept_id)}
                  />
                  {d.label}
                  {blocked ? (
                    <span className="text-caption text-text-secondary">
                      not proved compatible with the selected measures
                    </span>
                  ) : null}
                </label>
              );
            })}
          </fieldset>
        </div>
      </Panel>

      <Panel data-testid="explore-governed-controls">
        <PanelHeader
          title="Governed controls"
          description="Time, grain, comparison, sort, limit and classification facets, exactly as the published version allows them."
        />
        <div className="grid gap-4 md:grid-cols-3">
          <Field label="Time member">
            {(field) => (
              <NativeSelect
                {...field}
                value={controls.timeMemberId}
                onChange={(event) => set("timeMemberId", event.target.value)}
              >
                <option value="">None</option>
                {ready.time.members.map((member) => (
                  <option key={member.concept_id} value={member.concept_id}>
                    {member.label}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="From">
            {(field) => (
              <Input
                {...field}
                type="date"
                value={controls.start}
                onChange={(event) => set("start", event.target.value)}
              />
            )}
          </Field>
          <Field label="To">
            {(field) => (
              <Input
                {...field}
                type="date"
                value={controls.end}
                onChange={(event) => set("end", event.target.value)}
              />
            )}
          </Field>
          {/* The grain list is whatever the published version declares. When it
              declares none the control is disabled and says why -- offering
              day/week/month here would be a vocabulary invented in a browser. */}
          <Field label="Grain" hint={ready.time.grains_unavailable_reason ?? undefined}>
            {(field) => (
              <NativeSelect
                {...field}
                value={controls.grain}
                disabled={ready.time.grains.length === 0}
                onChange={(event) => set("grain", event.target.value)}
              >
                <option value="">None</option>
                {ready.time.grains.map((grain) => (
                  <option key={grain} value={grain}>
                    {grain}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Source comparison" hint={comparisonBlocker ?? undefined}>
            {(field) => (
              <NativeSelect
                {...field}
                value={controls.comparison}
                disabled={unmetComparisonCondition !== null}
                onChange={(event) => set("comparison", event.target.value)}
              >
                {ready.time.comparisons.map((comparison) => (
                  <option key={comparison} value={comparison}>
                    {comparison}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Reported as of">
            {(field) => (
              <Input
                {...field}
                type="date"
                value={controls.asOf}
                onChange={(event) => set("asOf", event.target.value)}
              />
            )}
          </Field>
          <Field label="Sort by">
            {(field) => (
              <NativeSelect
                {...field}
                value={controls.sortMemberId}
                onChange={(event) => set("sortMemberId", event.target.value)}
              >
                <option value="">None</option>
                {/* Only members this request actually asks for: sorting by a
                    member that is not in the answer is a meaning change wearing
                    a presentation control's clothes, and the server refuses it. */}
                {[...measures, ...dimensions].map((id) => (
                  <option key={id} value={id}>
                    {memberLabel(id)}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Direction">
            {(field) => (
              <NativeSelect
                {...field}
                value={controls.sortDirection}
                onChange={(event) => set("sortDirection", event.target.value)}
              >
                {ready.sort.directions.map((direction) => (
                  <option key={direction} value={direction}>
                    {direction}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field
            label="Row limit"
            hint={`Up to ${ready.limits.max_row_limit}. Left empty, the server applies ${ready.limits.default_row_limit}.`}
          >
            {(field) => (
              <Input
                {...field}
                inputMode="numeric"
                value={controls.rowLimit}
                onChange={(event) => set("rowLimit", event.target.value)}
              />
            )}
          </Field>
        </div>

        {ready.classification_facets.length > 0 ? (
          <div className="mt-4 grid gap-4 md:grid-cols-2">
            <Field label="Classification facet">
              {(field) => (
                <NativeSelect
                  {...field}
                  value={controls.facetMemberId}
                  onChange={(event) => {
                    set("facetMemberId", event.target.value);
                    set("facetValue", "");
                  }}
                >
                  <option value="">None</option>
                  {ready.classification_facets.map((facet) => (
                    <option key={facet.dimension_id} value={facet.dimension_id}>
                      {facet.facet}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
            <Field label="Facet value">
              {(field) => (
                <NativeSelect
                  {...field}
                  value={controls.facetValue}
                  disabled={!controls.facetMemberId}
                  onChange={(event) => set("facetValue", event.target.value)}
                >
                  <option value="">Any</option>
                  {(
                    ready.classification_facets.find(
                      (facet) => facet.dimension_id === controls.facetMemberId,
                    )?.members ?? []
                  ).map((member) => (
                    <option
                      key={member.classification_object_id}
                      value={member.classification_object_id}
                    >
                      {member.label}
                    </option>
                  ))}
                  {/* `Other` and `Unknown` stay visible rather than being filtered
                      out as "not real": a governed classification defines them,
                      and hiding them loses every row that landed there. */}
                  {(
                    ready.classification_facets.find(
                      (facet) => facet.dimension_id === controls.facetMemberId,
                    )?.reserved_members ?? []
                  ).map((reserved) => (
                    <option key={reserved} value={reserved}>
                      {reserved}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
          </div>
        ) : null}

        {ready.classification_facets_unavailable.length > 0 ? (
          <Status
            as="block"
            tone="info"
            title="Some governed facets are not available here"
            className="mt-4"
            data-testid="explore-facets-unavailable"
          >
            <ul className="mt-1 list-disc pl-5">
              {ready.classification_facets_unavailable.map((entry) => (
                <li key={entry.facet} className="text-body">
                  <code className="text-technical">{entry.facet}</code> — {entry.reason}
                </li>
              ))}
            </ul>
          </Status>
        ) : null}

        <div className="mt-4 flex items-center gap-3">
          <Button
            ref={runButton}
            onClick={run}
            disabled={running || measures.length === 0}
            data-testid="explore-run"
          >
            {running ? "Running…" : "Run query"}
          </Button>
          {measures.length === 0 ? (
            <span className="text-caption text-text-secondary">
              A query needs at least one measure.
            </span>
          ) : null}
        </div>
      </Panel>

      <div role="status" aria-live="polite">
        {running ? <span className="text-body text-text-secondary">Executing…</span> : null}
      </div>

      {failure ? (
        <Status as="block" tone="error" title="The request was refused">
          <p>{failure.message}</p>
          {Array.isArray((failure.detail as { refusals?: unknown[] })?.refusals) ? (
            <ul className="mt-2 list-disc pl-5">
              {((failure.detail as { refusals: { code: string; message: string; subject?: string }[] })
                .refusals).map((r, i) => (
                <li key={i} className="text-body">
                  <code>{r.subject ?? r.code}</code> — {r.message}
                </li>
              ))}
            </ul>
          ) : null}
        </Status>
      ) : null}

      {receipt ? (
        <Panel data-testid="explore-receipt">
          <PanelHeader
            title="Result"
            description={`Immutable Result ${receipt.result_id}. Running it again creates another one.`}
          />
          <Status as="block" tone={stateTone(receipt.outcome)} title={stateLabel(receipt.outcome)}>
            {receipt.outcome === "unavailable"
              ? `The query could not be asked: ${String(
                  (evidence?.manifest as { unavailable_reason?: string })?.unavailable_reason ??
                    "the governed path did not resolve",
                )} (missing link: ${String(
                  (evidence?.manifest as { missing_link?: string })?.missing_link ?? "unknown",
                )}). This is NOT an empty answer.`
              : receipt.outcome === "empty"
                ? "The query ran and nothing matched. The data path is healthy."
                : `${receipt.row_count} row(s)${receipt.truncated ? ", truncated" : ""}.`}
          </Status>
          <dl className="mt-4 grid gap-2 md:grid-cols-3">
            <div>
              <dt className="text-caption text-text-secondary">AI Path</dt>
              <dd className="text-body">{receipt.ai_path}</dd>
            </div>
            <div>
              <dt className="text-caption text-text-secondary">Content hash</dt>
              <dd className="text-technical break-all">{receipt.content_hash}</dd>
            </div>
            <div>
              <dt className="text-caption text-text-secondary">Attempt</dt>
              <dd className="m-0"><ObjectId value={receipt.attempt_id} title="Attempt" /></dd>
            </div>
          </dl>
          {evidence && evidence.rows.length > 0 ? (
            <TableScroll label="Returned rows" className="mt-4">
              <Table>
                <TableHeader>
                  <TableRow>
                    {(evidence.schema.fields ?? []).map((f) => (
                      <TableHead key={f.name}>{f.name}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {evidence.rows.slice(0, 20).map((row, i) => (
                    <TableRow key={i}>
                      {(evidence.schema.fields ?? []).map((f) => (
                        <TableCell key={f.name}>{String(row[f.name] ?? "")}</TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          ) : null}
        </Panel>
      ) : null}
    </Stack>
  );
}
