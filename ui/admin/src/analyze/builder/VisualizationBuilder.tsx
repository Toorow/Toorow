/**
 * The Visualization Builder (Story 50.4, AC7 / AC9 / AC12 / AC13).
 *
 * THREE VISIBLY SEPARATE PARTS, exactly as `visualization-and-rendering.md:138-146`
 * describes them: the governed field catalog on the left, typed binding wells and
 * the live preview in the centre, presentation/responsive/evidence on the right.
 *
 * IT IS A ROUTE, NOT COMPONENT STATE. The Visualization identity, the tab, the
 * Visualization Spec version and the optional Result pin all live in the address,
 * so "send me what you are looking at" works and a refresh does not lose the
 * work. A copyable state that lives only in `useState` is not shareable, which is
 * most of the point of a workbench.
 *
 * IT AUTHORS NO SEMANTICS. It reads members from the pinned Query Spec version
 * and never proposes one the query did not select. It keeps no family rule, no
 * role rule and no cardinality limit: every verdict on this screen came from the
 * server. `analyze-and-test.md:421` names "Analyze creates another source of
 * metric definitions" as an anti-regression, and a browser-side catalog is
 * exactly that.
 *
 * IT DRAWS NOTHING. See `BuilderPreview` and decision D5.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Button, Cluster, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, FieldComposer, Label, NativeSelect, NavTabs, ObjectId, PageHeader, Panel, PanelHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../../ui";
import { fetchResultEvidence } from "../queryClient";
import { FALLBACK_FAMILY } from "./seedVisualization";
import VisualizationMount from "../VisualizationMount";
import { FORMATTER_VERSION, resolveRenderer, RUNTIME_BUILD, THEME_VERSION, type DisplayState, type VizSpec, type VizSpecDocument } from "@toorow/card-shell/viz";
import { addVisualizationVersion, createCanonicalRender, createVisualization, fetchResultPin, fetchVisualization, fetchVisualizationFamilies, fetchVisualizationOptions, fetchVisualizationSpecVersion, refusalsByWell, refusalsOf, type ResultDisclosures, type VisualizationFamily, type VisualizationHead, type VisualizationOptions, type VisualizationRefusal, type VisualizationRegistry, type VisualizationSpecVersion, validateVisualizationSpecVersion } from "../visualizationClient";
import BindingWells, { membersAsFields, renderableWells, type Bindings } from "./BindingWells";
import BuilderPreview, { previewState, type PreviewResult } from "./BuilderPreview";
import FieldCatalogRail from "./FieldCatalogRail";
import PresentationRail, { type Draft } from "./PresentationRail";

export const BUILDER_TABS = ["build", "versions"] as const;
export type BuilderTab = (typeof BUILDER_TABS)[number];
export const DEFAULT_BUILDER_TAB: BuilderTab = "build";

function runtimeSpec(version: VisualizationSpecVersion): VizSpec {
  return {
    visualization_spec_version_id: version.id,
    spec_contract_version: version.spec_contract_version as VizSpec["spec_contract_version"],
    schema_version: version.schema_version,
    document: version.spec as unknown as VizSpecDocument,
  };
}

export interface BuilderScope {
  organizationId: string;
  projectId: string;
}

/**
 * Deterministic family suitability (AC7). Computed from the SERVER's registry
 * rules -- required wells, accepted roles, grain and comparison -- and nothing
 * else. No AI ranking may override it, and none is consulted: this function is
 * the whole recommendation surface.
 */
export function familySuitability(
  family: VisualizationFamily,
  options: VisualizationOptions,
): { compatible: boolean; reason: string } {
  if (family.requires_time_grain && !options.grain) {
    return { compatible: false, reason: "needs a time grain; this query pinned none" };
  }
  if (family.requires_comparison && (options.comparison ?? "none") === "none") {
    return { compatible: false, reason: "needs a comparison; this query pinned none" };
  }
  const available = new Set<string>();
  if (options.measures.length) available.add("measure");
  if (options.dimensions.length) available.add("dimension");
  for (const well of family.wells) {
    if (!well.required) continue;
    if (!well.accepts.some((role) => available.has(role))) {
      return {
        compatible: false,
        reason: `needs a ${well.accepts.join(" or ")} in ${well.label}; this query selected none`,
      };
    }
  }
  return { compatible: true, reason: "every required well can be filled from this query" };
}

function emptyBindings(): Bindings {
  return {};
}

