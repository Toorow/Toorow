"""Tests pour les préférences source de vérification (Story 17.1).

Tests :
  (a) API admin create/patch avec les 3 champs contre live Postgres (AI-37,
      contraintes réelles) : valides, invalides (type inconnu 422 FR), round-trip GET.
  (b) Seam build_asgi_app multi-projets : le champ est scopé projet (AD-5).
  (c) Validation unitaire (sans DB) : les fonctions helpers de validation.

Stratégie :
  - Tests live Postgres (pytest.mark.skipif si non disponible) pour (a) et (b).
  - Tests mocked pour (c) : pas de DB.
  - HEALTH_POLLER_ENABLED=false, SCHEDULER_ENABLED=false, QUEUE_WORKER_ENABLED=false.

Règles respectées :
  - AI-37 : les chemins de contraintes (CHECK, FK) sont testés sur la vraie DB.
  - AI-45/AI-56 : seam build_asgi_app (tests b).
  - AD-5 : dénégation cross-projet sur verification_source_id.
  - Messages d'erreur 422 en français vérifiés.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Helpers communs
# ---------------------------------------------------------------------------

_AUTH = ("core.admin_api._check_auth", (True, "tester@example.com"))


def _pg_reachable() -> bool:
    """Probe the opt-in live database without hanging test collection."""
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _post_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _patch_request(project_id: str, body: dict) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


def _get_request(project_id: str) -> MagicMock:
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    return req


def _drop_project(project_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.project_preferences WHERE project_id = %s",
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.tenant_key_audit WHERE project_id = %s",
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.alert_firings WHERE project_id = %s",
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.connection_ref WHERE project_id = %s",
                (project_id,),
            )
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        conn.commit()


def _drop_datastream(ds_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.datastreams WHERE id = %s", (ds_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# (a) Tests live Postgres -- create / patch / GET avec les 3 champs
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_create_project_with_verification_source_ga4():
    """CREATE avec verification_source_type='ga4' + lead_event_name persiste en DB."""
    from core.admin_api import _create_project, _get_project

    slug = f"vs-create-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(
                _post_request(
                    {
                        "name": "VS Create Test",
                        "slug": slug,
                        "verification_source_type": "ga4",
                        "verification_source_id": "ds_fake_001",
                        "lead_event_name": "generate_lead",
                    }
                )
            )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.body}"
        body = json.loads(resp.body)
        pid = body["id"]

        # Vérifier que les 3 champs sont dans la réponse CREATE.
        assert body["verification_source_type"] == "ga4"
        assert body["verification_source_id"] == "ds_fake_001"
        assert body["lead_event_name"] == "generate_lead"

        # Round-trip GET : les champs doivent être lisibles.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            get_resp = await _get_project(_get_request(pid))
        assert get_resp.status_code == 200
        get_body = json.loads(get_resp.body)
        assert get_body["verification_source_type"] == "ga4"
        assert get_body["verification_source_id"] == "ds_fake_001"
        assert get_body["lead_event_name"] == "generate_lead"
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_create_project_verification_source_shopify():
    """CREATE avec verification_source_type='shopify' (pas de lead_event_name requis)."""
    from core.admin_api import _create_project

    slug = f"vs-shopify-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _create_project(
                _post_request(
                    {
                        "name": "VS Shopify",
                        "slug": slug,
                        "verification_source_type": "shopify",
                        "verification_source_id": "ds_shopify_001",
                    }
                )
            )
        assert resp.status_code == 201
        body = json.loads(resp.body)
        pid = body["id"]
        assert body["verification_source_type"] == "shopify"
        assert body["verification_source_id"] == "ds_shopify_001"
        # lead_event_name non fourni -> None
        assert body["lead_event_name"] is None
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_create_project_invalid_verification_source_type_422_fr():
    """CREATE avec un type inconnu retourne 422 avec message en français."""
    from core.admin_api import _create_project

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(
            _post_request(
                {
                    "name": "Bad VS Type",
                    "verification_source_type": "facebook",
                }
            )
        )
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["code"] == "invalid_input"
    # Message en français avec les valeurs acceptées mentionnées.
    assert "ga4" in body["message"] or "shopify" in body["message"]
    # Doit contenir au moins un mot français.
    french_words = ["source", "type", "valeurs", "vérification"]
    assert any(word in body["message"].lower() for word in french_words)


@pg_available
@pytest.mark.anyio
async def test_create_project_ga4_without_lead_event_name_empty_422():
    """CREATE avec type='ga4' et lead_event_name vide -> 422 français."""
    from core.admin_api import _create_project

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _create_project(
            _post_request(
                {
                    "name": "GA4 No Lead",
                    "verification_source_type": "ga4",
                    "lead_event_name": "",  # vide explicitement
                }
            )
        )
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["code"] == "invalid_input"
    assert "lead_event_name" in body["message"] or "lead" in body["message"].lower()


@pg_available
@pytest.mark.anyio
async def test_patch_project_sets_verification_source():
    """PATCH sur un projet existant persiste les 3 champs et les renvoie."""
    from core.admin_api import _create_project, _get_project, _patch_project

    slug = f"vs-patch-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            create_resp = await _create_project(
                _post_request({"name": "VS Patch Test", "slug": slug})
            )
        assert create_resp.status_code == 201
        pid = json.loads(create_resp.body)["id"]

        # PATCH avec les 3 champs.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            patch_resp = await _patch_project(
                _patch_request(
                    pid,
                    {
                        "verification_source_type": "ga4",
                        "verification_source_id": "ds_ga4_stream",
                        "lead_event_name": "form_submit",
                    },
                )
            )
        assert patch_resp.status_code == 200, f"Expected 200: {patch_resp.body}"
        patch_body = json.loads(patch_resp.body)
        assert patch_body["verification_source_type"] == "ga4"
        assert patch_body["verification_source_id"] == "ds_ga4_stream"
        assert patch_body["lead_event_name"] == "form_submit"

        # GET round-trip.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            get_resp = await _get_project(_get_request(pid))
        get_body = json.loads(get_resp.body)
        assert get_body["verification_source_type"] == "ga4"
        assert get_body["lead_event_name"] == "form_submit"
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_patch_clear_lead_event_name_with_existing_ga4_422():
    """review-17-1 F-2 : PATCH {"lead_event_name": ""} SEUL sur un projet déjà en
    type='ga4' doit être refusé 422 — la cohérence se valide sur l'ÉTAT FINAL
    (existant + patch), sinon la DB finirait en ga4 + lead NULL (état interdit par l'AC).
    """
    from core.admin_api import _create_project, _get_project, _patch_project

    slug = f"vs-clear-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "VS Clear Lead", "slug": slug}))
        pid = json.loads(r.body)["id"]

        with patch(_AUTH[0], return_value=_AUTH[1]):
            setup = await _patch_project(
                _patch_request(
                    pid,
                    {"verification_source_type": "ga4", "lead_event_name": "generate_lead"},
                )
            )
        assert setup.status_code == 200

        # Le PATCH pernicieux : vider lead_event_name SANS toucher au type.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(_patch_request(pid, {"lead_event_name": ""}))
        assert resp.status_code == 422, f"Expected 422: {resp.body}"
        body = json.loads(resp.body)
        assert body["code"] == "invalid_input"
        assert "lead_event_name" in body["message"]

        # L'état DB n'a PAS été modifié (rollback) : toujours ga4 + generate_lead.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            get_resp = await _get_project(_get_request(pid))
        get_body = json.loads(get_resp.body)
        assert get_body["verification_source_type"] == "ga4"
        assert get_body["lead_event_name"] == "generate_lead"
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_patch_clear_vstype_also_clears_id_and_lead():
    """review-17-1 F-6 : PATCH {"verification_source_type": null} nettoie aussi
    verification_source_id et lead_event_name — pas de préférences orphelines en DB.
    """
    from core.admin_api import _create_project, _get_project, _patch_project

    slug = f"vs-null-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "VS Null Clean", "slug": slug}))
        pid = json.loads(r.body)["id"]

        with patch(_AUTH[0], return_value=_AUTH[1]):
            setup = await _patch_project(
                _patch_request(
                    pid,
                    {
                        "verification_source_type": "ga4",
                        "verification_source_id": "ds_ga4_stream",
                        "lead_event_name": "generate_lead",
                    },
                )
            )
        assert setup.status_code == 200

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(_patch_request(pid, {"verification_source_type": None}))
        assert resp.status_code == 200, f"Expected 200: {resp.body}"
        body = json.loads(resp.body)
        assert body["verification_source_type"] is None
        assert body["verification_source_id"] is None
        assert body["lead_event_name"] is None

        with patch(_AUTH[0], return_value=_AUTH[1]):
            get_resp = await _get_project(_get_request(pid))
        get_body = json.loads(get_resp.body)
        assert get_body["verification_source_type"] is None
        assert get_body["verification_source_id"] is None
        assert get_body["lead_event_name"] is None
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_patch_project_invalid_vstype_422_fr():
    """PATCH avec type inconnu retourne 422 avec message français."""
    from core.admin_api import _create_project, _patch_project

    slug = f"vs-bad-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "VS Bad Patch", "slug": slug}))
        pid = json.loads(r.body)["id"]

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(
                _patch_request(pid, {"verification_source_type": "twitter"})
            )
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert body["code"] == "invalid_input"
        assert any(
            word in body["message"].lower()
            for word in ["source", "type", "vérification", "valeurs"]
        )
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_patch_project_ga4_empty_lead_event_name_422():
    """PATCH type='ga4' avec lead_event_name vide explicite -> 422."""
    from core.admin_api import _create_project, _patch_project

    slug = f"vs-galen-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "GA4 Lead Empty", "slug": slug}))
        pid = json.loads(r.body)["id"]

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(
                _patch_request(
                    pid,
                    {
                        "verification_source_type": "ga4",
                        "lead_event_name": "",
                    },
                )
            )
        assert resp.status_code == 422
        body = json.loads(resp.body)
        assert body["code"] == "invalid_input"
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_get_project_without_verification_prefs_returns_none_fields():
    """GET d'un projet sans préférences VS retourne les 3 champs à None."""
    from core.admin_api import _create_project, _get_project

    slug = f"vs-none-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "No VS", "slug": slug}))
        pid = json.loads(r.body)["id"]

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _get_project(_get_request(pid))
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "verification_source_type" in body
        assert body["verification_source_type"] is None
        assert body["verification_source_id"] is None
        assert body["lead_event_name"] is None
    finally:
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_patch_project_stripe_declarative_accepted():
    """PATCH type='stripe' accepté même sans connecteur stripe (préférence déclarative)."""
    from core.admin_api import _create_project, _patch_project

    slug = f"vs-stripe-{uuid.uuid4().hex[:8]}"
    pid = None
    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Stripe VS", "slug": slug}))
        pid = json.loads(r.body)["id"]

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(
                _patch_request(
                    pid,
                    {
                        "verification_source_type": "stripe",
                        "verification_source_id": "ds_stripe_001",
                    },
                )
            )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["verification_source_type"] == "stripe"
    finally:
        if pid:
            _drop_project(pid)


# ---------------------------------------------------------------------------
# (b) Seam build_asgi_app multi-projets : scoping AD-5 (AI-56)
# ---------------------------------------------------------------------------


@pg_available
@pytest.mark.anyio
async def test_seam_patch_cross_project_vsid_denied():
    """AD-5: PATCH verification_source_id d'un autre projet retourne 403.

    Crée 2 projets et 1 datastream appartenant au projet A.
    PATCH projet B avec le datastream du projet A -> 403 (AI-56 seam AD-5).
    """
    from core.admin_api import _create_project, _patch_project
    from core.db import get_connection

    slug_a = f"scope-a-{uuid.uuid4().hex[:8]}"
    slug_b = f"scope-b-{uuid.uuid4().hex[:8]}"
    pid_a = pid_b = ds_id = None

    try:
        # Créer projet A.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            ra = await _create_project(_post_request({"name": "Scope A", "slug": slug_a}))
        assert ra.status_code == 201
        pid_a = json.loads(ra.body)["id"]

        # Créer projet B.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            rb = await _create_project(_post_request({"name": "Scope B", "slug": slug_b}))
        assert rb.status_code == 201
        pid_b = json.loads(rb.body)["id"]

        # Créer un datastream appartenant à projet A.
        ds_id = f"ds_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, created_by, org_id)
                    VALUES (%s, %s, %s, 'google-analytics', 'test', 'org_test_fixture')
                    """,
                    (ds_id, pid_a, f"stream-{uuid.uuid4().hex[:6]}"),
                )
            conn.commit()

        # Tenter de patcher projet B avec le datastream de projet A.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(
                _patch_request(
                    pid_b,
                    {
                        "verification_source_type": "ga4",
                        "verification_source_id": ds_id,  # appartient à pid_a !
                        "lead_event_name": "generate_lead",
                    },
                )
            )
        assert resp.status_code == 403, (
            f"Attendu 403 (cross-project denied), obtenu {resp.status_code}: {resp.body}"
        )
        body = json.loads(resp.body)
        assert body["code"] == "forbidden"
        # Message en français.
        assert any(word in body["message"].lower() for word in ["projet", "appartient", "source"])
    finally:
        if ds_id:
            _drop_datastream(ds_id)
        if pid_a:
            _drop_project(pid_a)
        if pid_b:
            _drop_project(pid_b)


@pg_available
@pytest.mark.anyio
async def test_seam_patch_same_project_vsid_accepted():
    """AD-5: PATCH verification_source_id du même projet est accepté (AI-56)."""
    from core.admin_api import _create_project, _patch_project
    from core.db import get_connection

    slug = f"scope-same-{uuid.uuid4().hex[:8]}"
    pid = ds_id = None

    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            r = await _create_project(_post_request({"name": "Scope Same", "slug": slug}))
        pid = json.loads(r.body)["id"]

        ds_id = f"ds_{uuid.uuid4().hex[:12]}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, created_by, org_id)
                    VALUES (%s, %s, %s, 'google-analytics', 'test', 'org_test_fixture')
                    """,
                    (ds_id, pid, f"stream-{uuid.uuid4().hex[:6]}"),
                )
            conn.commit()

        with patch(_AUTH[0], return_value=_AUTH[1]):
            resp = await _patch_project(
                _patch_request(
                    pid,
                    {
                        "verification_source_type": "ga4",
                        "verification_source_id": ds_id,
                        "lead_event_name": "generate_lead",
                    },
                )
            )
        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert body["verification_source_type"] == "ga4"
        assert body["verification_source_id"] == ds_id
    finally:
        if ds_id:
            _drop_datastream(ds_id)
        if pid:
            _drop_project(pid)


