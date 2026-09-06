/**
 * « Ces champs portent le nom d'une identité connue — les épingler ? »
 *
 * MESURE QUI A OUVERT CE PANNEAU, 2026-08-13 : les neuf Datastreams publiés du
 * projet portaient ZÉRO champ lié au MDM, tous `status: confirmed`. Un mapping
 * peut donc être confirmé sans nommer une seule identité partagée, et aucun
 * croisement n'est possible. Le sélecteur par colonne existait depuis le
 * 2026-08-11 et n'avait jamais servi : un contrôle qu'aucun parcours ne demande
 * n'est pas un contrôle, c'est une possibilité.
 *
 * UNE QUESTION, ET ELLE RÉDUIT LA SUIVANTE. Le serveur a déjà fait la
 * correspondance par le nom (`evidence.identity_candidates`) ; ce panneau la
 * pose une fois, pour tout le lot. Ce qui reste après le geste est exactement ce
 * qui n'a pas d'équivalent — et c'est là que la personne réfléchit, sur trois
 * lignes au lieu de neuf.
 *
 * RIEN N'EST ÉCRIT ICI. Accepter remplit `pendingTargets`, la même intention
 * locale que le sélecteur de la rangée, et sort par `Prepare mapping change`
 * avec les autres décisions de la visite. Le mapping en vigueur n'est jamais
 * édité en place.
 *
 * LA CONSÉQUENCE EST DANS LE PANNEAU, PAS APRÈS. Épingler n'est pas une
 * formalité de saisie : c'est ce qui rend un croisement possible. Une personne
 * qui ne voit pas l'effet ne refait pas le geste sur le flux suivant.
 */
import { Badge, Button, ObjectId, Panel, PanelHeader, Status } from "../../../ui";
import { record, records, text } from "../evidence";
import { scopeLabel } from "../../../governance/contracts";

export interface IdentityCandidate {
  field_id: string;
  canonical_field_id: string;
  canonical_name: string;
  scope: string;
  /** Combien d'AUTRES flux actifs nomment ce champ. Zero se dit aussi. */
  also_named_by: number;
}

/** What the server measured about this mapping's shared identities. */
export function readIdentityCandidates(evidence: unknown): {
  proposed: IdentityCandidate[];
  bound: number;
  unbound: number;
  crossings: { common_key: string; partners: string[] }[];
  namedObjects: string[];
  calendarOnly: boolean;
  /** `known` : le vocabulaire qualifie des objets, donc une absence en est une.
   *  `undeterminable` : aucun champ canonique ne porte d'`object_kind`, donc la
   *  question n'a pas de repondant. Les deux s'ecrivaient `[]` -- le faux zero. */
  namedObjectsState: "known" | "undeterminable";
} {
  const block = record(record(evidence)?.identity_candidates) ?? {};
  const proposed = records(block.proposed).flatMap((entry) => {
    const field_id = text(entry.field_id, "");
    const canonical_field_id = text(entry.canonical_field_id, "");
    if (!field_id || !canonical_field_id) return [];
    return [
      {
        field_id,
        canonical_field_id,
        canonical_name: text(entry.canonical_name, canonical_field_id),
        scope: text(entry.scope, "project"),
        also_named_by: Number(entry.also_named_by ?? 0),
      },
    ];
  });
  const unlocks = record(block.unlocks) ?? {};
  const crossings = records(unlocks.crossings).map((entry) => ({
    common_key: text(entry.common_key, ""),
    partners: records(entry.with).map((partner) => text(partner.name, text(partner.datastream_id, ""))),
  }));
  return {
    proposed,
    namedObjects: records(block.named_objects).length
      ? []
      : (Array.isArray(block.named_objects) ? block.named_objects.map(String) : []),
    calendarOnly: block.calendar_only === true,
    //  Un serveur qui ne dit rien est un serveur d'avant ce champ : `known` garde
    //  la lecture d'alors, ou `calendar_only` etait le seul verdict.
    namedObjectsState:
      block.named_objects_state === "undeterminable" ? "undeterminable" : "known",
    bound: Number(block.bound ?? 0),
    unbound: Number(block.unbound ?? 0),
    crossings,
  };
}

