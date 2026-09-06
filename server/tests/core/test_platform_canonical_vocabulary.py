"""La moitie plateforme du vocabulaire est DERIVEE, et ce fichier fixe de quoi.

AI-288. Le registre `app.mdm_canonical_fields` portait 0 ligne a la portee
plateforme pendant que six modules validaient chaque liaison contre lui. La
reparation n'est pas une liste ecrite a la main : c'est la projection du
dictionnaire gouverne (`app.target_fields`, 13 lignes ratifiees par la migration
023) a travers le lien que la migration 032 avait deja pose.

Ces tests sont OFFLINE : ils portent sur la DERIVATION, pas sur l'ecriture. Ce
qui se passe en base est prouve par `tests/integration/test_platform_canonical_vocabulary_pg.py`.
"""

from __future__ import annotations

import pytest
from core.platform_canonical_vocabulary import (
    PlatformField,
    PlatformVocabularyError,
    divergences,
    non_additive_metric_names,
    project_dictionary,
)

NON_ADDITIVE = frozenset({"average_position", "roas", "ctr", "cpa"})


def _row(name, kind, data_type, measure=None, description="d"):
    return {
        "name": name,
        "field_kind": kind,
        "data_type": data_type,
        "measure": measure,
        "description": description,
    }


def test_a_currency_field_lands_as_money_and_the_rest_keep_their_name():
    """Les deux vocabulaires different d'UN mot, et c'est le seul a traduire."""
    projected = project_dictionary(
        [
            _row("cost", "metric", "currency", "sum"),
            _row("sessions", "metric", "integer", "sum"),
            _row("date", "dimension", "date"),
            _row("page", "dimension", "string"),
        ],
        non_additive=NON_ADDITIVE,
    )
    by_name = {field.canonical_name: field for field in projected}

    assert by_name["cost"].value_type == "money"
    assert by_name["sessions"].value_type == "integer"
    assert by_name["date"].value_type == "date"
    assert by_name["page"].value_type == "string"


def test_average_position_needs_the_two_sources_to_agree():
    """LE cas ou lire une seule source donne un resultat faux.

    Le dictionnaire dit `measure = 'average'`, le catalogue dit `additive =
    false`. Projeter la seule mesure declarerait la metrique sommable-par-moyenne
    et laisserait le garde AD-4 en derniere ligne ; projeter le seul catalogue lui
    ferait perdre son agregation. La regle : non-additif gagne, et l'agregation
    tombe -- ce que la CHECK de la migration 032 accepte.
    """
    projected = project_dictionary(
        [_row("average_position", "metric", "decimal", "average")], non_additive=NON_ADDITIVE
    )[0]

    assert projected.non_additive is True
    assert projected.aggregation is None


def test_a_dimension_carries_neither_aggregation_nor_non_additivity():
    projected = project_dictionary(
        [_row("country", "dimension", "string")], non_additive=NON_ADDITIVE
    )[0]

    assert projected.aggregation is None
    assert projected.non_additive is False
    #  Et la declaration passe le validateur du registre, pas un second jeu de regles.
    assert projected.as_declaration()["concept_kind"] == "dimension"


def test_an_unknown_dictionary_type_is_refused_by_name_rather_than_landing_as_a_string():
    """Un type neuf doit se decider, pas se deviner."""
    with pytest.raises(PlatformVocabularyError) as excinfo:
        project_dictionary([_row("weird", "metric", "geography", "sum")], non_additive=frozenset())

    assert "geography" in str(excinfo.value)
    assert "weird" in str(excinfo.value)


def test_the_projection_is_ordered_so_two_runs_produce_the_same_list():
    rows = [
        _row("sessions", "metric", "integer", "sum"),
        _row("country", "dimension", "string"),
        _row("clicks", "metric", "integer", "sum"),
    ]
    names = [field.canonical_name for field in project_dictionary(rows, non_additive=NON_ADDITIVE)]

    assert names == ["country", "clicks", "sessions"]


def test_the_catalogue_is_read_not_copied():
    """`dim_metric.csv` est la seule source d'additivite, et elle est lue au disque."""
    declared = non_additive_metric_names()

    assert "average_position" in declared
    assert {"cpa", "ctr", "roas"} <= declared
    assert "sessions" not in declared


def test_a_stored_row_that_disagrees_is_reported_and_never_rewritten():
    """Entre le depot et un registre vivant, choisir un camp detruit l'autre."""
    projected = [
        PlatformField(
            canonical_name="cost",
            concept_kind="metric",
            value_type="money",
            aggregation="sum",
            non_additive=False,
            description=None,
            dictionary_field_name="cost",
        )
    ]
    stored = {
        "cost": {
            "concept_kind": "metric",
            "value_type": "decimal",   # quelqu'un l'a change en base
            "aggregation": "sum",
            "non_additive": False,
        }
    }

    problems = divergences(stored, projected)

    assert len(problems) == 1
    assert "value_type" in problems[0]
    assert "decimal" in problems[0] and "money" in problems[0]


