"""Project-scoped two-confirmation REST seams for Story 47.4."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


@contextmanager
def _connection():
    conn = MagicMock()
    yield conn


def _client() -> TestClient:
    from core.main import build_asgi_app

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


def _auth():
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
    )


def test_preview_dispatch_uses_only_exact_persisted_references() -> None:
    draft = {
        "draft_ref": "dsd_1",
        "current_revision": 4,
        "operator_input": {"source": {"channel": "file_upload"}},
    }
    proposal = {
        "proposal_ref": "dspp_1",
        "draft_revision_ref": "dsdr_1",
        "is_stale": False,
        "dependency_fingerprint": "d" * 64,
        "dependency_snapshot": {
            "project_configuration": {"version_id": "pcv_1"},
            "capabilities": [{"active_version_id": "pcap_1"}],
        },
        "section_fingerprints": {
            "physical_mapping": "a" * 64,
            "processing": "b" * 64,
        },
    }
    observation = {
        "observation_ref": "dso_1",
        "draft_revision_ref": "dsdr_1",
        "connector_contract_version_ref": None,
    }
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch("core.datastream_preconfiguration_api.read_draft", return_value=draft),
        patch("core.datastream_preconfiguration_api.read_proposal", return_value=proposal),
        patch(
            "core.datastream_preconfiguration_api.read_observation",
            return_value=observation,
        ),
        patch(
            "core.queue.enqueue_activation_work",
            return_value={"job_id": "dsaj_1", "state": "queued", "replayed": False},
        ) as enqueue,
    ):
        response = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/previews",
            headers={"Idempotency-Key": "preview-1"},
            json={
                "expected_revision": 4,
                "proposal_ref": "dspp_1",
                "observation_ref": "dso_1",
            },
        )

    assert response.status_code == 202
    assert enqueue.call_args.kwargs["correlation_id"] == "dsdr_1"
    payload = enqueue.call_args.kwargs["payload"]
    assert payload["pins"]["capability_version_ids"] == ["pcap_1"]
    assert {"rows", "content_hash", "credential", "secret"}.isdisjoint(payload)


def test_draft_confirmation_preparation_binds_exact_final_review_hash() -> None:
    review = {
        "is_stale": False,
        "content_hash": "f" * 64,
        "org_id": "org_1",
    }
    issued = SimpleNamespace(
        confirmation_id="econf_1",
        confirmation_secret="ecfs_once",
        command_type="datastream.setup.create_draft",
        payload_hash="a" * 64,
        expires_at=datetime.now(timezone.utc),
    )
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.read_final_review",
            return_value=review,
        ),
        patch(
            "core.datastream_preconfiguration_api.prepare_confirmation",
            return_value=issued,
        ) as prepare,
    ):
        response = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/draft-confirmations",
            headers={"Idempotency-Key": "prepare-draft-1"},
            json={"final_review_ref": "dsfr_1"},
        )

    assert response.status_code == 201
    assert response.json()["confirmation_secret"] == "ecfs_once"
    assert prepare.call_args.kwargs["content_hash"] == "f" * 64
    assert prepare.call_args.kwargs["resource_id"] == "dsfr_1"


def test_candidate_review_and_confirmation_are_execution_scoped() -> None:
    review = {
        "project_id": "proj_1",
        "datastream_id": "ds_1",
        "execution_id": "dse_1",
        "state": "ready",
        "review_hash": "r" * 64,
    }
    issued = SimpleNamespace(
        confirmation_id="econf_2",
        confirmation_secret="ecfs_candidate",
        command_type="datastream.candidate.publish_activate",
        payload_hash="b" * 64,
        expires_at=datetime.now(timezone.utc),
    )
    endpoint = "/api/projects/proj_1/datastreams/ds_1/executions/dse_1"
    with (
        _auth()[0],
        _auth()[1],
        _auth()[2],
        patch(
            "core.datastream_preconfiguration_api.read_candidate_review",
            return_value=review,
        ) as read,
        patch(
            "core.datastream_preconfiguration_api.prepare_confirmation",
            return_value=issued,
        ) as prepare,
    ):
        fetched = _client().get(f"{endpoint}/candidate-review")
        confirmation = _client().post(
            f"{endpoint}/publish-confirmations",
            headers={"Idempotency-Key": "prepare-candidate-1"},
            json={},
        )

    assert fetched.status_code == 200
    assert confirmation.status_code == 201
    assert read.call_args.kwargs["execution_id"] == "dse_1"
    assert prepare.call_args.kwargs["resource_id"] == "dse_1"
    assert prepare.call_args.kwargs["content_hash"] == "r" * 64


def test_cross_project_candidate_read_is_nondisclosing() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.read_candidate_review") as read,
    ):
        response = _client().get(
            "/api/projects/proj_foreign/datastreams/ds_1/executions/dse_1/candidate-review"
        )

    assert response.status_code == 404
    read.assert_not_called()
