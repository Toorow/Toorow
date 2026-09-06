/**
 * « This metric is reported by these dimensions — declare the grain? »
 *
 * THE MEASURE-AXIS TWIN OF `IdentityCandidatesPanel` (story 71.3). That panel
 * pins a SHARED IDENTITY so two feeds can be crossed; this one derives the GRAIN
 * a metric is reported at — the metric as the head, the dimensions bound beside
 * it as its members — and offers to declare it as a governed MDM version.
 *
 * IT IS DERIVED, NEVER WRITTEN, AND IT RIDES `mdm_target`. The server reads the
 * candidate from the columns already bound to canonical fields
 * (`evidence.measurement_grain_candidates`); a column not bound to the MDM cannot
 * enter a grain, because the binding is the wire and there is nothing else to name
 * a canonical field by. No metric bound means no candidate — an honest empty, not
 * a zero.
 *
 * GOVERNANCE OWNS THE GRAIN; THIS IS ONLY WHERE A PERSON STATES IT. Confirming
 * posts to the Datastream's own door, which refuses a head or a member the
 * mapping has not bound to the MDM before the MDM's own refusals run, then
 * declares the grain referencing MDM canonical fields on BOTH sides — *via the
 * MDM*, not a private per-Datastream model.
 */
import { useState } from "react";
import { Badge, Button, Input, Panel, PanelHeader, Status } from "../../../ui";
import { ApiError, apiPost } from "../../../lib/apiFetch";
import { record, records, text } from "../evidence";

export interface GrainMember {
  field_id: string;
  canonical_field_id: string;
  canonical_name: string;
}

export interface GrainCandidate {
  head: GrainMember;
  members: GrainMember[];
}

/** What the server measured about the grains this mapping could declare. */
export function readGrainCandidates(evidence: unknown): {
  candidates: GrainCandidate[];
  boundMetrics: number;
  boundDimensions: number;
  boundUnresolved: number;
} {
  const block = record(record(evidence)?.measurement_grain_candidates) ?? {};
  const member = (value: unknown): GrainMember | null => {
    const entry = record(value);
    const canonical = text(entry?.canonical_field_id, "");
    if (!canonical) return null;
    return {
      field_id: text(entry?.field_id, ""),
      canonical_field_id: canonical,
      canonical_name: text(entry?.canonical_name, canonical),
    };
  };
  const candidates = records(block.candidates).flatMap((entry) => {
    const head = member(entry.head);
    if (!head) return [];
    const members = records(entry.members)
      .map(member)
      .filter((one): one is GrainMember => one !== null);
    return [{ head, members }];
  });
  return {
    candidates,
    boundMetrics: Number(block.bound_metrics ?? 0),
    boundDimensions: Number(block.bound_dimensions ?? 0),
    boundUnresolved: Number(block.bound_unresolved ?? 0),
  };
}

function CandidateRow({
  candidate,
  editable,
  projectId,
  datastreamId,
  onConfirmed,
}: {
  candidate: GrainCandidate;
  editable: boolean;
  projectId: string;
  datastreamId: string;
  onConfirmed: () => void;
}) {
  //  Le nom par defaut se LIT du candidat : « spend by day, campaign » -- ce que
  //  la personne voit deja sur la ligne, jamais un identifiant.
  const suggested =
    candidate.members.length > 0
      ? `${candidate.head.canonical_name} by ${candidate.members
          .map((one) => one.canonical_name)
          .join(", ")}`
      : `${candidate.head.canonical_name} (total)`;
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState(suggested);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const confirm = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await apiPost(
        `/api/projects/${projectId}/datastreams/${datastreamId}/workbench/mapping/measurement-grains`,
        {
          name: name.trim(),
          head: candidate.head.canonical_field_id,
          members: candidate.members.map((one) => one.canonical_field_id),
        },
      );
      setDone(true);
      setNaming(false);
      onConfirmed();
    } catch (err) {
      //  Le refus NOMME la colonne a lier, pas la cause technique : le serveur le
      //  dit deja (`grain_head_not_bound_to_mdm`), le panneau ne fait que le
      //  montrer.
      setError(err instanceof ApiError ? err.message : "The grain could not be declared.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <li className="flex flex-col gap-2 border-b border-divider-base px-4 py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2 text-body">
        <span className="font-mono">{candidate.head.canonical_name}</span>
        <Badge outline>metric</Badge>
        <span aria-hidden>reported by</span>
        {candidate.members.length > 0 ? (
          candidate.members.map((one) => (
            <span key={one.canonical_field_id} className="flex items-center gap-1">
              <span className="font-mono">{one.canonical_name}</span>
              <Badge tone="info">dimension</Badge>
            </span>
          ))
        ) : (
          <span className="text-caption text-text-secondary">
            no dimension — a total
          </span>
        )}
      </div>
      {done ? (
        <Status tone="success">Grain declared</Status>
      ) : editable && !naming ? (
        <div>
          <Button
            variant="secondary"
            size="sm"
            data-testid={`declare-grain-${candidate.head.canonical_field_id}`}
            onClick={() => setNaming(true)}
          >
            Confirm this grain…
          </Button>
        </div>
      ) : editable && naming ? (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            aria-label="Name for this measurement grain"
            data-testid={`grain-name-${candidate.head.canonical_field_id}`}
            value={name}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) => setName(event.target.value)}
          />
          <Button
            size="sm"
            data-testid={`confirm-grain-${candidate.head.canonical_field_id}`}
            disabled={submitting || name.trim() === ""}
            onClick={confirm}
          >
            Declare the grain
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setNaming(false)}>
            Cancel
          </Button>
        </div>
      ) : null}
      {error && (
        <Status as="block" tone="error" title="This grain was not declared" data-testid="grain-error">
          {error}
        </Status>
      )}
    </li>
  );
}

