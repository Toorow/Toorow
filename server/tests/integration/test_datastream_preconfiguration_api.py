"""Real ASGI seams for Story 47.2 setup drafts and proposals."""

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


def test_create_requires_idempotency_and_cross_scope_is_not_disclosed() -> None:
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
    ):
        missing = _client().post("/api/projects/proj_1/datastream-setup-drafts", json={})
    assert missing.status_code == 422
    assert missing.json()["code"] == "missing_idempotency_key"

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_foreign"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.create_or_resume_draft") as create,
    ):
        denied = _client().post(
            "/api/projects/proj_foreign/datastream-setup-drafts",
            headers={"Idempotency-Key": "scope-denied"},
            json={},
        )
    assert denied.status_code == 404
    create.assert_not_called()


def test_create_replay_and_proposal_read_use_canonical_routes() -> None:
    draft = {
        "draft_ref": "dsd_1",
        "current_revision": 1,
        "idempotent_replay": True,
        "resume_href": "/projects/proj_1/data/datastreams/add?draft=dsd_1",
    }
    proposal = {
        "proposal_ref": "dspp_1",
        "draft_ref": "dsd_1",
        "is_stale": False,
        "proposal_token": "a" * 64,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.create_or_resume_draft", return_value=draft),
    ):
        response = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts",
            headers={"Idempotency-Key": "create-1"},
            json={},
        )
    assert response.status_code == 200
    assert response.json()["resume_href"] == draft["resume_href"]

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.read_proposal", return_value=proposal),
    ):
        response = _client().get(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/proposals/dspp_1"
        )
    assert response.status_code == 200
    assert response.json()["proposal_token"] == "a" * 64


def test_compile_requires_exact_revision_and_is_non_authorizing() -> None:
    compiled = {
        "draft_ref": "dsd_1",
        "draft_revision_ref": "dsdr_1",
        "proposal_ref": "dspp_1",
        "dependency_fingerprint": "b" * 64,
        "proposal_token": "c" * 64,
        "is_stale": False,
        "idempotent_replay": False,
    }
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.datastream_preconfiguration_api.compile_draft", return_value=compiled
        ) as compile_call,
    ):
        response = _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/compile",
            headers={"Idempotency-Key": "compile-1"},
            json={"expected_revision": 3},
        )
    assert response.status_code == 201
    assert response.json()["proposal_ref"] == "dspp_1"
    assert compile_call.call_args.kwargs["expected_revision"] == 3
    assert {"datastream_id", "enabled", "current_plan_version_id"}.isdisjoint(response.json())


def _preview_fixtures(*, quota_cost):
    """One draft, one proposal and one observation, pinned to each other."""
    draft = {
        "draft_ref": "dsd_1",
        "current_revision": 4,
        "operator_input": {"mode": "external_bq", "source": {"object_ref": "wh.raw.events"}},
    }
    proposal = {
        "proposal_ref": "dspp_1",
        "draft_revision_ref": "dsdr_4",
        "dependency_fingerprint": "b" * 64,
        "is_stale": False,
        "dependency_snapshot": {
            "project_configuration": {"version_id": "pcv_1"},
            "capabilities": [],
        },
        "section_fingerprints": {"physical_mapping": "m" * 64, "processing": "p" * 64},
    }
    observation = {
        "observation_ref": "dso_1",
        "draft_revision_ref": "dsdr_4",
        "connector_contract_version_ref": None,
        "safe_metadata": {"fields": [], **({"quota_cost": quota_cost} if quota_cost else {})},
    }
    return draft, proposal, observation


def _post_preview(draft, proposal, observation, queued=None):
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.read_draft", return_value=draft),
        patch("core.datastream_preconfiguration_api.read_proposal", return_value=proposal),
        patch("core.datastream_preconfiguration_api.read_observation", return_value=observation),
        patch("core.queue.enqueue_activation_work", return_value=queued or {"job_id": "job_1"}),
        patch("core.datastream_preconfiguration_api._dispatch_activation_task"),
    ):
        return _client().post(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1/previews",
            headers={"Idempotency-Key": "preview-1"},
            json={
                "expected_revision": 4,
                "proposal_ref": "dspp_1",
                "observation_ref": "dso_1",
            },
        )


