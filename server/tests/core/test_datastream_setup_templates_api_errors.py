"""Ce que la porte REST des templates fait d'une exception, connue ou non.

Ce module se declare une COPIE de `datastream_preconfiguration_api` -- meme seam
d'autorisation, meme discipline d'idempotence, meme cartographie d'erreur. Une
copie qui n'est pas testee est une copie qui derive : les deux avalements
silencieux mesures le 2026-08-04 en production (`_error` rendant un 503 sans
trace, `_authorize` transformant toute panne en 404) sont exactement ce qu'une
copie non couverte reintroduit sans que personne ne le voie.

Ces tests epinglent donc, sur CE module, les quatre reponses qu'il promet :

  * l'inconnu -> 503 JOURNALISE avec sa trace, et un corps client muet ;
  * le refus nomme -> 409 avec le code de l'exception, pas un code generique ;
  * la validation -> 422 avec le code PORTE par l'exception (`422
    no_recorded_operator_input` est au contrat serveur de la story) ;
  * << pas pu evaluer >> -> 503 journalise, jamais 404 ; le refus ordinaire, lui,
    reste un 404 de non-divulgation et ne pollue pas les journaux.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from core import datastream_setup_templates_api as api
from core.datastream_setup_templates import (
    NoRecordedOperatorInput,
    TemplateConflictLabel,
    TemplateLimitReached,
    TemplateNotFound,
    TemplateValidationError,
)


class _Boom(Exception):
    """Une exception qu'aucune branche de `_error` ne connait."""


def _body(response) -> dict:
    return json.loads(bytes(response.body))


class _Request:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.path_params = {"project_id": "proj_EXAMPLE"}
        self.headers = headers or {}


def _patch_auth(monkeypatch, *, allowed):
    async def _check_auth(_request):
        return True, "person_EXAMPLE"

    import core.admin_api as admin_api
    import core.db as db

    @contextlib.contextmanager
    def _fake_connection():
        yield object()

    # Sans ce faux, `get_connection()` echoue AVANT la garde testee, et le test
    # mesurerait l'absence de base au lieu du comportement vise.
    monkeypatch.setattr(db, "get_connection", _fake_connection, raising=False)
    monkeypatch.setattr(admin_api, "_check_auth", _check_auth, raising=False)
    monkeypatch.setattr(admin_api, "_strict_project_capability_allowed", allowed, raising=False)


def test_an_unmapped_exception_is_logged_with_its_traceback(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        response = api._error(_Boom("le detail qui manquait"))

    assert response.status_code == 503
    assert _body(response)["code"] == "setup_templates_unavailable"
    assert "unmapped_error" in caplog.text
    assert "_Boom" in caplog.text
    assert "le detail qui manquait" in caplog.text


def test_the_client_body_stays_mute(caplog) -> None:
    """Journaliser n'est pas divulguer."""
    with caplog.at_level(logging.ERROR):
        response = api._error(_Boom("secret-interne-ne-doit-pas-sortir"))

    assert b"secret-interne-ne-doit-pas-sortir" not in bytes(response.body)
    assert _body(response)["message"] == "Setup templates are unavailable"


def test_each_named_refusal_keeps_its_own_code(caplog) -> None:
    """LES CODES SONT LE CONTRAT, et un code generique les efface tous.

    L'ecran distingue trois refus par leur code : la limite dit quoi retirer, le
    nom en double dit qu'il est pris, et l'entree operateur absente dit que ce
    Datastream n'a pas ete cree par l'assistant. Aplatis en `invalid_request`,
    les trois deviennent la meme phrase inutile.
    """
    with caplog.at_level(logging.ERROR):
        limit = api._error(TemplateLimitReached("dix deja"))
        duplicate = api._error(TemplateConflictLabel("nom pris"))
        norigin = api._error(NoRecordedOperatorInput("pas d'entree operateur"))
        invalid = api._error(TemplateValidationError("label trop long"))
        missing = api._error(TemplateNotFound("inconnu"))

    assert (limit.status_code, _body(limit)["code"]) == (409, "setup_template_limit_reached")
    assert (duplicate.status_code, _body(duplicate)["code"]) == (409, "duplicate_label")
    assert (norigin.status_code, _body(norigin)["code"]) == (422, "no_recorded_operator_input")
    assert (invalid.status_code, _body(invalid)["code"]) == (422, "invalid_setup_template")
    assert missing.status_code == 404
    # Aucun de ces cinq n'est un incident : le fourre-tout ne les a pas avales.
    assert "unmapped_error" not in caplog.text


def test_a_write_without_an_idempotency_key_is_refused_before_anything_is_read() -> None:
    """La meme discipline que toutes les ecritures de cette surface."""
    assert api._key(_Request()).status_code == 422
    assert _body(api._key(_Request()))["code"] == "missing_idempotency_key"
    assert api._key(_Request({"Idempotency-Key": "x" * 201})).status_code == 422
    assert api._key(_Request({"Idempotency-Key": "request-1"})) == "request-1"


def test_a_failed_capability_check_is_503_and_logged_not_404(monkeypatch, caplog) -> None:
    """<< Pas pu evaluer >> n'est pas << n'existe pas >>."""

    def _raises(*_args, **_kwargs):
        raise _Boom("la base ne repond pas")

    _patch_auth(monkeypatch, allowed=_raises)

    with caplog.at_level(logging.ERROR):
        response = asyncio.run(api._authorize(_Request(), "edit"))

    assert response.status_code == 503
    assert _body(response)["code"] == "setup_templates_unavailable"
    assert "capability_check_failed" in caplog.text
    assert "proj_EXAMPLE" in caplog.text


def test_a_plain_denial_still_answers_404(monkeypatch, caplog) -> None:
    """La NON-DIVULGATION reste : un refus ordinaire ne dit pas qu'il refuse."""

    def _denied(*_args, **_kwargs):
        return False

    _patch_auth(monkeypatch, allowed=_denied)

    with caplog.at_level(logging.ERROR):
        response = asyncio.run(api._authorize(_Request(), "view"))

    assert response.status_code == 404
    assert "capability_check_failed" not in caplog.text


def test_the_three_doors_are_the_three_the_contract_names() -> None:
    """Et AUCUNE porte << appliquer >>.

    Appliquer un template est un `PATCH` sur un brouillon de setup, une route qui
    existe deja et qui accepte un `operator_input` entier. Une quatrieme route
    ici mettrait la meme regle a deux endroits.
    """
    doors = {(route.path, tuple(sorted(route.methods - {"HEAD"})))
             for route in api.datastream_setup_templates_routes}

    assert doors == {
        ("/api/projects/{project_id}/datastream-setup-templates", ("GET",)),
        ("/api/projects/{project_id}/datastream-setup-templates", ("POST",)),
        ("/api/projects/{project_id}/datastream-setup-templates/{template_ref}", ("DELETE",)),
    }
