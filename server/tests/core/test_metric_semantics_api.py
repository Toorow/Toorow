"""Tests for core.metric_semantics_api (Story 27.2).

Two layers:
  - Offline validation (no DB): guards, bad input, PLATFORM forbidden, contract shape.
  - Pg-gated (skipped when TEST_POSTGRES_DSN absent): full end-to-end on migration 049.

Pattern follows test_orgs_api.py.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import enrol_fixture_identity, purge_fixture_org

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Pg guard
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
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

# Shared auth patch target (core.admin_api._check_auth, same as datamodel_api).
_AUTH = "core.admin_api._check_auth"
_AUTH_OK = (True, "tester@example.com")
_AUTH_ANON = (False, "")

#: The canonical person `_create_test_org_pg` enrols, rebound on every setup.
_PG_CALLER = "admin@example.com"


# ---------------------------------------------------------------------------
# Request builders (mirrors test_orgs_api pattern)
# ---------------------------------------------------------------------------


def _get_request(params: dict | None = None, path_params: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.query_params = params or {}
    req.path_params = path_params or {}
    return req


def _post_request(body: dict, path_params: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.path_params = path_params or {}
    req.query_params = {}
    return req


def _delete_request(params: dict | None = None, path_params: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.query_params = params or {}
    req.path_params = path_params or {}
    return req


# ---------------------------------------------------------------------------
# Offline -- auth guard
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reference_requires_auth():
    from core.metric_semantics_api import _reference

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _reference(_get_request({"org_id": "org_x"}))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_mappings_requires_auth():
    from core.metric_semantics_api import _list_mappings

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _list_mappings(_get_request({"org_id": "org_x"}))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_confirm_requires_auth():
    from core.metric_semantics_api import _confirm_mapping

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _confirm_mapping(_post_request({"org_id": "org_x"}, {"id": "smm_x"}))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_rename_requires_auth():
    from core.metric_semantics_api import _rename_mapping

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _rename_mapping(
            _post_request({"org_id": "org_x", "canonical_name": "cost"}, {"id": "smm_x"})
        )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_reject_requires_auth():
    from core.metric_semantics_api import _reject_mapping

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _reject_mapping(_post_request({"org_id": "org_x"}, {"id": "smm_x"}))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_bootstrap_requires_auth():
    from core.metric_semantics_api import _trigger_bootstrap

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _trigger_bootstrap(_post_request({"org_id": "org_x"}))
    assert resp.status_code == 401



# ---------------------------------------------------------------------------
# Offline -- missing params
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bootstrap_missing_org_id():
    from core.metric_semantics_api import _trigger_bootstrap

    with patch(_AUTH, return_value=_AUTH_OK):
        resp = await _trigger_bootstrap(_post_request({}))
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_reference_missing_org_id():
    from core.metric_semantics_api import _reference

    with patch(_AUTH, return_value=_AUTH_OK):
        resp = await _reference(_get_request({}))
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_list_mappings_missing_org_id():
    from core.metric_semantics_api import _list_mappings

    with patch(_AUTH, return_value=_AUTH_OK):
        resp = await _list_mappings(_get_request({}))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Offline -- manage guard (403)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bootstrap_non_manage_forbidden():
    """identity_can_manage_org -> False => 403."""
    from core.metric_semantics_api import _trigger_bootstrap

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_can_manage_org", return_value=False),
        patch("core.db.get_connection") as mock_gc,
    ):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = mock_conn

        resp = await _trigger_bootstrap(_post_request({"org_id": "org_x"}))
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_confirm_non_manage_forbidden():
    from core.metric_semantics_api import _confirm_mapping

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_can_manage_org", return_value=False),
        patch("core.db.get_connection") as mock_gc,
    ):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = mock_conn

        resp = await _confirm_mapping(
            _post_request({"org_id": "org_x"}, path_params={"id": "smm_x"})
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Offline -- non-member org read guard (404)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reference_non_member_404():
    """identity_has_org_access -> False => 404 (existence not disclosed)."""
    from core.metric_semantics_api import _reference

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=False),
        patch("core.db.get_connection") as mock_gc,
    ):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = mock_conn

        resp = await _reference(_get_request({"org_id": "org_x"}))
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_list_mappings_non_member_404():
    from core.metric_semantics_api import _list_mappings

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=False),
        patch("core.db.get_connection") as mock_gc,
    ):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = mock_conn

        resp = await _list_mappings(_get_request({"org_id": "org_x"}))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Offline -- PLATFORM scope forbidden via API
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Offline -- rename with unknown canonical_name -> 422
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_rename_unknown_canonical_422():
    """rename with canonical_name not resolvable -> 422 (cible introuvable)."""
    from core.metric_semantics_api import (
        _rename_mapping,
    )

    fake_existing = {
        "id": "smm_x",
        "connector": "c",
        "source_field_path": "f",
        "metric_definition_id": "def_x",
        "scope_level": "ORG",
        "org_id": "org_x",
        "project_id": None,
        "extraction_note": None,
        "status": "proposed",
        "created_by": "s",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "canonical_name": "old_metric",
    }

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_can_manage_org", return_value=True),
        patch("core.db.get_connection") as mock_gc,
        patch("core.metric_semantics_api._get_mapping_by_id", return_value=fake_existing),
        patch("core.metric_semantics_api._resolve_definition_id", return_value=None),
    ):
        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = mock_conn

        resp = await _rename_mapping(
            _post_request(
                {"org_id": "org_x", "canonical_name": "no_such_metric"},
                path_params={"id": "smm_x"},
            )
        )
    assert resp.status_code == 422
    body = json.loads(resp.body)
    # English error message (the product's English ratchet)
    assert "not found" in body.get("message", "").lower()


# ---------------------------------------------------------------------------
# Offline -- cross-org authorization (Story 27.2 review fixes F-1/F-2/F-3)
#
# These negative cross-org tests ARE the fix: the IDOR / bypass holes shipped
# precisely because no test exercised the mismatch between the guarded org and
# the resource's actual org.
# ---------------------------------------------------------------------------


def _conn_mock():
    """A get_connection() context-manager mock (guards resolve True by default)."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: mock_conn
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


