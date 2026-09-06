"""Story 22.11 / 22.19 / 22.23 -- la porte du template existe et elle est gardee.

AI-123 : `create_file_source_template` n'avait qu'un appelant,
`lock_adaptation_template`, qui n'en avait aucun. Ni route, ni outil MCP. Un
template ne pouvait donc naitre que par ecriture directe en base -- ce qui est
exactement l'origine de l'unique ligne de `app.file_source_templates` en
production (AI-89).

Ce que ces tests epinglent, dans l'ordre d'importance :

  1. la ROUTE existe et est montee (c'est la lecon d'AI-88 : une fonction
     qu'aucune route n'appelle ne sert a rien) ;
  2. elle est fail-closed AVANT tout travail -- pas d'auth, pas de role, pas de
     projet : meme enveloppe, et le magasin n'est jamais atteint ;
  3. elle ne REVALIDE rien elle-meme : un contrat invalide est refuse par
     `core.file_source_template`, et la route se contente de transporter son
     message. Un second validateur ici serait la faute que cet epic a deja
     commise deux fois.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

PROJECT_ID = "proj_EXAMPLE"
ORG_ID = "org_EXAMPLE"
_HDR = {"Authorization": "Bearer test-secret"}
_BASE = f"/api/projects/{PROJECT_ID}/file-source-templates"

VALID_CONTRACT = {
    "kind": "catalog",
    "format": "csv",
    "grain": "daily",
    "class": "actual",
    "placement": {"metric": "clicks", "period": "day", "dimension": []},
    "required_fields": ["day", "clicks"],
    "optional_fields": [],
}


def _conn(org_row=(ORG_ID,)):
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.execute = MagicMock(return_value=None)
    cursor.fetchone = MagicMock(return_value=org_row)
    cursor.fetchall = MagicMock(return_value=[])
    cursor.description = []

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.commit = MagicMock()
    conn.rollback = MagicMock()

    @contextmanager
    def _get_connection():
        yield conn

    return conn, _get_connection


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


@contextmanager
def _seam(*, authorized=True, role_ok=True, org_row=(ORG_ID,), store=None):
    conn, get_connection = _conn(org_row)
    role_response = None
    if not role_ok:
        from starlette.responses import JSONResponse

        role_response = JSONResponse({"code": "not_found", "message": "Not found"}, 404)
    patches = [
        patch(
            "core.admin_api._check_auth",
            new=AsyncMock(return_value=(authorized, "owner@example.com" if authorized else None)),
        ),
        patch("core.admin_api._require_datastream_role", return_value=role_response),
        patch("core.db.get_connection", new=get_connection),
    ]
    if store is not None:
        patches.append(patch("core.file_source_template_api.create_file_source_template", store))
    with patches[0], patches[1], patches[2]:
        if store is not None:
            with patches[3]:
                yield conn
        else:
            yield conn


# ---------------------------------------------------------------------------
# 1. La route existe et est montee.
# ---------------------------------------------------------------------------


def test_the_create_route_is_mounted_and_reaches_the_store():
    created = {"id": "fst_0123456789ABCDEFGH0123456J", "version": 1}
    store = MagicMock(return_value=created)
    with _seam(store=store):
        resp = _client().post(
            _BASE, headers=_HDR, json={"template_code": "MEDIA_PLAN", "contract": VALID_CONTRACT}
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == created["id"]
    store.assert_called_once()
    kwargs = store.call_args.kwargs
    assert kwargs["project_id"] == PROJECT_ID
    assert kwargs["org_id"] == ORG_ID
    # L'identite vient du jeton, jamais du corps : un corps ne signe rien.
    assert kwargs["created_by"] == "owner@example.com"


def test_the_route_commits_because_the_store_deliberately_does_not():
    """Le magasin documente que l'appelant possede la transaction.

    Une route qui l'oublierait validerait, ecrirait l'audit... et annulerait tout
    en silence. Le test regarde le commit, pas le code.
    """
    store = MagicMock(return_value={"id": "fst_X", "version": 1})
    with _seam(store=store) as conn:
        _client().post(
            _BASE, headers=_HDR, json={"template_code": "MEDIA_PLAN", "contract": VALID_CONTRACT}
        )
    assert conn.commit.called


def test_an_idempotency_key_reaches_the_store():
    """La replay-abilite est celle du magasin ; la route doit lui passer la cle."""
    store = MagicMock(return_value={"id": "fst_X", "version": 1})
    with _seam(store=store):
        _client().post(
            _BASE,
            headers={**_HDR, "Idempotency-Key": "idem-1"},
            json={"template_code": "MEDIA_PLAN", "contract": VALID_CONTRACT},
        )
    assert store.call_args.kwargs["idempotency_key"] == "idem-1"


# ---------------------------------------------------------------------------
# 2. Fail-closed AVANT tout travail.
# ---------------------------------------------------------------------------


def test_no_auth_never_reaches_the_store():
    store = MagicMock()
    with _seam(authorized=False, store=store):
        resp = _client().post(
            _BASE, headers=_HDR, json={"template_code": "X", "contract": VALID_CONTRACT}
        )
    assert resp.status_code == 401
    store.assert_not_called()


def test_an_insufficient_role_never_reaches_the_store():
    store = MagicMock()
    with _seam(role_ok=False, store=store):
        resp = _client().post(
            _BASE, headers=_HDR, json={"template_code": "X", "contract": VALID_CONTRACT}
        )
    assert resp.status_code == 404
    store.assert_not_called()


def test_an_unknown_project_answers_the_same_not_found():
    """Sonder cet endpoint ne doit rien reveler d'un autre projet (AD-5)."""
    store = MagicMock()
    with _seam(org_row=None, store=store):
        resp = _client().post(
            _BASE, headers=_HDR, json={"template_code": "X", "contract": VALID_CONTRACT}
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    store.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"contract": VALID_CONTRACT},
        {"template_code": "MEDIA_PLAN"},
        {"template_code": "  ", "contract": VALID_CONTRACT},
        {"template_code": "MEDIA_PLAN", "contract": "not-an-object"},
    ],
)
def test_a_malformed_body_is_refused_before_the_store(body):
    store = MagicMock()
    with _seam(store=store):
        resp = _client().post(_BASE, headers=_HDR, json=body)
    assert resp.status_code == 400
    store.assert_not_called()