export default function IdentityCandidatesPanel({
  evidence,
  editable,
  pendingTargets,
  onPin,
}: {
  evidence: unknown;
  editable: boolean;
  pendingTargets: Record<string, string | null>;
  onPin: (targets: Record<string, string>) => void;
}) {
  const { proposed, bound, unbound, crossings, namedObjects, calendarOnly, namedObjectsState } =
    readIdentityCandidates(evidence);
  const remaining = proposed.filter((entry) => !(entry.field_id in pendingTargets));

  //  Un mapping dont aucun champ ne nomme d'identité n'est PAS un mapping
  //  complet, et le dire est le seul moyen qu'une personne le sache avant de
  //  poser la question qui a besoin du croisement.
  //  DE QUEL OBJET CE FLUX PARLE-T-IL. Une date est un AXE, pas une identite : un
  //  flux qui ne nomme aucun objet ne se croisera jamais que sur le calendrier --
  //  on saura que quelque chose s est passe ce jour-la, jamais quoi. Le dire ici
  //  est le seul moment ou quelqu un peut encore reprendre la collecte.
  //
  //  ET IL NE SE DIT QUE QUAND C EST VRAI (tranche le 2026-08-16). Mesure : sur
  //  une base ou tout le vocabulaire de plateforme est provisionne, 272 champs
  //  canoniques et ZERO `object_kind` -- la colonne vient « de personne », c est
  //  une regle ecrite de `governance.md`. `calendar_only` etait donc vrai pour
  //  TOUT flux, ce retour anticipe se prenait toujours, et le panneau qui existe
  //  pour faire epingler n offrait jamais l epinglage. Un ecran qui refuse
  //  toujours ne refuse rien : il est absent. Le serveur distingue maintenant
  //  « aucun objet nomme » de « personne n a encore dit quels champs en nomment »,
  //  et seul le premier ferme ce panneau.
  if (calendarOnly) {
    return (
      <Panel>
        <PanelHeader
          title="This Datastream names no object"
          description={
            "It carries a date and no identity of what it is about, so it can only ever " +
            "be joined on the calendar: you will know something happened that day, never " +
            "what. No mapping can repair a column that was never collected — the source " +
            "step is where this is fixed."
          }
        />
      </Panel>
    );
  }

  if (proposed.length === 0) {
    if (bound > 0 || unbound === 0) return null;
    return (
      <Panel>
        <PanelHeader
          title="No shared identity is named"
          description={
            `${unbound} ${unbound === 1 ? "column" : "columns"} of this mapping name no MDM ` +
            "identity, and none of them carries the name of a known one. This Datastream " +
            "cannot be crossed with another until a column names what it shares — pick one " +
            "on a row below, or declare it."
          }
        />
      </Panel>
    );
  }

  const count = remaining.length || proposed.length;
  return (
    <Panel>
      <PanelHeader
        title={`${count} ${count === 1 ? "column names" : "columns name"} a known identity`}
        description={
          //  CE DONT LE FLUX PARLE, et le serveur le mesure deja : `named_objects`
          //  est l'ensemble des `object_kind` des champs canoniques presents. Le
          //  panneau le LISAIT et ne l'affichait nulle part, si bien que la seule
          //  fois ou il atteignait quelqu'un etait sa negation -- le refus
          //  `calendar_only` ci-dessus. Dire de QUOI parle le flux est ce qui
          //  rend le croisement previsible avant de l'essayer.
          (namedObjects.length > 0
            ? `This Datastream names ${namedObjects.join(", ")}. `
            : namedObjectsState === "undeterminable"
              ? "Which object this Datastream is about cannot be told yet: no canonical " +
                "field in this Project declares the object it qualifies. That is a gap in " +
                "the vocabulary, not a fact about this Datastream. "
              : "") +
          "Pinning a column to a shared identity is what makes a cross possible. " +
          "Nothing is written until you prepare the change."
        }
        actions={
          editable && remaining.length > 0 ? (
            <Button
              data-testid="pin-identity-candidates"
              onClick={() =>
                onPin(
                  Object.fromEntries(
                    remaining.map((entry) => [entry.field_id, entry.canonical_field_id]),
                  ),
                )
              }
            >
              Pin {remaining.length} {remaining.length === 1 ? "column" : "columns"}
            </Button>
          ) : null
        }
      />
      <ul className="flex flex-col gap-2 px-4 pb-4">
        {proposed.map((entry) => {
          const pinned = entry.field_id in pendingTargets;
          return (
            <li key={entry.field_id} className="flex items-center gap-2 text-body">
              <ObjectId value={entry.field_id} title="Source field name" />
              <span aria-hidden>→</span>
              <span>{entry.canonical_name}</span>
              <Badge outline>{scopeLabel(entry.scope)}</Badge>
              {/* Ce qui decide si l epinglage vaut la peine : le champ est-il
                  COMMUN ? Un `date` que huit autres flux nomment est une cle de
                  croisement ; un champ que personne d autre ne porte n en sera
                  jamais une, et le dire evite de le chercher. */}
              <span className="text-caption text-muted-foreground">
                {entry.also_named_by > 0
                  ? `shared with ${entry.also_named_by} other ${entry.also_named_by === 1 ? "Datastream" : "Datastreams"}`
                  : "in this Datastream only"}
              </span>
              {pinned ? (
                <Status tone="success">Prepared</Status>
              ) : editable ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onPin({ [entry.field_id]: entry.canonical_field_id })}
                >
                  Pin
                </Button>
              ) : null}
            </li>
          );
        })}
      </ul>
      {crossings.length > 0 && (
        <p className="px-4 pb-4 text-caption text-muted-foreground">
          {crossings
            .map(
              (entry) =>
                `${entry.common_key}: crosses with ${entry.partners.join(", ")}`,
            )
            .join(" · ")}
        </p>
      )}
    </Panel>
  );
}