def _mapping_org_b() -> dict:
    """A confirmed-able ORG mapping that belongs to org_B (not the guarded org)."""
    return {
        "id": "smm_b",
        "connector": "meta-ads",
        "source_field_path": "spend",
        "metric_definition_id": "def_b",
        "scope_level": "ORG",
        "org_id": "org_B",
        "project_id": None,
        "extraction_note": None,
        "status": "proposed",
        "created_by": "system",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
        "canonical_name": "cost",
    }


@pytest.mark.anyio
async def test_idor_confirm_cross_org_404_no_upsert():
    """F-1: manage guard passes on org_A but mapping is org_B -> 404, no upsert."""
    from core.metric_semantics_api import _confirm_mapping

    upsert = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_can_manage_org", return_value=True),
        patch("core.db.get_connection", return_value=_conn_mock()),
        patch("core.metric_semantics_api._get_mapping_by_id", return_value=_mapping_org_b()),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        resp = await _confirm_mapping(
            _post_request({"org_id": "org_A"}, path_params={"id": "smm_b"})
        )
    assert resp.status_code == 404
    upsert.assert_not_called()


@pytest.mark.anyio
async def test_idor_rename_cross_org_404_no_upsert():
    """F-1: rename mapping of org_B while guarded on org_A -> 404, no upsert."""
    from core.metric_semantics_api import _rename_mapping

    upsert = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_can_manage_org", return_value=True),
        patch("core.db.get_connection", return_value=_conn_mock()),
        patch("core.metric_semantics_api._get_mapping_by_id", return_value=_mapping_org_b()),
        patch("core.metric_semantics_api._resolve_definition_id", return_value="def_target"),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        resp = await _rename_mapping(
            _post_request(
                {"org_id": "org_A", "canonical_name": "clicks"},
                path_params={"id": "smm_b"},
            )
        )
    assert resp.status_code == 404
    upsert.assert_not_called()