def test_external_bigquery_preview_is_refused_without_a_scan_estimate() -> None:
    """ESTIMATE BEFORE SCAN. The preview is the read an operator can launch.

    BigQuery bills bytes scanned, so a read whose cost is unknown is refused
    rather than taken, and the refusal names what is missing and how to get it.
    """
    refused = _post_preview(*_preview_fixtures(quota_cost=None))
    assert refused.status_code == 422
    assert "No scan estimate is attached to this observation" in refused.json()["message"]
    assert "Discover source" in refused.json()["message"]


def test_external_bigquery_preview_runs_once_the_estimate_is_attached() -> None:
    """The same request with the estimate is queued -- the guard is not a wall."""
    accepted = _post_preview(
        *_preview_fixtures(quota_cost={"bytes_scanned_estimate": 4096, "unit": "bytes"})
    )
    assert accepted.status_code == 202
    assert accepted.json()["draft_ref"] == "dsd_1"


def test_competing_autosave_returns_conflict_without_scope_disclosure() -> None:
    from core.datastream_preconfiguration import PreconfigurationConflict

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.datastream_preconfiguration_api.update_draft",
            side_effect=PreconfigurationConflict(
                "Expected draft revision does not match current revision"
            ),
        ),
    ):
        response = _client().patch(
            "/api/projects/proj_1/datastream-setup-drafts/dsd_1",
            headers={"Idempotency-Key": "autosave-competing"},
            json={
                "expected_revision": 2,
                "operator_input": {"mode": "connector_pull"},
            },
        )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


# ---------------------------------------------------------------------------
# Live-Postgres walks of the wizard flow (create -> configure -> compile).
# They need TEST_POSTGRES_DSN on a disposable instance; without it they skip.
# ---------------------------------------------------------------------------


def _pg_project(conn) -> str:
    import uuid

    from tests.conftest import TEST_ORG_ID

    project_id = f"proj_dsd_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', 'test@example.com')",
            (project_id, TEST_ORG_ID, "Datastream setup test", project_id.lower()),
        )
    return project_id


def test_create_draft_and_idempotent_replay_return_the_saved_operator_input(
    live_postgres,
) -> None:
    """The create route is ALSO the resume door (one resumable draft per Project).

    Reopening a configured draft returned every field BUT `operator_input`, so
    the wizard rendered EMPTY on a filled draft and the first edit overwrote the
    saved input at the same accepted revision. Both create returns now carry the
    input, in the same shape `read_draft` already used.
    """
    from core.datastream_preconfiguration import create_or_resume_draft, update_draft

    project_id = _pg_project(live_postgres)
    created = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-1"
    )
    assert created["operator_input"] == {}

    saved = {
        "mode": "managed_feed",
        "source": {"channel": "webhook"},
        "wizard_state": {"step": "configure"},
    }
    update_draft(
        live_postgres,
        project_id=project_id,
        draft_id=created["draft_ref"],
        actor="test",
        idempotency_key="autosave-1",
        expected_revision=created["current_revision"],
        operator_input=saved,
    )

    replayed = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-2"
    )
    assert replayed["idempotent_replay"] is True
    assert replayed["draft_ref"] == created["draft_ref"]
    assert replayed["operator_input"] == saved