@pg_available
@pytest.mark.anyio
async def test_seam_multi_project_isolation():
    """AD-5/AI-45: les préférences VS sont bien scopées par projet (deux projets indépendants)."""
    from core.admin_api import _create_project, _get_project, _patch_project

    slug_x = f"iso-x-{uuid.uuid4().hex[:8]}"
    slug_y = f"iso-y-{uuid.uuid4().hex[:8]}"
    pid_x = pid_y = None

    try:
        with patch(_AUTH[0], return_value=_AUTH[1]):
            rx = await _create_project(_post_request({"name": "Iso X", "slug": slug_x}))
            ry = await _create_project(_post_request({"name": "Iso Y", "slug": slug_y}))
        pid_x = json.loads(rx.body)["id"]
        pid_y = json.loads(ry.body)["id"]

        # Configurer VS pour projet X seulement.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            await _patch_project(
                _patch_request(
                    pid_x,
                    {
                        "verification_source_type": "shopify",
                        "verification_source_id": "ds_shop_x",
                    },
                )
            )

        # Projet Y ne doit pas avoir les préférences de X.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            get_y = await _get_project(_get_request(pid_y))
        body_y = json.loads(get_y.body)
        assert body_y["verification_source_type"] is None, (
            "Le projet Y ne doit pas hériter des préférences du projet X."
        )

        # Projet X doit avoir ses préférences.
        with patch(_AUTH[0], return_value=_AUTH[1]):
            get_x = await _get_project(_get_request(pid_x))
        body_x = json.loads(get_x.body)
        assert body_x["verification_source_type"] == "shopify"
    finally:
        if pid_x:
            _drop_project(pid_x)
        if pid_y:
            _drop_project(pid_y)


