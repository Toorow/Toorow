"""Deux exécutions d'un même Notebook par la porte MCP sont DEUX Runs.

CE QUE CE FICHIER TENAIT, ET OÙ CETTE PROPRIÉTÉ VIT MAINTENANT. Il prouvait
mécaniquement que la provenance est re-résolue à chaque exécution et jamais
recopiée de la ligne précédente (AD-9 / AD-7 / AC7), en jouant `run_notebook`
contre un Postgres mocké et un `render_report` mocké — c'est-à-dire contre le
moteur HÉRITÉ, celui qui lisait `app.notebooks` et écrivait `app.notebook_runs`.

Ce moteur a été retiré le 2026-08-22 (story 67.23) : la porte MCP appelle
désormais `analyze_artifacts.run_notebook`, le même service que la console. La
propriété n'est pas perdue, elle est gardée **mieux** — contre une vraie base, au
niveau qui la possède :

  * `test_analyze_artifacts_pg.py`,
    `test_a_notebook_run_pins_its_version_and_a_retry_returns_the_same_run`
    — la version est épinglée AVANT qu'un bloc s'exécute, et un rejeu de la même
    clef rend le Run d'origine plutôt qu'un doublon ;
  * `::test_one_idempotency_key_admits_exactly_one_run` — la contrainte est dans
    le schéma, pas seulement dans le code ;
  * `::test_a_run_cannot_be_rebound_to_another_notebook_version` — ce qu'un Run
    dit avoir joué ne peut pas changer après coup.

CE QUI RESTE ICI est la seule moitié qui appartient à la PORTE et à personne
d'autre : que deux appels distincts composent deux clefs distinctes. Une clef
constante rendrait le second appel idempotent avec le premier, donc un
utilisateur qui redemande une exécution en recevrait une ancienne — et aucun des
tests ci-dessus ne peut le voir, parce qu'ils reçoivent la clef en argument.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _conn_returning(row, columns):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.description = [(col,) for col in columns]
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cursor_cm)
    return conn


def _keys_of_two_calls(**call_kwargs) -> list[str]:
    """Les clefs d'idempotence composées par deux appels successifs de la porte."""
    from core.main import run_notebook

    seen: list[str] = []
    answer = {
        "run_id": "nbkrun_x",
        "notebook_version_id": "nbkv_1",
        "state": "accepted",
        "blocks": [],
    }

    def _capture(_conn, **kwargs):
        seen.append(kwargs["idempotency_key"])
        return answer

    with (
        patch(
            "core.db.get_connection",
            return_value=_conn_returning(("org_t", "proj_t"), ["org_id", "project_id"]),
        ),
        patch("core.notebook_mcp.refuse_unless_project_scope"),
        patch("core.analyze_artifacts.run_notebook", side_effect=_capture),
        patch("core.audit.write_audit_row"),
    ):
        run_notebook(notebook_id="nbk_TEST", **call_kwargs)
        run_notebook(notebook_id="nbk_TEST", **call_kwargs)
    return seen


def test_two_calls_of_the_same_notebook_are_two_runs_and_not_one_replayed():
    """Une clef constante rendrait la seconde demande idempotente avec la première.

    L'utilisateur redemanderait une exécution et recevrait l'ancienne, sans que
    rien ne le dise. Les tests du service ne peuvent pas l'attraper : ils
    reçoivent la clef en argument.
    """
    first, second = _keys_of_two_calls()
    assert first != second, (
        "les deux appels composent la MEME clef : le second serait servi par le "
        "Run du premier"
    )


def test_the_key_names_what_the_run_is_about():
    """Une clef opaque est indéboguable ; celle-ci porte son notebook et sa date."""
    key = _keys_of_two_calls(as_of="2026-08-01")[0]
    assert "nbk_TEST" in key
    assert "2026-08-01" in key


def test_an_as_of_replay_and_a_current_run_never_share_a_key():
    """Sinon rejouer au 1er août rendrait le Run d'aujourd'hui, ou l'inverse."""
    current = _keys_of_two_calls()[0]
    replay = _keys_of_two_calls(as_of="2026-08-01")[0]
    assert current.split(":")[:2] == replay.split(":")[:2]
    assert current != replay
