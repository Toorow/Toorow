"""Effective and honest BigQuery dataset-access grant tests."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from tests.conftest import enrol_fixture_identity, purge_fixture_org

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_AUTH_SUBJECT = "tester@example.com"
_AUTH = ("core.admin_api._check_auth", (True, _AUTH_SUBJECT))
_VALID_PRINCIPAL = "serviceAccount:sa@project.iam.gserviceaccount.com"


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


def _post(org_id: str, body: dict, key: str | None = None) -> MagicMock:
    request = MagicMock()
    request.path_params = {"org_id": org_id}
    request.headers = {"Idempotency-Key": key or f"test-{uuid.uuid4()}"}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    return request


def _get(org_id: str) -> MagicMock:
    request = MagicMock()
    request.path_params = {"org_id": org_id}
    return request


def _delete(org_id: str, grant_id: str, key: str | None = None) -> MagicMock:
    request = MagicMock()
    request.path_params = {"org_id": org_id, "grant_id": grant_id}
    request.headers = {"Idempotency-Key": key or f"test-{uuid.uuid4()}"}
    return request


def _setup_org(suffix: str) -> dict[str, str]:
    from core.db import get_connection

    org_id = f"dag_org_{suffix}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, 'system')",
                (org_id, f"DAGOrg-{suffix}", f"dag-org-{suffix}"),
            )
            identity = enrol_fixture_identity(cur, _AUTH_SUBJECT, org_id=org_id)
        conn.commit()
    return {"org_id": org_id, "identity": identity}


def _teardown_org(ids: dict[str, str]) -> None:
    from core.db import get_connection

    with get_connection() as conn:
        purge_fixture_org(conn, ids["org_id"])
        conn.commit()


@pytest.mark.anyio
async def test_grant_validates_auth_body_principal_and_idempotency_key():
    from core.dataset_access_api import _grant_dataset_access

    with patch(_AUTH[0], return_value=(False, "")):
        assert (await _grant_dataset_access(_post("org_x", {}))).status_code == 401
    with patch(_AUTH[0], return_value=_AUTH[1]):
        assert (await _grant_dataset_access(_post("org_x", {}))).status_code == 422
        assert (
            await _grant_dataset_access(_post("org_x", {"principal": "role:x@example.com"}))
        ).status_code == 422
        request = _post("org_x", {"principal": _VALID_PRINCIPAL})
        request.headers = {}
        assert (await _grant_dataset_access(request)).status_code == 422


@pytest.mark.anyio
async def test_manage_authorization_refusal_stops_grant_and_revoke_before_provider():
    from core.dataset_access_api import _grant_dataset_access, _revoke_dataset_access

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = False
    denied = JSONResponse({"code": "forbidden", "message": "manage required"}, 403)
    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.db.get_connection", return_value=connection),
        patch("core.dataset_access_api._enforce_org_manage", return_value=denied),
        patch("core.warehouse_tenancy.mutate_bigquery_dataset_access") as provider,
    ):
        assert (
            await _grant_dataset_access(_post("org_x", {"principal": _VALID_PRINCIPAL}))
        ).status_code == 403
        assert (await _revoke_dataset_access(_delete("org_x", "dagrant_x"))).status_code == 403
    provider.assert_not_called()


def test_provider_error_is_actionable_bounded_and_redacts_credentials():
    from core.dataset_access_api import _provider_error

    detail = _provider_error(
        RuntimeError("permission denied token=abc123 Authorization: BearerSecret")
    )
    assert "permission denied" in detail
    assert "abc123" not in detail
    assert "BearerSecret" not in detail
    assert len(detail) <= 1000


@pytest.mark.parametrize(
    "secret",
    [
        "access_token=access-value",
        "refresh_token: refresh-value",
        "client_secret='client-value'",
    ],
)
def test_provider_error_redacts_compound_credential_keys(secret):
    from core.dataset_access_api import _provider_error

    detail = _provider_error(RuntimeError(f"provider refused {secret}"))
    assert "value" not in detail
    assert "[redacted]" in detail


@pytest.mark.parametrize(
    ("action", "present"),
    [("grant", True), ("revoke", False)],
)
def test_ambiguous_provider_timeout_is_reconciled_from_acl(action, present):
    from core.dataset_access_api import _provider_verdict

    with (
        patch(
            "core.warehouse_tenancy.mutate_bigquery_dataset_access",
            side_effect=TimeoutError("deadline"),
        ),
        patch(
            "core.warehouse_tenancy.read_bigquery_dataset_access",
            return_value={"present": present},
        ) as read_acl,
    ):
        verdict = _provider_verdict(_schemas(), _VALID_PRINCIPAL, action)

    assert verdict.outcome == "succeeded"
    read_acl.assert_called_once_with(_schemas(), _VALID_PRINCIPAL)


def test_ambiguous_provider_timeout_stays_unknown_when_acl_read_fails():
    from core.dataset_access_api import _provider_verdict

    with (
        patch(
            "core.warehouse_tenancy.mutate_bigquery_dataset_access",
            side_effect=TimeoutError("deadline access_token=grant-secret"),
        ),
        patch(
            "core.warehouse_tenancy.read_bigquery_dataset_access",
            side_effect=ConnectionError("read refresh_token=read-secret"),
        ),
    ):
        verdict = _provider_verdict(_schemas(), _VALID_PRINCIPAL, "grant")

    assert verdict.outcome == "unknown"
    assert "retry with the same Idempotency-Key" in (verdict.error or "")
    assert "grant-secret" not in (verdict.error or "")
    assert "read-secret" not in (verdict.error or "")


def _schemas(marts: str = "org_demo_marts"):
    from core.warehouse_tenancy import OrgSchemas

    return OrgSchemas("org_demo", "demo", "demo", "org_demo_raw", marts)


@pytest.mark.parametrize(
    ("principal", "entity_type"),
    [
        ("user:person@example.com", "userByEmail"),
        ("serviceAccount:sa@project.iam.gserviceaccount.com", "userByEmail"),
        ("group:team@example.com", "groupByEmail"),
    ],
)
def test_bigquery_grant_uses_dataset_acl_and_preserves_unrelated_entries(
    principal, entity_type, monkeypatch
):
    from core.warehouse_tenancy import mutate_bigquery_dataset_access
    from google.cloud import bigquery

    unrelated = bigquery.AccessEntry("OWNER", "groupByEmail", "owners@example.com")
    dataset = SimpleNamespace(access_entries=[unrelated], etag="etag-1")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "configured-project")
    client = MagicMock(project="credentials-default-project")
    client.get_dataset.return_value = dataset

    result = mutate_bigquery_dataset_access(_schemas(), principal, "grant", client=client)

    client.get_dataset.assert_called_once_with("configured-project.org_demo_marts")
    client.update_dataset.assert_called_once_with(dataset, ["access_entries"])
    assert dataset.access_entries[0] is unrelated
    added = dataset.access_entries[1]
    assert (added.role, added.entity_type, added.entity_id) == (
        "READER", entity_type, principal.split(":", 1)[1]
    )
    assert result == {
        "dataset_id": "org_demo_marts",
        "dataset_ref": "configured-project.org_demo_marts",
        "role": "roles/bigquery.dataViewer",
        "changed": True,
    }


def test_bigquery_revoke_removes_only_exact_reader_and_is_idempotent(monkeypatch):
    from core.warehouse_tenancy import mutate_bigquery_dataset_access
    from google.cloud import bigquery

    target = bigquery.AccessEntry("READER", "userByEmail", "person@example.com")
    other_role = bigquery.AccessEntry("OWNER", "userByEmail", "person@example.com")
    other_member = bigquery.AccessEntry("READER", "userByEmail", "other@example.com")
    dataset = SimpleNamespace(access_entries=[target, other_role, other_member], etag="etag-2")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "configured-project")
    client = MagicMock(project="credentials-default-project")
    client.get_dataset.return_value = dataset

    first = mutate_bigquery_dataset_access(
        _schemas(), "user:person@example.com", "revoke", client=client
    )
    assert dataset.access_entries == [other_role, other_member]
    assert first["changed"] is True
    client.update_dataset.reset_mock()
    second = mutate_bigquery_dataset_access(
        _schemas(), "user:person@example.com", "revoke", client=client
    )
    assert second["changed"] is False
    client.update_dataset.assert_not_called()


@pytest.mark.parametrize(
    "marts", ["org_demo_raw", "mirror_demo_marts", "foreign", "org_other_marts"]
)
def test_bigquery_seam_refuses_non_marts_targets_before_client_use(marts):
    from core.warehouse_tenancy import mutate_bigquery_dataset_access

    client = MagicMock(project="qa-project")
    with pytest.raises(ValueError):
        mutate_bigquery_dataset_access(
            _schemas(marts), "user:person@example.com", "grant", client=client
        )
    client.get_dataset.assert_not_called()
    client.update_dataset.assert_not_called()


def test_bigquery_seam_fails_closed_without_configured_project(monkeypatch):
    from core.warehouse_tenancy import mutate_bigquery_dataset_access

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    client = MagicMock(project="credentials-default-project")
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT is required"):
        mutate_bigquery_dataset_access(
            _schemas(), "user:person@example.com", "grant", client=client
        )
    client.get_dataset.assert_not_called()


def test_bigquery_seam_refuses_acl_write_without_etag(monkeypatch):
    from core.warehouse_tenancy import mutate_bigquery_dataset_access

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "configured-project")
    dataset = SimpleNamespace(access_entries=[], etag=None)
    client = MagicMock(project="credentials-default-project")
    client.get_dataset.return_value = dataset

    with pytest.raises(RuntimeError, match="no ETag"):
        mutate_bigquery_dataset_access(
            _schemas(), "user:person@example.com", "grant", client=client
        )
    client.update_dataset.assert_not_called()


def _saga_patches(ids: dict[str, str], schemas, provider) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch(_AUTH[0], return_value=(True, ids["identity"])))
    stack.enter_context(
        patch("core.warehouse_tenancy.org_schemas_enabled", return_value=True)
    )
    stack.enter_context(
        patch("core.warehouse_tenancy.bq_provisioning_enabled", return_value=True)
    )
    stack.enter_context(
        patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas)
    )
    stack.enter_context(
        patch("core.warehouse_tenancy.mutate_bigquery_dataset_access", provider)
    )
    return stack


@pg_available
@pytest.mark.anyio
async def test_effective_grant_replay_list_revoke_and_history():
    from core.dataset_access_api import (
        _grant_dataset_access,
        _list_dataset_access_grants,
        _revoke_dataset_access,
    )

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    key = f"stable-{uuid.uuid4()}"
    provider = MagicMock(return_value={
        "dataset_id": schemas.marts,
        "dataset_ref": f"qa.{schemas.marts}",
        "role": "roles/bigquery.dataViewer",
        "changed": True,
    })
    try:
        with (
            patch(_AUTH[0], return_value=(True, ids["identity"])),
            patch("core.warehouse_tenancy.org_schemas_enabled", return_value=True),
            patch("core.warehouse_tenancy.bq_provisioning_enabled", return_value=True),
            patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas),
            patch("core.warehouse_tenancy.mutate_bigquery_dataset_access", provider),
        ):
            first = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, key)
            )
            assert first.status_code == 201
            granted = json.loads(first.body)
            assert granted["lifecycle_state"] == "effective"
            assert granted["dataset_id"] == schemas.marts
            assert granted["role"] == "roles/bigquery.dataViewer"

            replay = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, key)
            )
            assert replay.status_code == 201
            assert json.loads(replay.body)["replayed"] is True
            assert provider.call_count == 1

            conflict = await _grant_dataset_access(
                _post(
                    ids["org_id"],
                    {"principal": "user:other@example.com"},
                    key,
                )
            )
            assert conflict.status_code == 409
            assert provider.call_count == 1

            listed = json.loads((await _list_dataset_access_grants(_get(ids["org_id"]))).body)
            assert listed["grants"][0]["lifecycle_state"] == "effective"

            revoked = await _revoke_dataset_access(
                _delete(ids["org_id"], granted["id"])
            )
            assert revoked.status_code == 200
            assert json.loads(revoked.body)["lifecycle_state"] == "revoked"
            listed = json.loads((await _list_dataset_access_grants(_get(ids["org_id"]))).body)
            assert listed["grants"][0]["lifecycle_state"] == "revoked"
            assert provider.call_count == 2
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_provider_failure_is_persisted_failed_and_never_effective():
    from core.dataset_access_api import _grant_dataset_access, _list_dataset_access_grants

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    provider = MagicMock(side_effect=RuntimeError("permission denied"))
    try:
        with (
            patch(_AUTH[0], return_value=(True, ids["identity"])),
            patch("core.warehouse_tenancy.org_schemas_enabled", return_value=True),
            patch("core.warehouse_tenancy.bq_provisioning_enabled", return_value=True),
            patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas),
            patch("core.warehouse_tenancy.mutate_bigquery_dataset_access", provider),
        ):
            response = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
            assert response.status_code == 502
            failed = json.loads(response.body)
            assert failed["lifecycle_state"] == "failed"
            assert failed["effective_at"] is None
            assert "permission denied" in failed["last_provider_error"]
            grants = json.loads(
                (await _list_dataset_access_grants(_get(ids["org_id"]))).body
            )["grants"]
            assert grants[0]["lifecycle_state"] == "failed"

            provider.side_effect = None
            provider.return_value = {"changed": True}
            retried = await _grant_dataset_access(
                _post(
                    ids["org_id"],
                    {"principal": _VALID_PRINCIPAL},
                    f"retry-{uuid.uuid4()}",
                )
            )
            assert retried.status_code == 201
            assert json.loads(retried.body)["lifecycle_state"] == "effective"
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_revoke_failure_keeps_grant_effective_and_reports_provider_error():
    from core.dataset_access_api import (
        _grant_dataset_access,
        _list_dataset_access_grants,
        _revoke_dataset_access,
    )

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    provider = MagicMock(return_value={
        "dataset_id": schemas.marts,
        "dataset_ref": f"qa.{schemas.marts}",
        "role": "roles/bigquery.dataViewer",
        "changed": True,
    })
    try:
        with (
            patch(_AUTH[0], return_value=(True, ids["identity"])),
            patch("core.warehouse_tenancy.org_schemas_enabled", return_value=True),
            patch("core.warehouse_tenancy.bq_provisioning_enabled", return_value=True),
            patch("core.warehouse_tenancy.resolve_org_schemas", return_value=schemas),
            patch("core.warehouse_tenancy.mutate_bigquery_dataset_access", provider),
        ):
            granted = json.loads((await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )).body)
            provider.side_effect = RuntimeError("policy update denied")
            response = await _revoke_dataset_access(
                _delete(ids["org_id"], granted["id"])
            )
            assert response.status_code == 502
            failed_revoke = json.loads(response.body)
            assert failed_revoke["lifecycle_state"] == "effective"
            assert failed_revoke["revoked_at"] is None
            assert "policy update denied" in failed_revoke["last_provider_error"]
            listed = json.loads(
                (await _list_dataset_access_grants(_get(ids["org_id"]))).body
            )["grants"][0]
            assert listed["lifecycle_state"] == "effective"
            assert listed["revoked_at"] is None
            assert listed["revocation_state"] == "failed"

            provider.side_effect = None
            provider.return_value = {
                "dataset_id": schemas.marts,
                "dataset_ref": f"configured-project.{schemas.marts}",
                "role": "roles/bigquery.dataViewer",
                "changed": True,
            }
            retried = await _revoke_dataset_access(
                _delete(ids["org_id"], granted["id"], f"retry-{uuid.uuid4()}")
            )
            assert retried.status_code == 200
            assert json.loads(retried.body)["lifecycle_state"] == "revoked"
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_requested_operation_is_committed_before_provider_call():
    from core.dataset_access_api import _grant_dataset_access
    from core.db import get_connection

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")

    def provider(_schemas_arg, principal, action):
        assert principal == _VALID_PRINCIPAL
        assert action == "grant"
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.lifecycle_state, o.outcome
                FROM app.dataset_access_grants g
                JOIN app.operations o ON o.id = g.grant_operation_id
                WHERE g.org_id = %s AND g.principal = %s
                """,
                (ids["org_id"], _VALID_PRINCIPAL),
            )
            assert cur.fetchone() == ("requested", "outcome_unknown")
        return {
            "dataset_id": schemas.marts,
            "dataset_ref": f"configured-project.{schemas.marts}",
            "role": "roles/bigquery.dataViewer",
            "changed": True,
        }

    try:
        with _saga_patches(ids, schemas, provider):
            response = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL})
            )
        assert response.status_code == 201
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_commit_ambiguity_replay_reads_terminal_row_without_second_provider_call():
    import core.dataset_access_api as api

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    key = f"commit-ambiguous-{uuid.uuid4()}"
    provider = MagicMock(return_value={"changed": True})
    finalize = api._finalize_grant

    def commit_then_disconnect(*args, **kwargs):
        finalize(*args, **kwargs)
        raise ConnectionError("connection dropped after commit")

    try:
        with (
            _saga_patches(ids, schemas, provider),
            patch("core.dataset_access_api._finalize_grant", side_effect=commit_then_disconnect),
        ):
            first = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, key)
            )
        assert first.status_code == 202

        with _saga_patches(ids, schemas, provider):
            replay = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, key)
            )
        assert replay.status_code == 201
        assert json.loads(replay.body)["replayed"] is True
        assert provider.call_count == 1
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_unknown_provider_outcome_remains_requested_then_same_key_resumes():
    from core.dataset_access_api import _grant_dataset_access, _revoke_dataset_access

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    key = f"timeout-{uuid.uuid4()}"
    timeout = MagicMock(side_effect=TimeoutError("deadline"))
    success = MagicMock(return_value={"changed": True})
    try:
        with (
            _saga_patches(ids, schemas, timeout),
            patch(
                "core.warehouse_tenancy.read_bigquery_dataset_access",
                side_effect=ConnectionError("ACL read unavailable"),
            ),
        ):
            first = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, key)
            )
        pending = json.loads(first.body)
        assert first.status_code == 202
        assert pending["lifecycle_state"] == "requested"
        assert pending["effective_at"] is None
        assert "ACL verification failed" in pending["last_provider_error"]

        with patch(_AUTH[0], return_value=(True, ids["identity"])):
            unsafe_cancel = await _revoke_dataset_access(
                _delete(ids["org_id"], pending["id"])
            )
        assert unsafe_cancel.status_code == 409

        with _saga_patches(ids, schemas, success):
            retried = await _grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, key)
            )
        assert retried.status_code == 201
        assert json.loads(retried.body)["lifecycle_state"] == "effective"
        assert timeout.call_count == 1
        assert success.call_count == 1
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_new_key_can_resume_phase_one_crash_after_bounded_lease():
    import core.dataset_access_api as api
    from core.db import get_connection

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    provider = MagicMock(return_value={"changed": True})
    first_key = f"phase-one-{uuid.uuid4()}"
    second_key = f"takeover-{uuid.uuid4()}"
    try:
        with (
            _saga_patches(ids, schemas, provider),
            patch(
                "core.dataset_access_api._claim_attempt",
                side_effect=ConnectionError("worker stopped after phase-one commit"),
            ),
        ):
            first = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, first_key)
            )
        assert first.status_code == 202
        assert provider.call_count == 0

        with _saga_patches(ids, schemas, provider):
            held = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, second_key)
            )
        assert held.status_code == 409

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.dataset_access_grants
                SET updated_at = updated_at - INTERVAL '2 minutes'
                WHERE org_id = %s AND principal = %s
                """,
                (ids["org_id"], _VALID_PRINCIPAL),
            )
            conn.commit()
        with _saga_patches(ids, schemas, provider):
            resumed = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, second_key)
            )
        assert resumed.status_code == 201
        assert json.loads(resumed.body)["lifecycle_state"] == "effective"
        assert provider.call_count == 1
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_new_key_takes_over_stale_phase_one_and_stale_worker_cannot_overwrite_success():
    import core.dataset_access_api as api
    from core.db import get_connection

    ids = _setup_org(uuid.uuid4().hex[:8])
    schemas = _schemas(f"org_{ids['org_id'].removeprefix('dag_org_')}_marts")
    first_key = f"first-{uuid.uuid4()}"
    second_key = f"second-{uuid.uuid4()}"
    provider = MagicMock(return_value={"changed": True})
    try:
        with (
            _saga_patches(ids, schemas, provider),
            patch(
                "core.dataset_access_api._finalize_grant",
                side_effect=ConnectionError("worker lost before finalization"),
            ),
        ):
            first = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, first_key)
            )
        assert first.status_code == 202
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.dataset_access_grants
                SET provider_attempt_started_at =
                        provider_attempt_started_at - INTERVAL '2 minutes',
                    updated_at = updated_at - INTERVAL '2 minutes'
                WHERE org_id = %s AND principal = %s
                RETURNING id, grant_operation_id, provider_attempt_started_at
                """,
                (ids["org_id"], _VALID_PRINCIPAL),
            )
            grant_id, stale_operation_id, stale_attempt_token = cur.fetchone()
            conn.commit()

        with _saga_patches(ids, schemas, provider):
            second = await api._grant_dataset_access(
                _post(ids["org_id"], {"principal": _VALID_PRINCIPAL}, second_key)
            )
        assert second.status_code == 201
        current = api._finalize_grant(
            grant_id,
            stale_operation_id,
            ids["identity"],
            schemas.marts,
            api.ProviderVerdict("failed", "late stale failure"),
            stale_attempt_token,
        )
        assert current["lifecycle_state"] == "effective"
        assert current["last_provider_error"] is None
        assert provider.call_count == 2
    finally:
        _teardown_org(ids)


