"""Une remarque peut porter le changement qu'elle propose -- et rien de plus.

POURQUOI PAS LE RAIL DE PUBLICATION GOUVERNEE, et c'est un choix argumente.
`governed_publication` fait porter a une confirmation un secret OPAQUE cache du
modele, avec protection de rejeu -- parce qu'une publication de mapping AVANCE UN
POINTEUR : elle change ce que les donnees veulent dire.

Un lien de taxonomie n'a rien de tout cela : reversible par un appel, il ne
deplace aucun pointeur. Lui imposer la meme ceremonie serait recopier le
mecanisme le plus lourd du depot parce qu'il existe -- et une ceremonie qu'on ne
peut pas justifier finit contournee.

CE QUI RESTE, ET QUI EST L'ESSENTIEL : un humain accepte, et le lien nait
`derived` -- distinguable pour toujours d'une decision humaine.

LES CONTROLES NEGATIFS COMPTENT AUTANT : un `kind` inconnu est refuse AU DEPOT et
non a l'acceptation, un refus n'applique rien, et une acceptation sans charge non
plus. Une file qui applique en silence ce qu'elle ne comprend pas est dangereuse.
"""

from __future__ import annotations

import pytest
from core.context_review import PROPOSAL_KINDS, ReviewRequestError, request_review


class _Conn:
    """Refuse toute execution : ces portes doivent echouer AVANT la base.

    Ecrit ainsi exprès -- si une validation laissait passer, le test casserait
    sur l'absence de base plutot que de passer en silence.
    """

    def cursor(self) -> "_Conn":
        raise AssertionError("no statement should reach the database")


def _deposit(**overrides):
    payload = {
        "org_id": "org_1",
        "project_id": "proj_1",
        "node_type": "procedure",
        "node_id": "proc_1",
        "node_version": 1,
        "note": "this constraint no longer exists",
        "requested_by": "agent:claude",
        "origin": "agent",
    }
    payload.update(overrides)
    return request_review(_Conn(), **payload)


def test_only_a_known_kind_may_be_queued() -> None:
    """Refuse AU DEPOT plutot qu'a l'acceptation : une file ne doit pas se
    remplir de choses que personne ne saura appliquer."""
    assert PROPOSAL_KINDS == ("business_link",)
    with pytest.raises(ReviewRequestError, match="kind must be one of"):
        _deposit(proposed_change={"kind": "delete_everything"})


def test_a_payload_must_be_an_attribute_mapping() -> None:
    with pytest.raises(ReviewRequestError, match="must be an attribute mapping"):
        _deposit(proposed_change=["business_link"])


def test_the_version_is_required_because_a_floating_remark_cannot_be_checked() -> None:
    with pytest.raises(ReviewRequestError, match="node_version must be a positive integer"):
        _deposit(node_version=0)


def test_an_empty_remark_is_refused() -> None:
    with pytest.raises(ReviewRequestError, match="note cannot be empty"):
        _deposit(note="   ")


def test_the_origin_is_closed_to_two_values() -> None:
    """Une remarque de machine confondue avec une remarque humaine vaut moins
    que rien -- c'est la raison d'etre du champ."""
    with pytest.raises(ReviewRequestError, match="origin must be"):
        _deposit(origin="robot")


def test_a_node_that_is_not_a_hub_node_is_refused() -> None:
    with pytest.raises(ReviewRequestError, match="node_type must be"):
        _deposit(node_type="datastream")