@pytest.mark.anyio
async def test_idor_reject_cross_org_404_no_upsert():
    """F-1: reject mapping of org_B while guarded on org_A -> 404, no upsert."""
    from core.metric_semantics_api import _reject_mapping

    upsert = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_can_manage_org", return_value=True),
        patch("core.db.get_connection", return_value=_conn_mock()),
        patch("core.metric_semantics_api._get_mapping_by_id", return_value=_mapping_org_b()),
        patch("core.metric_semantics.upsert_source_metric_mapping", upsert),
    ):
        resp = await _reject_mapping(
            _post_request({"org_id": "org_A"}, path_params={"id": "smm_b"})
        )
    assert resp.status_code == 404
    upsert.assert_not_called()






@pytest.mark.anyio
async def test_cross_org_reference_foreign_project_404():
    """F-3: /reference with a project_id belonging to another org -> 404."""
    from core.metric_semantics_api import _reference

    # First get_connection() call: org-read guard (identity_has_org_access mocked True).
    # Second get_connection() call: project->org lookup returns org_B (not org_A).
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda s: fake_cursor
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.fetchone.return_value = ("org_B",)
    fake_conn = _conn_mock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    load_rows = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=True),
        patch("core.db.get_connection", return_value=fake_conn),
        patch("core.metric_semantics._load_definition_rows", load_rows),
    ):
        resp = await _reference(
            _get_request({"org_id": "org_A", "project_id": "prj_of_org_b"})
        )
    assert resp.status_code == 404
    # The definition loader must never run for a foreign project.
    load_rows.assert_not_called()


# ---------------------------------------------------------------------------
# Offline -- GET /definitions authorization (Story 27.2 re-review N-1/N-2)
#
# N-1: the org-read guard was conditioned on `org_id is not None`, so a caller
# could OMIT org_id with scope=PROJECT&project_id=<foreign project> and read
# another org's PROJECT definitions (org_id NULL in DB) unguarded.  These tests
# ARE the fix: no test previously exercised GET /definitions at all.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_definitions_requires_auth():
    """No auth -> 401 on GET /definitions."""
    from core.metric_semantics_api import _list_definitions

    with patch(_AUTH, return_value=_AUTH_ANON):
        resp = await _list_definitions(
            _get_request({"scope_level": "ORG", "org_id": "org_x"})
        )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_list_definitions_non_member_404():
    """Non-member of an enrolled org -> 404 (existence not disclosed), no store call."""
    from core.metric_semantics_api import _list_definitions

    store = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=False),
        patch("core.db.get_connection", return_value=_conn_mock()),
        patch("core.metric_semantics.list_metric_definitions_by_scope", store),
    ):
        resp = await _list_definitions(
            _get_request({"scope_level": "ORG", "org_id": "org_x"})
        )
    assert resp.status_code == 404
    store.assert_not_called()


@pytest.mark.anyio
async def test_cross_org_list_definitions_foreign_project_404():
    """N-1: scope=PROJECT with a project of org_B while guarded on org_A -> 404.

    org-read guard passes on org_A (identity_has_org_access True) but the project
    belongs to org_B: the store/loader must NEVER run.
    """
    from core.metric_semantics_api import _list_definitions

    # Project lookup returns org_B (foreign to the supplied org_A).
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda s: fake_cursor
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.fetchone.return_value = ("org_B",)
    fake_conn = _conn_mock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    store = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=True),
        patch("core.db.get_connection", return_value=fake_conn),
        patch("core.metric_semantics.list_metric_definitions_by_scope", store),
    ):
        resp = await _list_definitions(
            _get_request(
                {
                    "scope_level": "PROJECT",
                    "org_id": "org_A",
                    "project_id": "prj_of_org_b",
                }
            )
        )
    assert resp.status_code == 404
    store.assert_not_called()


