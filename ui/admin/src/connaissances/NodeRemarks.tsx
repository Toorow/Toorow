import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import { Button } from "../ui";

/**
 * Les remarques ouvertes SUR UN NOEUD du Context Hub, lisibles et closables
 * partout ou ce noeud se lit -- AI-158.
 *
 * POURQUOI UN COMPOSANT ET PAS UN SECOND PANNEAU. Le panneau est ne dans le
 * tiroir du mindmap, ou l'on DEPOSE la remarque. Mais on ne travaille pas une
 * Skill dans un mindmap : on l'ouvre sur son etabli, et l'etabli n'en disait
 * rien -- ouvrir une Skill, c'etait ne rien savoir de ce qu'on lui reproche.
 * Recopier le panneau la-bas aurait repare l'exemplaire montre ; la classe,
 * c'est « partout ou un noeud du Hub se lit ». Un troisieme point de lecture
 * (une vue projet-large, si elle vient) monte ce meme composant.
 *
 * ZERO LIGNE DE CSS, et une migration au passage : le panneau d'origine
 * portait le vocabulaire `kg-*`, defini dans `knowledge-graph.css`, que
 * l'etabli du Skill n'importe pas -- le partager aurait force une feuille de
 * graphe sur un ecran qui n'est pas un graphe. Les internes sont donc en
 * primitives Tailwind (la cible ratifiee du 2026-07-29), et seul le cadre
 * exterieur reste au choix de l'appelant via `className`.
 *
 * "ZERO LIGNE DE CSS" was true of `knowledge-graph.css` and FALSE of
 * `shell/application.css`: this component carried `.secondary-button` (x3) and
 * `.number` until 2026-09-01. It was the LAST non-bespoke consumer of the
 * legacy vocabulary, so it forced `Procedures`, `ContextObjectPage` and
 * `KnowledgeBasePage` to import that sheet for a child. The three buttons are
 * now the `Button variant="secondary"` primitive — same geometry, a 40px pill
 * at 13px/700 — and `.number` is the theme token pair `ui/ActivityLog.tsx`
 * already writes. The legacy vocabulary now has one caller left:
 * `KnowledgeGraphPage`, the wave-4 bespoke surface.
 */

/** Une remarque en file sur ce noeud (AI-157 / AI-158). */
export interface ReviewRequestRow {
  id: string;
  node_id: string;
  node_type: "topic" | "procedure";
  node_version: number;
  note: string;
  requested_by: string;
  origin: "human" | "agent";
  status: "open" | "accepted" | "declined";
  created_at: string;
  proposed_change: Record<string, unknown> | null;
}

type QueueState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; rows: ReviewRequestRow[]; canResolve: boolean }
  | { status: "error"; message: string };

