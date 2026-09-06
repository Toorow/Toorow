"""48.5 -- l'ecrivain des candidats observes a enfin un appelant de production.

MESURE DU 2026-08-04, qui est la raison de ce fichier :

    grep -rn record_observation server --include=*.py | grep -v /tests/
    -> entity_bindings.py:271 (la definition) et :940 (son nom dans __all__)
    SELECT count(*) FROM app.entity_observation_versions -> 0

Pendant ce temps `observation_summary` et `capability_compilers` LISENT cette
table pour afficher la couverture de source. La surface competitors ne pouvait
donc repondre qu'une chose -- << aucun candidat observe >> -- quoi qu'un
Datastream collecte. Un critere de completude ferme sur un ecrivain que rien
n'invoque : c'est ce que la review de 48.5 a refuse, et elle avait raison.

C'est le MEME defaut qu'AI-161 avait enregistre une story plus tot pour la preuve
de frontiere de journee, au MEME endroit du code. Ces tests tiennent les deux
moities : que l'orchestrateur ecrive ce qu'il a vu, et que la couture existe
vraiment dans `queue.py` -- sans quoi on aurait remplace un ecrivain orphelin par
un orchestrateur orphelin.

Tout OFFLINE : le lecteur d'entrepot et le magasin sont mockes.
"""

from __future__ import annotations

import os
import pathlib
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import entity_bindings as eb  # noqa: E402

_DECLARATION = {
    "reports": [
        {
            "report_id": "search_terms",
            "direction": "observe",
            "entity_kinds": ("brand",),
            "candidate_field_ids": ("advertiser_name",),
            "identity_field_id": "advertiser_id",
        }
    ]
}

_DATASTREAM = {
    "id": "ds_EXAMPLE",
    "project_id": "proj_EXAMPLE",
    "module_name": "example-connector",
    "config": {"source": {"report_id": "search_terms"}},
    "capability_fingerprint": "f" * 64,
}


def _conn_returning_org(org_id: str = "org_EXAMPLE") -> MagicMock:
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=(org_id,))
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _record_for(observed: dict, **kwargs) -> tuple[int, list]:
    calls: list = []

    def _fake_record(conn, **kw):
        calls.append(kw)
        return {"id": "eobs_1"}

    with patch.object(eb, "tracked_entity_declaration", return_value=_DECLARATION), patch(
        "core.verification.distinct_raw_values", return_value=observed
    ), patch.object(eb, "record_observation", _fake_record):
        written = eb.record_observations_for_pull(
            _conn_returning_org(),
            datastream=_DATASTREAM,
            project_id="proj_EXAMPLE",
            pull_id="pull_1",
            date_from="2026-03-01",
            date_to="2026-03-07",
            actor="worker",
            **kwargs,
        )
    return written, calls


# ---------------------------------------------------------------------------
# Ce qui est ecrit, et sous quelle identite
# ---------------------------------------------------------------------------


def test_each_distinct_observed_value_becomes_one_observation():
    written, calls = _record_for({"advertiser_name": {"Acme": 3, "Globex": 1}})
    assert written == 2
    assert {c["raw_value"] for c in calls} == {"Acme", "Globex"}
    # Le compte d'occurrences voyage : c'est lui qui fera un denominateur credible.
    assert {c["raw_value"]: c["occurrence_count"] for c in calls} == {"Acme": 3, "Globex": 1}


def test_the_observation_carries_the_pull_that_saw_it():
    """Sans le `pull_id`, l'observation ne peut pas etre rattachee a une execution
    -- et une preuve qu'on ne peut pas rejouer n'est pas une preuve."""
    _, calls = _record_for({"advertiser_name": {"Acme": 1}})
    assert calls[0]["pull_id"] == "pull_1"
    assert calls[0]["observed_from"] == "2026-03-01"
    assert calls[0]["observed_to"] == "2026-03-07"


def test_normalisation_is_casefold_only_and_never_an_identity_decision():
    """Decider que deux graphies sont la MEME entite appartient au registre, avec
    une confiance et une confirmation. Le faire ici serait le 5e `Incomplete if`
    de cette surface (<< source matches apply without confidence and
    confirmation >>). On garde donc la valeur source telle quelle en `display`."""
    _, calls = _record_for({"advertiser_name": {"  ACME Corp ": 1}})
    assert calls[0]["display_value"] == "  ACME Corp "
    assert calls[0]["normalized_value"] == "acme corp"


def test_the_org_is_resolved_rather_than_trusted_from_the_caller():
    """`get_datastream` ne rend pas d'org_id. Le resoudre ici plutot que de le
    faire passer evite qu'un appelant l'oublie -- une observation ecrite sous la
    mauvaise org serait lisible par le mauvais tenant."""
    _, calls = _record_for({"advertiser_name": {"Acme": 1}})
    assert calls[0]["org_id"] == "org_EXAMPLE"


# ---------------------------------------------------------------------------
# Ce qui n'est PAS ecrit -- les silences volontaires
# ---------------------------------------------------------------------------


def test_a_connector_that_declares_no_tracked_entity_records_nothing():
    with patch.object(eb, "tracked_entity_declaration", return_value=None), patch(
        "core.verification.distinct_raw_values"
    ) as reader:
        written = eb.record_observations_for_pull(
            MagicMock(), datastream=_DATASTREAM, project_id="p",
            pull_id="pull_1", date_from="2026-03-01", date_to="2026-03-07", actor="w",
        )
    assert written == 0
    # Et l'entrepot n'est meme pas interroge : une lecture par pull sur un
    # connecteur qui ne declare rien serait payee pour rien.
    reader.assert_not_called()


def test_an_empty_pull_writes_no_observation_at_all():
    """Zero valeur vue n'est PAS << une observation vide >>. Ecrire une ligne
    nulle ferait passer << rien vu ce jour-la >> pour << vu, et c'etait rien >>."""
    written, calls = _record_for({})
    assert written == 0
    assert calls == []


# ---------------------------------------------------------------------------
# La couture existe vraiment -- sinon on a juste deplace l'orphelin
# ---------------------------------------------------------------------------


def test_the_pull_path_actually_calls_the_recorder():
    """Le point du fichier. `record_observation` avait un test et zero appelant ;
    remplacer cela par un orchestrateur qui a un test et zero appelant n'aurait
    rien repare."""
    source = (
        pathlib.Path(__file__).resolve().parents[2] / "core" / "queue.py"
    ).read_text(encoding="utf-8")
    assert "record_observations_for_pull" in source
    # Best-effort comme ses deux voisines : un echec de gouvernance ne defait
    # jamais un pull qui a reussi.
    assert "entity_observations_failed" in source


def test_the_recorder_is_exported():
    assert "record_observations_for_pull" in eb.__all__
