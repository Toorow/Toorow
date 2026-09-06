"""ASGI seams for Story 47.3 setup observations."""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


class _Connection:
    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _connection():
    yield _Connection()


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth():
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
    )


def _real_source_options() -> dict:
    """The payload the REAL projections build, not a hand-written stand-in.

    A seam test that asserts on a literal it wrote itself pins the literal. Both
    halves below are therefore produced by the functions production calls --
    `project_source_account` for the accounts, `_external_access_options` for the
    External BigQuery entries -- from a raw row shaped like the `sources` query
    returns, secrets included, so what travels is measured and what is withheld
    is measured with it.
    """
    from core.data_surface import project_source_account
    from core.datastream_setup_observations import _external_access_options

    rows = [
        {
            "source_account_id": "sacct_1",
            "label": "Billing export",
            "connector_id": "bigquery",
            "connection_ref_id": "conn_1",
            "external_account_id": "warehouse.billing.export_v1",
            "available": True,
            "used_by_count": 0,
            # Never on the wire, and the assertions below are what says so.
            "credential_id": "conn-secret-owner-link",
            "nango_connection_id": "nango-raw-id",
        }
    ]
    accounts = [project_source_account(row, project_id="proj_1") for row in rows]
    return {
        "draft_ref": "dsd_1",
        "project_ref": "proj_1",
        "source_accounts": accounts,
        "connectors": [{"connector_ref": "generic", "contract_version_ref": "ccv_1"}],
        "managed_channels": [{"channel": "file_upload", "availability": "available"}],
        "external_access": _external_access_options(accounts, rows),
    }


def test_source_options_are_draft_scoped_and_never_expose_credentials() -> None:
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.get_source_options",
            return_value=_real_source_options(),
        ),
    ):
        response = _client().get(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/source-options"
        )
    assert response.status_code == 200
    rendered = str(response.json()).lower()
    assert "credential" not in rendered and "provider_account_id" not in rendered
    # The one identifier this route must never carry, named rather than implied:
    # `nango_connection_id` addresses the credential store, and a screen has no
    # use for it. It was in the raw row above and it is not here.
    assert "nango" not in rendered


def test_every_account_on_the_wire_names_its_authorization() -> None:
    """Story 57.11 -- question 2 of step 1, pinned at the seam it travels through.

    The unit tests pin the two projections; this pins the ASGI response, which is
    the only thing the wizard actually reads. Both lists carry it, because both
    feed a mode of step 1: `source_accounts` the Connector pull path, and
    `external_access` the External BigQuery path whose browse door takes exactly
    this id in its URL.
    """
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.get_source_options",
            return_value=_real_source_options(),
        ),
    ):
        response = _client().get(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/source-options"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["source_accounts"], "the fixture must carry at least one account"
    assert body["external_access"], "the fixture must carry at least one access"
    for entry in [*body["source_accounts"], *body["external_access"]]:
        assert entry["connection_ref"]["id"], entry
        assert entry["connection_ref"]["object_type"] == "connection"
        # It is an address, not a name: the display metadata stays beside it and
        # never grows an id of its own (a second way to name one thing).
        assert "id" not in entry["authorization_ref"]


def test_create_observation_requires_idempotency_and_exact_revision() -> None:
    created = {
        "observation_ref": "dso_1",
        "draft_ref": "dsd_1",
        "draft_revision": 3,
        "mode": "external_bq",
        "discovery_kind": "warehouse_schema",
        "adapter_ref": "external_bq.readonly.v1",
        "evidence_fingerprint": "a" * 64,
        "safe_metadata": {"location": "EU"},
        "coverage": {"schema": "available"},
        "exceptions": [],
        "idempotent_replay": False,
    }
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.create_observation", return_value=created
        ) as create,
    ):
        missing = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/observations",
            json={"expected_revision": 3},
        )
        response = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/observations",
            headers={"Idempotency-Key": "observe-bq-1"},
            json={
                "expected_revision": 3,
                "mode": "external_bq",
                "discovery_kind": "warehouse_schema",
                "access_ref": "bqacc_1",
                "object_ref": "analytics.raw.events",
                "declared_writer": "Agency ETL",
                "readonly_acknowledged": True,
            },
        )
    assert missing.status_code == 422
    assert response.status_code == 201
    assert create.call_args.kwargs["expected_revision"] == 3
    assert response.json()["safe_metadata"] == {"location": "EU"}


def test_observation_replay_returns_200_and_revision_conflict_is_stable() -> None:
    from core.datastream_setup_observations import ObservationConflict

    replay = {
        "observation_ref": "dso_1",
        "draft_ref": "dsd_1",
        "draft_revision": 3,
        "mode": "connector_pull",
        "discovery_kind": "connector_contract",
        "adapter_ref": "connector.contract.v1",
        "evidence_fingerprint": "a" * 64,
        "safe_metadata": {"report_refs": ["daily"]},
        "coverage": {"contract": "available"},
        "exceptions": [],
        "idempotent_replay": True,
    }
    request = {
        "expected_revision": 3,
        "mode": "connector_pull",
        "discovery_kind": "connector_contract",
        "source_account_ref": "sacct_1",
        "connector_ref": "generic",
        "connector_contract_version_ref": "ccv_1",
    }
    endpoint = "/api/projects/proj_1/datastream-setup-drafts/dsd_1/observations"

    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch("core.datastream_preconfiguration_api.create_observation", return_value=replay),
    ):
        replay_response = _client().post(
            endpoint,
            headers={"Idempotency-Key": "observe-connector-1"},
            json=request,
        )

    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.create_observation",
            side_effect=ObservationConflict("Draft revision changed"),
        ),
    ):
        conflict_response = _client().post(
            endpoint,
            headers={"Idempotency-Key": "observe-connector-2"},
            json=request,
        )

    assert replay_response.status_code == 200
    assert replay_response.json()["idempotent_replay"] is True
    assert conflict_response.status_code == 409
    assert conflict_response.json()["code"] == "observation_revision_conflict"