@pytest.mark.anyio
async def test_cross_org_list_definitions_omitted_org_project_404():
    """N-1: org_id OMITTED + scope=PROJECT&project_id -> 404, no store call.

    This is the core bypass: with the old `and org_id is not None` guard the read
    proceeded unguarded (PROJECT rows carry org_id NULL).  The org must be resolved
    from the project and the org-read guard must run; here identity_has_org_access
    is False -> 404 and the loader never runs.
    """
    from core.metric_semantics_api import _list_definitions

    # Project lookup resolves to org_B.
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda s: fake_cursor
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.fetchone.return_value = ("org_B",)
    fake_conn = _conn_mock()
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    store = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=False),
        patch("core.db.get_connection", return_value=fake_conn),
        patch("core.metric_semantics.list_metric_definitions_by_scope", store),
    ):
        resp = await _list_definitions(
            _get_request({"scope_level": "PROJECT", "project_id": "prj_of_org_b"})
        )
    assert resp.status_code == 404
    store.assert_not_called()


@pytest.mark.anyio
async def test_list_definitions_omitted_org_scope_org_404():
    """N-1: scope=ORG without org_id -> 404 before any store call (no unguarded read)."""
    from core.metric_semantics_api import _list_definitions

    store = MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.db.get_connection", return_value=_conn_mock()),
        patch("core.metric_semantics.list_metric_definitions_by_scope", store),
    ):
        resp = await _list_definitions(_get_request({"scope_level": "ORG"}))
    assert resp.status_code == 404
    store.assert_not_called()


# ---------------------------------------------------------------------------
# Offline -- contract shape of /reference (B.1)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reference_contract_shape():
    """GET /reference returns the B.1 contract shape with mocked store."""
    from core.metric_semantics_api import _reference

    fake_def_rows = [
        {
            "id": "metdef_cost",
            "canonical_name": "cost",
            "display_name": "Cost",
            "aggregation_type": "sum",
            "additive": True,
            "ratio_numerator": None,
            "ratio_denominator": None,
            "format": None,
            "unit": None,
            "currency_mode": None,
            "non_additive_dimensions": [],
            "synonyms": [],
            "ai_context": None,
            "certified": False,
            "scope_level": "PLATFORM",
            "org_id": None,
            "project_id": None,
        }
    ]

    # AI-295: the reference serves the Project's GOVERNED rule, so the fake is a
    # governed runtime rule and its scope is PROJECT -- there is no cascade left
    # for a PLATFORM row to win.
    fake_rec_rule = {
        "method": "PRIORITY",
        "priority_order": ["connector-a", "connector-b"],
        "join_key": None,
        "truth_connector": None,
        "resolved_scope": "PROJECT",
        "rule_set_version_id": "grsv_EXAMPLE",
    }

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda s: fake_cursor
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = [
        ("metric_definition_id",), ("connector",), ("source_field_path",), ("status",)
    ]
    fake_cursor.fetchall.return_value = [
        ("metdef_cost", "connector-a", "spend", "proposed")
    ]

    fake_conn = MagicMock()
    fake_conn.__enter__ = lambda s: fake_conn
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=True),
        patch("core.db.get_connection", return_value=fake_conn),
        patch("core.metric_semantics._load_definition_rows", return_value=fake_def_rows),
        patch(
            "core.metric_semantics.reference_reconciliation", return_value=fake_rec_rule
        ),
        patch(
            "core.metric_semantics.reduce_definitions_by_specificity",
            return_value={"cost": fake_def_rows[0]},
        ),
    ):
        resp = await _reference(_get_request({"org_id": "org_test"}))

    assert resp.status_code == 200
    body = json.loads(resp.body)

    # Top-level keys
    assert "scope" in body
    assert "metrics" in body
    assert body["scope"]["org_id"] == "org_test"
    assert body["scope"]["project_id"] is None

    # Metric contract keys (§B.1)
    assert len(body["metrics"]) == 1
    m = body["metrics"][0]
    for key in (
        "canonical_name",
        "display_name",
        "aggregation_type",
        "additive",
        "ratio_numerator",
        "ratio_denominator",
        "format",
        "unit",
        "currency_mode",
        "non_additive_dimensions",
        "synonyms",
        "ai_context",
        "certified",
        "resolved_scope",
        "reconciliation",
        "source_mappings",
    ):
        assert key in m, f"Missing key {key!r} in metric contract"

    # Reconciliation is non-null (we have a rule).
    assert m["reconciliation"] is not None
    for rkey in ("method", "priority_order", "join_key", "truth_connector", "resolved_scope"):
        assert rkey in m["reconciliation"]

    # source_mappings present.
    assert isinstance(m["source_mappings"], list)


