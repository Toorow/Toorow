"""AI-167 -- le signal de decalage de jour dit LEQUEL des deux problemes il a vu.

CE QUI ETAIT MESURE, LE 2026-08-16. Deux moteurs repondaient a la meme question :
`timezone_signal.check_cross_source_day_offset`, branche, et
`time_boundary.compare_boundaries`, ecrit pour le remplacer et jamais appele.

Le premier N'EST PAS aveugle -- c'est ce que la mesure a montre, contre le
docstring du second qui lui attribue encore le defaut de la story 48.3 :

    deux d accord + trois non placables   -> TIMEZONE_DAY_OFFSET
    deux horloges differentes, grain DATE -> TIMEZONE_DAY_OFFSET
    tous d accord                         -> aucun signal

Ce qui manquait est plus fin, et c'est une regle de ce depot : un message nomme
le GESTE qui repare. « Ces sources tirent leur jour sur des horloges
differentes » se repare par une decision de politique ; « cette source n'a pu
etre placee sur aucune horloge » se repare en FAISANT TOURNER le Datastream pour
que sa publication en enregistre une. Le message vivant distinguait deja les
deux -- mais un message est de la prose, et un consommateur qui branche sur
`code` voyait la meme valeur pour les deux, donc ne pouvait proposer qu'un geste.

Le vocabulaire vient de `core.time_boundary`, pas d'un troisieme invente ici :
plutot que de brancher une seconde comparaison a cote de la vivante -- deux
moteurs pour une question, la forme que ce depot paie le plus cher -- ce sont ses
CODES qui deviennent la moitie typee du signal deja branche.
"""
from __future__ import annotations

import pytest
from core.time_boundary import GAP_NO_SOURCE_ZONE, SIGNAL_BOUNDARY_DIFFERS
from core.timezone_signal import check_cross_source_day_offset


def _stream(name: str, zone: str | None) -> dict:
    return {"datastream": name, "report_timezone": zone}


def test_different_clocks_name_the_policy_problem():
    signal = check_cross_source_day_offset(
        metric="clicks",
        streams=[_stream("a", "Europe/Paris"), _stream("b", "America/New_York")],
    )

    assert signal["reasons"] == [SIGNAL_BOUNDARY_DIFFERS]


def test_an_unplaceable_source_names_the_other_problem():
    """Et il se repare autrement : en faisant tourner le flux, pas en decidant."""
    signal = check_cross_source_day_offset(
        metric="clicks",
        streams=[_stream("a", "Europe/Paris"), _stream("b", "Europe/Paris"), _stream("c", None)],
    )

    assert signal["reasons"] == [GAP_NO_SOURCE_ZONE]


def test_both_at_once_is_a_list_and_neither_is_dropped():
    """La moitie qu'un << pire cas >> ferait disparaitre.

    Collapser sur le plus grave cacherait une reparation que l'operateur peut
    faire AUJOURD'HUI -- faire tourner le flux non place -- au motif qu'une
    decision de politique est aussi en attente.
    """
    signal = check_cross_source_day_offset(
        metric="clicks",
        streams=[
            _stream("a", "Europe/Paris"),
            _stream("b", "America/New_York"),
            _stream("c", None),
        ],
    )

    assert set(signal["reasons"]) == {SIGNAL_BOUNDARY_DIFFERS, GAP_NO_SOURCE_ZONE}


def test_agreement_still_says_nothing_at_all():
    """La garde anti-faux-positif, qui ne doit pas bouger.

    Elargir un signal sans verifier ce qu'il refuse encore de dire, c'est le
    desarmer : un avertissement qui apparait toujours n'est plus lu.
    """
    assert (
        check_cross_source_day_offset(
            metric="clicks",
            streams=[_stream("a", "Europe/Paris"), _stream("b", "Europe/Paris")],
        )
        is None
    )


@pytest.mark.parametrize("reason", [SIGNAL_BOUNDARY_DIFFERS, GAP_NO_SOURCE_ZONE])
def test_the_reason_codes_are_the_time_boundary_vocabulary(reason):
    """Une seule orthographe par verdict, dans tout le produit.

    `SIGNAL_BOUNDARY_DIFFERS` etait un litteral au milieu de
    `compare_boundaries` ; deux orthographes d'un meme verdict est la maniere
    dont un consommateur finit par n'en traiter qu'une.
    """
    import core.time_boundary as tb

    assert reason in {tb.SIGNAL_BOUNDARY_DIFFERS, tb.GAP_NO_SOURCE_ZONE}
