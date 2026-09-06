/**
 * Analyze > Renders — the preserved-artifact browser, and the Render Workbench.
 *
 * WHAT THIS REPLACES. `RenderGalleryPage.tsx` lists `app.render_snapshots` and,
 * to show one, opens a widget URI in ANOTHER WINDOW and retries `postMessage`
 * until it answers. That is not a browser of preserved artifacts; it is a remote
 * control for a page it does not own. It also creates and lists raw path-token
 * shares, which puts a bearer in a list response.
 *
 * This screen does neither. Two families, kept visibly apart:
 *
 *   * CANONICAL Renders — `app.renders`, each with the ten replay pins of
 *     `visualization-and-rendering.md:318-322`. The shared runtime mounts
 *     directly over them, IN PAGE, through `analyze/VisualizationMount.tsx`
 *     (Story 50.5). There is no `postMessage` path here.
 *   * LEGACY snapshots — preserved, readable, and labelled unverifiable with the
 *     exact list of pins they lack. They are never offered as exact replay and
 *     never as a Share input.
 *
 * The three tabs are `analyze-and-test.md:98` exactly — Result, Evidence,
 * Sharing. Sharing is `RenderSharing.tsx` (Story 50.7): the AD-30 one-Render
 * fragment exchange, whose link is shown once, never listed, and revocable behind
 * a confirmation. This file still creates no share itself.
 *
 * Its visual vocabulary comes only from `ui/admin/src/ui/index.ts`. The two
 * imports from `@toorow/card-shell/viz` are not vocabulary: they are the shared
 * runtime's own contract (`resolveProfile`) and this screen's adapter onto it.
 * Re-declaring either here is exactly the drift Epic 50 exists to remove.
 */
import { resolveProfile, type DisplayState, type VizSpec } from "@toorow/card-shell/viz";
import { useCallback, useEffect, useState } from "react";

import VisualizationMount from "../analyze/VisualizationMount";
import {
  fetchVisualizationSpecVersion,
  type VisualizationSpecVersion,
} from "../analyze/visualizationClient";
import { ApiError } from "../lib/apiFetch";
import {
  ObjectId,
  Badge,
  Button,
  Cluster,
  ConfirmDialog,
  EmptyState,
  EvidenceRows,
  Failure,
  Loading,
  Metric,
  NavTabs,
  NoScope,
  ObjectHeader,
  ObjectNotFound,
  ProjectNotFound,
  Retry,
  PageHeader,
  Panel,
  PanelHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Timestamp,
} from "../ui";
import {
  fetchRender,
  listDossiers,
  listLegacySnapshotShares,
  listLegacySnapshots,
  listRenders,
  revokeLegacySnapshotShare,
  type DossierSummary,
  type LegacySnapshot,
  type LegacySnapshotShare,
  type RenderCollection,
  type RenderDetail,
} from "./client";
import { RenderSharing } from "./RenderSharing";
import { ContractUnavailable, humanize } from "./Shared";

/** `analyze-and-test.md:98`, in that order. Declared, never inferred. */
export const RENDER_TABS = [
  { key: "result", label: "Result" },
  { key: "evidence", label: "Evidence" },
  { key: "sharing", label: "Sharing" },
] as const;

export const RENDER_DEFAULT_TAB = "result";

type Phase<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "no-scope" }
  | { status: "not-found" }
  | { status: "error"; message: string };

interface Both {
  canonical: RenderCollection;
  legacy: LegacySnapshot[];
}

