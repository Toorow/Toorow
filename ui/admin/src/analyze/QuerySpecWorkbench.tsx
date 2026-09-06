/**
 * The Query Spec workbench: one stable head, one immutable version, one tab.
 *
 * `query` is the only tab this object declares, and it is declared rather than
 * inferred — a single-tab object still needs its default written down, or the
 * day a second tab appears the address silently changes meaning.
 *
 * The version segment is what makes this useful: `/tab/query/version/{id}` opens
 * the exact analytical intent that produced a given Result, not "whatever this
 * Query Spec says now". Editing intent creates a NEW version; nothing on this
 * screen mutates one.
 */
import { useEffect, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import { EmptyState, EvidenceRows, Metric, ObjectHeader, ObjectId, PageHeader, Panel, PanelHeader, Stack, stateLabel, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../ui";
import { ownerTarget, resultTarget, type AnalyzeScope } from "./analyzeTargets";
import { EnvelopeMismatch, fetchQuerySpecVersion, type QuerySpecVersionDetail } from "./workbenchClient";

type Phase =
  | { status: "loading" }
  | { status: "ready"; detail: QuerySpecVersionDetail }
  | { status: "denied" }
  | { status: "unpinned" }
  | { status: "error"; message: string };

export default function QuerySpecWorkbench({
  scope,
  querySpecId,
  querySpecVersionId,
  onOpenTarget,
}: {
  scope: AnalyzeScope;
  querySpecId: string;
  querySpecVersionId: string | null;
  onOpenTarget?: (href: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });

  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    if (!querySpecVersionId) {
      // A Query Spec address without its version pins no analytical intent. The
      // current version is NOT opened instead: that would make a shared link
      // show a question nobody asked.
      setPhase({ status: "unpinned" });
      return;
    }
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    fetchQuerySpecVersion(scope.projectId, querySpecVersionId, { signal: controller.signal })
      .then((detail) => {
        if (live) setPhase({ status: "ready", detail });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        if (error instanceof EnvelopeMismatch) {
          setPhase({ status: "error", message: error.message });
          return;
        }
        if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
          setPhase({ status: "denied" });
          return;
        }
        setPhase({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [scope.projectId, querySpecVersionId, reloadToken]);

  const detail = phase.status === "ready" ? phase.detail : null;

  return (
    <Stack className="gap-6" data-owner="analyze/explore/query-spec">
      <PageHeader
        title="Query Spec"
        description="The immutable analytical intent behind a Result. Revising it creates another version."
      />
      <ObjectHeader
        name={querySpecId}
        source={
          querySpecVersionId
            ? `Version ${querySpecVersionId}`
            : "No version is pinned in this address"
        }
      />

      <div role="status" aria-live="polite">
        {phase.status === "loading" ? (
          <span className="text-body text-text-secondary">Reading this Query Spec version…</span>
        ) : null}
      </div>

      {phase.status === "unpinned" ? (
        <Panel>
          <EmptyState
            title="No Query Spec version is pinned"
            description="A Query Spec address carries its exact version. Without one, nothing is opened: the current version would answer a different question from the one this address was shared for."
          />
        </Panel>
      ) : null}
      {phase.status === "denied" ? (
        <Status as="block" tone="warning" title="This Query Spec version is not available to you">
          No other version has been opened in its place.
        </Status>
      ) : null}
      {phase.status === "error" ? (
        <Status as="block" tone="error" title="This Query Spec version could not be read"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >
          {phase.message}
        </Status>
      ) : null}

      {detail ? (
        <>
          <Panel className="grid gap-4 md:grid-cols-4">
            <Metric label="Version" value={`v${detail.version_number}`} />
            <Metric
              label="Is current"
              value={detail.is_current_version ? "Yes" : "No"}
              hint={detail.is_current_version ? undefined : "A later version exists."}
            />
            <Metric label="Created" value={detail.created_at ?? "Unknown"} />
            <Metric label="Results" value={String(detail.results.length)} />
          </Panel>

          <Panel>
            <PanelHeader
              title="Analytical intent"
              description="Exactly what was asked, as the server canonicalized and hashed it."
            />
            <EvidenceRows label="Query Spec" source={detail.spec} />
            <p className="mt-3 text-caption text-text-secondary">
              Content hash <code className="text-technical">{detail.content_hash}</code>
            </p>
          </Panel>

          <Panel>
            <PanelHeader
              title="Semantic View"
              description="The exact published version this intent is pinned to."
            />
            <p className="text-body">
              {(() => {
                const href = ownerTarget(
                  scope.organizationId,
                  scope.projectId,
                  detail.semantic_view_owner_ref,
                );
                const label = detail.semantic_view_version_id;
                return href ? (
                  <a
                    className="text-primary underline underline-offset-2 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                    href={href}
                    onClick={(event) => {
                      if (!onOpenTarget || event.button !== 0 || event.metaKey || event.ctrlKey) return;
                      event.preventDefault();
                      onOpenTarget(href);
                    }}
                  >
                    {label}
                  </a>
                ) : (
                  <span className="text-text-secondary">{label}</span>
                );
              })()}
            </p>
          </Panel>

          <Panel>
            <PanelHeader
              title="Results from this version"
              description="Each execution produced its own immutable Result."
            />
            {detail.results.length === 0 ? (
              <p className="text-body text-text-secondary">
                This version has not been executed. No Result has been fabricated for it.
              </p>
            ) : (
              <TableScroll label="Results from this Query Spec version">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Result</TableHead>
                      <TableHead>Outcome</TableHead>
                      <TableHead>Rows</TableHead>
                      <TableHead>Ended</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {detail.results.map((result) => {
                      const href = resultTarget(scope, result.result_id);
                      return (
                        <TableRow key={result.result_id}>
                          <TableCell>
                            <a
                              className="text-primary underline underline-offset-2 focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                              href={href}
                              onClick={(event) => {
                                if (!onOpenTarget || event.button !== 0 || event.metaKey || event.ctrlKey) return;
                                event.preventDefault();
                                onOpenTarget(href);
                              }}
                            >
                              <ObjectId value={result.result_id} title="Result" />
                            </a>
                          </TableCell>
                          <TableCell>{stateLabel(result.outcome)}</TableCell>
                          <TableCell>
                            {String(result.row_count ?? "Unknown")}
                            {result.truncated ? " (truncated)" : ""}
                          </TableCell>
                          <TableCell>{result.ended_at ?? "Unknown"}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </Panel>
        </>
      ) : null}
    </Stack>
  );
}