@pg_available
@pytest.mark.anyio
async def test_identity_from_other_org_cannot_read_or_revoke_grant_history():
    from core.dataset_access_api import (
        _list_dataset_access_grants,
        _revoke_dataset_access,
    )
    from core.db import get_connection

    org_a = _setup_org(uuid.uuid4().hex[:8])
    org_b = _setup_org(uuid.uuid4().hex[:8])
    grant_id = f"dagrant_{uuid.uuid4().hex}"
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.org_members WHERE org_id = %s AND identity = %s",
                (org_b["org_id"], org_a["identity"]),
            )
            cur.execute(
                """
                INSERT INTO app.dataset_access_grants
                    (id, org_id, principal, granted_by, lifecycle_state)
                VALUES (%s, %s, %s, %s, 'requested')
                """,
                (grant_id, org_b["org_id"], _VALID_PRINCIPAL, org_b["identity"]),
            )
            conn.commit()
        with patch(_AUTH[0], return_value=(True, org_a["identity"])):
            listed = await _list_dataset_access_grants(_get(org_b["org_id"]))
            revoked = await _revoke_dataset_access(
                _delete(org_b["org_id"], grant_id)
            )
        assert listed.status_code == 404
        assert revoked.status_code == 403
    finally:
        _teardown_org(org_b)
        _teardown_org(org_a)