function documentOf(
  contractVersion: string,
  schemaVersion: number,
  family: string,
  bindings: Bindings,
  draft: Draft,
): Record<string, unknown> {
  return {
    ...draft,
    spec_contract_version: contractVersion,
    schema_version: schemaVersion,
    family,
    bindings,
  };
}

export default function VisualizationBuilder({
  scope,
  visualizationId,
  visualizationSpecVersionId,
  resultId,
  querySpecVersionId,
  tab,
  onNavigateTab,
  tabHref,
  exploreHref,
  resultHref,
}: {
  scope: BuilderScope;
  /** `null` for a Visualization that has not been created yet. */
  visualizationId: string | null;
  visualizationSpecVersionId: string | null;
  /** The declared, optional query pin this section owns. */
  resultId: string | null;
  /** The pin a new Visualization is built against, carried by the Explore address. */
  querySpecVersionId: string | null;
  tab: string;
  onNavigateTab: (tab: BuilderTab) => void;
  tabHref: (tab: BuilderTab) => string;
  /** Null when Explore is not mounted; the rail then says so instead of linking. */
  exploreHref: string | null;
  /** Null when the Result workbench (Story 50.2) is not mounted. */
  resultHref: ((resultId: string) => string) | null;
}) {
  const [registry, setRegistry] = useState<VisualizationRegistry | null>(null);
  const [options, setOptions] = useState<VisualizationOptions | null>(null);
  const [head, setHead] = useState<VisualizationHead | null>(null);
  const [mountedSpec, setMountedSpec] = useState<VizSpec | null>(null);
  const [result, setResult] = useState<PreviewResult | null>(null);
  const [resultContentHash, setResultContentHash] = useState<string | null>(null);
  const [disclosures, setDisclosures] = useState<ResultDisclosures | null>(null);
  const [refusals, setRefusals] = useState<VisualizationRefusal[]>([]);
  //  THE STARTING FAMILY IS NOT A BROWSER CONSTANT ANY MORE (story 72.5, AC22).
  //  This read `useState<string>("bar")`, and that literal was the product's real
  //  answer to "what does a new Visualization look like": every Builder opened on
  //  a bar chart because one file said so, while the Chart Template — the object
  //  whose whole purpose is to be a validated starting point — reached no surface
  //  at all. A Visualization that carries a stored version takes ITS family two
  //  hundred lines below (`setFamily(stored.family)`), including one materialised
  //  from a template; one that carries none opens on the NAMED fallback, the
  //  family the architecture obliges every visual to be presentable in.
  const [family, setFamily] = useState<string>(FALLBACK_FAMILY);
  const [bindings, setBindings] = useState<Bindings>(emptyBindings);
  const [draft, setDraft] = useState<Draft>({});
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [freezing, setFreezing] = useState(false);
  const [renderId, setRenderId] = useState<string | null>(null);
  const [display, setDisplay] = useState<DisplayState>({});
  const [announcement, setAnnouncement] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const evidenceInvoker = useRef<HTMLButtonElement | null>(null);

  /**
   * The Query Spec version this presentation is built against, resolved from the
   * three sources that can supply one, in order of authority.
   *
   * The third is the repair for a real dead end: `ContentRouter` mounts this
   * screen with `querySpecVersionId={null}`, so a Visualization that has no
   * version yet had no pin at all — `POST /visualizations` was unreachable from
   * a browser and Save was permanently disabled on that path. A Result the
   * address already pins DOES name its Query Spec version, and reading it from
   * the object the person is looking at is the one source that cannot disagree
   * with what they saw.
   */
  const [resolvedPin, setResolvedPin] = useState<string | null>(null);
  const pin =
    querySpecVersionId ?? head?.versions[0]?.query_spec_version_id ?? resolvedPin ?? null;

  // A Project switch must not retain a prior Project's Visualization, version,
  // labels, counts or evidence. Everything below is keyed on the Project, and the
  // effect clears before it fetches rather than merging onto stale state.
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setLoading(true);
    setDenied(false);
    setError(null);
    setResult(null);
    setResultContentHash(null);
    setRenderId(null);
    setDisclosures(null);
    setRefusals([]);
    setHead(null);
    setMountedSpec(null);
    setOptions(null);
    setResolvedPin(null);

    (async () => {
      try {
        const loadedRegistry = await fetchVisualizationFamilies(scope.projectId, {
          signal: controller.signal,
        });
        if (!live) return;
        setRegistry(loadedRegistry);

        let loadedHead: VisualizationHead | null = null;
        let pinned = querySpecVersionId;
        if (visualizationId) {
          loadedHead = await fetchVisualization(scope.projectId, visualizationId, {
            signal: controller.signal,
          });
          if (!live) return;
          setHead(loadedHead);
          pinned = pinned ?? loadedHead.versions[0]?.query_spec_version_id ?? null;

          const versionId = visualizationSpecVersionId ?? loadedHead.current_version_id;
          if (versionId) {
            const stored = await fetchVisualizationSpecVersion(scope.projectId, versionId, {
              signal: controller.signal,
            });
            if (!live) return;
            const spec = stored.spec as Record<string, unknown>;
            setFamily(stored.family);
            setBindings((spec.bindings as Bindings) ?? emptyBindings());
            setDraft(spec);
            setMountedSpec(runtimeSpec(stored));
            pinned = stored.query_spec_version_id;

            const report = await validateVisualizationSpecVersion(
              scope.projectId,
              versionId,
              resultId,
            );
            if (!live) return;
            setRefusals(report.shape.refusals ?? []);
            setDisclosures(report.result_disclosures);
          }
        }

        // Neither the address nor an existing version supplied a pin, and a
        // Result IS pinned: ask the Result which Query Spec version produced it.
        // This is a read of Story 50.1's own Result envelope, not a second
        // authority — the Builder still never chooses a Query Spec.
        if (!pinned && resultId) {
          const resultPin = await fetchResultPin(scope.projectId, resultId, {
            signal: controller.signal,
          });
          if (!live) return;
          pinned = resultPin.query_spec_version_id || null;
          setResolvedPin(pinned);
        }

        if (pinned) {
          const loadedOptions = await fetchVisualizationOptions(scope.projectId, pinned, {
            signal: controller.signal,
          });
          if (!live) return;
          setOptions(loadedOptions);
        }

        if (resultId) {
          const evidence = await fetchResultEvidence(scope.projectId, resultId, {
            signal: controller.signal,
          });
          if (!live) return;
          const manifest = evidence.manifest ?? {};
          setResult({
            outcome: String(manifest["outcome"] ?? "unavailable"),
            rows: evidence.rows ?? [],
            schema: evidence.schema ?? {},
            manifest,
            rowCount: (evidence.rows ?? []).length,
            truncated: Boolean(manifest["truncated"]),
          });
          setResultContentHash(evidence.content_hash);
        }
      } catch (exc) {
        if (!live) return;
        if (exc instanceof ApiError && (exc.status === 404 || exc.unauthenticated)) {
          setDenied(true);
        } else {
          setError(exc instanceof Error ? exc.message : "The Builder could not load.");
        }
      } finally {
        if (live) setLoading(false);
      }
    })();

    return () => {
      live = false;
      controller.abort();
    };
  }, [scope.projectId, visualizationId, visualizationSpecVersionId, resultId, querySpecVersionId]);

  const currentFamily = useMemo(
    () => registry?.families.find((f) => f.id === family) ?? registry?.families[0] ?? null,
    [registry, family],
  );

  const save = useCallback(async () => {
    if (!registry || !currentFamily || !pin) return;
    setSaving(true);
    setRefusals([]);
    setAnnouncement("Validating the presentation on the server.");
    const document = documentOf(
      registry.spec_contract_version,
      registry.schema_version,
      currentFamily.id,
      bindings,
      draft,
    );
    try {
      const saved = visualizationId
        ? await addVisualizationVersion(scope.projectId, visualizationId, { spec: document })
        : await createVisualization(scope.projectId, {
            query_spec_version_id: pin,
            spec: document,
          });
      setAnnouncement(`Saved version ${saved.version_number}.`);
      setMountedSpec(runtimeSpec(saved));
      const reloaded = await fetchVisualization(scope.projectId, saved.visualization_id);
      setHead(reloaded);
    } catch (exc) {
      const body = exc instanceof ApiError ? exc.body : null;
      const list = refusalsOf(body);
      if (list) {
        setRefusals(list);
        setAnnouncement(`The presentation was refused on ${list.length} point(s).`);
      } else {
        setAnnouncement("");
        setError(exc instanceof Error ? exc.message : "The presentation could not be saved.");
      }
    } finally {
      setSaving(false);
    }
  }, [registry, currentFamily, pin, bindings, draft, visualizationId, scope.projectId]);

  const freezeRender = useCallback(async () => {
    if (!mountedSpec || !resultId || !resultContentHash || !pin) return;
    const resolved = resolveRenderer(
      mountedSpec.document.family,
      mountedSpec.schema_version,
      "console",
    );
    if (resolved.kind !== "renderer") {
      setError(resolved.message);
      return;
    }
    setFreezing(true);
    setError(null);
    try {
      const created = await createCanonicalRender(scope.projectId, {
        result_id: resultId,
        result_content_hash: resultContentHash,
        visualization_spec_version_id: mountedSpec.visualization_spec_version_id,
        renderer_adapter: resolved.renderer.renderer_id,
        renderer_build_id: resolved.renderer.build,
        runtime_build_id: RUNTIME_BUILD,
        theme_version: THEME_VERSION,
        formatter_version: FORMATTER_VERSION,
        responsive_profile: "console",
        display_state: display,
        evidence_manifest: {
          result_id: resultId,
          result_content_hash: resultContentHash,
          query_spec_version_id: pin,
          visualization_spec_version_id: mountedSpec.visualization_spec_version_id,
        },
        datum_evidence_keys: {},
        creation_surface: "explore",
        origin_kind: "explore",
      });
      setRenderId(created.id);
      setAnnouncement(`Frozen Render ${created.id}.`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "The Render could not be frozen.");
    } finally {
      setFreezing(false);
    }
  }, [display, mountedSpec, pin, resultContentHash, resultId, scope.projectId]);

  const state = previewState(result, refusals, loading, denied);
  const grouped = refusalsByWell(refusals);
  const pageRefusals = grouped["__page__"] ?? [];

  // What the composer moves, and where it may be moved to. Both are the
  // SERVER's: the members are exactly what the pinned Query Spec selected, and
  // the wells are the family's declaration widened to the full vocabulary of
  // ten. Time and Classifications are included even when they are empty, so a
  // member that arrives in either of them tomorrow is carried without a client
  // change -- and so a well that has no source still says which role it takes.
  const composableFields = useMemo(
    () => (options
      ? membersAsFields([
          ...options.measures,
          ...options.dimensions,
          ...options.time,
          ...options.classifications,
        ])
      : []),
    [options],
  );
  const composableWells = useMemo(
    () => (registry && currentFamily ? renderableWells(currentFamily, registry.wells) : []),
    [currentFamily, registry],
  );

  if (!BUILDER_TABS.includes(tab as BuilderTab)) {
    // An unregistered tab is Unknown, never a fallback to `build`: silently
    // showing another tab makes a wrong address look like a right one.
    return (
      <main>
        <PageHeader
          title="Unknown tab"
          description={`This Builder has no \`${tab}\` tab. Its tabs are build and versions.`}
        />
      </main>
    );
  }

  if (denied) {
    return (
      <main>
        <PageHeader
          title="Not found"
          description="This Visualization does not exist, or it is not in this Project."
        />
      </main>
    );
  }

  return (
    <main>
      <PageHeader
        eyebrow="Analyze · Explore"
        title="Visualization Builder"
        description="Change how one governed answer looks. Changing what it answers happens in Explore."
        actions={
          <Cluster>
            <Button type="button" onClick={save} disabled={saving || !pin || !currentFamily}>
              {saving ? "Saving..." : "Save a new version"}
            </Button>
          </Cluster>
        }
      />

      <NavTabs
        label="Visualization"
        tabs={BUILDER_TABS.map((key) => ({
          key,
          label: key === "build" ? "Build" : "Versions",
          href: tabHref(key),
        }))}
        current={tab}
        onNavigate={(key) => onNavigateTab(key as BuilderTab)}
      />

      <div aria-live="polite" role="status" className="mt-3 text-caption text-text-secondary">
        {announcement}
      </div>

      {error && (
        <Status as="block" tone="error" title="The Builder could not complete that" className="mt-3">
          {error}
        </Status>
      )}

      {!pin && !loading && (
        <Status as="block" tone="neutral" title="No Query Spec version is pinned" className="mt-3">
          A Visualization presents one governed answer, so it needs a Query Spec version to
          build against. Open Explore, run a query, then return here.
        </Status>
      )}

      {tab === "versions" ? (
        <Panel flush className="mt-5">
          <PanelHeader
            title="Versions"
            description="Every version is immutable. A presentation change appends one; it never rewrites one."
          />
          {head && head.versions.length > 0 ? (
            <TableScroll label="Visualization Spec versions">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Version</TableHead>
                    <TableHead>Family</TableHead>
                    <TableHead>Query Spec version</TableHead>
                    <TableHead>Proposed by</TableHead>
                    <TableHead>Content hash</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {head.versions.map((version) => (
                    <TableRow key={version.id}>
                      <TableCell>{version.version_number}</TableCell>
                      <TableCell>{version.family}</TableCell>
                      <TableCell><ObjectId value={version.query_spec_version_id} title="Query Spec version" /></TableCell>
                      <TableCell>{version.proposed_by}</TableCell>
                      <TableCell>{version.content_hash.slice(0, 12)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          ) : (
            <p className="m-0 p-5 text-ui text-text-secondary">
              No version has been saved yet.
            </p>
          )}
        </Panel>
      ) : (
        // ONE composer over the three parts: a member is picked up in the left
        // rail and placed in a well in the centre, so the catalog and the wells
        // are one surface and not two lists that happen to sit side by side
        // (`visualization-and-rendering.md`, *Composition is direct
        // manipulation*). The keyboard path is inside the same component.
        <FieldComposer
          fields={composableFields}
          wells={composableWells}
          bindings={bindings}
          onChange={(next) => {
            setBindings(next);
            setRefusals([]);
          }}
          className="mt-5 grid grid-cols-1 gap-5 xl:grid-cols-[minmax(240px,1fr)_minmax(0,2fr)_minmax(240px,1fr)]"
        >
          {options ? (
            <FieldCatalogRail options={options} exploreHref={exploreHref} />
          ) : (
            <Panel>
              <p className="m-0 text-ui text-text-secondary">
                {loading ? "Reading the pinned Query Spec version." : "No member list is pinned."}
              </p>
            </Panel>
          )}

          <Stack>
            {options?.analysis_context && (
              <Panel flush data-testid="builder-analysis-context">
                <PanelHeader
                  title="Pinned analysis context"
                  description="Inherited from this exact Query Spec version. Changing it creates a new query, not a presentation edit."
                />
                <div className="grid gap-3 p-5 text-ui sm:grid-cols-3">
                  <div>
                    <p className="m-0 text-caption font-semibold uppercase tracking-wide text-text-secondary">
                      Business Domain
                    </p>
                    <p className="mb-0 mt-1 font-medium text-text-primary">
                      {options.analysis_context.business_domain?.name ?? "Not pinned"}
                    </p>
                    {options.analysis_context.business_domain && (
                      <p className="m-0 text-caption text-text-secondary">
                        {options.analysis_context.business_domain.id} · v
                        {options.analysis_context.business_domain.version_number}
                      </p>
                    )}
                  </div>
                  <div>
                    <p className="m-0 text-caption font-semibold uppercase tracking-wide text-text-secondary">
                      Golden Question
                    </p>
                    <p className="mb-0 mt-1 font-medium text-text-primary">
                      {options.analysis_context.golden_question?.title ?? "Not pinned"}
                    </p>
                    {options.analysis_context.golden_question && (
                      <p className="m-0 text-caption text-text-secondary">
                        v{options.analysis_context.golden_question.version_number}
                      </p>
                    )}
                  </div>
                  <div>
                    <p className="m-0 text-caption font-semibold uppercase tracking-wide text-text-secondary">
                      Requested Skills
                    </p>
                    <p className="mb-0 mt-1 font-medium text-text-primary">
                      {options.analysis_context.requested_skills.length
                        ? options.analysis_context.requested_skills
                            .map((skill) => `${skill.name} · v${skill.version_number}`)
                            .join(", ")
                        : "None requested"}
                    </p>
                  </div>
                </div>
              </Panel>
            )}

            {registry && currentFamily && (
              <Panel flush>
                <PanelHeader
                  title="Visual family"
                  description="Suitability is computed from the server's rules. No ranking overrides it."
                />
                <div className="flex flex-col gap-2 p-5">
                  <Label htmlFor="family-select">Family</Label>
                  <NativeSelect
                    id="family-select"
                    value={currentFamily.id}
                    onChange={(event) => {
                      setFamily(event.target.value);
                      setRefusals([]);
                    }}
                  >
                    {registry.families.map((candidate) => {
                      const verdict = options
                        ? familySuitability(candidate, options)
                        : { compatible: true, reason: "" };
                      return (
                        <option key={candidate.id} value={candidate.id}>
                          {candidate.label}
                          {verdict.compatible ? "" : ` — ${verdict.reason}`}
                        </option>
                      );
                    })}
                  </NativeSelect>
                  {registry.deferred_families.length > 0 && (
                    <p className="m-0 text-caption text-text-secondary">
                      {registry.deferred_families.length} families named by the architecture are
                      not in this release, each for a stated reason. They are absent, not
                      forgotten.
                    </p>
                  )}
                </div>
              </Panel>
            )}

            {pageRefusals.length > 0 && (
              <Status as="block" tone="error" title="This presentation was refused">
                <ul className="m-0 list-none p-0">
                  {pageRefusals.map((refusal, index) => (
                    <li key={index}>
                      {refusal.message} {refusal.remedy}
                    </li>
                  ))}
                </ul>
              </Status>
            )}

            {registry && currentFamily && options && (
              <BindingWells
                family={currentFamily}
                vocabulary={registry.wells}
                refusalsByWellName={grouped}
              />
            )}

            {currentFamily && (
              <BuilderPreview
                state={state}
                family={currentFamily}
                bindings={bindings}
                result={result}
                disclosures={disclosures}
                refusals={refusals}
                rendererMounted={Boolean(mountedSpec && resultId)}
              />
            )}

            {mountedSpec && resultId ? (
              <Panel flush data-testid="builder-runtime-preview">
                <PanelHeader
                  title="Rendered preview"
                  description="The shared Console and MCP runtime draws the saved version over this same Result."
                />
                <div className="p-5">
                  <VisualizationMount
                    projectId={scope.projectId}
                    resultId={resultId}
                    spec={mountedSpec}
                    display={display}
                    onDisplayChange={setDisplay}
                  />
                  <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-border-subtle pt-4">
                    <Button
                      type="button"
                      variant="secondary"
                      disabled={freezing || !resultContentHash}
                      onClick={() => void freezeRender()}
                    >
                      {freezing ? "Freezing Render…" : "Freeze this Render"}
                    </Button>
                    {renderId ? (
                      <Status as="inline" tone="success" title="Render preserved">
                        {renderId}
                      </Status>
                    ) : (
                      <p className="m-0 text-ui text-text-secondary">
                        Preserve this exact Result, saved spec, runtime and local display state.
                      </p>
                    )}
                  </div>
                </div>
              </Panel>
            ) : null}

            <Panel flush>
              <PanelHeader
                title="Inspect the evidence"
                description="Data, Definitions, Quality, Provenance and AI Path belong to the Result workbench."
              />
              <div className="flex flex-col gap-2 p-5">
                {resultId && resultHref ? (
                  <a className="underline" href={resultHref(resultId)}>
                    Open this Result in its workbench
                  </a>
                ) : (
                  <p className="m-0 text-ui text-text-secondary">
                    {resultId
                      ? "The Result workbench is not mounted in this build, so there is no address to open."
                      : "No Result is pinned to this Builder, so there is no evidence to inspect."}
                  </p>
                )}
                <Button
                  type="button"
                  variant="secondary"
                  ref={evidenceInvoker}
                  onClick={() => setEvidenceOpen(true)}
                >
                  Show the saved plan
                </Button>
              </div>
            </Panel>
          </Stack>

          {registry ? (
            <PresentationRail
              draft={draft}
              registry={registry}
              onChange={(next) => {
                setDraft(next);
                setRefusals([]);
              }}
            />
          ) : (
            <Panel>
              <p className="m-0 text-ui text-text-secondary">Reading the presentation registry.</p>
            </Panel>
          )}
        </FieldComposer>
      )}

      <Dialog open={evidenceOpen} onOpenChange={setEvidenceOpen}>
        <DialogContent
          // One contextual drawer, and it gives the keyboard back where it took
          // it from. The drawer is opened from a plain Button rather than a
          // `DialogTrigger`, so the primitive has no trigger to return focus to
          // and would drop it on the document body -- which makes the whole page
          // unreachable without a mouse.
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            evidenceInvoker.current?.focus();
          }}
        >
          <DialogHeader>
            <DialogTitle>The saved plan</DialogTitle>
            <DialogDescription>
              Exactly what would be persisted. No renderer option, no code, no URL.
            </DialogDescription>
          </DialogHeader>
          <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-caption">
            <dt className="text-text-secondary">Family</dt>
            <dd className="m-0 text-text">{currentFamily?.label ?? "none"}</dd>
            <dt className="text-text-secondary">Query Spec version</dt>
            <dd className="m-0 text-text">{pin ?? "none pinned"}</dd>
            {Object.entries(bindings)
              .filter(([, members]) => members.length > 0)
              .map(([well, members]) => (
                <div key={well} className="contents">
                  <dt className="text-text-secondary">{well}</dt>
                  <dd className="m-0 text-text">{members.join(", ")}</dd>
                </div>
              ))}
          </dl>
        </DialogContent>
      </Dialog>
    </main>
  );
}