@pytest.mark.anyio
async def test_reference_null_reconciliation():
    """reconciliation=null when no governed Rule Set answers for the metric."""
    from core.metric_semantics_api import _reference

    fake_def_rows = [
        {
            "id": "metdef_cost",
            "canonical_name": "cost",
            "display_name": None,
            "aggregation_type": "sum",
            "additive": True,
            "ratio_numerator": None,
            "ratio_denominator": None,
            "format": None,
            "unit": None,
            "currency_mode": None,
            "non_additive_dimensions": [],
            "synonyms": [],
            "ai_context": None,
            "certified": False,
            "scope_level": "PLATFORM",
            "org_id": None,
            "project_id": None,
        }
    ]

    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda s: fake_cursor
    fake_cursor.__exit__ = MagicMock(return_value=False)
    fake_cursor.description = [
        ("metric_definition_id",), ("connector",), ("source_field_path",), ("status",)
    ]
    fake_cursor.fetchall.return_value = []

    fake_conn = MagicMock()
    fake_conn.__enter__ = lambda s: fake_conn
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.cursor = MagicMock(return_value=fake_cursor)

    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.project_access.identity_has_org_access", return_value=True),
        patch("core.db.get_connection", return_value=fake_conn),
        patch("core.metric_semantics._load_definition_rows", return_value=fake_def_rows),
        patch("core.metric_semantics.reference_reconciliation", return_value=None),
        patch(
            "core.metric_semantics.reduce_definitions_by_specificity",
            return_value={"cost": fake_def_rows[0]},
        ),
    ):
        resp = await _reference(_get_request({"org_id": "org_test"}))

    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["metrics"][0]["reconciliation"] is None


# ---------------------------------------------------------------------------
# Pg-gated tests (require TEST_POSTGRES_DSN + migration 049 applied)
# ---------------------------------------------------------------------------


def _create_test_org_pg(slug: str | None = None) -> str:
    """Insert a minimal org row and return its id."""
    from core.db import get_connection
    from ulid import ULID

    org_id = f"org_{ULID()}"
    slug = slug or f"testapi-{uuid.uuid4().hex[:8]}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations "
                "(id, name, slug, status, created_at, created_by) "
                "VALUES (%s, %s, %s, 'active', now(), 'test-fixture')",
                (org_id, f"API Test org {slug}", slug),
            )
            # THE CALLER MUST HOLD THE ORG. These routes take a production org
            # decision; an identity that is a member of nothing is refused 403,
            # which is what these tests were measuring instead of the bootstrap.
            # `_PG_CALLER` is the person the routes will see.
            globals()["_PG_CALLER"] = enrol_fixture_identity(
                cur, "admin@example.com", org_id=org_id
            )
        conn.commit()
    return org_id


def _delete_test_org_pg(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        # `mdm_business_domains` holds an org by ON DELETE RESTRICT; the graph the
        # production purge walks knows it, a hand-written DELETE does not.
        purge_fixture_org(conn, org_id)
        conn.commit()


@pg_available
@pytest.mark.anyio
async def test_pg_bootstrap_via_api():
    """Live DB: POST /bootstrap -> proposed>0; ORG mappings in DB."""
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            resp = await _trigger_bootstrap(_post_request({"org_id": org_id}))

        assert resp.status_code == 200
        report = json.loads(resp.body)
        assert report["proposed"] > 0

        from core.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.source_metric_mappings "
                    "WHERE scope_level = 'ORG' AND org_id = %s AND status = 'proposed'",
                    (org_id,),
                )
                count = cur.fetchone()[0]
        assert count > 0
    finally:
        _delete_test_org_pg(org_id)


@pg_available
@pytest.mark.anyio
async def test_pg_bootstrap_idempotent_no_new_audit():
    """Live DB: second bootstrap -> proposed=0, unchanged>0, no new audit rows."""
    from core.db import get_connection
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _trigger_bootstrap(_post_request({"org_id": org_id}))

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping'",
                )
                audit_before = cur.fetchone()[0]

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            resp2 = await _trigger_bootstrap(_post_request({"org_id": org_id}))

        report2 = json.loads(resp2.body)
        assert report2["proposed"] == 0
        assert report2["unchanged"] > 0

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping'",
                )
                audit_after = cur.fetchone()[0]

        assert audit_after == audit_before
    finally:
        _delete_test_org_pg(org_id)