@pg_available
def test_postgres_rejects_invalid_or_contradictory_lifecycle():
    from core.db import get_connection

    ids = _setup_org(uuid.uuid4().hex[:8])
    try:
        with get_connection() as conn, conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute(
                    """
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, principal, granted_by, lifecycle_state)
                    VALUES (%s, %s, %s, %s, 'effective')
                    """,
                    (f"dagrant_{uuid.uuid4().hex}", ids["org_id"], _VALID_PRINCIPAL, "tester"),
                )
            conn.rollback()
        with get_connection() as conn, conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute(
                    """
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, principal, granted_by, lifecycle_state)
                    VALUES (%s, %s, %s, %s, 'unknown')
                    """,
                    (f"dagrant_{uuid.uuid4().hex}", ids["org_id"], _VALID_PRINCIPAL, "tester"),
                )
            conn.rollback()
        with get_connection() as conn, conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute(
                    """
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, principal, granted_by, lifecycle_state,
                         dataset_id, effective_at, last_provider_error,
                         provider_error_at)
                    VALUES (%s, %s, %s, %s, 'effective', 'org_demo_marts',
                            NOW(), 'contradictory', NOW())
                    """,
                    (
                        f"dagrant_{uuid.uuid4().hex}",
                        ids["org_id"],
                        _VALID_PRINCIPAL,
                        "tester",
                    ),
                )
            conn.rollback()
        with get_connection() as conn, conn.cursor() as cur:
            with pytest.raises(Exception):
                cur.execute(
                    """
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, principal, granted_by, lifecycle_state,
                         last_provider_error)
                    VALUES (%s, %s, %s, %s, 'requested', 'missing error timestamp')
                    """,
                    (
                        f"dagrant_{uuid.uuid4().hex}",
                        ids["org_id"],
                        _VALID_PRINCIPAL,
                        "tester",
                    ),
                )
            conn.rollback()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.dataset_access_grants
                    (id, org_id, principal, granted_by, lifecycle_state,
                     dataset_id, effective_at, revocation_state,
                     last_provider_error, provider_error_at)
                VALUES (%s, %s, %s, %s, 'effective', 'org_demo_marts', NOW(),
                        'failed', 'revocation failed', NOW())
                """,
                (
                    f"dagrant_{uuid.uuid4().hex}",
                    ids["org_id"],
                    _VALID_PRINCIPAL,
                    "tester",
                ),
            )
            conn.commit()
    finally:
        _teardown_org(ids)