export default function MeasurementGrainCandidatesPanel({
  evidence,
  editable,
  projectId,
  datastreamId,
  onConfirmed,
}: {
  evidence: unknown;
  editable: boolean;
  projectId: string;
  datastreamId: string;
  onConfirmed: () => void;
}) {
  const { candidates, boundMetrics, boundDimensions, boundUnresolved } =
    readGrainCandidates(evidence);

  //  UN BINDING QUI POINTE UN CHAMP CANONIQUE QUI NE RESOUT PLUS (archive,
  //  hors-projet, illisible) ne peut backer aucun grain -- et le taire serait
  //  laisser tomber le binding en silence. Il est nomme, avec le geste : reparer
  //  le binding. Rendu dans les deux branches, parce qu'un mapping peut porter
  //  des candidats valides ET des bindings a reparer.
  const unresolvedNotice =
    boundUnresolved > 0 ? (
      <Status
        as="block"
        tone="warning"
        title="A binding points to a canonical field that no longer resolves"
        data-testid="grain-unresolved"
      >
        {`${boundUnresolved} ${boundUnresolved === 1 ? "column is" : "columns are"} bound to a ` +
          "canonical field that is archived, out of this project, or unreadable, so it backs no " +
          "grain. Repair the binding on its row."}
      </Status>
    ) : null;

  //  AUCUNE METRIQUE BINDEE = AUCUN GRAIN, et le dire est le seul moyen qu'une
  //  personne sache POURQUOI le geste n'est pas la : le grain se lit des colonnes
  //  deja liees au MDM, et il n'y en a pas une de mesure. « Rien a declarer » et
  //  « la mesure n'est pas encore liee » sont deux faits, et c'est le second.
  if (candidates.length === 0) {
    return (
      <Panel>
        {unresolvedNotice}
        {boundMetrics === 0 ? (
          <PanelHeader
            title="No measure is bound to declare a grain for"
            description={
              `${boundDimensions} ${boundDimensions === 1 ? "dimension is" : "dimensions are"} bound to ` +
              "the MDM on this mapping, but no measure is — and a grain is a measure reported by " +
              "dimensions. Bind a metric column to a canonical field on a row below, then its grain " +
              "can be declared here."
            }
          />
        ) : null}
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader
        title={`${candidates.length} ${candidates.length === 1 ? "measure declares" : "measures declare"} a grain`}
        description={
          "Each measure bound on this mapping is reported by the dimensions bound beside it. " +
          "Declaring a grain governs that link through the MDM, so the same measure is sliced and " +
          "aligned the same way across Datastreams. It references canonical fields on both sides — " +
          "nothing is written to the mapping."
        }
      />
      {unresolvedNotice}
      <ul className="flex flex-col">
        {candidates.map((candidate) => (
          <CandidateRow
            key={candidate.head.canonical_field_id}
            candidate={candidate}
            editable={editable}
            projectId={projectId}
            datastreamId={datastreamId}
            onConfirmed={onConfirmed}
          />
        ))}
      </ul>
    </Panel>
  );
}
