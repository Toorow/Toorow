/**
 * "Used by / Related" — what a Knowledge item or a Skill is tied to, READ ONLY.
 *
 * WHAT CHANGED, AND WHY IT HAD TO (story 49-6, lot 3). This panel used to read
 * EVERY edge of the project (`GET /api/context/graph/edges`) plus the WHOLE
 * graph bundle, and filter both in the browser. Three consequences, all of them
 * named by `context-hub.md`:
 *
 *   * it was the browser fan-out the document forbids — two unbounded reads to
 *     show at most a handful of rows;
 *   * it could only see what `app.context_graph` can hold, and the projection's
 *     CHECK knows five endpoint types. A `semantic_view`, a `metric` or a
 *     `datastream` peer — the three the relationship authority exists for — was
 *     INVISIBLE here, and nothing said so;
 *   * it knew three peer types by name (`topic`, `procedure`, `schema_doc`), so
 *     everything else printed a wire token at a person.
 *
 * It now reads the one authority's own facet,
 * `GET /api/context/relationships?project_id=&node_type=&node_id=`: both
 * directions, every endpoint type, bounded server-side, permission-filtered
 * server-side, and typed `unavailable` when the read fails.
 *
 * A FAILED READ IS NOT "NO RELATIONS". That is the story's *Incomplete if*, and
 * it is why there are three distinct answers below and not two: an unreadable
 * facet says why and names the gesture, an empty facet says why it is empty and
 * names the gesture that fills it, and a refusal is an error with a retry.
 *
 * THE AUTHORITY NAMES AN OBJECT BY ITS TYPE AND ITS ID, never by a copied
 * label — so a peer is shown under its product noun and its identifier, and the
 * console does not read a second surface to decorate it.
 */
import { useCallback, useEffect, useState } from "react";

import { Badge, Button, EmptyState, ObjectId, Separator, Spinner, stateLabel, stateTone, Status } from "../ui";
import { listNodeRelationships, type NodeRelationships, type RelatedItem } from "./contextApi";
import {
  endpointTypeLabel,
  graphWord,
  ownerSurfaceLabel,
  OPENABLE_ENDPOINT_TYPES,
} from "./relationshipVocabulary";
import type { ApiError } from "./types";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "answered"; facet: NodeRelationships };

