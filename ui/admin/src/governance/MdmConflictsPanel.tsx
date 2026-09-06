/**
 * Les conflits MDM, la ou governance.md place la DECISION.
 *
 * LE PARTAGE, ratifie, et il n'est pas arbitraire. `governance.md:75` donne a
 * Mapping Coverage la PROJECTION -- quels Datastreams se lient a chaque champ,
 * avec la confiance, les conflits et les compteurs used-by. `governance.md:528`
 * donne a Controls & Quality « mapping conflicts and approval cases » : la
 * decision, l'arbitrage, le cas. Les deux ecrans lisent la MEME route et la MEME
 * lecture (`parseConflicts`), et le geste qu'ils offrent est le MEME composant.
 * Deux surfaces, un seul ecrivain -- ce qui separe un partage d'une seconde
 * autorite.
 *
 * CE QUE CET ECRAN AJOUTE A L'AUTRE : il ne montre que ce qui appelle une
 * decision, sans la table des champs. Un ecran d'arbitrage qui commence par
 * lister 200 champs sains fait chercher le probleme au lieu de le poser.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import { Button, EmptyState, Panel, PanelHeader, Status } from "../ui";
import MdmConflictResolutionDialog from "./MdmConflictResolutionDialog";
import { parseConflicts, type ConflictEvidence } from "../shell/pages/ProjectMapping";

/** Les codes que cet ecran ferme lui-meme ; les autres nomment leur levier. */
const RESOLVABLE_HERE = new Set(["CURRENCY_CONFLICT", "CURRENCY_GAP", "MEASURE_NULL"]);

type PanelState =
  | { status: "loading" }
  | { status: "ready"; conflicts: ConflictEvidence[] }
  | { status: "denied" }
  | { status: "error" };

export default function MdmConflictsPanel({ projectId }: { projectId: string }) {
  const [state, setState] = useState<PanelState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const [resolving, setResolving] = useState<ConflictEvidence | null>(null);
  const reload = useCallback(() => setReloadKey((value) => value + 1), []);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    const controller = new AbortController();
    setState({ status: "loading" });
    apiFetch(`/api/mdm/conflicts?project_id=${encodeURIComponent(projectId)}`, {
      method: "GET",
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (cancelled) return;
        if (response.status === 401 || response.status === 403 || response.status === 404) {
          setState({ status: "denied" });
          return;
        }
        if (!response.ok) {
          setState({ status: "error" });
          return;
        }
        setState({ status: "ready", conflicts: parseConflicts(await response.json()) });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error" });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [projectId, reloadKey]);

  if (state.status === "loading") {
    return (
      <Status tone="neutral" data-testid="mdm-conflicts-loading">
        Reading the conflicts this Project has to arbitrate.
      </Status>
    );
  }

  if (state.status === "denied") {
    // « Aucun conflit » et « je n'ai pas le droit de lire » ne sont pas la meme
    // phrase, et confondre les deux fait croire a un projet sain.
    return (
      <Status tone="warning">
        Conflicts were not disclosed for this Project. Ask for access to it, then reopen this
        lens — nothing has been hidden or substituted.
      </Status>
    );
  }

  if (state.status === "error") {
    return (
      <Status tone="error">
        The conflict read is unavailable right now.{" "}
        <Button variant="ghost" size="sm" onClick={reload}>
          Retry
        </Button>
      </Status>
    );
  }

  if (state.conflicts.length === 0) {
    return (
      <EmptyState
        title="Nothing to arbitrate"
        description="No Datastream of this Project reports a field in two currencies, and no metric is missing its aggregation. New conflicts appear here as soon as a mapping version is published."
      />
    );
  }

  const blocking = state.conflicts.filter((entry) => entry.severity === "blocking").length;
  return (
    <>
      <Panel>
        <PanelHeader
          title={`${state.conflicts.length} ${state.conflicts.length === 1 ? "conflict" : "conflicts"} to arbitrate`}
          description={
            blocking > 0
              ? `${blocking} of them ${blocking === 1 ? "blocks" : "block"} a number from being computed. The rest explain a difference without stopping anything.`
              : "None of them blocks a number: each explains a difference someone should decide about."
          }
        />
        <ul className="flex flex-col gap-3 px-4 pb-4">
          {state.conflicts.map((conflict, index) => (
            <li
              key={`${conflict.fieldName}-${conflict.code}-${index}`}
              className="flex items-start justify-between gap-4 rounded-lg border border-divider-base p-3"
            >
              <div className="min-w-0">
                <p className="m-0 text-body">
                  <strong>{conflict.fieldLabel}</strong>{" "}
                  <span className="font-mono text-caption text-text-secondary">{conflict.code}</span>
                </p>
                <p className="m-0 text-caption text-text-secondary">{conflict.message}</p>
                {conflict.affectedStreams.length > 0 && (
                  <p className="m-0 text-caption text-text-secondary">
                    {conflict.affectedStreams.length}{" "}
                    {conflict.affectedStreams.length === 1 ? "Datastream" : "Datastreams"} affected
                  </p>
                )}
              </div>
              <Button variant="secondary" size="sm" onClick={() => setResolving(conflict)}>
                {RESOLVABLE_HERE.has(conflict.code) ? "Resolve" : "What resolves this"}
              </Button>
            </li>
          ))}
        </ul>
      </Panel>

      <MdmConflictResolutionDialog
        open={resolving !== null}
        projectId={projectId}
        conflict={resolving}
        onClose={() => setResolving(null)}
        onResolved={reload}
      />
    </>
  );
}
