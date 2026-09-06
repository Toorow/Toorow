/**
 * The Dossier, read in the console -- story 74-2.
 *
 * A Dossier is several frozen Renders and a narrative, immutable per version
 * (epic 73). Until 2026-09-02 it could be composed over HTTP and read on the
 * public share page, and the console had no page for it: a model that had
 * composed one through `compose_dossier` (74-1) had nowhere to send a person.
 *
 * What this page holds, from the amendment of 2026-09-02
 * (`visualization-and-rendering.md`, "the model composes the Dossier"):
 *
 *   * every figure is drawn by the SAME shared runtime as the Renders page and
 *     the share page (`RenderVisual` -> `VisualizationMount`), so the three
 *     cannot disagree, and the mount carries the existing per-figure feedback;
 *   * a narrative SAYS who wrote it -- a figure is governed (a pinned Result),
 *     a narrative is generated or a person's (D3);
 *   * each figure carries the address of the reasoning path that produced its
 *     Result (D4), resolved by the server off the Result, never stored.
 *
 * It lives under Analyze > Renders, as story 73-2 placed it: a Dossier is a
 * composition OF Renders, not a fifth Level 2 screen.
 */
import { useEffect, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import { Badge, Button, Cluster, EmptyState, Failure, Loading, Metric, NavTabs, NoScope, ObjectHeader, ObjectId, ObjectNotFound, Panel, PanelHeader, Retry, Stack, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Timestamp } from "../ui";
import { fetchDossier, fetchRender, type DossierDetail, type DossierNarrativeAuthor, type DossierResolvedBlock, type RenderDetail } from "./client";
import { RenderVisual } from "./Renders";

export const DOSSIER_TABS = [
  { key: "document", label: "Document" },
  { key: "versions", label: "Versions" },
] as const;

export const DOSSIER_DEFAULT_TAB = "document";

/** The words a reader sees. A figure never carries one: it is governed by construction. */
export const NARRATIVE_AUTHOR_LABEL: Readonly<Record<DossierNarrativeAuthor, string>> = {
  model: "Written by the model",
  human: "Written by a person",
};

type Phase<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "no-scope" }
  | { status: "not-found" }
  | { status: "error"; message: string };

// The LIST lives in `Renders.tsx` (`DossiersPanel`), beside the Renders it
// composes, so this module depends on that one and never the other way round.

// ---------------------------------------------------------------------------
// One figure of the document: the frozen Render, its provenance, its path.
// ---------------------------------------------------------------------------

function FigureBlock({
  projectId,
  index,
  block,
  collectionHref,
  onOpenResult,
  onOpenReasoningPath,
}: {
  projectId: string;
  index: number;
  block: Extract<DossierResolvedBlock, { kind: "render" }>;
  /** The Renders collection — the way out when a pinned figure cannot be read. */
  collectionHref: string;
  onOpenResult?: (resultId: string) => void;
  onOpenReasoningPath?: (resultId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<RenderDetail>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchRender(projectId, block.render_id, { signal: controller.signal })
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
  }, [projectId, block.render_id, reloadToken]);

  const provenance = block.provenance;

  return (
    <Panel data-dossier-figure={block.render_id}>
      <PanelHeader
        title={`Figure ${index + 1}`}
        description="Drawn in page by the shared runtime, from the exact rows this Render pins. The same runtime draws the Renders page, the MCP App and a shared link."
      />
      {phase.status === "loading" ? <Loading label="the frozen figure" /> : null}
      {phase.status === "not-found" ? (
        <EmptyState
          title="This figure's Render cannot be read"
          description={`The Dossier pins ${block.render_id}, which this Project cannot read. No other Render is drawn in its place.`}
        />
      ) : null}
      {phase.status === "error" ? (
        <Failure what="This figure" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />
      ) : null}
      {phase.status === "ready" ? <RenderVisual projectId={projectId} render={phase.data} collectionHref={collectionHref} /> : null}

      {provenance ? (
        <Cluster>
          <Metric
            label="Result"
            value={
              onOpenResult ? (
                <Button variant="ghost" onClick={() => onOpenResult(provenance.result_id)}>
                  <ObjectId value={provenance.result_id} title="Result" />
                </Button>
              ) : (
                <ObjectId value={provenance.result_id} title="Result" />
              )
            }
          />
          <Metric
            label="Visualization Spec version"
            value={<ObjectId value={provenance.visualization_spec_version_id} title="Visualization version" />}
          />
          <Metric label="Runtime build" value={<ObjectId value={provenance.runtime_build_id} title="Runtime build" />} />
          <Metric
            label="Reasoning path"
            value={
              provenance.ai_path ? (
                onOpenReasoningPath ? (
                  <Button
                    variant="ghost"
                    data-dossier-reasoning-path={provenance.ai_path}
                    onClick={() => onOpenReasoningPath(provenance.result_id)}
                  >
                    Open the path that produced this figure
                  </Button>
                ) : (
                  <code data-dossier-reasoning-path={provenance.ai_path}>{provenance.ai_path}</code>
                )
              ) : (
                <span data-dossier-reasoning-path="">
                  None was recorded for this Result
                </span>
              )
            }
          />
        </Cluster>
      ) : (
        <EmptyState
          title="This figure's provenance cannot be resolved"
          description="The Render this block pins is not readable from this Project, so its Result, Spec version and path are not shown. Nothing is substituted."
        />
      )}
    </Panel>
  );
}