def _pin_contract_version(conn, *, connector_ref: str) -> None:
    """Pin the module's REAL manifest as its contract snapshot, the way a binding does."""
    import hashlib
    import json

    from core.data_identities import mint_data_id

    from tests.conftest import REPO_ROOT

    manifest = json.loads(
        (REPO_ROOT / "server" / "modules" / connector_ref / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.connector_contract_versions "
            "(id, environment, connector_id, version_number, contract_schema_version, "
            "connector_fingerprint, contract_snapshot, validation_evidence, created_by) "
            "VALUES (%s, 'test', %s, 1, '1', %s, %s::jsonb, '{}'::jsonb, 'test')",
            (
                mint_data_id("ccv"),
                connector_ref,
                hashlib.sha256(f"test-pin:{connector_ref}".encode()).hexdigest(),
                json.dumps(manifest),
            ),
        )


def _connector_pull_input(metrics: str) -> dict:
    return {
        "mode": "connector_pull",
        "name": "Sponsored Products",
        "data_role": "Spend",
        "source": {
            "connector_ref": "amazon-ads",
            "source_account_ref": "sacct_test",
            "report_ref": "sp_campaigns_daily",
        },
        "configure": {
            "date_field": "date",
            "metrics": metrics,
            "dimensions": "date,campaignId,campaignName,campaignStatus,campaignBudgetCurrencyCode",
            "grain": "date,campaignId",
        },
        "schedule": {"mode": "manual"},
    }


_FULL_BUNDLE = "impressions,clicks,cost,purchases14d,purchases30d,sales14d,sales30d"


def _configured_connector_pull_draft(conn, *, metrics: str) -> tuple[str, dict]:
    from core.datastream_preconfiguration import create_or_resume_draft, update_draft

    project_id = _pg_project(conn)
    _pin_contract_version(conn, connector_ref="amazon-ads")
    draft = create_or_resume_draft(
        conn, project_id=project_id, actor="test", idempotency_key="create-1"
    )
    configured = update_draft(
        conn,
        project_id=project_id,
        draft_id=draft["draft_ref"],
        actor="test",
        idempotency_key="autosave-1",
        expected_revision=draft["current_revision"],
        operator_input=_connector_pull_input(metrics),
    )
    return project_id, {"draft": draft, "configured": configured}


def test_connector_pull_compile_names_the_pinned_contract_scope(live_postgres) -> None:
    """The intent's scope is the scope the contract was normalized with.

    The intent carried `source_account_ref` -- a Source Account id -- in
    `connection_ref_id`, while the pinned contract is normalized as
    `contract:<connector>`. The scope check compared unlike values, so EVERY
    configured connector_pull compile answered 422 `capability_scope_mismatch`.
    """
    from core.datastream_preconfiguration import compile_draft

    project_id, state = _configured_connector_pull_draft(
        live_postgres, metrics=_FULL_BUNDLE
    )
    proposal = compile_draft(
        live_postgres,
        project_id=project_id,
        draft_id=state["draft"]["draft_ref"],
        actor="test",
        idempotency_key="compile-1",
        expected_revision=state["configured"]["current_revision"],
    )
    assert proposal["draft_ref"] == state["draft"]["draft_ref"]
    assert proposal["proposal_ref"]


def test_connector_pull_compile_still_refuses_a_field_the_report_does_not_offer(
    live_postgres,
) -> None:
    """The scope fix must not weaken the capability check itself."""
    import pytest
    from core.datastream_preconfiguration import (
        PreconfigurationValidationError,
        compile_draft,
    )

    project_id, state = _configured_connector_pull_draft(
        live_postgres, metrics=f"{_FULL_BUNDLE},not_a_metric"
    )
    with pytest.raises(PreconfigurationValidationError, match="unknown_report_field"):
        compile_draft(
            live_postgres,
            project_id=project_id,
            draft_id=state["draft"]["draft_ref"],
            actor="test",
            idempotency_key="compile-1",
            expected_revision=state["configured"]["current_revision"],
        )


# ---------------------------------------------------------------------------
# `Discard this draft` -- the terminal path, ratified by Jean 2026-08-31
# (AI-336). Migration 284 kept `archived` declared and OUTSIDE the resumable
# predicate so a terminal path could land on it; until this gesture the state
# had zero writers and no path could reach it.
# ---------------------------------------------------------------------------


def test_discarding_a_draft_writes_the_state_the_check_declares(live_postgres) -> None:
    """The state stops being a word no path can produce.

    Asserted against the ROW rather than the return payload: the CHECK constraint
    `state IN ('draft','materialized','archived')` and the pairing CHECK
    `(state='materialized') = (materialized_datastream_id IS NOT NULL)` are what
    an `archived` write has to satisfy, and a payload cannot prove either.
    """
    from core.datastream_preconfiguration import create_or_resume_draft, discard_draft

    project_id = _pg_project(live_postgres)
    created = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-discard-1"
    )

    discarded = discard_draft(
        live_postgres, project_id=project_id, draft_id=created["draft_ref"], actor="test"
    )
    assert discarded["state"] == "archived"
    assert discarded["idempotent_replay"] is False

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT state, materialized_datastream_id FROM app.datastream_setup_drafts "
            "WHERE id=%s AND project_id=%s",
            (created["draft_ref"], project_id),
        )
        row = cur.fetchone()
    assert row == ("archived", None)

    # THE ROW SURVIVES, and so does its history: this is a soft archive, and the
    # revisions carry what was tried. Deleting them is what the state exists to
    # avoid.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.datastream_setup_draft_revisions WHERE draft_id=%s",
            (created["draft_ref"],),
        )
        assert cur.fetchone()[0] >= 1