export function RendersCollection({
  projectId,
  onOpenRender,
  onOpenResult,
  onOpenDossier,
}: {
  projectId?: string;
  onOpenRender?: (renderId: string) => void;
  onOpenResult?: (resultId: string) => void;
  /** Opens a Dossier -- a composition of Renders -- in its workbench (74-2). */
  onOpenDossier?: (dossierId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<Both>>({ status: "loading" });
  /** Bumped by the failure block's `Retry` (76-4): an error is never a dead end. */
  const [reloadToken, setReloadToken] = useState(0);
  /** The legacy snapshot whose share links are open. Never more than one. */
  const [legacyShares, setLegacyShares] = useState<LegacySnapshot | null>(null);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    Promise.all([
      listRenders(projectId, {}, { signal: controller.signal }),
      listLegacySnapshots(projectId, { signal: controller.signal }),
    ])
      .then(([canonical, legacy]) =>
        setPhase({ status: "ready", data: { canonical, legacy: legacy.snapshots } }),
      )
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, reloadToken]);

  if (phase.status === "no-scope") return <NoScope what="preserved renders" />;
  if (phase.status === "loading") return <Loading label="preserved renders" />;
  if (phase.status === "not-found") return <ProjectNotFound />;
  if (phase.status === "error") {
    return <Failure what="The preserved Renders" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const { canonical, legacy } = phase.data;

  return (
    <Stack>
      <PageHeader
        title="Renders"
        description="Preserved presentation snapshots. A Render is frozen evidence over one exact Result; it carries no live query and grants no rerun."
      />

      <ContractUnavailable contract={canonical.contract} what="Creating a Render" />

      <DossiersPanel projectId={projectId} onOpenDossier={onOpenDossier} />

      <Panel>
        <PanelHeader
          title="Canonical Renders"
          description="Each pins its exact Result, Visualization Spec version, renderer and runtime build, theme, formatter, responsive profile, display state and evidence manifest."
        />
        {canonical.renders.length === 0 ? (
          <EmptyState
            title="No canonical Render yet"
            description={
              canonical.contract.available
                ? "Preserve a presentation from a Result, a Report run or a Notebook run."
                : "None can exist until the replay contract above lands. Legacy snapshots below are preserved and readable."
            }
          />
        ) : (
          <TableScroll label="Canonical Renders">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Render</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead>Origin</TableHead>
                  <TableHead>Visualization Spec</TableHead>
                  <TableHead>Runtime build</TableHead>
                  <TableHead>Replay</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {canonical.renders.map((render) => (
                  <TableRow key={render.id}>
                    <TableCell>
                      {onOpenRender ? (
                        <Button variant="ghost" onClick={() => onOpenRender(render.id)}>
                          <ObjectId value={render.id} title="Render" />
                        </Button>
                      ) : (
                        <ObjectId value={render.id} title="Render" />
                      )}
                    </TableCell>
                    <TableCell>
                      {onOpenResult ? (
                        <Button variant="ghost" onClick={() => onOpenResult(render.result_id)}>
                          <ObjectId value={render.result_id} title="Result" />
                        </Button>
                      ) : (
                        <ObjectId value={render.result_id} title="Result" />
                      )}
                    </TableCell>
                    <TableCell>{humanize(render.origin_kind)}</TableCell>
                    <TableCell>
                      <ObjectId value={render.visualization_spec_version_id} title="Visualization version" />
                    </TableCell>
                    <TableCell>
                      <ObjectId value={render.runtime_build_id} title="Runtime build" />
                    </TableCell>
                    <TableCell>
                      <Badge tone={render.replayable ? "success" : "warning"}>
                        {render.replayable ? "Replayable" : "Runtime retired"}
                      </Badge>
                    </TableCell>
                    <TableCell><Timestamp value={render.created_at} /></TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="Legacy snapshots"
          description="Preserved for evidence. These are not Renders: they pin no Result, no Visualization Spec and no runtime, so they cannot be replayed exactly and are never offered as a Share input."
        />
        {legacy.length === 0 ? (
          <EmptyState title="No legacy snapshot in this project" />
        ) : (
          <TableScroll label="Legacy snapshots">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Snapshot</TableHead>
                  <TableHead>Tool</TableHead>
                  <TableHead>Summary</TableHead>
                  <TableHead>Classification</TableHead>
                  <TableHead>Missing pins</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Share links</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {legacy.map((snapshot) => (
                  <TableRow key={snapshot.id}>
                    <TableCell>
                      <ObjectId value={snapshot.id} title="Snapshot" />
                    </TableCell>
                    <TableCell>{snapshot.tool_name}</TableCell>
                    <TableCell>{snapshot.summary_snippet ?? "—"}</TableCell>
                    <TableCell>
                      <Badge tone="warning">Legacy, unverifiable</Badge>
                    </TableCell>
                    <TableCell>{snapshot.missing_pins.length} of 10</TableCell>
                    <TableCell><Timestamp value={snapshot.created_at} /></TableCell>
                    <TableCell>
                      {/* Reading a legacy snapshot's links is a request per
                          snapshot, so it is asked for rather than fetched for
                          every row of a list nobody may be looking at. */}
                      <Button
                        variant="ghost"
                        onClick={() =>
                          setLegacyShares((current) =>
                            current?.id === snapshot.id ? null : snapshot,
                          )
                        }
                      >
                        {legacyShares?.id === snapshot.id ? "Hide links" : "Manage links"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      {legacyShares && projectId ? (
        <LegacyShares
          projectId={projectId}
          snapshot={legacyShares}
          onClose={() => setLegacyShares(null)}
        />
      ) : null}
    </Stack>
  );
}

/**
 * LEGACY share links of ONE retired snapshot — revocation and history.
 *
 * ABSORBED FROM `RenderGalleryPage.tsx` on 2026-08-04, which is deleted in the
 * same commit. That screen was the only place this capability existed, Story 50.7
 * had just repaired it there (it stopped the server returning a plaintext token
 * and a live URL on every listing row), and NOTHING MOUNTED THE SCREEN — so the
 * repair reached nobody. Restyling a screen no route reaches would have moved 419
 * lines of CSS and made no capability reachable; porting it here does both.
 *
 * WHY IT BELONGS ON THIS PANEL AND NOT ON A TAB. The three Render tabs are
 * `analyze-and-test.md:98` exactly, and they are the tabs of a CANONICAL Render.
 * A legacy snapshot is not one and never gets a workbench: giving it tabs would
 * be inventing a surface for an object the target does not carry.
 *
 * WHAT IS DELIBERATELY ABSENT: creation. `POST /api/rendus/snapshots/{id}/share`
 * answers 410 Gone, and the panel above states that a legacy snapshot is never a
 * Share input. Revocation is the opposite act — these links do not expire, so
 * switching one off is the only way to end a grant nobody remembers giving.
 */
function LegacyShares({
  projectId,
  snapshot,
  onClose,
}: {
  projectId: string;
  snapshot: LegacySnapshot;
  onClose: () => void;
}) {
  const [phase, setPhase] = useState<Phase<LegacySnapshotShare[]>>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<LegacySnapshotShare | null>(null);

  const load = useCallback(
    (signal?: AbortSignal) => {
      setPhase({ status: "loading" });
      listLegacySnapshotShares(projectId, snapshot.id, { signal })
        .then((collection) => setPhase({ status: "ready", data: collection.shares }))
        .catch((error: unknown) => {
          if (signal?.aborted) return;
          if (error instanceof ApiError && error.status === 404) {
            setPhase({ status: "not-found" });
            return;
          }
          setPhase({ status: "error", message: (error as Error).message });
        });
    },
    [projectId, snapshot.id],
  );

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const revoke = async (share: LegacySnapshotShare) => {
    setBusy(true);
    setActionError(null);
    try {
      await revokeLegacySnapshotShare(projectId, share.id);
      setPendingRevoke(null);
      load();
    } catch (error: unknown) {
      // The link may well be off already while this screen failed to re-read the
      // list. Saying "could not revoke" would be a guess about the wrong half.
      setActionError(
        (error as Error).message || "The link could not be switched off.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel>
      <PanelHeader
        title={`Legacy links · ${snapshot.id}`}
        description="Created by the retired snapshot sharing, which set no expiry — every one of these is live until it is revoked. No link and no token is shown: the server no longer selects either, so a listing cannot leak a grant into a screenshot. New shares are made from a Render's Sharing tab instead: that link expires, it is revocable, and it is shown once."
      />
      <Stack>
        <Cluster>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </Cluster>
        {actionError ? (
          <Status as="block" tone="error" title="The revocation did not complete">
            {actionError}
          </Status>
        ) : null}
        {phase.status === "loading" ? <Loading label="legacy links" /> : null}
        {phase.status === "not-found" ? (
          <EmptyState
            title="This snapshot is not readable in this project"
            description="No link is listed, and none is assumed to exist."
          />
        ) : null}
        {phase.status === "error" ? (
          <Failure what="The legacy links" message={phase.message} action={<Retry onClick={() => load()} />} />
        ) : null}
        {phase.status === "ready" && phase.data.length === 0 ? (
          <EmptyState
            title="This snapshot was never shared"
            description="Nothing to revoke. New shares are made over one immutable Render, from its Sharing tab."
          />
        ) : null}
        {phase.status === "ready" && phase.data.length > 0 ? (
          <TableScroll label="Legacy share links">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Link</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Shared</TableHead>
                  <TableHead>Shared by</TableHead>
                  {/* "Revoked at", not "Revoked": the state badge already says
                      the word, and two columns reading the same is a question
                      about which one answers. */}
                  <TableHead>Revoked at</TableHead>
                  <TableHead>Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {phase.data.map((share) => (
                  <TableRow key={share.id}>
                    <TableCell>
                      <ObjectId value={share.id} title="Share" />
                    </TableCell>
                    <TableCell>
                      <Badge tone={share.revoked_at ? "error" : "warning"}>
                        {share.revoked_at ? "Revoked" : "Live, never expires"}
                      </Badge>
                    </TableCell>
                    <TableCell><Timestamp value={share.shared_at} /></TableCell>
                    <TableCell>{share.shared_by ?? "Unknown"}</TableCell>
                    <TableCell>
                      <Timestamp value={share.revoked_at} absentMeaning="Not revoked" />
                    </TableCell>
                    <TableCell>
                      {share.revoked_at ? (
                        <span>—</span>
                      ) : (
                        <Button
                          variant="ghost"
                          disabled={busy}
                          onClick={() => setPendingRevoke(share)}
                        >
                          Revoke
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        ) : null}
      </Stack>

      {/* Irreversible FOR THE RECIPIENT, so it is confirmed before it is sent —
          the Epic 46 class. Both revoke confirmations in this feature are now the
          same primitive; they had been two hand-rolled dialogs differing only in
          their sentences, which is how two confirmations drift apart. */}
      <ConfirmDialog
        open={pendingRevoke !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRevoke(null);
        }}
        title="Revoke this legacy link?"
        description="The recipient's link stops opening immediately. This cannot be undone, and the retired sharing cannot issue another: if this snapshot still needs to be shared, it has to be preserved as a Render first."
        confirmLabel="Revoke it"
        cancelLabel="Keep the link"
        destructive
        busy={busy}
        onConfirm={() => pendingRevoke && void revoke(pendingRevoke)}
      />
    </Panel>
  );
}

/**
 * The preserved visual itself, drawn by the shared runtime (Story 50.5).
 *
 * IT IS A CHILD RATHER THAN A BLOCK INSIDE THE WORKBENCH for one reason: it owns
 * a fetch, and the workbench returns early on five phases before its Result tab
 * is reached. A hook after an early return is not allowed, and hoisting the fetch
 * into the workbench would run it for the Evidence and Sharing tabs too.
 *
 * THREE STATES REFUSE TO DRAW, and each says which pin is missing rather than
 * showing an empty canvas:
 *
 *   * runtime material RETIRED (`retention_actions`) — the builds this Render
 *     pins no longer exist, so anything drawn now would be this build's picture
 *     presented as the preserved one;
 *   * Result payload NOT RETAINED — the rows are gone under retention, and the
 *     evidence route cannot return them. The pins stay readable on the Evidence
 *     tab; the visual cannot come back;
 *   * the Spec version unreadable — a Render pins its Visualization Spec version
 *     by id, and this screen resolves that id rather than accepting a spec from
 *     anywhere else. No fallback spec is invented.
 */
export function RenderVisual({ projectId, render, collectionHref }: { projectId: string; render: RenderDetail; collectionHref: string }) {
  const retired = render.retention_actions.length > 0;
  const retained = render.result_payload_retained;
  const specVersionId = render.visualization_spec_version_id;
  const [phase, setPhase] = useState<Phase<VisualizationSpecVersion>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (retired || !retained) return;
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchVisualizationSpecVersion(projectId, specVersionId, { signal: controller.signal })
      .then((data) => setPhase({ status: "ready", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, specVersionId, retired, retained, reloadToken]);

  if (retired) {
    return (
      <EmptyState
        title="This Render cannot be replayed"
        description="Its runtime material has been retired, so drawing it now would show this build's picture rather than the one that was preserved. The pins and the evidence manifest stay readable on the Evidence tab."
      />
    );
  }
  if (!retained) {
    return (
      <EmptyState
        title="The Result payload was not retained"
        description="This Render pins an exact Result whose rows are no longer stored, so there is nothing to draw. Its ten pins remain on the Evidence tab; the visual does not come back."
      />
    );
  }
  if (phase.status === "loading") return <Loading label="the preserved visual" />;
  if (phase.status === "no-scope") return <NoScope what="this Render" />;
  if (phase.status === "not-found") {
    return (
      <ObjectNotFound
        what="Visualization Spec version"
        collection="Renders"
        collectionHref={collectionHref}
        detail={`This Render pins ${specVersionId}, which this Project cannot read. No other spec is drawn in its place.`}
      />
    );
  }
  if (phase.status === "error") {
    return <Failure what="The preserved visual" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const version = phase.data;
  // `responsive_profile` is a stored string; the runtime's enum is the authority
  // on what it may be. A value the enum does not carry is SAID, never quietly
  // swapped — that is `responsive.ts`'s own rule, applied here.
  const resolution = resolveProfile(render.responsive_profile);

  return (
    <Stack>
      {resolution.substitution ? (
        <Status as="block" tone="warning" title="This Render pins a layout this build does not have">
          {resolution.substitution}
        </Status>
      ) : null}
      <VisualizationMount
        projectId={projectId}
        renderId={render.id}
        resultId={render.result_id}
        expectedResultContentHash={render.result_content_hash}
        spec={{
          visualization_spec_version_id: version.id,
          spec_contract_version: "visualization-spec.v1",
          schema_version: version.schema_version,
          // The server stores the document as JSONB and types it `Record<string,
          // unknown>`; the runtime's validator is what proves its shape, and it
          // REFUSES by name rather than throwing. A cast through `unknown` is the
          // honest admission that this boundary is validated downstream, not here.
          document: version.spec as unknown as VizSpec["document"],
        }}
        pins={{
          theme_version: render.theme_version,
          formatter_version: render.formatter_version,
          renderer_build: render.renderer_build_id,
          runtime_build: render.runtime_build_id,
        }}
        display={render.display_state as DisplayState}
        profile={resolution.profile}
      />
    </Stack>
  );
}

export function RenderWorkbench({
  projectId,
  renderId,
  tab,
  onNavigateTab,
  tabHref,
  collectionHref,
  onOpenResult,
}: {
  projectId?: string;
  renderId: string;
  tab: string;
  onNavigateTab?: (tab: string) => void;
  /** The canonical address of each tab, supplied by the shell router.
   *  A workbench tab is route navigation, not local state: without an
   *  address the tab renders disabled rather than pretending to be a link. */
  tabHref?: (tab: string) => string;
  /** The way back when this address names no Render (76-4). REQUIRED for the
   *  reason `ObjectNotFound` gives: this state draws no rail and no tabs. */
  collectionHref: string;
  onOpenResult?: (resultId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<RenderDetail>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchRender(projectId, renderId, { signal: controller.signal })
      .then((data) => setPhase({ status: "ready", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, renderId, reloadToken]);

  if (phase.status === "no-scope") return <NoScope what="this Render" />;
  if (phase.status === "loading") return <Loading label="this Render" />;
  if (phase.status === "not-found") {
    return <ObjectNotFound what="Render" collection="Renders" collectionHref={collectionHref} />;
  }
  if (phase.status === "error") {
    return <Failure what="This Render" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const render = phase.data;
  const known = RENDER_TABS.some((entry) => entry.key === tab);
  const retired = render.retention_actions.length > 0;

  return (
    <Stack>
      <ObjectHeader name={render.id} source={`Render · from ${humanize(render.origin_kind)}`} />
      <NavTabs label="Render" tabs={RENDER_TABS.map((entry) => ({ ...entry, href: tabHref?.(entry.key) }))} current={tab} onNavigate={onNavigateTab} />

      {!known ? (
        <EmptyState
          title="Unknown tab"
          description="This Render does not have that tab. No other tab is opened in its place."
        />
      ) : null}

      {retired ? (
        <Status as="block" tone="warning" title="Runtime material for this Render has been retired">
          The frozen evidence is intact. Exact replay is not available, and that is
          recorded as an audited retention action rather than by changing this Render.
        </Status>
      ) : null}

      {known && tab === "result" ? (
        <Stack>
          <Panel>
            <PanelHeader
              title="Preserved visual"
              description="Drawn in page by the shared runtime, from the exact rows this Render pins. There is no rerun here and no second window: the same runtime draws this, the MCP App and a shared link, so the three cannot disagree."
            />
            {projectId ? (
              <RenderVisual projectId={projectId} render={render} collectionHref={collectionHref} />
            ) : (
              <NoScope what="this Render" />
            )}
          </Panel>
          <Panel>
            <PanelHeader
              title="Result"
              description="The exact immutable Result this Render was drawn from. A Render carries no live query instruction."
            />
            <Cluster>
              <Metric
                label="Result"
                value={
                  onOpenResult ? (
                    <Button variant="ghost" onClick={() => onOpenResult(render.result_id)}>
                      <ObjectId value={render.result_id} title="Result" />
                    </Button>
                  ) : (
                    <ObjectId value={render.result_id} title="Result" />
                  )
                }
              />
              <Metric
                label="Retained payload"
                value={render.result_payload_retained ? "Retained" : "Not retained"}
              />
              <Metric
                label="Result content hash"
                value={<code>{render.result_content_hash.slice(0, 12)}…</code>}
              />
            </Cluster>
          </Panel>
        </Stack>
      ) : null}

      {known && tab === "evidence" ? (
        <Stack>
          <Panel>
            <PanelHeader
              title="Replay pins"
              description="The ten inputs an exact replay needs. Every one is an exact identity; none may be a placeholder."
            />
            <EvidenceRows
              label="Replay pins"
              source={{
                result_id: render.result_id,
                result_content_hash: render.result_content_hash,
                visualization_spec_version_id: render.visualization_spec_version_id,
                renderer_adapter: render.renderer_adapter,
                renderer_build_id: render.renderer_build_id,
                runtime_build_id: render.runtime_build_id,
                theme_version: render.theme_version,
                formatter_version: render.formatter_version,
                responsive_profile: render.responsive_profile,
                content_hash: render.content_hash,
                creation_surface: render.creation_surface,
                created_by: render.created_by,
                created_at: render.created_at,
              }}
            />
          </Panel>
          <Panel>
            <PanelHeader
              title="Evidence manifest"
              description="Every rendered datum resolves through this mapping — tooltips, drill-through, export and feedback alike."
            />
            <EvidenceRows label="Evidence manifest" source={render.evidence_manifest} />
          </Panel>
        </Stack>
      ) : null}

      {known && tab === "sharing" ? (
        <RenderSharing
          projectId={projectId}
          renderId={render.id}
          canonicalShareAvailable={render.sharing.canonical_share_available}
          contractReason={render.sharing.reason}
        />
      ) : null}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Dossiers -- the compositions OF these Renders (epic 73/74). The list lives
// here, beside what it composes; the reading page is `Dossiers.tsx`, which
// imports `RenderVisual` from this module and never the other way round.
// ---------------------------------------------------------------------------

export function DossiersPanel({
  projectId,
  onOpenDossier,
}: {
  projectId?: string;
  onOpenDossier?: (dossierId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<DossierSummary[]>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    listDossiers(projectId, { signal: controller.signal })
      .then((body) => setPhase({ status: "ready", data: body.dossiers }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, reloadToken]);

  return (
    <Panel>
      <PanelHeader
        title="Dossiers"
        description="Several frozen Renders and a narrative, kept as one document with versions. A Dossier pins the exact Renders it shows; it reruns nothing."
      />
      {phase.status === "no-scope" ? <NoScope what="dossiers" /> : null}
      {phase.status === "loading" ? <Loading label="dossiers" /> : null}
      {phase.status === "not-found" ? <ProjectNotFound /> : null}
      {phase.status === "error" ? (
        <Failure what="The Dossiers" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />
      ) : null}
      {phase.status === "ready" && phase.data.length === 0 ? (
        <EmptyState
          title="No Dossier yet"
          description="Compose one from your MCP host with the compose_dossier tool: it keeps the figures you asked for and the commentary you wrote as one document. Composing a Dossier from the Renders below is not delivered yet."
        />
      ) : null}
      {phase.status === "ready" && phase.data.length > 0 ? (
        <TableScroll label="Dossiers">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dossier</TableHead>
                <TableHead>Version</TableHead>
                <TableHead>Figures</TableHead>
                <TableHead>Narrative</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {phase.data.map((dossier) => (
                <TableRow key={dossier.id}>
                  <TableCell>
                    {onOpenDossier ? (
                      <Button variant="ghost" onClick={() => onOpenDossier(dossier.id)}>
                        {dossier.label}
                      </Button>
                    ) : (
                      dossier.label
                    )}
                  </TableCell>
                  <TableCell>{dossier.current_version_number ?? "—"}</TableCell>
                  <TableCell>{dossier.render_count}</TableCell>
                  <TableCell>
                    {dossier.narrative_count === 0
                      ? "None"
                      : `${dossier.narrative_count} block${dossier.narrative_count === 1 ? "" : "s"}`}
                  </TableCell>
                  <TableCell>
                    <Timestamp value={dossier.updated_at} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      ) : null}
    </Panel>
  );
}

export default RendersCollection;