async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { message?: string; code?: string };
    return body.message ?? `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export default function NodeRemarks({
  projectId,
  nodeId,
  nodeType,
  nodeVersion,
  className,
  testIdPrefix = "kg-review",
  reloadToken = 0,
  rows,
  canResolve,
  onResolved,
  onAdjust,
  adjustDisabled = false,
}: {
  projectId: string;
  nodeId: string | null;
  nodeType: "topic" | "procedure" | null;
  /** La version courante du noeud. `null` = on ne sait pas, donc on ne marque rien. */
  nodeVersion: number | null;
  className?: string;
  testIdPrefix?: string;
  /**
   * MODE CONTROLE. Quand l'hote a DEJA la file, on ne la redemande pas : un
   * ecran qui liste N Skills ferait N requetes pour ce que la route rend en une
   * seule (`node_id` est optionnel). L'hote lit le projet une fois et distribue.
   */
  rows?: ReviewRequestRow[];
  canResolve?: boolean;
  /** Mode controle : l'hote relit, puisque c'est lui qui detient la file. */
  onResolved?: () => void;
  /**
   * AGIR sur la remarque : ajuster la Skill dont elle parle. Une file qu'on ne
   * peut que fermer est une file qu'on ferme sans rien corriger -- et la
   * remarque est epinglee a une VERSION, donc la reponse juste est une nouvelle
   * version, pas une note effacee. L'hote fournit l'editeur ; le composant ne
   * connait que le geste.
   */
  onAdjust?: (remark: ReviewRequestRow) => void;
  adjustDisabled?: boolean;
  /**
   * A incrementer par l'appelant quand il vient LUI-MEME de remplir la file --
   * le mindmap depose une remarque depuis sa propre modale. Sans cela l'ecran
   * affirmerait « aucune remarque ouverte » juste apres en avoir depose une.
   */
  reloadToken?: number;
}) {
  const [queue, setQueue] = useState<QueueState>({ status: "idle" });
  const [reload, setReload] = useState(0);
  const [resolving, setResolving] = useState<string | null>(null);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const controlled = rows !== undefined;

  useEffect(() => {
    // En mode controle l'hote detient la file : la relire ici la ferait lire
    // deux fois, et les deux lectures pourraient ne pas dire la meme chose.
    if (controlled) return;
    if (!nodeId || (nodeType !== "topic" && nodeType !== "procedure")) {
      setQueue({ status: "idle" });
      return;
    }
    let cancelled = false;
    setQueue({ status: "loading" });
    void (async () => {
      try {
        const res = await apiFetch(
          `/api/context/review-requests?project_id=${encodeURIComponent(projectId)}` +
            `&node_id=${encodeURIComponent(nodeId)}`,
        );
        if (cancelled) return;
        if (!res.ok) {
          setQueue({ status: "error", message: await readErrorMessage(res) });
          return;
        }
        const body = (await res.json()) as {
          requests?: ReviewRequestRow[];
          can_resolve?: boolean;
        };
        if (cancelled) return;
        setQueue({
          status: "ok",
          rows: body.requests ?? [],
          // Deposer une remarque est un droit de lecteur ; la trancher est un
          // acte d'ecriture. Le serveur tranche, la vue n'invente pas.
          canResolve: body.can_resolve === true,
        });
      } catch (err) {
        if (cancelled) return;
        setQueue({
          status: "error",
          message: err instanceof Error ? err.message : "Network error",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [controlled, nodeId, nodeType, projectId, reload, reloadToken]);

  // La file effectivement affichee : celle de l'hote, ou la mienne.
  const view: QueueState = controlled
    ? { status: "ok", rows: rows ?? [], canResolve: canResolve === true }
    : queue;

  /** Clore une remarque : `accepted` ou `declined`, jamais un silence. */
  const resolve = useCallback(
    async (requestId: string, status: "accepted" | "declined") => {
      setResolving(requestId);
      setResolveError(null);
      try {
        const res = await apiFetch(
          `/api/context/review-requests/${encodeURIComponent(requestId)}/resolve` +
            `?project_id=${encodeURIComponent(projectId)}`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status }),
          },
        );
        if (!res.ok) {
          const serverMessage = await readErrorMessage(res);
          // ⚠️ UN 404 ICI VEUT DIRE TROIS CHOSES et le serveur refuse de les
          // distinguer, expres : pas a vous, inconnue, ou DEJA CLOSE. Dire
          // « Not permitted » en choisirait une sur trois, et se tromperait
          // souvent -- une remarque close par quelqu'un d'autre est le cas le
          // plus courant des trois.
          setResolveError(
            res.status === 404 || res.status === 403
              ? "This remark could not be closed — it may already be closed, or it is not "
                + "yours to close. Reload to see the current queue."
              : `The remark could not be closed. (${serverMessage})`,
          );
          return;
        }
        // Relire plutot que retirer la ligne a la main : une acceptation peut
        // POSER un lien en base, et une vue qui devine ce que le serveur a
        // fait finit par afficher autre chose que ce qui est ecrit.
        if (onResolved) onResolved();
        else setReload((n) => n + 1);
      } catch (err) {
        setResolveError(err instanceof Error ? err.message : "Network error");
      } finally {
        setResolving(null);
      }
    },
    [onResolved, projectId],
  );

  if (!nodeId || (nodeType !== "topic" && nodeType !== "procedure")) return null;
  // En mode controle, un noeud sans remarque n'affiche RIEN. Sur une liste de
  // N Skills, N fois « No open remark on this node » est du bruit qui repousse
  // les vraies remarques hors de l'ecran. Le panneau autonome, lui, garde son
  // etat vide : il repond a une question qu'on vient de poser.
  if (controlled && (rows ?? []).length === 0 && !resolveError) return null;

  return (
    <section className={className} data-testid={`${testIdPrefix}-queue`}>
      <h3>
        {view.status === "ok" ? `Open remarks (${view.rows.length})` : "Open remarks"}
      </h3>
      {view.status === "loading" && (
        <p className="text-sm opacity-70" role="status">
          Loading the open remarks…
        </p>
      )}
      {view.status === "error" && (
        <div role="alert" data-testid={`${testIdPrefix}-queue-error`}>
          {view.message}
        </div>
      )}
      {view.status === "ok" && view.rows.length === 0 && (
        <p className="text-sm opacity-70" data-testid={`${testIdPrefix}-queue-empty`}>
          No open remark on this node.
        </p>
      )}
      {view.status === "ok" && view.rows.length > 0 && (
        <ul className="flex flex-col gap-3">
          {view.rows.map((row) => (
            <li
              key={row.id}
              className="flex flex-col gap-1"
              data-testid={`${testIdPrefix}-request-${row.id}`}
            >
              <p>{row.note}</p>
              <p className="text-sm opacity-70">
                {row.requested_by}
                {/* Une remarque de machine confondue avec une remarque humaine
                    vaut moins que rien. */}
                {row.origin === "agent" ? (
                  <span data-testid={`${testIdPrefix}-origin-${row.id}`}> agent</span>
                ) : null}
                {" · "}
                {/* La version dont elle parle. « Cette contrainte n'existe
                    plus » ne veut rien dire sans elle — et si le noeud a
                    avance depuis, ca se voit. */}
                <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                  v{row.node_version}
                </span>
                {nodeVersion !== null && row.node_version !== nodeVersion ? (
                  <span
                    data-testid={`${testIdPrefix}-stale-${row.id}`}
                    title={`This node is now v${nodeVersion}`}
                  >
                    {" "}
                    older version
                  </span>
                ) : null}
              </p>
              {/* AGIR, pas seulement classer. Une file qu'on ne peut que
                  fermer est une file qu'on ferme sans rien corriger. La
                  remarque est epinglee a une VERSION : la reponse juste est
                  une nouvelle version de la Skill, et c'est ce que ce bouton
                  ouvre. Il vit AVANT Accept/Decline parce que c'est l'ordre
                  reel du geste -- on corrige, puis on clot. */}
              {onAdjust && view.canResolve ? (
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="secondary"
                    type="button"
                    data-testid={`${testIdPrefix}-adjust-${row.id}`}
                    disabled={adjustDisabled || resolving !== null}
                    onClick={() => onAdjust(row)}
                  >
                    {/* The noun follows the NODE, not the screen. A Knowledge
                        item queued a remark reads "Adjust this Skill" only
                        because the button was born on the Skills collection —
                        one notion, one word, everywhere (CLAUDE.md). */}
                    {row.node_type === "topic" ? "Adjust this Knowledge item" : "Adjust this Skill"}
                  </Button>
                </div>
              ) : null}
              {view.canResolve ? (
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="secondary"
                    type="button"
                    data-testid={`${testIdPrefix}-accept-${row.id}`}
                    disabled={resolving !== null}
                    onClick={() => void resolve(row.id, "accepted")}
                  >
                    {resolving === row.id ? "Closing…" : "Accept"}
                  </Button>
                  <Button
                    variant="secondary"
                    type="button"
                    data-testid={`${testIdPrefix}-decline-${row.id}`}
                    disabled={resolving !== null}
                    onClick={() => void resolve(row.id, "declined")}
                  >
                    Decline
                  </Button>
                </div>
              ) : (
                <p
                  className="text-sm opacity-70"
                  data-testid={`${testIdPrefix}-readonly-${row.id}`}
                >
                  Read-only — closing a remark needs edit rights here.
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {resolveError && (
        <div role="alert" data-testid={`${testIdPrefix}-resolve-error`}>
          {resolveError}
        </div>
      )}
    </section>
  );
}