def test_discarding_frees_the_projects_one_resumable_slot(live_postgres) -> None:
    """The half migration 284 protected when it refused to reuse `exited`.

    `uq_datastream_setup_draft_resumable` is partial over `state = 'draft'`. Had
    the terminal state stayed inside that predicate, this second create would
    have resumed the abandoned draft instead of starting a clean one -- the
    semantic inversion 284 names.
    """
    from core.datastream_preconfiguration import create_or_resume_draft, discard_draft

    project_id = _pg_project(live_postgres)
    first = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-slot-1"
    )
    discard_draft(live_postgres, project_id=project_id, draft_id=first["draft_ref"], actor="test")

    second = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-slot-2"
    )
    assert second["draft_ref"] != first["draft_ref"]
    assert second["idempotent_replay"] is False
    assert second["state"] == "draft"


def test_discarding_twice_is_the_same_discard(live_postgres) -> None:
    """Idempotent, and answered as a replay -- never an error about a state."""
    from core.datastream_preconfiguration import create_or_resume_draft, discard_draft

    project_id = _pg_project(live_postgres)
    created = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-twice"
    )
    discard_draft(live_postgres, project_id=project_id, draft_id=created["draft_ref"], actor="test")

    again = discard_draft(
        live_postgres, project_id=project_id, draft_id=created["draft_ref"], actor="test"
    )
    assert again["state"] == "archived"
    assert again["idempotent_replay"] is True


def test_a_discarded_draft_no_longer_autosaves(live_postgres) -> None:
    """Terminal on every WRITE, not only on the resume.

    The autosave used to write `state='draft'` on the same statement that moved
    the revision pointer, so without this refusal the first keystroke after a
    discard would have brought the draft back to life -- and the refusal alone
    would not be enough either, which is why that clause is gone.
    """
    import pytest
    from core.datastream_preconfiguration import (
        PreconfigurationConflict,
        create_or_resume_draft,
        discard_draft,
        update_draft,
    )

    project_id = _pg_project(live_postgres)
    created = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-autosave"
    )
    discard_draft(live_postgres, project_id=project_id, draft_id=created["draft_ref"], actor="test")

    with pytest.raises(PreconfigurationConflict, match="was discarded"):
        update_draft(
            live_postgres,
            project_id=project_id,
            draft_id=created["draft_ref"],
            actor="test",
            idempotency_key="autosave-after-discard",
            expected_revision=created["current_revision"],
            operator_input={"mode": "managed_feed"},
        )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.datastream_setup_drafts WHERE id=%s",
            (created["draft_ref"],),
        )
        assert cur.fetchone()[0] == "archived"


