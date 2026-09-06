"""AI-273 -- une carte figee dans un Render partage regarde sa propre fraicheur.

CE QUI ETAIT FAUX, ET POURQUOI IL FALLAIT UN TEST. `core.health_enrichment` est
le seul evaluateur de fraicheur du depot, et il n'etait atteint que depuis
`get_daily_report`. Les quatre batisseurs de `cards.py` et celui de `reports.py`
expediaient `stale_since_evaluated: False` -- honnete, mais definitivement
honnete : rien en aval ne le passait jamais a True. Une carte partagee en mars
portait donc « personne n'a regarde » pour toujours, et le lecteur voyait une
absence de peremption la ou il n'y avait qu'une absence de regard.

CE QUE CE FICHIER EPINGLE : que le regard ait lieu, et qu'il ne mente pas quand
il n'a pas lieu. Deux tests, deux directions -- un evaluateur branche qui ne
peut PAS se debrancher, et un `False` qui survit quand la base ne repond pas.
Une seule des deux moities suffirait a rendre le garde rassurant et faux.
"""
from __future__ import annotations

from unittest.mock import patch

from core import cards as cards_module


def _envelope_of(monkeyed_evaluator):
    """Rend l'enveloppe d'une carte `connectors`, evaluateur remplace.

    La carte `connectors` est choisie parce qu'elle est la plus pauvre en
    dependances : aucun mart, aucun plan, et son resolveur est l'un des quatre
    qui expediaient `False`.
    """
    from unittest.mock import MagicMock

    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=ctx),
        patch("core.cards._reports_by_module", return_value={}),
        patch("core.health_enrichment.enrich_envelope_with_health", monkeyed_evaluator),
    ):
        _summary, envelope, _uri = cards_module.get_card(
            [], "proj_EXAMPLE", template="connectors"
        )
    return envelope


def test_a_card_reaches_the_one_evaluator_with_its_own_window():
    """L'evaluateur est appele, avec le projet et la fenetre de CETTE carte.

    On epingle les arguments et pas seulement l'appel : une evaluation menee sur
    la mauvaise fenetre est exactement le defaut que 53.3 a corrige ailleurs --
    un badge de peremption decrivant une connexion qui n'a rien servi sur la
    periode lue.
    """
    seen: dict = {}

    def _evaluator(envelope, project_id, *, date_from=None, date_to=None):
        seen["project_id"] = project_id
        seen["window"] = (date_from, date_to)
        envelope.setdefault("meta", {}).setdefault("freshness", {})[
            "stale_since_evaluated"
        ] = True
        return envelope

    envelope = _envelope_of(_evaluator)

    assert seen["project_id"] == "proj_EXAMPLE"
    assert seen["window"][0] and seen["window"][1], (
        "la fenetre doit voyager avec l'appel -- sans elle l'evaluateur ne dit rien"
    )
    assert envelope["meta"]["freshness"]["stale_since_evaluated"] is True


def test_the_mart_path_looks_too_and_not_only_the_context_cards():
    """LES DEUX sorties de `get_card`, pas une.

    Ecrit apres avoir sonde le garde : retirer l'appel du chemin mart le laissait
    VERT, parce que la carte `connectors` sort par le retour anticipe des cartes
    de contexte. Un garde qui ne couvre qu'une sortie sur deux rassure sur la
    moitie qu'il ne regarde pas -- et c'est la moitie que toute carte KPI
    emprunte.
    """
    seen: dict = {}

    def _evaluator(envelope, project_id, *, date_from=None, date_to=None):
        seen["window"] = (date_from, date_to)
        envelope.setdefault("meta", {}).setdefault("freshness", {})[
            "stale_since_evaluated"
        ] = True
        return envelope

    rows = [
        {
            "date": "2026-07-01",
            "metric": "sessions",
            "value": 100,
            "connector": "google-analytics",
            "pull_id": "pull_EXAMPLE",
        }
    ]
    with (
        patch("core.warehouse.query_daily_report", return_value=rows),
        patch("core.health_enrichment.enrich_envelope_with_health", _evaluator),
    ):
        _summary, envelope, _uri = cards_module.get_card(
            [], "proj_EXAMPLE", metrics=["sessions"],
            date_from="2026-07-01", date_to="2026-07-06",
        )

    assert seen["window"] == ("2026-07-01", "2026-07-06")
    assert envelope["meta"]["freshness"]["stale_since_evaluated"] is True


def test_an_evaluator_that_cannot_answer_leaves_the_honest_false():
    """Base injoignable : la carte repond, et dit que personne n'a regarde.

    C'est la moitie qu'il serait tentant d'oublier. Une carte qui ne peut pas
    enoncer sa fraicheur repond quand meme a sa question -- mais elle ne doit pas
    laisser un null passer pour « evalue, et frais » (README.md:123,
    invariant 8).
    """

    def _evaluator(envelope, project_id, *, date_from=None, date_to=None):
        raise RuntimeError("platform database unreachable")

    envelope = _envelope_of(_evaluator)

    assert envelope["meta"]["freshness"]["stale_since_evaluated"] is False