export default function GraphLinks({
  nodeId,
  nodeType,
  projectId,
  onOpenGraph,
  onOpenPeer,
}: {
  /** The object whose relations are read. */
  nodeId: string;
  /**
   * Its type, in the authority's vocabulary. A facet is asked for ONE node, and
   * an id alone does not name one: the same identifier space is shared by the
   * eight endpoint types the authority knows.
   */
  nodeType: string;
  /** The project scope, as on every other context route. */
  projectId: string | null;
  /**
   * Opens the Knowledge Graph, where links are created and removed. Absent
   * prop => the sentence still names the surface, without a control: a shell
   * that wired no handler must not turn into a button that does nothing.
   */
  onOpenGraph?: () => void;
  /** Opens a peer this console has a workbench for. Same rule as above. */
  onOpenPeer?: (peer: { type: string; id: string }) => void;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
      const facet = await listNodeRelationships({ projectId, nodeType, nodeId });
      setState({ status: "answered", facet });
    } catch (err) {
      const failure = err as ApiError;
      setState({
        status: "error",
        message: failure.message ?? "The relations of this object could not be read.",
      });
    }
  }, [nodeId, nodeType, projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const manage = onOpenGraph ? (
    <div className="mt-3">
      <Button variant="secondary" size="sm" onClick={onOpenGraph} data-testid="graph-links-manage">
        Manage links on the knowledge graph
      </Button>
    </div>
  ) : (
    <p className="m-0 mt-3 text-caption text-text-secondary" data-testid="graph-links-manage-note">
      Links are created and removed on the knowledge graph.
    </p>
  );

  if (state.status === "loading") {
    return (
      <div className="flex justify-center py-2" data-testid="graph-links-loading">
        <Spinner label="Reading the relations" />
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <>
        {/* A refusal — this caller, this scope. Distinct from `unavailable`
            below, which is the store failing to answer for anybody. */}
        <Status
          as="block"
          tone="error"
          data-testid="graph-links-error"
          action={<Button variant="secondary" size="sm" onClick={() => void load()}>Retry</Button>}
        >
          {state.message} This is not an object without relations.
        </Status>
        {manage}
      </>
    );
  }

  const { facet } = state;

  if (facet.state === "unavailable") {
    return (
      <>
        {/* THE TYPED ANSWER, RENDERED. The server answers 200 with a sentence
            precisely so this panel can say "I could not look" instead of
            drawing an empty list that reads as "nothing is related". */}
        <Status
          as="block"
          tone="warning"
          data-testid="graph-links-unavailable"
          title="The related items could not be read"
          action={<Button variant="secondary" size="sm" onClick={() => void load()}>Read again</Button>}
        >
          {facet.message} This is not an object without relations — nothing has been
          removed, and the knowledge graph still holds them.
        </Status>
        {manage}
      </>
    );
  }

  if (facet.outgoing.length === 0 && facet.incoming.length === 0) {
    return (
      <>
        <EmptyState
          title="Not linked to anything yet"
          description="Nothing in the knowledge graph points at this object, and it points at nothing. Draw a link between two nodes on the knowledge graph, or bind this object from the Skill editor, and it is listed here."
        />
        {manage}
      </>
    );
  }

  const row = (item: RelatedItem) => {
    const openable = onOpenPeer && OPENABLE_ENDPOINT_TYPES.has(item.other.type);
    return (
      <div className="grid gap-0.5 py-2">
        <div className="flex flex-wrap items-center gap-1">
          {/* A KIND and a TYPE are labels; a STATUS is a state. All three read
              as one grey chip before 76-2, so a link that was `archived` looked
              exactly like a link that was `derived_from`. */}
          <Badge outline>{graphWord(item.relationship_kind)}</Badge>
          <Badge outline>{endpointTypeLabel(item.other.type)}</Badge>
          {item.status !== "active" && (
            <Badge tone={stateTone(item.status)}>{stateLabel(item.status)}</Badge>
          )}
        </div>
        <ObjectId value={item.other.id} title="Peer" />
        {openable ? (
          <div>
            <Button
              variant="secondary"
              size="sm"
              data-testid={`graph-link-open-${item.relationship_id}`}
              onClick={() => onOpenPeer?.({ type: item.other.type, id: item.other.id })}
            >
              Open this {endpointTypeLabel(item.other.type).toLowerCase()}
            </Button>
          </div>
        ) : (
          /* No workbench here answers for this peer, so the row NAMES where it
             is held rather than offering a control that opens nothing. */
          <span className="text-caption text-text-secondary">
            Held on {ownerSurfaceLabel(item.owner?.surface ?? "")}.
          </span>
        )}
      </div>
    );
  };

  const group = (
    key: string,
    heading: string,
    items: RelatedItem[],
  ) =>
    items.length === 0 ? null : (
      <section data-testid={`graph-links-${key}`}>
        {/* The direction is a SENTENCE, never an arrow alone: "→" tells a
            screen reader nothing about which way a relation runs. */}
        <h3 className="m-0 mt-2 text-label text-text-secondary">{heading}</h3>
        <ul className="m-0 list-none p-0">
          {items.map((item, index) => (
            <li key={item.relationship_id} data-testid={`graph-link-row-${item.relationship_id}`}>
              {index > 0 && <Separator />}
              {row(item)}
            </li>
          ))}
        </ul>
      </section>
    );

  return (
    <>
      {group("outgoing", `This object points at (${facet.outgoing.length})`, facet.outgoing)}
      {group("incoming", `Used by (${facet.incoming.length})`, facet.incoming)}
      {/* BOUNDED, AND SAID SO. The server cuts the facet and reports the cut;
          a reader is never left to believe a truncated list is complete. */}
      {facet.truncated && (
        <p className="m-0 mt-2 text-caption text-text-secondary" data-testid="graph-links-truncated">
          The first {facet.limit} relations are shown; this object has more. The knowledge
          graph shows them all around the node.
        </p>
      )}
      {manage}
    </>
  );
}
