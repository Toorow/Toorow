"""Story 62.3 -- the governed outbound read on the published marts of ONE project.

The organization saga already had its file (``test_dataset_access_grants.py``)
and these tests follow its patterns exactly: the provider is a seam patched with
a fake, no network is reached, and the durable behaviour is exercised against a
live PostgreSQL when one is reachable and skipped honestly when it is not.

Fixtures are generic on purpose -- ``proj_EXAMPLE``, ``owner@example.com``. No
real project, no real address, no real dataset.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from tests.conftest import enrol_fixture_identity, purge_fixture_org, purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_AUTH_SUBJECT = "owner@example.com"
_AUTH = "core.admin_api._check_auth"
_BROUGHT_PRINCIPAL = "serviceAccount:reader@example-project.iam.gserviceaccount.com"


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
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "configured-project")
    monkeypatch.setenv("TOOROW_BQ_PROVISION_ENABLED", "1")
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "0")
    monkeypatch.delenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", raising=False)
    import core.warehouse_tenancy as wt

    wt._reset_cache()


def _post(project_id: str, body: dict, key: str | None = None) -> MagicMock:
    request = MagicMock()
    request.path_params = {"project_id": project_id}
    request.headers = {"Idempotency-Key": key or f"test-{uuid.uuid4()}"}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    return request


def _get(project_id: str) -> MagicMock:
    request = MagicMock()
    request.path_params = {"project_id": project_id}
    return request


def _delete(project_id: str, grant_id: str, key: str | None = None) -> MagicMock:
    request = MagicMock()
    request.path_params = {"project_id": project_id, "grant_id": grant_id}
    request.headers = {"Idempotency-Key": key or f"test-{uuid.uuid4()}"}
    return request


def _target(project_id: str = "proj_EXAMPLE", org_id: str = "org_EXAMPLE"):
    from core.warehouse_tenancy import ProjectMarts

    return ProjectMarts(
        project_id=project_id,
        org_id=org_id,
        warehouse_slug="example",
        marts=f"marts_{project_id}",
    )


def _allowed():
    return patch("core.admin_api._refuse_unless_project_allowed", return_value=None)


# ---------------------------------------------------------------------------
# The scope: what the physical topology allows, and what it refuses
# ---------------------------------------------------------------------------


def test_project_scope_is_the_project_marts_dataset_when_the_topology_has_one(monkeypatch):
    import core.warehouse_tenancy as wt

    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "0")
    wt._reset_cache()
    owner = wt.OrgSchemas(
        "org_EXAMPLE", "example", "example", "org_example_raw", "org_example_marts"
    )
    with patch("core.warehouse_tenancy.resolve_org_schemas", return_value=owner):
        target, refusal = wt.project_marts_scope("proj_EXAMPLE")

    assert refusal is None
    assert target.marts == "marts_proj_EXAMPLE"
    assert target.expected_marts == "marts_proj_EXAMPLE"
    assert target.org_id == "org_EXAMPLE"


def test_a_project_with_no_organization_cannot_have_its_dataset_named(monkeypatch):
    """The name is not invented when the project is attached to nothing."""
    import core.warehouse_tenancy as wt

    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "0")
    wt._reset_cache()
    with patch("core.warehouse_tenancy.resolve_org_schemas", return_value=None):
        target, refusal = wt.project_marts_scope("proj_EXAMPLE")

    assert target is None
    assert "Attach the project to an organization" in refusal


def test_project_scope_is_refused_when_one_dataset_holds_every_project(monkeypatch):
    """The arbitration this story could not decide alone, said out loud.

    With ``TOOROW_ORG_SCHEMAS`` ON the marts dataset is per ORGANIZATION, so a
    grant asked from a project would also open its neighbours. The product
    refuses and names the limit instead of opening more than it was asked for.
    """
    import core.warehouse_tenancy as wt

    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    wt._reset_cache()
    schemas = wt.OrgSchemas(
        "org_EXAMPLE", "example", "example", "org_example_raw", "org_example_marts"
    )
    with patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas):
        target, refusal = wt.project_marts_scope("proj_EXAMPLE")

    assert target is None
    assert "org_example_marts" in refusal
    assert "would also open its neighbours" in refusal
    assert "Organization settings" in refusal


@pytest.mark.parametrize(
    "dataset",
    [
        "marts_proj_sandbox_client",
        "marts_proj_tmp_client",
        "org_scratch_marts",
        "marts_proj_client_20260825",
        "marts_proj_client_2026_08",
    ],
)
def test_a_slate_target_is_refused_with_the_gesture_that_repairs_it(dataset):
    from core.warehouse_tenancy import slate_dataset_reason

    reason = slate_dataset_reason(dataset)

    assert reason is not None
    assert "Rename the project or organization" in reason


def test_a_durable_target_is_not_mistaken_for_a_slate():
    from core.warehouse_tenancy import slate_dataset_reason

    assert slate_dataset_reason("marts_proj_EXAMPLE") is None
    assert slate_dataset_reason("org_example_marts") is None


def test_the_provider_seam_refuses_raw_staging_and_a_neighbours_dataset():
    """The class, not the instance: every refusal already known to the org scope
    holds for the project scope, and staging joins them."""
    from core.warehouse_tenancy import ProjectMarts, mutate_bigquery_dataset_access

    client = MagicMock(project="qa-project")
    for marts in (
        "raw_proj_EXAMPLE",
        "staging_proj_EXAMPLE",
        "mirror_proj_EXAMPLE",
        "marts_proj_NEIGHBOUR",
        "marts_proj_sandbox",
    ):
        target = ProjectMarts("proj_EXAMPLE", "org_EXAMPLE", "example", marts)
        with pytest.raises(ValueError):
            mutate_bigquery_dataset_access(target, _BROUGHT_PRINCIPAL, "grant", client=client)
    client.get_dataset.assert_not_called()
    client.update_dataset.assert_not_called()


def test_the_provider_seam_opens_exactly_the_project_marts_dataset(monkeypatch):
    from types import SimpleNamespace

    from core.warehouse_tenancy import mutate_bigquery_dataset_access
    from google.cloud import bigquery

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "configured-project")
    unrelated = bigquery.AccessEntry("OWNER", "groupByEmail", "owners@example.com")
    dataset = SimpleNamespace(access_entries=[unrelated], etag="etag-1")
    client = MagicMock(project="credentials-default-project")
    client.get_dataset.return_value = dataset

    result = mutate_bigquery_dataset_access(_target(), _BROUGHT_PRINCIPAL, "grant", client=client)

    client.get_dataset.assert_called_once_with("configured-project.marts_proj_EXAMPLE")
    assert dataset.access_entries[0] is unrelated
    assert result["dataset_id"] == "marts_proj_EXAMPLE"
    assert result["role"] == "roles/bigquery.dataViewer"


# ---------------------------------------------------------------------------
# What the story refuses, refused before anything is written
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"principal": _BROUGHT_PRINCIPAL, "destination": "warehouse.example"},
        {"principal": _BROUGHT_PRINCIPAL, "gcs_uri": "gs://example/marts"},
        {"principal": _BROUGHT_PRINCIPAL, "snowflake": "example"},
    ],
)
async def test_a_request_to_copy_the_mart_out_of_bigquery_is_refused(body):
    from core.dataset_access_api import _grant_project_dataset_access

    with patch(_AUTH, return_value=(True, _AUTH_SUBJECT)), _allowed():
        response = await _grant_project_dataset_access(_post("proj_EXAMPLE", body))

    assert response.status_code == 422
    message = json.loads(response.body)["message"]
    assert "does not copy it anywhere" in message
    assert "READ the mart where it lives" in message


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"principal": _BROUGHT_PRINCIPAL, "permanent": True},
        {"principal": _BROUGHT_PRINCIPAL, "irrevocable": True},
        {"principal": _BROUGHT_PRINCIPAL, "expires_at": "never"},
    ],
)
async def test_a_grant_nobody_could_revoke_is_refused(body):
    from core.dataset_access_api import _grant_project_dataset_access

    with patch(_AUTH, return_value=(True, _AUTH_SUBJECT)), _allowed():
        response = await _grant_project_dataset_access(_post("proj_EXAMPLE", body))

    assert response.status_code == 422
    assert "revocable in one gesture" in json.loads(response.body)["message"]


@pytest.mark.anyio
async def test_minting_an_account_this_deployment_could_not_retire_is_refused():
    from core.dataset_access_api import _grant_project_dataset_access

    with patch(_AUTH, return_value=(True, _AUTH_SUBJECT)), _allowed():
        response = await _grant_project_dataset_access(
            _post("proj_EXAMPLE", {"create_service_account": True})
        )

    assert response.status_code == 422
    message = json.loads(response.body)["message"]
    assert "could not retire one either" in message
    assert "name a service account that already exists" in message


@pytest.mark.anyio
async def test_a_project_the_caller_cannot_see_is_not_found():
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _list_project_dataset_access_grants,
        _revoke_project_dataset_access,
    )

    not_found = JSONResponse({"code": "not_found", "message": "Project not found"}, 404)
    with (
        patch(_AUTH, return_value=(True, _AUTH_SUBJECT)),
        patch("core.admin_api._refuse_unless_project_allowed", return_value=not_found),
        patch("core.warehouse_tenancy.mutate_bigquery_dataset_access") as provider,
    ):
        grant = await _grant_project_dataset_access(
            _post("proj_EXAMPLE", {"principal": _BROUGHT_PRINCIPAL})
        )
        listed = await _list_project_dataset_access_grants(_get("proj_EXAMPLE"))
        revoked = await _revoke_project_dataset_access(_delete("proj_EXAMPLE", "dagrant_x"))

    assert (grant.status_code, listed.status_code, revoked.status_code) == (404, 404, 404)
    provider.assert_not_called()


@pytest.mark.anyio
async def test_the_topology_refusal_reaches_the_person_before_any_row_is_written(monkeypatch):
    from core.dataset_access_api import _grant_project_dataset_access

    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    import core.warehouse_tenancy as wt

    wt._reset_cache()
    schemas = wt.OrgSchemas(
        "org_EXAMPLE", "example", "example", "org_example_raw", "org_example_marts"
    )
    with (
        patch(_AUTH, return_value=(True, _AUTH_SUBJECT)),
        _allowed(),
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas),
        patch("core.db.get_connection") as connect,
    ):
        response = await _grant_project_dataset_access(
            _post("proj_EXAMPLE", {"principal": _BROUGHT_PRINCIPAL})
        )

    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["code"] == "project_scope_unavailable"
    assert "org_example_marts" in body["message"]
    connect.assert_not_called()


# ---------------------------------------------------------------------------
# The service-account lifecycle, and the ORDER the two provider calls run in
# ---------------------------------------------------------------------------


def test_the_account_is_minted_before_the_dataset_is_opened():
    import core.dataset_access_api as api

    calls: list[str] = []
    payload = {
        "id": "dagrant_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "principal": "serviceAccount:toorow-read-abc@configured-project.iam.gserviceaccount.com",
        "managed_service_account": True,
    }
    with (
        patch(
            "core.service_account_provider.create_service_account",
            side_effect=lambda *a, **k: calls.append("create_account") or {"created": True},
        ),
        patch(
            "core.warehouse_tenancy.mutate_bigquery_dataset_access",
            side_effect=lambda *a, **k: calls.append("open_dataset") or {"changed": True},
        ),
    ):
        verdict = api._project_provider_verdict(_target(), payload, "grant")

    assert verdict.outcome == "succeeded"
    assert calls == ["create_account", "open_dataset"]


def test_the_role_is_removed_before_the_account_is_retired():
    import core.dataset_access_api as api

    calls: list[str] = []
    payload = {
        "id": "dagrant_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "principal": "serviceAccount:toorow-read-abc@configured-project.iam.gserviceaccount.com",
        "managed_service_account": True,
    }
    with (
        patch(
            "core.warehouse_tenancy.mutate_bigquery_dataset_access",
            side_effect=lambda *a, **k: calls.append("close_dataset") or {"changed": True},
        ),
        patch(
            "core.service_account_provider.delete_service_account",
            side_effect=lambda *a, **k: calls.append("delete_account") or {"deleted": True},
        ),
    ):
        verdict = api._project_provider_verdict(_target(), payload, "revoke")

    assert verdict.outcome == "succeeded"
    assert calls == ["close_dataset", "delete_account"]


def test_the_account_is_never_deleted_when_the_role_could_not_be_removed():
    import core.dataset_access_api as api

    payload = {
        "id": "dagrant_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "principal": "serviceAccount:toorow-read-abc@configured-project.iam.gserviceaccount.com",
        "managed_service_account": True,
    }
    with (
        patch(
            "core.warehouse_tenancy.mutate_bigquery_dataset_access",
            side_effect=RuntimeError("policy update denied"),
        ),
        patch("core.service_account_provider.delete_service_account") as delete_account,
    ):
        verdict = api._project_provider_verdict(_target(), payload, "revoke")

    assert verdict.outcome == "failed"
    delete_account.assert_not_called()


def test_a_revocation_that_left_the_account_behind_does_not_call_itself_done():
    import core.dataset_access_api as api

    payload = {
        "id": "dagrant_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "principal": "serviceAccount:toorow-read-abc@configured-project.iam.gserviceaccount.com",
        "managed_service_account": True,
    }
    with (
        patch(
            "core.warehouse_tenancy.mutate_bigquery_dataset_access",
            return_value={"changed": True},
        ),
        patch(
            "core.service_account_provider.delete_service_account",
            side_effect=RuntimeError("account still in use"),
        ),
    ):
        verdict = api._project_provider_verdict(_target(), payload, "revoke")

    assert verdict.outcome == "failed"
    assert "was removed from the dataset" in verdict.error
    assert "Retry the revocation to finish it" in verdict.error


def test_the_minted_account_is_the_same_one_on_every_retry_of_one_grant(monkeypatch):
    from core.service_account_provider import managed_account_id, managed_principal

    monkeypatch.setenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", "1")
    first = managed_account_id("dagrant_EXAMPLE")
    second = managed_account_id("dagrant_EXAMPLE")

    assert first == second
    assert first.startswith("toorow-read-")
    assert managed_account_id("dagrant_OTHER") != first
    assert managed_principal("dagrant_EXAMPLE").startswith("serviceAccount:toorow-read-")


def test_an_account_that_already_exists_is_adopted_rather_than_failed(monkeypatch):
    from core.service_account_provider import create_service_account

    monkeypatch.setenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", "1")
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=409)

    result = create_service_account("dagrant_EXAMPLE", display_name="read", client=session)

    assert result["created"] is False
    assert result["email"].endswith("@configured-project.iam.gserviceaccount.com")


def test_a_last_read_that_cannot_be_measured_is_reported_as_unmeasured(monkeypatch):
    from core.service_account_provider import read_last_authentication

    monkeypatch.setenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", "1")
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=403)

    assert read_last_authentication("reader@example.com", client=session) is None


def test_a_measured_last_read_is_returned_as_measured(monkeypatch):
    from core.service_account_provider import read_last_authentication

    monkeypatch.setenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", "1")
    session = MagicMock()
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "activities": [{"activity": {"lastAuthenticatedTime": "2026-08-20T00:00:00Z"}}]
    }
    session.get.return_value = response

    assert read_last_authentication("reader@example.com", client=session) == "2026-08-20T00:00:00Z"


# ---------------------------------------------------------------------------
# The durable saga, against a live PostgreSQL
# ---------------------------------------------------------------------------


def _setup_project(suffix: str) -> dict[str, str]:
    from core.db import get_connection

    org_id = f"org_p62_{suffix}"
    project_id = f"proj_p62_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"P62Org-{suffix}", f"p62-org-{suffix}"),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, %s, 'active', 'system')",
                (project_id, org_id, f"P62-{suffix}", f"p62-{suffix}"),
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


def _saga_patches(ids: dict[str, str], provider):
    target = _target(ids["project_id"], ids["org_id"])
    return (
        patch(_AUTH, return_value=(True, ids["identity"])),
        patch("core.warehouse_tenancy.mutate_bigquery_dataset_access", provider),
        patch("core.dataset_access_api._resolve_project_target", return_value=(target, None)),
    )


@pg_available
@pytest.mark.anyio
async def test_a_project_grant_opens_that_project_and_nothing_else_then_is_revoked():
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _list_project_dataset_access_grants,
        _revoke_project_dataset_access,
    )

    ids = _setup_project(uuid.uuid4().hex[:8])
    dataset = f"marts_{ids['project_id']}"
    provider = MagicMock(return_value={"dataset_id": dataset, "changed": True})
    key = f"stable-{uuid.uuid4()}"
    try:
        auth, mutate, resolve = _saga_patches(ids, provider)
        with auth, mutate, resolve:
            first = await _grant_project_dataset_access(
                _post(ids["project_id"], {"principal": _BROUGHT_PRINCIPAL}, key)
            )
            assert first.status_code == 201
            granted = json.loads(first.body)
            assert granted["lifecycle_state"] == "effective"
            assert granted["project_id"] == ids["project_id"]
            assert granted["dataset_id"] == dataset
            assert granted["role"] == "roles/bigquery.dataViewer"
            assert granted["managed_service_account"] is False

            # The dataset the provider was asked to open is the project's own.
            assert provider.call_args[0][0].marts == dataset

            replay = await _grant_project_dataset_access(
                _post(ids["project_id"], {"principal": _BROUGHT_PRINCIPAL}, key)
            )
            assert replay.status_code == 201
            assert json.loads(replay.body)["replayed"] is True
            assert provider.call_count == 1

            listed = json.loads(
                (await _list_project_dataset_access_grants(_get(ids["project_id"]))).body
            )["grants"]
            assert [row["lifecycle_state"] for row in listed] == ["effective"]
            assert listed[0]["last_read_state"] == "not_applicable"

            revoked = await _revoke_project_dataset_access(
                _delete(ids["project_id"], granted["id"])
            )
            assert revoked.status_code == 200
            assert json.loads(revoked.body)["lifecycle_state"] == "revoked"
            assert provider.call_count == 2
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_grant_and_a_revocation_each_write_who_what_and_when():
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _revoke_project_dataset_access,
    )
    from core.db import get_connection

    ids = _setup_project(uuid.uuid4().hex[:8])
    dataset = f"marts_{ids['project_id']}"
    provider = MagicMock(return_value={"dataset_id": dataset, "changed": True})
    try:
        auth, mutate, resolve = _saga_patches(ids, provider)
        with auth, mutate, resolve:
            granted = json.loads(
                (
                    await _grant_project_dataset_access(
                        _post(ids["project_id"], {"principal": _BROUGHT_PRINCIPAL})
                    )
                ).body
            )
            await _revoke_project_dataset_access(_delete(ids["project_id"], granted["id"]))

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, identity, metadata, created_at
                FROM app.audit_log
                WHERE metadata->>'grant_id' = %s
                ORDER BY created_at ASC
                """,
                (granted["id"],),
            )
            rows = cur.fetchall()
        actions = [row[0] for row in rows]
        assert "dataset_access.grant_finalized" in actions
        assert "dataset_access.revoke_finalized" in actions
        for _action, identity, metadata, created_at in rows:
            assert identity == ids["identity"]
            assert metadata["project_id"] == ids["project_id"]
            assert metadata["principal"] == _BROUGHT_PRINCIPAL
            assert created_at is not None
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_managed_account_is_created_listed_and_retired_with_its_grant(monkeypatch):
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _list_project_dataset_access_grants,
        _revoke_project_dataset_access,
    )

    monkeypatch.setenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", "1")
    ids = _setup_project(uuid.uuid4().hex[:8])
    dataset = f"marts_{ids['project_id']}"
    provider = MagicMock(return_value={"dataset_id": dataset, "changed": True})
    create = MagicMock(return_value={"created": True})
    delete = MagicMock(return_value={"deleted": True})
    try:
        auth, mutate, resolve = _saga_patches(ids, provider)
        with (
            auth,
            mutate,
            resolve,
            patch("core.service_account_provider.create_service_account", create),
            patch("core.service_account_provider.delete_service_account", delete),
            patch(
                "core.service_account_provider.read_last_authentication",
                return_value="2026-08-20T00:00:00Z",
            ),
        ):
            response = await _grant_project_dataset_access(
                _post(ids["project_id"], {"create_service_account": True})
            )
            assert response.status_code == 201
            granted = json.loads(response.body)
            assert granted["managed_service_account"] is True
            assert granted["principal"].startswith("serviceAccount:toorow-read-")
            assert create.call_count == 1

            listed = json.loads(
                (await _list_project_dataset_access_grants(_get(ids["project_id"]))).body
            )["grants"][0]
            assert listed["last_read_at"] == "2026-08-20T00:00:00Z"
            assert listed["last_read_state"] == "measured"

            revoked = await _revoke_project_dataset_access(
                _delete(ids["project_id"], granted["id"])
            )
            assert revoked.status_code == 200
            retired = json.loads(revoked.body)
            assert retired["lifecycle_state"] == "revoked"
            assert retired["service_account_deleted_at"] is not None
            assert delete.call_count == 1
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_revocation_that_the_provider_refused_is_not_reported_as_done():
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _list_project_dataset_access_grants,
        _revoke_project_dataset_access,
    )

    ids = _setup_project(uuid.uuid4().hex[:8])
    dataset = f"marts_{ids['project_id']}"
    provider = MagicMock(return_value={"dataset_id": dataset, "changed": True})
    try:
        auth, mutate, resolve = _saga_patches(ids, provider)
        with auth, mutate, resolve:
            granted = json.loads(
                (
                    await _grant_project_dataset_access(
                        _post(ids["project_id"], {"principal": _BROUGHT_PRINCIPAL})
                    )
                ).body
            )
            provider.side_effect = RuntimeError("policy update denied")
            response = await _revoke_project_dataset_access(
                _delete(ids["project_id"], granted["id"])
            )
            assert response.status_code == 502
            failed = json.loads(response.body)
            assert failed["lifecycle_state"] == "effective"
            assert failed["revoked_at"] is None
            assert failed["revocation_state"] == "failed"

            listed = json.loads(
                (await _list_project_dataset_access_grants(_get(ids["project_id"]))).body
            )["grants"][0]
            assert listed["revocation_state"] == "failed"

            provider.side_effect = None
            retried = await _revoke_project_dataset_access(
                _delete(ids["project_id"], granted["id"], f"retry-{uuid.uuid4()}")
            )
            assert retried.status_code == 200
            assert json.loads(retried.body)["lifecycle_state"] == "revoked"
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_grant_of_one_project_is_invisible_and_unrevocable_from_another():
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _list_project_dataset_access_grants,
        _revoke_project_dataset_access,
    )

    left = _setup_project(uuid.uuid4().hex[:8])
    right = _setup_project(uuid.uuid4().hex[:8])
    dataset = f"marts_{left['project_id']}"
    provider = MagicMock(return_value={"dataset_id": dataset, "changed": True})
    try:
        auth, mutate, resolve = _saga_patches(left, provider)
        with auth, mutate, resolve:
            granted = json.loads(
                (
                    await _grant_project_dataset_access(
                        _post(left["project_id"], {"principal": _BROUGHT_PRINCIPAL})
                    )
                ).body
            )
        with patch(_AUTH, return_value=(True, right["identity"])):
            listed = json.loads(
                (await _list_project_dataset_access_grants(_get(right["project_id"]))).body
            )["grants"]
            stolen = await _revoke_project_dataset_access(
                _delete(right["project_id"], granted["id"])
            )
        assert listed == []
        assert stolen.status_code == 404
    finally:
        _teardown_project(right)
        _teardown_project(left)