# ---------------------------------------------------------------------------
# 3. La route ne revalide rien -- elle transporte le refus du magasin.
# ---------------------------------------------------------------------------


def test_an_invalid_contract_is_refused_by_the_store_and_named():
    """422 avec le message du magasin, pas un « invalid template » generique.

    La personne qui a ecrit le contrat est celle qui peut le corriger : lui
    cacher quelle cle est fautive la renvoie a deviner.
    """
    from core.file_source_template import FileSourceTemplateValidationError

    store = MagicMock(
        side_effect=FileSourceTemplateValidationError("'class' must be one of [...]")
    )
    with _seam(store=store):
        resp = _client().post(
            _BASE,
            headers=_HDR,
            json={"template_code": "MEDIA_PLAN", "contract": {"kind": "catalog"}},
        )
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["code"] == "invalid_contract"
    assert "class" in payload["message"]


def test_the_route_does_not_validate_the_contract_itself():
    """Un contrat vide part au magasin INTACT : c'est lui le juge.

    Si la route filtrait, elle deviendrait un second validateur -- la faute que
    cet epic a deja commise sur le gate (22.20) et sur le placement.
    """
    store = MagicMock(return_value={"id": "fst_X", "version": 1})
    with _seam(store=store):
        _client().post(
            _BASE, headers=_HDR, json={"template_code": "MEDIA_PLAN", "contract": {}}
        )
    assert store.call_args.kwargs["contract"] == {}


# ---------------------------------------------------------------------------
# Lecture.
# ---------------------------------------------------------------------------


def test_listing_requires_a_template_code():
    """Sans filtre, on listerait tout le parc de templates d'un projet."""
    with _seam():
        resp = _client().get(_BASE, headers=_HDR)
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_field"