def test_a_discarded_draft_can_no_longer_be_published(live_postgres) -> None:
    """The publication gates refuse it, and the SECOND one is not redundant.

    A final review frozen BEFORE the discard stays confirmable afterwards, and
    the materialization's own `AND state IN ('draft','materialized')` would have
    updated zero rows in silence while a Datastream was created anyway -- a
    Datastream whose draft never recorded it.
    """
    import pytest
    from core.datastream_activation import materialize_draft_mutation, persist_final_review
    from core.datastream_preconfiguration import (
        PreconfigurationConflict,
        create_or_resume_draft,
        discard_draft,
    )

    project_id = _pg_project(live_postgres)
    created = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-publish"
    )
    draft_id = created["draft_ref"]
    discard_draft(live_postgres, project_id=project_id, draft_id=draft_id, actor="test")

    with pytest.raises(PreconfigurationConflict, match="was discarded"):
        persist_final_review(
            live_postgres,
            project_id=project_id,
            draft_id=draft_id,
            preview_id="dspv_unused",
            actor="test",
            snapshot={"content_hash": "a" * 64},
        )

    with pytest.raises(PreconfigurationConflict, match="was discarded"):
        materialize_draft_mutation(
            live_postgres,
            operation_id="op_unused",
            review={"project_id": project_id, "draft_id": draft_id, "org_id": "org_unused"},
            actor="test",
        )


def test_a_materialized_draft_is_refused_and_the_refusal_names_the_right_gesture(
    live_postgres,
) -> None:
    """Not a state word: the Datastream is what gets archived, on its own screen.

    The database says the same thing -- `(state='materialized') =
    (materialized_datastream_id IS NOT NULL)` -- so writing `archived` here would
    raise a CheckViolation nobody could act on.
    """
    import pytest
    from core.datastream_preconfiguration import (
        PreconfigurationConflict,
        create_or_resume_draft,
        discard_draft,
    )

    project_id = _pg_project(live_postgres)
    created = create_or_resume_draft(
        live_postgres, project_id=project_id, actor="test", idempotency_key="create-materialized"
    )
    from tests.conftest import TEST_ORG_ID

    datastream_id = "ds_" + ("0" * 26)
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams (id,org_id,project_id,name,source_kind,created_by) "
            "VALUES (%s,%s,%s,%s,'managed_feed','test@example.com')",
            (datastream_id, TEST_ORG_ID, project_id, "Materialized by a draft"),
        )
        cur.execute(
            "UPDATE app.datastream_setup_drafts "
            "SET state='materialized',materialized_datastream_id=%s WHERE id=%s",
            (datastream_id, created["draft_ref"]),
        )

    with pytest.raises(PreconfigurationConflict, match="already created a Datastream"):
        discard_draft(
            live_postgres, project_id=project_id, draft_id=created["draft_ref"], actor="test"
        )


def test_the_discard_route_is_the_delete_verb_on_the_draft_resource() -> None:
    """One verb, one meaning on this surface.

    `DELETE /api/datastreams/{id}` already archives softly; the draft resource
    answers the same verb the same way. A `POST .../discard` would have been a
    second spelling of an act the surface already has a word for.
    """
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.datastream_preconfiguration_api.discard_draft",
            return_value={"draft_ref": "dsd_1", "state": "archived", "idempotent_replay": False},
        ) as discard,
    ):
        response = _client().delete("/api/projects/proj_1/datastream-setup-drafts/dsd_1")
    assert response.status_code == 200
    assert response.json()["state"] == "archived"
    discard.assert_called_once()

    # A person without `edit` is told nothing, and the writer is not reached --
    # the same non-disclosure every other draft write applies.
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_foreign"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=False),
        patch("core.db.get_connection", side_effect=_connection),
        patch("core.datastream_preconfiguration_api.discard_draft") as never,
    ):
        denied = _client().delete("/api/projects/proj_foreign/datastream-setup-drafts/dsd_1")
    assert denied.status_code == 404
    never.assert_not_called()


def test_the_discard_refusal_reaches_the_console_as_a_named_conflict() -> None:
    """A refusal that falls into the 503 catch-all names no gesture."""
    from core.datastream_preconfiguration import PreconfigurationConflict

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person_1"))),
        patch("core.admin_api._strict_project_capability_allowed", return_value=True),
        patch("core.db.get_connection", side_effect=_connection),
        patch(
            "core.datastream_preconfiguration_api.discard_draft",
            side_effect=PreconfigurationConflict(
                "This setup already created a Datastream. Archive the Datastream from its "
                "own screen; the draft that created it is kept as its record."
            ),
        ),
    ):
        response = _client().delete("/api/projects/proj_1/datastream-setup-drafts/dsd_1")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert "Archive the Datastream from its own screen" in response.json()["message"]