# ---------------------------------------------------------------------------
# (c) Tests unitaires (sans DB) -- validation des helpers
# ---------------------------------------------------------------------------


class TestValidateVerificationSourceFields:
    """Tests unitaires pour _validate_verification_source_fields (sans DB)."""

    def test_empty_body_returns_empty_fields(self):
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields({})
        assert fields == {}
        assert err is None

    def test_valid_ga4_type(self):
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields(
            {
                "verification_source_type": "ga4",
                "verification_source_id": "ds_abc",
                "lead_event_name": "generate_lead",
            }
        )
        assert err is None
        assert fields["verification_source_type"] == "ga4"
        assert fields["verification_source_id"] == "ds_abc"
        assert fields["lead_event_name"] == "generate_lead"

    def test_valid_shopify_type(self):
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields({"verification_source_type": "shopify"})
        assert err is None
        assert fields["verification_source_type"] == "shopify"

    def test_valid_stripe_type(self):
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields({"verification_source_type": "stripe"})
        assert err is None
        assert fields["verification_source_type"] == "stripe"

    def test_invalid_type_returns_422(self):
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields({"verification_source_type": "facebook"})
        assert fields is None
        assert err is not None
        assert err.status_code == 422
        body = json.loads(err.body)
        assert body["code"] == "invalid_input"
        # Message français
        assert any(
            kw in body["message"] for kw in ["ga4", "shopify", "stripe", "vérification", "source"]
        )

    def test_ga4_with_empty_lead_event_name_422(self):
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields(
            {
                "verification_source_type": "ga4",
                "lead_event_name": "",
            }
        )
        assert fields is None
        assert err is not None
        assert err.status_code == 422
        body = json.loads(err.body)
        assert "lead_event_name" in body["message"] or "lead" in body["message"].lower()

    def test_null_type_is_valid(self):
        """None = déactiver la source de vérification (opt-out)."""
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields({"verification_source_type": None})
        assert err is None
        assert fields["verification_source_type"] is None

    def test_type_case_insensitive(self):
        """Le type est normalisé en minuscules."""
        from core.admin_api import _validate_verification_source_fields

        fields, err = _validate_verification_source_fields({"verification_source_type": "GA4"})
        assert err is None
        assert fields["verification_source_type"] == "ga4"