@pg_available
def test_postgres_refuses_a_project_grant_on_a_dataset_that_is_not_that_projects():
    from core.db import get_connection

    ids = _setup_project(uuid.uuid4().hex[:8])
    try:
        for dataset in (
            f"raw_{ids['project_id']}",
            "marts_proj_NEIGHBOUR",
            "org_example_marts",
            "marts_proj_sandbox_client",
        ):
            with get_connection() as conn, conn.cursor() as cur:
                with pytest.raises(Exception):
                    cur.execute(
                        """
                        INSERT INTO app.dataset_access_grants
                            (id, org_id, project_id, principal, granted_by,
                             lifecycle_state, dataset_id, effective_at)
                        VALUES (%s, %s, %s, %s, 'system', 'effective', %s, NOW())
                        """,
                        (
                            f"dagrant_{uuid.uuid4().hex}",
                            ids["org_id"],
                            ids["project_id"],
                            _BROUGHT_PRINCIPAL,
                            dataset,
                        ),
                    )
                conn.rollback()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.dataset_access_grants
                    (id, org_id, project_id, principal, granted_by,
                     lifecycle_state, dataset_id, effective_at)
                VALUES (%s, %s, %s, %s, 'system', 'effective', %s, NOW())
                """,
                (
                    f"dagrant_{uuid.uuid4().hex}",
                    ids["org_id"],
                    ids["project_id"],
                    _BROUGHT_PRINCIPAL,
                    f"marts_{ids['project_id']}",
                ),
            )
            conn.commit()
    finally:
        _teardown_project(ids)


@pg_available
def test_postgres_refuses_a_retired_account_on_a_read_that_is_still_open():
    from core.db import get_connection

    ids = _setup_project(uuid.uuid4().hex[:8])
    try:
        with get_connection() as conn, conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute(
                    """
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, project_id, principal, granted_by,
                         lifecycle_state, dataset_id, effective_at,
                         managed_service_account, service_account_deleted_at)
                    VALUES (%s, %s, %s, %s, 'system', 'effective', %s, NOW(), TRUE, NOW())
                    """,
                    (
                        f"dagrant_{uuid.uuid4().hex}",
                        ids["org_id"],
                        ids["project_id"],
                        _BROUGHT_PRINCIPAL,
                        f"marts_{ids['project_id']}",
                    ),
                )
            conn.rollback()
    finally:
        _teardown_project(ids)


def _seed_grant(ids: dict, **cols) -> str:
    """Insert one grant row directly, in whatever lifecycle state a test needs."""
    from core.db import get_connection

    grant_id = f"dagrant_{uuid.uuid4().hex}"
    columns = {
        "id": grant_id,
        "org_id": ids["org_id"],
        "project_id": ids["project_id"],
        "principal": _BROUGHT_PRINCIPAL,
        "granted_by": "system",
        "dataset_id": f"marts_{ids['project_id']}",
        **cols,
    }
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    with get_connection() as conn, conn.cursor() as cur:
        operation_id = columns.get("grant_operation_id")
        if operation_id:
            # The grant_operation_id has a FK to app.operations; a minimal row lets
            # a provider-attempt state exist without driving the whole grant saga.
            cur.execute(
                """
                INSERT INTO app.operations
                    (id, effective_org_id, command_type, actor, resource_path,
                     host_context, versions, request_hash, confirmation_mode,
                     idempotency_key_hash, state)
                VALUES (%s, %s, 'dataset_access.grant', 'system', '[]'::jsonb,
                        '{}'::jsonb, '{}'::jsonb, %s, 'server', %s, 'outcome_unknown')
                """,
                (operation_id, ids["org_id"], "a" * 64, "b" * 64),
            )
        cur.execute(
            f"INSERT INTO app.dataset_access_grants ({names}) VALUES ({marks})",
            tuple(columns.values()),
        )
        conn.commit()
    return grant_id


@pg_available
@pytest.mark.anyio
async def test_a_requested_grant_with_an_unknown_provider_verdict_is_not_reported_revoked():
    """The 62.3 defect: the Project revoke lost the Org saga's safe_legacy_cancel.

    A `requested` grant that made a provider attempt whose verdict is unknown --
    a role may sit on the dataset -- cannot be closed by flipping a column. The
    revoke must refuse (409) and name the retry, exactly as the Org saga does,
    never report `revoked` with no provider call.
    """
    from core.dataset_access_api import _revoke_project_dataset_access

    ids = _setup_project(uuid.uuid4().hex[:8])
    try:
        grant_id = _seed_grant(
            ids,
            lifecycle_state="requested",
            grant_operation_id=f"op_{uuid.uuid4().hex}",
            last_provider_error="policy update timed out",
            provider_error_at=datetime.now(timezone.utc),
        )
        # last_provider_error set => the verdict is unknown, so the guard refuses
        # and names the gesture: retry the GRANT to settle it, then revoke.
        with patch(_AUTH, return_value=(True, ids["identity"])):
            response = await _revoke_project_dataset_access(_delete(ids["project_id"], grant_id))
        assert response.status_code == 409
        assert b"retry the grant" in response.body
    finally:
        _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_failed_grant_whose_account_was_minted_retires_it_before_revoked():
    """The second probe: a `failed` grant that minted a service account must NOT
    read `revoked` while the account still lives. The revoke routes through the
    provider so the account is retired first.

    `grant_operation_id` is set, as `request_grant` sets it for a managed grant:
    a `failed` grant is a TERMINAL verdict, so the revoke never consults it and
    never 409s -- it retires the account and closes the read."""
    from core.dataset_access_api import _revoke_project_dataset_access

    ids = _setup_project(uuid.uuid4().hex[:8])
    provider = MagicMock(return_value={"dataset_id": f"marts_{ids['project_id']}", "changed": True})
    try:
        grant_id = _seed_grant(
            ids,
            lifecycle_state="failed",
            managed_service_account=True,
            grant_operation_id=f"op_{uuid.uuid4().hex}",
            last_provider_error="acl update failed after the account was minted",
            provider_error_at=datetime.now(timezone.utc),
        )
        auth, mutate, resolve = _saga_patches(ids, provider)
        with (
            auth,
            mutate,
            resolve,
            patch("core.service_account_provider.delete_service_account") as retire,
        ):
            response = await _revoke_project_dataset_access(_delete(ids["project_id"], grant_id))
        body = json.loads(response.body)
        # The account was retired, and only then did the grant read revoked.
        retire.assert_called_once()
        assert body["lifecycle_state"] == "revoked"
        assert body["service_account_deleted_at"] is not None
    finally:
        _teardown_project(ids)


@pg_available
def test_the_check_refuses_a_slate_token_in_the_final_position_like_the_code_does():
    """Migration 313: the 308 CHECK caught `tmp`/`temp` only between underscores
    and did not know `wip`/`throwaway`, so a slate token at the END of the name
    passed the constraint while `slate_dataset_reason` refused it. The CHECK now
    refuses every slate token in every position, exactly as the code does."""
    from core.db import get_connection
    from core.warehouse_tenancy import slate_dataset_reason

    for token in ("tmp", "temp", "wip", "throwaway"):
        ids = _setup_project(f"{uuid.uuid4().hex[:6]}_{token}")
        dataset = f"marts_{ids['project_id']}"
        # The code already refuses it -- this is what the CHECK must match.
        assert slate_dataset_reason(dataset) is not None
        try:
            with get_connection() as conn, conn.cursor() as cur:
                with pytest.raises(Exception):
                    cur.execute(
                        """
                        INSERT INTO app.dataset_access_grants
                            (id, org_id, project_id, principal, granted_by,
                             lifecycle_state, dataset_id, effective_at)
                        VALUES (%s, %s, %s, %s, 'system', 'effective', %s, NOW())
                        """,
                        (
                            f"dagrant_{uuid.uuid4().hex}",
                            ids["org_id"],
                            ids["project_id"],
                            _BROUGHT_PRINCIPAL,
                            dataset,
                        ),
                    )
                conn.rollback()
        finally:
            _teardown_project(ids)


@pg_available
@pytest.mark.anyio
async def test_a_minted_grant_whose_provider_revoke_fails_is_not_a_check_violation():
    """The re-review defect: routing a minted `failed` grant through the provider
    follow-through set `revocation_state` on a non-effective row via
    `_finalize_revoke`, violating the lifecycle CHECK -- swallowed to a 202, the
    account never retired, the grant blocked forever. Now the failure records the
    error on the lifecycle row, keeps the state, retires nothing it could not, and
    a retry that succeeds finishes the job."""
    from core.dataset_access_api import (
        _list_project_dataset_access_grants,
        _revoke_project_dataset_access,
    )

    ids = _setup_project(uuid.uuid4().hex[:8])
    provider = MagicMock(return_value={"dataset_id": f"marts_{ids['project_id']}", "changed": True})
    try:
        grant_id = _seed_grant(
            ids,
            lifecycle_state="failed",
            managed_service_account=True,
            grant_operation_id=f"op_{uuid.uuid4().hex}",
            last_provider_error="acl update failed after the account was minted",
            provider_error_at=datetime.now(timezone.utc),
        )
        auth, mutate, resolve = _saga_patches(ids, provider)
        # First revoke: the provider REVOKE fails. It must not be a CheckViolation
        # (500), the account must NOT read as retired, and the grant must stay
        # `failed` and retryable.
        with (
            auth,
            mutate,
            resolve,
            patch(
                "core.service_account_provider.delete_service_account",
                side_effect=AssertionError("must not be called when the role removal failed"),
            ),
            patch("core.warehouse_tenancy.mutate_bigquery_dataset_access") as revoke_provider,
        ):
            revoke_provider.side_effect = RuntimeError("policy update denied")
            first = await _revoke_project_dataset_access(_delete(ids["project_id"], grant_id))
        assert first.status_code != 500
        listed = json.loads(
            (await _run_list(_list_project_dataset_access_grants, ids)).body
        )["grants"][0]
        assert listed["lifecycle_state"] == "failed"
        assert listed["service_account_deleted_at"] is None

        # Retry with the provider healthy: role removed, account retired, revoked.
        auth2, mutate2, resolve2 = _saga_patches(ids, provider)
        with (
            auth2,
            mutate2,
            resolve2,
            patch("core.service_account_provider.delete_service_account") as retire,
        ):
            retried = await _revoke_project_dataset_access(
                _delete(ids["project_id"], grant_id, f"retry-{uuid.uuid4()}")
            )
        body = json.loads(retried.body)
        retire.assert_called_once()
        assert body["lifecycle_state"] == "revoked"
        assert body["service_account_deleted_at"] is not None
    finally:
        _teardown_project(ids)


async def _run_list(handler, ids):
    with patch(_AUTH, return_value=(True, ids["identity"])):
        return await handler(_get(ids["project_id"]))


@pg_available
@pytest.mark.anyio
async def test_a_managed_grant_that_failed_after_minting_is_revocable_end_to_end(monkeypatch):
    """The re-review's production scenario, driven through the real endpoints.

    A managed grant whose dataset ACL fails AFTER the account is minted lands
    `failed` with `grant_operation_id` set and the account alive -- the state
    `request_grant` actually produces. It must be revocable in one gesture: the
    revoke retires the account and closes the read, never a perpetual 409."""
    from core.dataset_access_api import (
        _grant_project_dataset_access,
        _revoke_project_dataset_access,
    )

    monkeypatch.setenv("TOOROW_MANAGED_SERVICE_ACCOUNTS_ENABLED", "1")
    ids = _setup_project(uuid.uuid4().hex[:8])
    provider = MagicMock(return_value={"dataset_id": f"marts_{ids['project_id']}", "changed": True})
    try:
        auth, mutate, resolve = _saga_patches(ids, provider)
        with (
            auth,
            mutate,
            resolve,
            patch("core.service_account_provider.create_service_account") as mint,
            patch(
                "core.service_account_provider.managed_service_accounts_enabled",
                return_value=True,
            ),
        ):
            # The account mints, then the ACL open fails -> the grant lands `failed`.
            provider.side_effect = RuntimeError("acl update denied")
            granted = await _grant_project_dataset_access(
                _post(ids["project_id"], {"create_service_account": True})
            )
            grant = json.loads(granted.body)
            mint.assert_called_once()
        assert grant["lifecycle_state"] == "failed"
        assert grant["managed_service_account"] is True
        assert grant["service_account_deleted_at"] is None

        # The revoke retires the account and closes the read -- no 409, no orphan.
        auth2, mutate2, resolve2 = _saga_patches(ids, provider)
        provider.side_effect = None
        with (
            auth2,
            mutate2,
            resolve2,
            patch("core.service_account_provider.delete_service_account") as retire,
        ):
            revoked = await _revoke_project_dataset_access(
                _delete(ids["project_id"], grant["id"])
            )
        body = json.loads(revoked.body)
        retire.assert_called_once()
        assert body["lifecycle_state"] == "revoked"
        assert body["service_account_deleted_at"] is not None
    finally:
        _teardown_project(ids)
