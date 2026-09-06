"""Story 62.3 -- the contract a published mart offers to a reader outside.

Two properties matter and each one has a test that fails without it:
a contract DERIVED from the dbt catalogue is the same twice in a row, and a
rebuild that changes a column moves the version instead of silently changing
the table under a dashboard that was told the old shape.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import enrol_fixture_identity, purge_fixture_org, purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_AUTH_SUBJECT = "owner@example.com"
_AUTH = "core.admin_api._check_auth"


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


@pytest.fixture(autouse=True)
def _production_auth_mode(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "0")
    import core.warehouse_tenancy as wt

    wt._reset_cache()


def _catalogue(tmp_path, columns: list[str], table: str = "fact_example_daily"):
    """Write a dbt-shaped marts catalogue and return its directory."""
    directory = tmp_path / "marts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "schema.yml").write_text(
        "version: 2\nmodels:\n"
        f"  - name: {table}\n"
        "    columns:\n"
        + "".join(f"      - name: {name}\n" for name in columns)
        + "  - name: int_example_helper\n"
        "    columns:\n"
        "      - name: project_id\n",
        encoding="utf-8",
    )
    return directory


# ---------------------------------------------------------------------------
# The catalogue is DERIVED, not retyped
# ---------------------------------------------------------------------------


def test_the_published_marts_come_from_the_dbt_catalogue_shipped_with_the_product():
    from core.mart_contract import published_marts

    tables = {shape.table for shape in published_marts()}

    assert "fact_daily_kpi" in tables
    columns = next(s for s in published_marts() if s.table == "fact_daily_kpi").columns
    assert columns[:3] == ("project_id", "date", "connector")


def test_an_intermediate_model_is_not_a_published_mart():
    from core.mart_contract import published_marts

    assert not [shape for shape in published_marts() if shape.table.startswith("int_")]


def test_a_missing_catalogue_says_so_instead_of_answering_an_empty_contract(tmp_path):
    from core.mart_contract import MartContractUnavailable, published_marts

    with pytest.raises(MartContractUnavailable):
        published_marts(tmp_path / "absent")


def test_the_fingerprint_moves_only_when_the_columns_move(tmp_path):
    from core.mart_contract import published_marts

    base = published_marts(_catalogue(tmp_path / "a", ["project_id", "date", "cost"]))[0]
    same = published_marts(_catalogue(tmp_path / "b", ["project_id", "date", "cost"]))[0]
    added = published_marts(
        _catalogue(tmp_path / "c", ["project_id", "date", "cost", "currency"])
    )[0]
    reordered = published_marts(_catalogue(tmp_path / "d", ["date", "project_id", "cost"]))[0]

    assert base.fingerprint == same.fingerprint
    assert base.fingerprint != added.fingerprint
    assert base.fingerprint != reordered.fingerprint


# ---------------------------------------------------------------------------
# The version, against a live PostgreSQL
# ---------------------------------------------------------------------------


def _setup_project(suffix: str) -> dict[str, str]:
    from core.db import get_connection

    org_id = f"org_mc_{suffix}"
    project_id = f"proj_mc_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"MCOrg-{suffix}", f"mc-org-{suffix}"),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, %s, 'active', 'system')",
                (project_id, org_id, f"MC-{suffix}", f"mc-{suffix}"),
            )
            identity = enrol_fixture_identity(
                cur, _AUTH_SUBJECT, org_id=org_id, project_id=project_id
            )
        conn.commit()
    return {"org_id": org_id, "project_id": project_id, "identity": identity}


def _teardown_project(ids: dict[str, str]) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        purge_fixture_project(conn, ids["project_id"])
        purge_fixture_org(conn, ids["org_id"])
        conn.commit()


@pg_available
def test_the_derived_contract_is_the_same_twice_and_the_read_writes_nothing(tmp_path):
    from core.db import get_connection
    from core.mart_contract import read_contracts

    ids = _setup_project(uuid.uuid4().hex[:8])
    catalogue = _catalogue(tmp_path, ["project_id", "date", "cost"])
    try:
        with get_connection() as conn:
            first = read_contracts(ids["project_id"], conn, catalogue)
            second = read_contracts(ids["project_id"], conn, catalogue)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.mart_contract_versions WHERE project_id = %s",
                    (ids["project_id"],),
                )
                written = cur.fetchone()[0]
            conn.commit()
        assert first == second
        assert written == 0
        assert first[0]["dataset"] == f"marts_{ids['project_id']}"
        assert first[0]["table"] == "fact_example_daily"
        assert first[0]["columns"] == ["project_id", "date", "cost"]
        assert first[0]["schema_version"] is None
        assert first[0]["state"] == "unpublished"
    finally:
        _teardown_project(ids)


@pg_available
def test_a_rebuild_that_changes_a_column_increments_the_version(tmp_path):
    from core.db import get_connection
    from core.mart_contract import publish_contract_version, read_contracts

    ids = _setup_project(uuid.uuid4().hex[:8])
    before = _catalogue(tmp_path / "before", ["project_id", "date", "cost"])
    after = _catalogue(tmp_path / "after", ["project_id", "date", "cost", "currency"])
    try:
        with get_connection() as conn:
            first = publish_contract_version(
                ids["project_id"], "fact_example_daily", conn,
                actor=ids["identity"], models_dir=before,
            )
            conn.commit()
        assert first == {"changed": True, "schema_version": 1}

        with get_connection() as conn:
            again = publish_contract_version(
                ids["project_id"], "fact_example_daily", conn,
                actor=ids["identity"], models_dir=before,
            )
            conn.commit()
        assert again == {"changed": False, "schema_version": 1}

        with get_connection() as conn:
            drifted = read_contracts(ids["project_id"], conn, after)[0]
            conn.commit()
        assert drifted["state"] == "rebuild_pending"
        assert drifted["schema_version"] == 1
        assert drifted["next_schema_version"] == 2

        with get_connection() as conn:
            bumped = publish_contract_version(
                ids["project_id"], "fact_example_daily", conn,
                actor=ids["identity"], previous_readable_days=7, models_dir=after,
            )
            contracts = read_contracts(ids["project_id"], conn, after)
            conn.commit()
        assert bumped == {"changed": True, "schema_version": 2}
        published = contracts[0]
        assert published["state"] == "published"
        assert published["schema_version"] == 2
        assert published["columns"] == ["project_id", "date", "cost", "currency"]
        # The dashboard bound to version 1 is told for how long it still reads.
        assert published["previous"]["schema_version"] == 1
        assert published["previous"]["readable_until"] is not None
    finally:
        _teardown_project(ids)


@pg_available
def test_publishing_a_version_writes_who_what_and_when(tmp_path):
    from core.db import get_connection
    from core.mart_contract import publish_contract_version

    ids = _setup_project(uuid.uuid4().hex[:8])
    catalogue = _catalogue(tmp_path, ["project_id", "date", "cost"])
    try:
        with get_connection() as conn:
            publish_contract_version(
                ids["project_id"], "fact_example_daily", conn,
                actor=ids["identity"], models_dir=catalogue,
            )
            conn.commit()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT identity, metadata, created_at FROM app.audit_log "
                "WHERE action = 'mart_contract.version_published' "
                "AND metadata->>'project_id' = %s",
                (ids["project_id"],),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        identity, metadata, created_at = rows[0]
        assert identity == ids["identity"]
        assert metadata["mart_table"] == "fact_example_daily"
        assert metadata["schema_version"] == 1
        assert created_at is not None
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_the_contract_route_refuses_a_mart_the_product_does_not_publish():
    from core.mart_contract import _publish_mart_contract_version

    ids = _setup_project(uuid.uuid4().hex[:8])
    request = MagicMock()
    request.path_params = {"project_id": ids["project_id"], "mart_table": "int_country_daily_kpi"}
    request.body = AsyncMock(return_value=b"{}")
    try:
        with patch(_AUTH, return_value=(True, ids["identity"])):
            response = await _publish_mart_contract_version(request)
        assert response.status_code == 404
        assert "is not a published mart" in json.loads(response.body)["message"]
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_project_the_caller_cannot_see_answers_not_found():
    from core.mart_contract import _list_mart_contracts

    request = MagicMock()
    request.path_params = {"project_id": "proj_EXAMPLE"}
    with patch(_AUTH, return_value=(True, "stranger@example.com")):
        response = await _list_mart_contracts(request)

    assert response.status_code == 404


@pg_available
@pytest.mark.anyio
async def test_the_contract_route_refuses_project_scope_under_org_schemas(monkeypatch):
    """Under TOOROW_ORG_SCHEMAS the marts dataset is per organization, so a
    contract at project scope would hand an external reader the dataset of every
    project of the org. The route names the same limit the grant route does,
    rather than naming the org-wide dataset."""
    import core.warehouse_tenancy as wt
    from core.mart_contract import _list_mart_contracts

    ids = _setup_project(uuid.uuid4().hex[:8])
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    wt._reset_cache()
    request = MagicMock()
    request.path_params = {"project_id": ids["project_id"]}
    try:
        with patch(_AUTH, return_value=(True, ids["identity"])):
            response = await _list_mart_contracts(request)
        assert response.status_code == 409
        assert json.loads(response.body)["code"] == "project_scope_unavailable"
    finally:
        monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "0")
        wt._reset_cache()
        _teardown_project(ids)