@pg_available
@pytest.mark.anyio
async def test_pg_confirm_mapping():
    """Live DB: confirm -> status='confirmed' + audit emitted; re-confirm is idempotent."""
    from core.db import get_connection
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _confirm_mapping, _list_mappings, _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _trigger_bootstrap(_post_request({"org_id": org_id}))

        # Fetch the first proposed mapping id.
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            list_resp = await _list_mappings(_get_request({"org_id": org_id, "status": "proposed"}))
        mappings = json.loads(list_resp.body)["mappings"]
        assert len(mappings) > 0
        mapping_id = mappings[0]["id"]

        # Count audit rows before.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping' AND action LIKE '%upserted'",
                )
                audit_before = cur.fetchone()[0]

        # Confirm.
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            confirm_resp = await _confirm_mapping(
                _post_request({"org_id": org_id}, path_params={"id": mapping_id})
            )
        assert confirm_resp.status_code == 200
        confirmed = json.loads(confirm_resp.body)
        assert confirmed["status"] == "confirmed"

        # Audit row added.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping' AND action LIKE '%upserted'",
                )
                audit_after = cur.fetchone()[0]
        assert audit_after > audit_before

        # Re-confirm is idempotent (no extra audit).
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _confirm_mapping(
                _post_request({"org_id": org_id}, path_params={"id": mapping_id})
            )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.metric_semantics_audit "
                    "WHERE entity_type = 'source_metric_mapping' AND action LIKE '%upserted'",
                )
                audit_after2 = cur.fetchone()[0]
        assert audit_after2 == audit_after
    finally:
        _delete_test_org_pg(org_id)


@pg_available
@pytest.mark.anyio
async def test_pg_confirmed_preserved_after_bootstrap():
    """Live DB: after confirm, re-bootstrap leaves the mapping confirmed (preserved)."""
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _confirm_mapping, _list_mappings, _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _trigger_bootstrap(_post_request({"org_id": org_id}))

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            list_resp = await _list_mappings(
                _get_request({"org_id": org_id, "status": "proposed"})
            )
        mapping_id = json.loads(list_resp.body)["mappings"][0]["id"]

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _confirm_mapping(
                _post_request({"org_id": org_id}, path_params={"id": mapping_id})
            )

        # Re-bootstrap.
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            resp3 = await _trigger_bootstrap(_post_request({"org_id": org_id}))
        report3 = json.loads(resp3.body)
        assert report3["preserved"] >= 1

        # The mapping is still confirmed.
        from core.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM app.source_metric_mappings WHERE id = %s",
                    (mapping_id,),
                )
                row = cur.fetchone()
        assert row is not None and row[0] == "confirmed"
    finally:
        _delete_test_org_pg(org_id)


@pg_available
@pytest.mark.anyio
async def test_pg_rename_mapping():
    """Live DB: rename -> metric_definition_id re-pointed, status='renamed'."""
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _list_mappings, _rename_mapping, _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _trigger_bootstrap(_post_request({"org_id": org_id}))

        # Get a proposed mapping and identify a different target canonical.
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            list_resp = await _list_mappings(
                _get_request({"org_id": org_id, "status": "proposed"})
            )
        mappings = json.loads(list_resp.body)["mappings"]
        assert len(mappings) > 0
        first = mappings[0]
        mapping_id = first["id"]
        original_canonical = first["canonical_name"]

        # Find a different target.
        all_canonicals = [m["canonical_name"] for m in mappings]
        other = next((c for c in all_canonicals if c != original_canonical), None)
        if other is None:
            pytest.skip("Only one canonical available, cannot rename to a different one")

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            resp = await _rename_mapping(
                _post_request(
                    {"org_id": org_id, "canonical_name": other},
                    path_params={"id": mapping_id},
                )
            )
        assert resp.status_code == 200
        result = json.loads(resp.body)
        assert result["status"] == "renamed"
        assert result["canonical_name"] == other
    finally:
        _delete_test_org_pg(org_id)