def test_cross_project_observation_read_is_nondisclosing() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.read_observation") as read,
    ):
        response = _client().get(
            "/api/projects/proj_foreign/datastream-setup-drafts/dsd_1/observations/dso_1"
        )
    assert response.status_code == 404
    read.assert_not_called()


def test_file_asset_upload_stages_bytes_outside_draft_json() -> None:
    staged = {
        "asset_ref": "dsa_1",
        "draft_ref": "dsd_1",
        "content_hash": "f" * 64,
        "detected_format": "csv",
        "byte_count": 14,
        "state": "available",
        "expires_at": "2026-08-05T10:00:00Z",
        "cleanup_owner": "datastream_setup_asset_retention",
        "idempotent_replay": False,
    }
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.stage_setup_asset", return_value=staged
        ) as stage,
    ):
        response = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/assets",
            headers={
                "Idempotency-Key": "asset-1",
                "X-File-Name": "daily.csv",
                "Content-Type": "text/csv",
            },
            content=b"date,spend\n",
        )
    assert response.status_code == 201
    assert response.json()["asset_ref"] == "dsa_1"
    assert stage.call_args.kwargs["data"] == b"date,spend\n"
    assert "data" not in response.json()


# ---------------------------------------------------------------------------
# Live-Postgres walk: discovery evidence vs. post-discovery navigation.
# Needs TEST_POSTGRES_DSN on a disposable instance; without it the test skips.
# ---------------------------------------------------------------------------


def test_observation_survives_navigation_revisions_and_source_change_invalidates(
    live_postgres,
) -> None:
    """The exact walk the live wizard took to a 404, then its inverse.

    create draft -> PATCH operator_input (source) -> POST observation -> PATCH
    wizard_state only ("Continue to configure") -> POST compile. The observation
    is bound to the revision its attachment minted, and the navigation PATCH
    mints the next one, so an exact-revision lookup orphaned the evidence and
    compile answered 404 for EVERY mode. The evidence stays compatible until a
    revision declares `mode`/`source` changed -- the second half proves that
    rule still drops the observation on a real input change.
    """
    import uuid
    from copy import deepcopy

    import pytest
    from core.datastream_preconfiguration import (
        compile_draft,
        create_or_resume_draft,
        read_draft,
        update_draft,
    )
    from core.datastream_setup_observations import ObservationNotFound, create_observation

    from tests.conftest import TEST_ORG_ID

    project_id = f"proj_dso_{uuid.uuid4().hex[:12]}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', 'test@example.com')",
            (project_id, TEST_ORG_ID, "Observation navigation test", project_id.lower()),
        )

    draft = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-1"
    )
    configured = update_draft(
        live_postgres,
        project_id=project_id,
        draft_id=draft["draft_ref"],
        actor="test",
        idempotency_key="autosave-source",
        expected_revision=draft["current_revision"],
        operator_input={
            "mode": "managed_feed",
            "name": "Deliveries",
            "data_role": "Spend",
            "source": {"channel": "inbound_email"},
            "configure": {"write_mode": "replace"},
            "wizard_state": {"step": "source"},
        },
    )

    def adapter(_request: dict) -> dict:
        return {
            "adapter_ref": "managed_feed.channel.v1",
            "safe_metadata": {"objects": []},
            "coverage": {"schema": "unavailable"},
            "exceptions": [{"code": "no_delivery_received_yet"}],
        }

    observation = create_observation(
        live_postgres,
        project_id=project_id,
        draft_id=draft["draft_ref"],
        actor="test",
        idempotency_key="observe-1",
        expected_revision=configured["current_revision"],
        request={
            "mode": "managed_feed",
            "discovery_kind": "channel_contract",
            "channel": "inbound_email",
        },
        adapter=adapter,
    )
    attached = read_draft(live_postgres, project_id=project_id, draft_id=draft["draft_ref"])
    assert (
        attached["operator_input"]["source"]["observation_ref"]
        == observation["observation_ref"]
    )

    # Navigation only: wizard_state moves, no discovery input changes.
    navigated_input = deepcopy(attached["operator_input"])
    navigated_input["wizard_state"] = {"step": "configure", "first_incomplete": "configure"}
    navigated = update_draft(
        live_postgres,
        project_id=project_id,
        draft_id=draft["draft_ref"],
        actor="test",
        idempotency_key="autosave-navigation",
        expected_revision=attached["current_revision"],
        operator_input=navigated_input,
    )
    assert navigated["current_revision"] == attached["current_revision"] + 1

    proposal = compile_draft(
        live_postgres,
        project_id=project_id,
        draft_id=draft["draft_ref"],
        actor="test",
        idempotency_key="compile-1",
        expected_revision=navigated["current_revision"],
    )
    assert proposal["proposal_ref"]

    # The inverse: a REAL source change drops the evidence, and compile refuses.
    changed_input = deepcopy(navigated_input)
    changed_input["source"]["channel"] = "webhook"
    changed = update_draft(
        live_postgres,
        project_id=project_id,
        draft_id=draft["draft_ref"],
        actor="test",
        idempotency_key="autosave-source-change",
        expected_revision=navigated["current_revision"],
        operator_input=changed_input,
    )
    with pytest.raises(ObservationNotFound, match="Compatible observation"):
        compile_draft(
            live_postgres,
            project_id=project_id,
            draft_id=draft["draft_ref"],
            actor="test",
            idempotency_key="compile-2",
            expected_revision=changed["current_revision"],
        )
