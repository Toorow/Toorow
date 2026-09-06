/**
 * Le geste que rien ne demandait, et ce qu'il ne fait pas.
 *
 * Mesure du 2026-08-13 : neuf Datastreams publiés, zéro champ lié au MDM, tous
 * `status: confirmed`. Le sélecteur par colonne existait et n'avait jamais servi.
 *
 * Ce fichier fixe trois règles plutôt que trois exemples : le panneau PROPOSE et
 * n'écrit rien lui-même ; une absence d'identité se DIT au lieu de disparaître ;
 * et la conséquence — quel croisement s'ouvre — voyage avec la proposition.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import IdentityCandidatesPanel from "../datastreams/workbench/mapping/IdentityCandidatesPanel";

const EVIDENCE = {
  identity_candidates: {
    proposed: [
      {
        field_id: "date",
        canonical_field_id: "mdm_EXAMPLE000000000000000001",
        canonical_name: "date",
        scope: "project",
        also_named_by: 8,
      },
      {
        field_id: "channel_id",
        canonical_field_id: "mdm_EXAMPLE000000000000000002",
        canonical_name: "channel_id",
        scope: "project",
        also_named_by: 0,
      },
    ],
    bound: 0,
    unbound: 4,
    unlocks: {
      crossings: [
        { common_key: "Day", with: [{ datastream_id: "ds_EXAMPLE", name: "Channel daily" }] },
      ],
      count: 1,
    },
  },
};

describe("IdentityCandidatesPanel", () => {
  it("proposes every column that names a known identity", () => {
    render(
      <IdentityCandidatesPanel
        evidence={EVIDENCE}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    //  Deux occurrences par ligne, et c'est le propos : le nom du champ ET
    //  l'identite qu'il prendrait. Une seule ferait lire une correspondance
    //  qu'on ne peut pas verifier de l'oeil.
    expect(screen.getAllByText("date")).toHaveLength(2);
    expect(screen.getAllByText("channel_id")).toHaveLength(2);
  });

  it("pins the whole batch in ONE gesture, and writes nothing itself", () => {
    const onPin = vi.fn();
    render(
      <IdentityCandidatesPanel
        evidence={EVIDENCE}
        editable
        pendingTargets={{}}
        onPin={onPin}
      />,
    );

    fireEvent.click(screen.getByTestId("pin-identity-candidates"));

    // The intention, and only the intention: the mapping in force is never
    // edited in place, it leaves through `Prepare mapping change`.
    expect(onPin).toHaveBeenCalledWith({
      date: "mdm_EXAMPLE000000000000000001",
      channel_id: "mdm_EXAMPLE000000000000000002",
    });
  });

  it("says how many other Datastreams name the same field -- the reason to pin at all", () => {
    render(
      <IdentityCandidatesPanel evidence={EVIDENCE} editable pendingTargets={{}} onPin={() => {}} />,
    );

    //  Un champ que huit autres flux nomment est une cle de croisement ; un champ
    //  que personne d autre ne porte n en sera jamais une, et le dire evite de le
    //  chercher.
    expect(screen.getByText("shared with 8 other Datastreams")).toBeTruthy();
    expect(screen.getByText("in this Datastream only")).toBeTruthy();
  });

  it("says what the pin would unlock, beside the gesture and not after it", () => {
    render(
      <IdentityCandidatesPanel
        evidence={EVIDENCE}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    expect(screen.getByText(/Day: crosses with Channel daily/)).toBeTruthy();
  });

  it("says WHAT the Datastream is about, which the server measured and the panel dropped", () => {
    //  `named_objects` etait lu par le parseur et destructure nulle part : la
    //  seule fois ou il atteignait quelqu'un etait sa negation, le refus
    //  `calendar_only`. Un flux qui nomme une campagne se croise autrement
    //  qu'un flux qui nomme une video, et c'est ce qui rend le croisement
    //  previsible avant de l'essayer.
    render(
      <IdentityCandidatesPanel
        evidence={{
          identity_candidates: {
            ...EVIDENCE.identity_candidates,
            named_objects: ["campaign", "channel"],
          },
        }}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    expect(screen.getByText(/This Datastream names campaign, channel/)).toBeTruthy();
  });

  it("offers nothing twice: a column already prepared reads as prepared", () => {
    render(
      <IdentityCandidatesPanel
        evidence={EVIDENCE}
        editable
        pendingTargets={{ date: "mdm_EXAMPLE000000000000000001" }}
        onPin={() => {}}
      />,
    );

    expect(screen.getByText("Prepared")).toBeTruthy();
    expect(screen.getByTestId("pin-identity-candidates").textContent).toContain("1 column");
  });

  it("a Datastream that names NO OBJECT says it can only ever be joined on the calendar", () => {
    //  La regle generale derriere le cas des publications video : une date est un
    //  AXE, pas une identite. Un flux qui ne nomme aucun objet ne se croisera
    //  jamais que sur le calendrier, et aucun mapping ne repare une colonne qui
    //  n a jamais ete collectee.
    render(
      <IdentityCandidatesPanel
        evidence={{ identity_candidates: { proposed: [], bound: 1, unbound: 0,
                                           named_objects: [], calendar_only: true } }}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    expect(screen.getByText(/names no object/)).toBeTruthy();
    expect(screen.getByText(/only ever be joined on the calendar/)).toBeTruthy();
  });

  it("still offers the pin when nobody has declared WHICH fields name an object", () => {
    /**
     * TRANCHE LE 2026-08-16, ET LA MESURE EST CE QUI TRANCHE. `calendar_only`
     * derive de `object_kind`, une colonne que `governance.md` fait venir « de
     * personne » : sur une base ou tout le vocabulaire de plateforme est
     * provisionne, 272 champs canoniques et ZERO en portent un. Le verdict etait
     * donc VRAI pour tout flux de toute instance, ce panneau se fermait toujours,
     * et le geste qu il existe pour offrir n etait jamais offert.
     *
     * « Aucun objet nomme » et « personne n a encore dit lesquels en nomment »
     * sont deux faits opposes. Le second n est pas un refus.
     */
    render(
      <IdentityCandidatesPanel
        evidence={{
          identity_candidates: {
            ...EVIDENCE.identity_candidates,
            named_objects: [],
            calendar_only: false,
            named_objects_state: "undeterminable",
          },
        }}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    //  Le geste est la -- c est tout l enjeu.
    expect(screen.getByTestId("pin-identity-candidates")).toBeTruthy();
    //  Et l ignorance se dit, sans etre maquillee en fait sur le flux.
    expect(screen.getByText(/cannot be told yet/)).toBeTruthy();
    expect(screen.getByText(/gap in the vocabulary, not a fact about this Datastream/)).toBeTruthy();
    //  Surtout PAS la phrase du refus : elle affirmerait ce qui n est pas su.
    expect(screen.queryByText(/only ever be joined on the calendar/)).toBeNull();
  });

  it("keeps the refusal when the vocabulary CAN answer and the answer is none", () => {
    //  Le verdict n est pas supprime, il est conditionne : un vocabulaire qui
    //  qualifie des objets rend l absence d objet significative.
    render(
      <IdentityCandidatesPanel
        evidence={{
          identity_candidates: {
            proposed: [], bound: 1, unbound: 0,
            named_objects: [], calendar_only: true, named_objects_state: "known",
          },
        }}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    expect(screen.getByText(/only ever be joined on the calendar/)).toBeTruthy();
  });

  it("a Datastream that names no identity SAYS so rather than showing nothing", () => {
    render(
      <IdentityCandidatesPanel
        evidence={{ identity_candidates: { proposed: [], bound: 0, unbound: 5 } }}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    expect(screen.getByText(/No shared identity is named/)).toBeTruthy();
    expect(screen.getByText(/cannot be crossed/)).toBeTruthy();
  });

  it("stays silent when every column that could be pinned already is", () => {
    const { container } = render(
      <IdentityCandidatesPanel
        evidence={{ identity_candidates: { proposed: [], bound: 6, unbound: 3 } }}
        editable
        pendingTargets={{}}
        onPin={() => {}}
      />,
    );

    expect(container.textContent).toBe("");
  });
});