def test_listing_returns_the_versions_of_one_code():
    versions = [{"id": "fst_A", "version": 1}, {"id": "fst_B", "version": 2}]
    with _seam():
        with patch(
            "core.file_source_template_api.list_file_source_template_versions",
            return_value=versions,
        ):
            resp = _client().get(f"{_BASE}?template_code=MEDIA_PLAN", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json()["versions"] == versions


def test_reading_one_version_by_code_and_version():
    with _seam():
        with patch(
            "core.file_source_template_api.get_file_source_template",
            return_value={"id": "fst_A", "version": 2},
        ) as reader:
            resp = _client().get(f"{_BASE}/MEDIA_PLAN/versions/2", headers=_HDR)
    assert resp.status_code == 200
    assert reader.call_args.kwargs["version"] == 2
    assert reader.call_args.kwargs["template_code"] == "MEDIA_PLAN"


def test_an_absent_version_answers_not_found():
    with _seam():
        with patch(
            "core.file_source_template_api.get_file_source_template", return_value=None
        ):
            resp = _client().get(f"{_BASE}/MEDIA_PLAN/versions/9", headers=_HDR)
    assert resp.status_code == 404


def test_a_non_integer_version_is_refused():
    with _seam():
        resp = _client().get(f"{_BASE}/MEDIA_PLAN/versions/latest", headers=_HDR)
    assert resp.status_code == 400
    assert json.loads(resp.text)["code"] == "invalid_version"


# ---------------------------------------------------------------------------
# Le catalogue : ce que le validateur accepte doit etre decouvrable.
# ---------------------------------------------------------------------------


def test_the_canonical_field_catalog_is_reachable():
    """Sans lui, la route de creation refuse des ids sans dire lesquels sont bons.

    `app.mdm_canonical_fields` n'etait interrogee QUE pour valider. Une personne
    pouvait s'entendre dire << ces ids ne sont pas des champs canoniques actifs >>
    et n'avait aucun moyen, nulle part dans le produit, de savoir lesquels
    l'etaient.
    """
    fields = [
        {"id": "mdm_A", "concept_kind": "dimension", "canonical_name": "Day"},
        {"id": "mdm_B", "concept_kind": "metric", "canonical_name": "Clicks"},
    ]
    with _seam():
        with patch(
            "core.file_source_template_api.list_canonical_fields", return_value=fields
        ) as reader:
            resp = _client().get(f"{_BASE}/canonical-fields", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json()["fields"] == fields
    assert reader.call_args.kwargs["project_id"] == PROJECT_ID


def test_the_catalog_segment_is_not_read_as_a_template_code():
    """`canonical-fields` est un segment litteral, pas un code de template.

    Declare apres `{template_code}`, Starlette l'aurait avale comme un code et le
    catalogue serait devenu injoignable -- sans erreur, juste une reponse d'un
    autre endpoint.
    """
    with _seam():
        with patch(
            "core.file_source_template_api.list_canonical_fields", return_value=[]
        ) as catalog:
            with patch(
                "core.file_source_template_api.list_file_source_template_versions",
                return_value=[{"id": "fst_X"}],
            ) as versions:
                resp = _client().get(f"{_BASE}/canonical-fields", headers=_HDR)
    assert resp.status_code == 200
    assert catalog.called
    assert not versions.called


def test_the_catalog_is_readable_by_a_viewer():
    """Lire le vocabulaire n'est pas ecrire avec : le role minimal suffit."""
    from core.admin_api import _require_datastream_role  # noqa: F401

    seen: list[str] = []

    def _role(project_id, identity, minimum_role, conn, **kwargs):
        seen.append(minimum_role)
        return None

    conn, get_connection = _conn()
    with patch(
        "core.admin_api._check_auth", new=AsyncMock(return_value=(True, "o@example.com"))
    ), patch("core.admin_api._require_datastream_role", new=_role), patch(
        "core.db.get_connection", new=get_connection
    ), patch("core.file_source_template_api.list_canonical_fields", return_value=[]):
        _client().get(f"{_BASE}/canonical-fields", headers=_HDR)
    assert seen == ["viewer"]

def test_generic_create_refuses_adaptation_without_human_gate():
    store = MagicMock()
    contract = {
        **VALID_CONTRACT,
        "kind": "adaptation",
        "py_source": "def adapt(i, t): return {'rows': [], 'rejected': []}",
    }
    with _seam(store=store):
        resp = _client().post(
            _BASE,
            headers=_HDR,
            json={"template_code": "BESPOKE", "contract": contract},
        )
    assert resp.status_code == 422
    assert resp.json()["code"] == "gate_confirmation_required"
    store.assert_not_called()


def test_confirmation_routes_are_mounted_and_validate_before_writing():
    with _seam():
        mapping = _client().post(f"{_BASE}/confirm", headers=_HDR, json={})
        adaptation = _client().post(
            f"{_BASE}/adaptations/confirm", headers=_HDR, json={}
        )
    assert mapping.status_code == 400
    assert adaptation.status_code == 400

def test_adaptation_confirmation_bounds_base64_before_decoding():
    body = {
        "file_base64": "!!!!!!!!",
        "template_code": "BESPOKE",
        "py_source": "def adapt(i, t): return {'rows': [], 'rejected': []}",
        "placement_class": "actual",
        "placement": {"metric": "mdm_cost", "period": "mdm_date"},
        "required_fields": ["mdm_cost"],
        "grain": "daily",
        "datastream_id": "ds_1",
        "mapping_version_id": "dmap_1",
    }
    with _seam(), patch("core.csv_excel_import.MAX_FILE_BYTES", 1):
        response = _client().post(
            f"{_BASE}/adaptations/confirm", headers=_HDR, json=body
        )
    assert response.status_code == 422
    assert response.json()["code"] == "file_too_large"


def test_mapping_confirmation_bounds_base64_before_decoding():
    with _seam(), patch("core.csv_excel_import.MAX_FILE_BYTES", 1):
        response = _client().post(
            f"{_BASE}/confirm",
            headers=_HDR,
            json={
                "datastream_id": "ds_1",
                "mapping_version_id": "dmap_1",
                "file_base64": "!!!!!!!!",
            },
        )
    assert response.status_code == 422
    assert response.json()["code"] == "file_too_large"


def test_the_resolution_vocabulary_includes_the_bound_templates_own_fields():
    """AI-321 (2026-08-29): the preview proposed `datastream_id -> datastream_id` as
    matched and the confirm door refused it -- the vocabulary was the MDM canonical
    fields alone, and no catalog Template field is one."""
    from unittest.mock import patch

    from core import file_source_template_api as api

    template = {"contract": {"required_fields": ["datastream_id", "media_date"],
                             "optional_fields": ["grp_value"]}}
    with patch.object(api, "list_canonical_fields", return_value=[{"id": "cost_eur"}]):
        vocabulary = api._resolution_vocabulary(
            object(), project_id="proj_EXAMPLE", template=template
        )
    assert vocabulary == {"cost_eur", "datastream_id", "media_date", "grp_value"}

    with patch.object(api, "list_canonical_fields", return_value=[{"id": "cost_eur"}]):
        bare = api._resolution_vocabulary(object(), project_id="proj_EXAMPLE", template=None)
    assert bare == {"cost_eur"}