@pg_available
@pytest.mark.anyio
async def test_pg_reject_mapping():
    """Live DB: reject -> status='rejected'."""
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _list_mappings, _reject_mapping, _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _trigger_bootstrap(_post_request({"org_id": org_id}))

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            list_resp = await _list_mappings(
                _get_request({"org_id": org_id, "status": "proposed"})
            )
        mapping_id = json.loads(list_resp.body)["mappings"][0]["id"]

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            resp = await _reject_mapping(
                _post_request({"org_id": org_id}, path_params={"id": mapping_id})
            )
        assert resp.status_code == 200
        assert json.loads(resp.body)["status"] == "rejected"
    finally:
        _delete_test_org_pg(org_id)


@pg_available
@pytest.mark.anyio
async def test_pg_reference_endpoint():
    """Live DB: GET /reference returns B.1 contract with real data."""
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_api import _reference, _trigger_bootstrap

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    try:
        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            await _trigger_bootstrap(_post_request({"org_id": org_id}))

        with patch(_AUTH, return_value=(True, _PG_CALLER)):
            resp = await _reference(_get_request({"org_id": org_id}))

        assert resp.status_code == 200
        body = json.loads(resp.body)
        assert "metrics" in body
        assert len(body["metrics"]) > 0

        # 'cost' must be present (it comes from dim_metric.csv).
        cost = next((m for m in body["metrics"] if m["canonical_name"] == "cost"), None)
        assert cost is not None, "cost metric must appear in /reference"
        assert cost["resolved_scope"] == "PLATFORM"
        # source_mappings for this org should contain at least one proposed entry.
        assert isinstance(cost["source_mappings"], list)
    finally:
        _delete_test_org_pg(org_id)



@pg_available
def test_pg_org_cascade_removes_mappings():
    """Live DB: deleting the org (CASCADE) removes its ORG source_metric_mappings."""
    from core.db import get_connection
    from core.metric_semantics import import_platform_defaults
    from core.metric_semantics_bootstrap import bootstrap_org_source_mappings

    import_platform_defaults(identity="system")
    org_id = _create_test_org_pg()
    deleted = False
    try:
        bootstrap_org_source_mappings(org_id=org_id, identity="system")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.source_metric_mappings WHERE org_id = %s",
                    (org_id,),
                )
                before = cur.fetchone()[0]
        assert before > 0

        _delete_test_org_pg(org_id)
        deleted = True

        # After CASCADE delete, no ORG mappings for this org_id should remain.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.source_metric_mappings WHERE org_id = %s",
                    (org_id,),
                )
                after = cur.fetchone()[0]
        assert after == 0
    finally:
        if not deleted:
            _delete_test_org_pg(org_id)


# ---------------------------------------------------------------------------
# THE TWO DEFINITION WRITE DOORS REFUSE -- story 49.3 AC1, cutover of 2026-08-25.
#
# WHAT THESE TESTS REPLACE, and it is ten of them: every test that proved POST
# and DELETE /definitions wrote -- the auth guard, the PLATFORM refusal, the two
# `additive` refusals of story 60.2, the three F-2 cross-org guards, the S-1
# no-leak test, and the pg-gated `test_pg_definitions_crud_cascade` which drove
# the ORG override / PLATFORM resurface round trip through the API. They proved
# guards on a door that no longer opens; a guard on a closed door is the greenest
# way there is to hide that it closed.
#
# WHAT REPLACES THEM IS NOT A WEAKER TEST. The cascade the pg test exercised is
# still covered where it lives -- `test_metric_semantics.py` drives
# `upsert_metric_definition` / `delete_metric_definition` and the PROJECT > ORG >
# PLATFORM resolution directly, on live Postgres, and `import_platform_defaults`
# (the ONE writer that remains) has its own test there. What is gone from here is
# the HTTP round trip, because the HTTP round trip is gone.
# ---------------------------------------------------------------------------


_DEFINITION_BODY = {
    "scope_level": "ORG",
    "org_id": "org_EXAMPLE",
    "canonical_name": "cost",
    "aggregation_type": "sum",
    "additive": True,
}


