"""Ce que la couche REST fait d'une exception qu'elle NE CONNAIT PAS.

Mesure du 2026-08-04, en production : la console rendait
<< Setup could not continue -- Datastream setup is unavailable >> pendant que
`PATCH /api/projects/{id}/datastream-setup-draft` echouait en boucle. Les
journaux Cloud Run ne portaient QUE la ligne HTTP -- 503, aucune trace
applicative. Le defaut etait indiagnosticable par construction : ni la personne
devant l'ecran, ni celui qui lit les journaux, ne pouvaient savoir ce qui avait
casse.

Deux avalements silencieux vivaient dans le meme fichier :

  * ``_error`` rendait le 503 fourre-tout sans rien journaliser ;
  * ``_authorize`` transformait TOUTE exception en 404, donc une panne de base
    devenait << ca n'existe pas >> -- indiscernable d'un refus de droits.

Ces tests epinglent la reponse ET la trace. Le corps rendu au client reste
volontairement muet : ce n'est pas ce qui change.
"""

from __future__ import annotations

import asyncio
import json
import logging

from core import datastream_preconfiguration_api as api


class _Boom(Exception):
    """Une exception qu'aucune branche de `_error` ne connait."""


def _body(response) -> dict:
    return json.loads(bytes(response.body))


def test_an_unmapped_exception_is_logged_with_its_traceback(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        response = api._error(_Boom("le detail qui manquait"))

    assert response.status_code == 503
    assert _body(response)["code"] == "preconfiguration_unavailable"
    # La trace est ce qui manquait : sans elle, le 503 est un mur.
    assert "unmapped_error" in caplog.text
    assert "_Boom" in caplog.text
    assert "le detail qui manquait" in caplog.text


def test_the_client_body_stays_mute(caplog) -> None:
    """Journaliser n'est pas divulguer.

    Le message rendu au navigateur ne doit pas se mettre a porter le detail
    interne -- c'est la trace serveur qui le porte, et elle seule.
    """
    with caplog.at_level(logging.ERROR):
        response = api._error(_Boom("secret-interne-ne-doit-pas-sortir"))

    assert b"secret-interne-ne-doit-pas-sortir" not in bytes(response.body)
    assert _body(response)["message"] == "Datastream setup is unavailable"


def test_a_known_exception_is_still_mapped_and_not_logged_as_unmapped(caplog) -> None:
    """Le fourre-tout ne doit pas avaler ce que les branches savent traiter."""
    from core.datastream_preconfiguration import PreconfigurationValidationError

    with caplog.at_level(logging.ERROR):
        response = api._error(PreconfigurationValidationError("champ absent"))

    assert response.status_code == 422
    assert _body(response)["code"] == "invalid_request"
    assert "unmapped_error" not in caplog.text


class _Request:
    def __init__(self) -> None:
        self.path_params = {"project_id": "proj_EXAMPLE"}
        self.headers: dict[str, str] = {}


def test_a_failed_capability_check_is_503_and_logged_not_404(monkeypatch, caplog) -> None:
    """<< Pas pu evaluer >> n'est pas << n'existe pas >>.

    Rendre 404 sur une panne fait DISPARAITRE un objet qui existe, sans une
    ligne de journal pour contredire l'ecran.
    """

    async def _check_auth(_request):
        return True, "person_EXAMPLE"

    def _allowed(*_args, **_kwargs):
        raise _Boom("la base ne repond pas")

    import contextlib

    import core.admin_api as admin_api
    import core.db as db

    @contextlib.contextmanager
    def _fake_connection():
        yield object()

    # Sans ce faux, `get_connection()` echoue AVANT la garde qu'on teste, et le
    # test mesurerait l'absence de base au lieu du comportement vise.
    monkeypatch.setattr(db, "get_connection", _fake_connection, raising=False)
    monkeypatch.setattr(admin_api, "_check_auth", _check_auth, raising=False)
    monkeypatch.setattr(admin_api, "_strict_project_capability_allowed", _allowed, raising=False)

    with caplog.at_level(logging.ERROR):
        response = asyncio.run(api._authorize(_Request(), "edit"))

    assert response.status_code == 503
    assert _body(response)["code"] == "preconfiguration_unavailable"
    assert "capability_check_failed" in caplog.text
    assert "proj_EXAMPLE" in caplog.text


def test_a_plain_denial_still_answers_404(monkeypatch, caplog) -> None:
    """La NON-DIVULGATION reste : un refus ordinaire ne dit pas qu'il refuse."""

    async def _check_auth(_request):
        return True, "person_EXAMPLE"

    def _denied(*_args, **_kwargs):
        return False

    import contextlib

    import core.admin_api as admin_api
    import core.db as db

    @contextlib.contextmanager
    def _fake_connection():
        yield object()

    # Sans ce faux, `get_connection()` echoue AVANT la garde qu'on teste, et le
    # test mesurerait l'absence de base au lieu du comportement vise.
    monkeypatch.setattr(db, "get_connection", _fake_connection, raising=False)
    monkeypatch.setattr(admin_api, "_check_auth", _check_auth, raising=False)
    monkeypatch.setattr(admin_api, "_strict_project_capability_allowed", _denied, raising=False)

    with caplog.at_level(logging.ERROR):
        response = asyncio.run(api._authorize(_Request(), "edit"))

    assert response.status_code == 404
    # Un refus ordinaire n'est pas un incident : il ne pollue pas les journaux.
    assert "capability_check_failed" not in caplog.text