def test_a_field_the_registry_does_not_carry_yet_is_not_a_divergence():
    """Manquant et divergent sont deux etats, et ils appellent deux gestes."""
    projected = [
        PlatformField("cost", "metric", "money", "sum", False, None, "cost"),
    ]

    assert divergences({}, projected) == []


# ---------------------------------------------------------------------------
# Le vocabulaire est borne par RIEN -- les connecteurs l'etendent
# ---------------------------------------------------------------------------
#
# « La paresse c'est de croire que tu n'en auras que deux la ou tu peux en avoir
# une infinite » (Jean, 2026-08-15). Ces cas fixent la regle qui remplace le
# plafond : le depot declare, la projection lit, et ce qui ne se lit pas se
# NOMME au lieu de se deviner.


def _decl(kind, physical_type, aggregation=None, non_additive=False):
    return {(kind, physical_type, aggregation, non_additive)}


def test_a_connector_target_the_dictionary_never_carried_joins_the_vocabulary():
    """`campaign_name` est porte par neuf connecteurs et par aucune des 13."""
    from core.platform_canonical_vocabulary import project_connectors

    fields, refused = project_connectors(
        {"campaign_name": _decl("dimension", "string")}, governed=["cost", "date"]
    )

    assert refused == []
    assert [field.canonical_name for field in fields] == ["campaign_name"]
    assert fields[0].value_type == "string"
    #  Il ne derive d'aucune ligne du dictionnaire, et le pretendre casserait la
    #  cle etrangere.
    assert fields[0].dictionary_field_name is None


def test_the_dictionary_governs_the_names_it_carries():
    """Un connecteur qui contredit le dictionnaire ne redefinit pas le nom."""
    from core.platform_canonical_vocabulary import project_connectors

    fields, refused = project_connectors(
        {"cost": _decl("metric", "integer", "sum")}, governed=["cost"]
    )

    assert fields == []
    assert refused == []


def test_two_connectors_that_disagree_mint_NOTHING_and_are_named():
    """`campaign_id` : seize connecteurs le portent, deux ne s'accordent pas sur son type."""
    from core.platform_canonical_vocabulary import project_connectors

    fields, refused = project_connectors(
        {
            "campaign_id": {
                ("dimension", "integer", None, False),
                ("dimension", "string", None, False),
            }
        },
        governed=[],
    )

    assert fields == []
    assert len(refused) == 1
    assert "campaign_id" in refused[0] and "disagree" in refused[0]


def test_a_physical_type_without_an_equivalent_is_refused_rather_than_landing_as_a_string():
    from core.platform_canonical_vocabulary import project_connectors

    fields, refused = project_connectors({"segment_ids": _decl("dimension", "json")}, governed=[])

    assert fields == []
    assert "json" in refused[0]


def test_a_metric_declaring_neither_aggregation_nor_non_additivity_is_refused():
    """La regle de la migration 032, appliquee avant l'ecriture plutot qu'apres."""
    from core.platform_canonical_vocabulary import project_connectors

    fields, refused = project_connectors({"weird": _decl("metric", "integer")}, governed=[])

    assert fields == []
    assert "summed by whatever read it first" in refused[0]


def test_a_third_kind_is_named_rather_than_forced_into_one_of_the_two():
    """Trois manifestes declarent `kind: event` -- un objet que le registre n'a pas."""
    from core.platform_canonical_vocabulary import project_connectors

    fields, refused = project_connectors({"product_launch": _decl("event", "date")}, governed=[])

    assert fields == []
    assert "event" in refused[0]


def test_the_repository_declares_far_more_than_the_dictionary():
    """La mesure qui a ouvert cette moitie, gardee comme un test.

    Le compte exact bougera -- c'est le propos, un 40e connecteur agrandit le
    vocabulaire. Ce qui est fixe est l'ORDRE DE GRANDEUR : le registre ne se
    reduit jamais aux 13 sans que quelqu'un ait supprime des connecteurs.
    """
    from core.platform_canonical_vocabulary import connector_declarations, project_connectors

    declared = connector_declarations()
    fields, refused = project_connectors(declared, governed=[])

    assert len(declared) > 200
    assert len(fields) > 200
    #  Et les refus restent une petite minorite NOMMEE, pas un silence de masse :
    #  moins d'un dixieme de ce que le depot declare, chacun avec sa phrase.
    assert len(refused) < len(fields) // 10
    assert all(": " in refusal for refusal in refused)
