"""Le contrat de reponse d'un croisement (story 69.3, AC2 et AC3).

CE QUE CE FICHIER EPINGLE. Le croisement lui-meme est du SQL, prouve par
`tests/conformance/test_semantic_fact_by_entity_attribute.py` sur un build dbt
reel. Ce qu'une doublure NE peut pas prouver la-bas et qui compte autant : ce
que la REPONSE porte.

  * le chemin analytique est declare, avec la relation lue -- pas un silence ;
  * la version de regle voyage quand la classification est derivee : republier
    une regle change les reponses sans toucher un fait, donc une reponse qui ne
    nomme pas sa version n'est pas re-derivable ;
  * le reste non rattache est une LIGNE du tableau, nommee et comptee, et le
    total de la reponse egale le total du mart -- la seule facon qu'un lecteur
    qui ne fait pas confiance au croisement puisse le verifier.
"""

from __future__ import annotations

import pytest
from core import entity_cross_read as ecr
from core.envelope import ANALYTICAL_PATH_KEYS


def _row(**kwargs):
    base = {
        "attribute_origin": ecr.ORIGIN_CARRIED,
        "attribute": "format",
        "attribute_value": "short",
        "resolution_state": "resolved",
        "rule_set_version_id": None,
        "value": 10.0,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Le chemin analytique.
# ---------------------------------------------------------------------------


def test_the_path_declares_the_relation_it_read():
    path = ecr.ANALYTICAL_PATH_ENTITY_CROSS
    assert set(path) == set(ANALYTICAL_PATH_KEYS)
    assert path["relation"] == ecr.CROSS_RELATION
    # `governed_result` est False PAR CONSTRUCTION : un constructeur capable de
    # revendiquer True finirait par se le faire demander.
    assert path["governed_result"] is False


def test_the_meta_carries_the_path_and_the_coverage():
    cross = ecr.compose_cross(
        [_row(), _row(attribute_value="long", value=5.0)], attribute="format"
    )
    meta = ecr.cross_meta(cross)
    assert meta["analytical_path"]["relation"] == ecr.CROSS_RELATION
    assert meta["attribute"] == "format"
    assert meta["coverage"]["total_value"] == 15.0


# ---------------------------------------------------------------------------
# La version de regle.
# ---------------------------------------------------------------------------


def test_a_derived_classification_names_the_rule_version_that_produced_it():
    cross = ecr.compose_cross(
        [
            _row(
                attribute_origin=ecr.ORIGIN_DERIVED,
                attribute="content_type",
                attribute_value="tutorial",
                rule_set_version_id="grsv_01V2",
            )
        ],
        attribute="content_type",
    )
    assert cross["rule_set_version_ids"] == ["grsv_01V2"]
    assert ecr.cross_meta(cross)["rule_set_version_ids"] == ["grsv_01V2"]


def test_a_carried_attribute_names_no_rule_and_the_key_is_absent():
    cross = ecr.compose_cross([_row()], attribute="format")
    assert cross["rule_set_version_ids"] == []
    # Une cle vide se lirait comme << il y a une regle, on ne sait pas laquelle >>.
    assert "rule_set_version_ids" not in ecr.cross_meta(cross)


# ---------------------------------------------------------------------------
# Le reste non rattache.
# ---------------------------------------------------------------------------


def test_the_unattached_group_is_a_named_line_with_its_count():
    cross = ecr.compose_cross(
        [
            _row(value=90.0),
            _row(attribute_origin=ecr.ORIGIN_UNATTACHED, resolution_state="unmatched",
                 attribute=None, attribute_value=None, value=10.0),
        ],
        attribute="format",
    )
    assert cross["unattached"]["label"] == ecr.UNATTACHED_LABEL
    assert cross["unattached"]["value"] == 10.0
    assert cross["unattached"]["rows"] == 1
    # Le detail des raisons vit A COTE : un lecteur n'a pas a distinguer
    # `unmatched` de `ambiguous` pour lire un total, mais celui qui repare si.
    assert cross["unattached"]["by_state"] == {"unmatched": 1}


def test_the_total_of_the_answer_is_the_total_of_the_mart():
    cross = ecr.compose_cross(
        [
            _row(value=90.0),
            _row(attribute_value="long", value=7.0),
            _row(attribute_origin=ecr.ORIGIN_UNATTACHED, resolution_state="ambiguous",
                 attribute=None, attribute_value=None, value=3.0),
        ],
        attribute="format",
    )
    assert cross["coverage"]["attached_value"] == 97.0
    assert cross["coverage"]["total_value"] == 100.0
    assert cross["coverage"]["attached_share"] == 0.97


def test_nothing_at_all_covers_nothing_rather_than_zero_percent():
    cross = ecr.compose_cross([], attribute="format")
    # Une fraction sans denominateur n'est pas une couverture de zero.
    assert cross["coverage"]["attached_share"] is None
    assert cross["groups"] == []


def test_a_row_of_another_attribute_is_not_counted_in_this_one():
    cross = ecr.compose_cross(
        [_row(), _row(attribute="content_type", attribute_value="tutorial", value=99.0)],
        attribute="format",
    )
    assert [group["label"] for group in cross["groups"]] == ["short"]
    assert cross["coverage"]["total_value"] == 10.0


def test_groups_are_ordered_so_two_answers_can_be_compared():
    cross = ecr.compose_cross(
        [_row(attribute_value="long", value=1.0), _row(attribute_value="short", value=2.0)],
        attribute="format",
    )
    assert [group["label"] for group in cross["groups"]] == ["long", "short"]


# ---------------------------------------------------------------------------
# Le refus nomme.
# ---------------------------------------------------------------------------


def test_an_incomplete_request_is_refused_before_any_read():
    class _NoConn:
        def cursor(self):  # pragma: no cover - must never be reached
            raise AssertionError("an incomplete request must not touch the database")

    with pytest.raises(ecr.CrossReadRefused) as excinfo:
        ecr.assert_attribute_is_published(
            _NoConn(), project_id="proj_EXAMPLE", object_kind="", attribute="format"
        )
    assert excinfo.value.code == "cross_request_incomplete"