@pytest.mark.anyio
async def test_create_definition_refuses_with_legacy_store_is_read_only():
    from core.metric_semantics_api import _create_definition

    with patch(_AUTH, return_value=_AUTH_OK):
        resp = await _create_definition(_post_request(dict(_DEFINITION_BODY)))

    assert resp.status_code == 409
    assert json.loads(bytes(resp.body))["code"] == "legacy_store_is_read_only"


@pytest.mark.anyio
async def test_delete_definition_refuses_with_legacy_store_is_read_only():
    from core.metric_semantics_api import _delete_definition

    with patch(_AUTH, return_value=_AUTH_OK):
        resp = await _delete_definition(
            _delete_request(
                params={"scope_level": "ORG", "org_id": "org_EXAMPLE"},
                path_params={"canonical_name": "cost"},
            )
        )

    assert resp.status_code == 409
    assert json.loads(bytes(resp.body))["code"] == "legacy_store_is_read_only"


@pytest.mark.anyio
async def test_the_refusal_names_the_semantic_model_and_never_a_table():
    """A refusal names the gesture that works, not the store that failed."""
    from core.metric_semantics_api import _create_definition

    with patch(_AUTH, return_value=_AUTH_OK):
        resp = await _create_definition(_post_request(dict(_DEFINITION_BODY)))
    message = json.loads(bytes(resp.body))["message"]

    assert "Semantic Model" in message, message
    for db_word in ("metric_definitions", "app.", "table", "column"):
        assert db_word not in message, (db_word, message)


@pytest.mark.anyio
async def test_a_refused_definition_write_never_reaches_the_store():
    """No connection is opened and no store function is called -- refusal comes first."""
    from core.metric_semantics_api import _create_definition, _delete_definition

    upsert, delete_fn = MagicMock(), MagicMock()
    with (
        patch(_AUTH, return_value=_AUTH_OK),
        patch("core.db.get_connection") as get_conn,
        patch("core.metric_semantics.upsert_metric_definition", upsert),
        patch("core.metric_semantics.delete_metric_definition", delete_fn),
    ):
        await _create_definition(_post_request(dict(_DEFINITION_BODY)))
        await _delete_definition(
            _delete_request(
                params={"scope_level": "ORG", "org_id": "org_EXAMPLE"},
                path_params={"canonical_name": "cost"},
            )
        )

    get_conn.assert_not_called()
    upsert.assert_not_called()
    delete_fn.assert_not_called()


def test_the_module_no_longer_imports_a_definition_writer():
    """The permanent attack: re-wiring a handler needs the import back, and goes red here.

    Read as an AST rather than as text, because `_create_definition` and
    `_delete_definition` are still the NAMES of the refusing handlers. The
    question is what this module imports from `core.metric_semantics`, and the
    READS must still be there or the guard is reading nothing.
    """
    import ast
    import inspect

    from core import metric_semantics_api

    tree = ast.parse(inspect.getsource(metric_semantics_api))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.metric_semantics"
        for alias in node.names
    }

    assert "get_metric_definition" in imported, sorted(imported)
    assert "list_metric_definitions_by_scope" in imported, sorted(imported)

    forbidden = imported & {"upsert_metric_definition", "delete_metric_definition"}
    assert not forbidden, sorted(forbidden)


def test_the_bootstrap_seed_import_is_the_one_writer_that_stayed():
    """`import_platform_defaults` is a DERIVATION of the delivered catalogue.

    It reads `dbt/seeds/dim_metric.csv` and upserts the PLATFORM rows. That is not
    a declaration anyone authored through a screen -- it is the product's own
    catalogue, the same doctrine as `platform_canonical_vocabulary` -- so it did
    not go with the authoring doors, and this test says so out loud rather than
    leaving the asymmetry to be rediscovered as a bug.
    """
    import ast
    import inspect

    from core import metric_semantics_api

    tree = ast.parse(inspect.getsource(metric_semantics_api))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.metric_semantics"
        for alias in node.names
    }

    assert "import_platform_defaults" in imported, sorted(imported)