function NarrativeBlock({ block }: { block: Extract<DossierResolvedBlock, { kind: "narrative" }> }) {
  const author: DossierNarrativeAuthor = block.authored_by === "model" ? "model" : "human";
  return (
    <Panel data-dossier-narrative-author={author}>
      <Stack>
        <p className="m-0 whitespace-pre-wrap">{block.text}</p>
        <Badge tone={author === "model" ? "info" : "neutral"}>{NARRATIVE_AUTHOR_LABEL[author]}</Badge>
      </Stack>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The workbench.
// ---------------------------------------------------------------------------

export function DossierWorkbench({
  projectId,
  dossierId,
  tab,
  onNavigateTab,
  tabHref,
  collectionHref,
  onOpenResult,
  onOpenReasoningPath,
}: {
  projectId?: string;
  dossierId: string;
  tab: string;
  onNavigateTab?: (tab: string) => void;
  tabHref?: (tab: string) => string;
  /** The way back when this address names no Dossier (76-4). REQUIRED for the
   *  reason `ObjectNotFound` gives: this state draws no rail and no tabs. */
  collectionHref: string;
  onOpenResult?: (resultId: string) => void;
  /** Opens the Result on its AI Path lens: the address of the reasoning path (D4). */
  onOpenReasoningPath?: (resultId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<DossierDetail>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchDossier(projectId, dossierId, { signal: controller.signal })
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
  }, [projectId, dossierId, reloadToken]);

  if (phase.status === "no-scope") return <NoScope what="this Dossier" />;
  if (phase.status === "loading") return <Loading label="the Dossier" />;
  if (phase.status === "not-found") {
    return (
      <ObjectNotFound
        what="Dossier"
        collection="Renders"
        collectionHref={collectionHref}
        detail="This Project holds no Dossier at that address. No other Dossier is opened in its place."
      />
    );
  }
  if (phase.status === "error") {
    return <Failure what="This Dossier" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const dossier = phase.data;
  const known = DOSSIER_TABS.some((entry) => entry.key === tab);
  const current = dossier.versions.find((v) => v.id === dossier.current_version_id) ?? null;

  return (
    <Stack>
      <ObjectHeader
        name={dossier.label}
        source={current ? `Dossier · version ${current.version_number}` : "Dossier"}
      />
      <NavTabs
        label="Dossier"
        tabs={DOSSIER_TABS.map((entry) => ({ ...entry, href: tabHref?.(entry.key) }))}
        current={tab}
        onNavigate={onNavigateTab}
      />

      {!known ? (
        <EmptyState
          title="Unknown tab"
          description="This Dossier does not have that tab. No other tab is opened in its place."
        />
      ) : null}

      {known && tab === "document" ? (
        <Stack>
          {dossier.description ? <p className="m-0">{dossier.description}</p> : null}
          {dossier.current_resolved_blocks.map((block, index) =>
            block.kind === "narrative" ? (
              <NarrativeBlock key={`n-${index}`} block={block} />
            ) : projectId ? (
              <FigureBlock
                key={block.render_id}
                projectId={projectId}
                index={index}
                block={block}
                collectionHref={collectionHref}
                onOpenResult={onOpenResult}
                onOpenReasoningPath={onOpenReasoningPath}
              />
            ) : (
              <NoScope key={block.render_id} what="this figure" />
            ),
          )}
        </Stack>
      ) : null}

      {known && tab === "versions" ? (
        <Panel>
          <PanelHeader
            title="Versions"
            description="A Dossier is never edited in place: a new version succeeds the current one and the earlier ones stay readable."
          />
          <TableScroll label="Dossier versions">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Label</TableHead>
                  <TableHead>Blocks</TableHead>
                  <TableHead>Created by</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {dossier.versions.map((version) => (
                  <TableRow key={version.id}>
                    <TableCell>
                      {version.version_number}
                      {version.id === dossier.current_version_id ? " (current)" : ""}
                    </TableCell>
                    <TableCell>{version.label}</TableCell>
                    <TableCell>{version.blocks.length}</TableCell>
                    <TableCell>{version.created_by}</TableCell>
                    <TableCell>
                      <Timestamp value={version.created_at} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      ) : null}
    </Stack>
  );
}

export default DossierWorkbench;
