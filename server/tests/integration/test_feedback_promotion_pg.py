"""Story 65.9 real-PostgreSQL promotion, evaluation, resolution and OAuth gates."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from core.feedback_regression import (
    FeedbackRegressionRefused,
    evaluate_feedback_regression_case,
    promote_feedback_regression,
    resolve_feedback_regression,
)
from ulid import ULID

from tests.core.test_feedback_review import _pg_seed
from tests.fixture_generators.feedback_regression import build_fixture

pytestmark = [pytest.mark.pg_app_role]

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "252_feedback_regression_cases.sql"
)


@pytest.fixture
def regression_actor(live_postgres) -> str:
    """The one identity this module writes as -- the canonical `person_<ULID>`.

    Idempotency here is scoped to the actor: `uq_feedback_regression_cases_retry`
    (`infra/nango/migrations/252_feedback_regression_cases.sql:103`) is UNIQUE on
    (feedback_id, created_by, retry_key_hash, org_id, project_id) and both
    `promote_feedback_regression` and `evaluate_feedback_regression_case` hash
    the actor into the retry key. Since 2026-08-24 the HTTP door attributes a
    write to the canonical person `authenticate_api_request` resolves
    (`core/api_auth.py:145-147`), never to the raw OIDC subject -- so evidence
    produced in-process under the bare address `owner@example.com` is evidence
    the door can no longer replay. The producer and the door use one key.
    """
    return _person(live_postgres, "owner@example.com")


@pytest.fixture
def regression_evidence(live_postgres, regression_actor) -> dict:
    # Depend on the per-test PG fixture so its isolation scrub completes before
    # the producer commits the immutable lineage exercised by this test.
    assert live_postgres.info.dbname
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    return asyncio.run(build_fixture(dsn, normalize=False, actor=regression_actor))


def _scope(conn, evidence: dict) -> tuple[str, str, str]:
    feedback_id = evidence["scope"]["feedback_id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, project_id FROM app.feedback_review_subjects "
            "WHERE subject_id = %s",
            (feedback_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(feedback_id)


def test_real_pg_producer_creates_only_trusted_exact_lineage(regression_evidence):
    document = regression_evidence
    assert document["schema_version"] == "feedback-regression-fixture.v1"
    assert document["draft"]["schema_version"] == "feedback-regression-draft.v1"
    assert document["draft"]["state"] == "available"
    assert document["create_receipt"]["schema_version"] == "feedback-regression-receipt.v1"
    assert document["create_receipt"]["status"] == "created"
    assert document["create_replay_receipt"] == document["create_receipt"]
    assert document["golden_question"]["current_version"]["contract_version"] == (
        "golden-question.v2"
    )
    assert document["evaluation_run"]["lifecycle"] == "finalized"
    assert document["evaluation_receipt"]["schema_version"] == (
        "evaluation-result-receipt.v1"
    )
    assert document["evaluation_receipt"]["producer"] == "result-case-evaluator.v1"
    assert len(document["evaluation_receipt"]["verdicts"]) == 6
    assert all(
        item["assertion_result_id"].startswith("ear_")
        for item in document["evaluation_receipt"]["assertion_results"]
    )
    assert all(
        verdict["verdict_id"].startswith("edv_")
        for verdict in document["evaluation_receipt"]["verdicts"].values()
    )
    assert document["resolution_receipt"]["status"] == "resolved"
    assert document["resolution_receipt"]["evaluation_run_id"] == document["evaluation_run"][
        "id"
    ]
    assert document["resolution_receipt"]["evaluation_case_id"] == document[
        "evaluation_case"
    ]["id"]
    # The resolution pins the verdict of the dimension the human review named as
    # affected (`core/feedback_regression.py:930` selects `rv.affected_dimension`),
    # not a fixed dimension: naming the literal here is how this assertion went
    # stale when 44edbf81 moved the producer from `path_quality` to
    # `semantic_correctness`.
    affected_dimension = document["review_command"]["affected_dimension"]
    assert document["resolution_receipt"]["verdict_id"] == document["evaluation_receipt"][
        "verdicts"
    ][affected_dimension]["verdict_id"]
    run_id = document["evaluation_receipt"]["run_id"]
    case_id = document["evaluation_receipt"]["evaluation_case_id"]
    base_link = {
        "workspace": "test",
        "section": "regression-runs",
        "object_type": "evaluation-run",
        "object_id": run_id,
        "tab": "cases",
    }
    pass_ids = [
        verdict["verdict_id"]
        for verdict in document["evaluation_receipt"]["verdicts"].values()
        if verdict["verdict"] == "pass"
    ]
    assert document["evaluation_receipt"]["owner_links"] == [
        {**base_link, "version_id": None},
        {**base_link, "version_id": case_id},
        *[{**base_link, "version_id": verdict_id} for verdict_id in pass_ids],
    ]
    assert document["resolution_receipt"]["owner_links"] == [
        {**base_link, "version_id": None},
        {**base_link, "version_id": case_id},
        {**base_link, "version_id": document["resolution_receipt"]["verdict_id"]},
    ]
    assert len(json.dumps(document, separators=(",", ":"), default=str).encode()) <= 262_144


def test_promotion_and_evaluation_replay_but_changed_retry_payload_is_refused(
    live_postgres, regression_evidence, regression_actor
):
    org_id, project_id, feedback_id = _scope(live_postgres, regression_evidence)
    created = regression_evidence["create_receipt"]
    replay = promote_feedback_regression(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        actor=regression_actor,
        payload=regression_evidence["create_command"],
    )
    assert replay == created

    changed = dict(regression_evidence["create_command"], title="Changed retry body")
    with pytest.raises(FeedbackRegressionRefused) as raised:
        promote_feedback_regression(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            actor=regression_actor,
            payload=changed,
        )
    assert raised.value.code == "idempotency_conflict"
    live_postgres.rollback()

    case_id = regression_evidence["evaluation_case"]["id"]
    replayed_evaluation = evaluate_feedback_regression_case(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        case_id=case_id,
        actor=regression_actor,
        payload=regression_evidence["evaluation_command"],
    )
    assert replayed_evaluation == regression_evidence["evaluation_receipt"]
    live_postgres.rollback()


def test_caller_cannot_supply_verdicts_and_failed_evaluation_writes_nothing(
    live_postgres, regression_evidence, regression_actor
):
    org_id, project_id, _feedback_id = _scope(live_postgres, regression_evidence)
    case_id = regression_evidence["evaluation_case"]["id"]
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.evaluation_assertion_results WHERE case_id = %s",
            (case_id,),
        )
        before_assertions = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM app.evaluation_case_dimension_verdicts WHERE case_id = %s",
            (case_id,),
        )
        before_verdicts = cur.fetchone()[0]
    crafted = dict(
        regression_evidence["evaluation_command"],
        retry_key="fixture-crafted-pass",
        verdicts={"path_quality": "pass"},
    )
    with pytest.raises(FeedbackRegressionRefused) as raised:
        evaluate_feedback_regression_case(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            case_id=case_id,
            actor=regression_actor,
            payload=crafted,
        )
    assert raised.value.code in {"unknown_field", "invalid_request"}
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.evaluation_assertion_results WHERE case_id = %s",
            (case_id,),
        )
        assert cur.fetchone()[0] == before_assertions
        cur.execute(
            "SELECT count(*) FROM app.evaluation_case_dimension_verdicts WHERE case_id = %s",
            (case_id,),
        )
        assert cur.fetchone()[0] == before_verdicts
    live_postgres.rollback()


def test_resolution_rejects_incompatible_or_absent_evidence_without_new_lineage(
    live_postgres, regression_evidence, regression_actor
):
    org_id, project_id, feedback_id = _scope(live_postgres, regression_evidence)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.feedback_regression_resolutions WHERE feedback_id = %s",
            (feedback_id,),
        )
        before = cur.fetchone()[0]
    receipt = regression_evidence["incompatible_resolution_receipt"]
    assert receipt["status"] == "unresolved"
    assert receipt["resolution_id"] is None
    assert receipt["reason"] == "trusted_pass_incompatible"
    assert regression_evidence["incompatible_resolution_count_before"] == 0
    assert regression_evidence["incompatible_resolution_count_after"] == 0
    mismatched_case = regression_evidence["mismatched_evaluation_case"]
    assert mismatched_case["id"] == receipt["evaluation_case_id"]
    assert mismatched_case["result_id"] != regression_evidence["evaluation_case"]["result_id"]
    assert (
        mismatched_case["golden_question_version_id"]
        != regression_evidence["evaluation_case"]["golden_question_version_id"]
    )
    assert mismatched_case["feedback_regression"] == {
        "feedback_id": feedback_id,
        "regression_case_id": regression_evidence["mismatched_regression_case_id"],
    }
    assert regression_evidence["mismatched_evaluation_receipt"]["producer"] == (
        "result-case-evaluator.v1"
    )
    replay = resolve_feedback_regression(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        actor=regression_actor,
        payload=regression_evidence["resolution_command"],
    )
    assert replay == regression_evidence["resolution_receipt"]
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.feedback_regression_resolutions WHERE feedback_id = %s",
            (feedback_id,),
        )
        assert cur.fetchone()[0] == before
    live_postgres.rollback()


def test_new_story_tables_are_force_rls_append_only_and_not_share_readable(
    live_postgres, regression_evidence
):
    tables = [
        "feedback_regression_cases",
        "evaluation_assertion_results",
        "feedback_regression_resolutions",
    ]
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'app' AND c.relname = ANY(%s)
             ORDER BY c.relname
            """,
            (tables,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT bool_or(has_table_privilege('toorow_share_reader',
                                                format('app.%%I', name), 'SELECT'))
              FROM unnest(%s::text[]) AS name
            """,
            (tables,),
        )
        share_can_read = cur.fetchone()[0]
    assert rows == sorted((name, True, True) for name in tables)
    assert share_can_read is False

    identifiers = {
        "feedback_regression_cases": regression_evidence["create_receipt"][
            "regression_case_id"
        ],
        "feedback_regression_resolutions": regression_evidence["resolution_receipt"][
            "resolution_id"
        ],
    }
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.evaluation_assertion_results WHERE case_id = %s "
            "ORDER BY assertion_ordinal LIMIT 1",
            (regression_evidence["evaluation_case"]["id"],),
        )
        assertion = cur.fetchone()
    assert assertion is not None
    identifiers["evaluation_assertion_results"] = assertion[0]
    for table, row_id in identifiers.items():
        # UPDATE is refused by the PRIVILEGE, not by the trigger. Migration 252
        # granted SELECT, INSERT only, but 207's `ALTER DEFAULT PRIVILEGES` had
        # already handed `connector` UPDATE on every future table, so the narrow
        # grant was declarative until migration 316 revoked it
        # (`316_a_narrow_grant_is_declarative_until_the_revoke_lands.sql:71-73`,
        # pinned by `tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py`).
        # PostgreSQL checks the privilege first, so the append-only trigger is
        # never reached and the refusal is 42501 -- the stricter of the two.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(f"UPDATE app.{table} SET id = id WHERE id = %s", (row_id,))
        # DELETE keeps its privilege -- the RGPD erasure hatch needs it (316's
        # header) -- so here the append-only trigger is what speaks.
        with pytest.raises(Exception, match="append-only"), live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(f"DELETE FROM app.{table} WHERE id = %s", (row_id,))
        with pytest.raises(Exception), live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(f"TRUNCATE app.{table}")
    live_postgres.rollback()


def test_migration_declares_additive_v2_and_trusted_producer_guards():
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert "golden-question.v1" in sql and "golden-question.v2" in sql
    assert "result-case-evaluator.v1" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "toorow_share_reader" in sql
    assert "REVOKE ALL" in sql


#: The issuer this suite's fake tokens are signed by. It has to be a real value
#: and it has to be CONFIGURED: canonical resolution keys a person on
#: (issuer, subject), so a token carrying no `iss` resolves to nobody and every
#: request 401s before it reaches the access seam under test.
_ISSUER = "https://feedback-promotion.test"


class _BearerVerifier:
    def __init__(self, identities: dict[str, str]) -> None:
        self.identities = identities

    async def verify_token(self, token: str):
        identity = self.identities.get(token)
        if identity is None:
            return None
        return SimpleNamespace(
            subject=identity,
            client_id=None,
            claims={"sub": identity, "iss": _ISSUER},
        )


def _person(conn, subject: str) -> str:
    """The canonical person a subject authenticates to -- the ONLY membership key.

    `app.org_members.identity` holds a `person_<ULID>`, never the raw OIDC
    subject. Until 2026-08-24 this suite could seed the subject directly because
    it set the identity flag to "0" and got the legacy resolver; with the flag
    removed there is one key, and the test seeds the one the server will look up.
    """
    from core.canonical_identity import resolve_canonical_identity

    person_id = resolve_canonical_identity(conn, issuer=_ISSUER, subject=subject).person_id
    conn.commit()
    return person_id


def test_oauth_asgi_uses_request_connection_and_hides_denied_foreign_missing(
    live_postgres, regression_evidence, regression_actor, monkeypatch
):
    from core import api_auth
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    org_id, project_id, feedback_id = _scope(live_postgres, regression_evidence)
    _other_org, foreign_project, _result = _pg_seed(live_postgres)
    viewer = "owner@example.com"
    outsider = f"regression-outsider-{secrets.token_hex(4)}@example.com"
    # The token's subject resolves to the SAME canonical person the producer
    # wrote as, which is what lets the three write doors below replay the
    # lineage instead of re-opening a promotion the resolution already closed.
    viewer_person = _person(live_postgres, viewer)
    assert viewer_person == regression_actor
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
            (f"omem_{ULID()}", org_id, viewer_person),
        )
    live_postgres.commit()

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_ISSUER", _ISSUER)
    monkeypatch.setenv("PLATFORM_DB_URL", live_postgres.info.dsn)
    monkeypatch.setattr(
        api_auth,
        "_verifier",
        lambda: _BearerVerifier({"viewer-token": viewer, "outsider-token": outsider}),
    )
    client = TestClient(build_asgi_app(), raise_server_exceptions=True)
    visible_path = (
        f"/api/projects/{project_id}/test/feedback/{feedback_id}/regression-draft"
    )
    visible = client.get(visible_path, headers={"Authorization": "Bearer viewer-token"})
    denied = client.get(visible_path, headers={"Authorization": "Bearer outsider-token"})
    foreign = client.get(
        f"/api/projects/{foreign_project}/test/feedback/{feedback_id}/regression-draft",
        headers={"Authorization": "Bearer viewer-token"},
    )
    missing = client.get(
        f"/api/projects/{project_id}/test/feedback/fba_missing/regression-draft",
        headers={"Authorization": "Bearer viewer-token"},
    )
    anonymous = client.get(visible_path)

    assert visible.status_code == 200, visible.text
    assert anonymous.status_code == 401
    assert denied.status_code == foreign.status_code == missing.status_code == 404
    assert denied.content == foreign.content == missing.content
    for response in (denied, foreign, missing):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Authorization"

    promotion = client.post(
        f"/api/projects/{project_id}/test/feedback/{feedback_id}/regression-cases",
        headers={"Authorization": "Bearer viewer-token"},
        json=regression_evidence["create_command"],
    )
    assert promotion.status_code == 201, promotion.text
    assert promotion.json() == regression_evidence["create_receipt"]

    evaluate = client.post(
        f"/api/projects/{project_id}/test/evaluation-cases/"
        f"{regression_evidence['evaluation_case']['id']}/evaluate",
        headers={"Authorization": "Bearer viewer-token"},
        json=regression_evidence["evaluation_command"],
    )
    assert evaluate.status_code == 201, evaluate.text
    assert evaluate.json() == regression_evidence["evaluation_receipt"]

    resolution = client.post(
        f"/api/projects/{project_id}/test/feedback/{feedback_id}/regression-resolution",
        headers={"Authorization": "Bearer viewer-token"},
        json=regression_evidence["resolution_command"],
    )
    assert resolution.status_code == 201, resolution.text
    assert resolution.json() == regression_evidence["resolution_receipt"]
