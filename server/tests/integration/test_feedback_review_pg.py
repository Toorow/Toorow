"""Story 65.8 PostgreSQL gates for review subjects and eligible observations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from core.feedback_review import (
    FeedbackRefused,
    aggregate_feedback,
    append_review_version,
    get_annotation,
    list_annotations,
    list_unresolved_critical_negatives,
)
from ulid import ULID

from tests.core.test_feedback_review import _pg_annotation, _pg_seed

# This module MINTS a Render Share (`core.render_shares`), so it declares the
# pepper and the origin instead of borrowing them from a neighbour's
# module-level `os.environ.setdefault` (AI-377).
from tests.support.render_share_env import render_share_env  # noqa: F401

pytestmark = pytest.mark.usefixtures("live_postgres")

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "251_feedback_review_exact_cohorts.sql"
)


def test_migration_declares_two_real_subject_foreign_keys_and_xor():
    sql = _MIGRATION.read_text(encoding="utf-8")
    assert "feedback_review_subjects" in sql
    assert "authenticated_feedback_id" in sql
    assert "share_feedback_id" in sql
    assert "num_nonnulls(authenticated_feedback_id, share_feedback_id) = 1" in sql
    assert "REFERENCES app.feedback_annotations (id, org_id, project_id)" in sql
    assert "REFERENCES app.render_share_feedback (id, org_id, project_id)" in sql


def test_migration_backfills_both_pre_251_stores_before_redirecting_review_heads():
    sql = _MIGRATION.read_text(encoding="utf-8")
    authenticated_backfill = sql.index("FROM app.feedback_annotations\n")
    share_backfill = sql.index("FROM app.render_share_feedback\n")
    redirected_head = sql.index("DROP CONSTRAINT IF EXISTS fk_feedback_reviews_annotation")
    missing_share_heads = sql.index("INSERT INTO app.feedback_reviews")

    assert authenticated_backfill < redirected_head
    assert share_backfill < redirected_head
    assert redirected_head < missing_share_heads
    assert "SELECT id, org_id, project_id, id\nFROM app.feedback_annotations" in sql
    assert "SELECT id, org_id, project_id, id\nFROM app.render_share_feedback" in sql
    assert "NULL, 'unreviewed', created_at" in sql


def test_story_65_8_tables_are_force_rls_and_runtime_role_is_ordinary(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'app' AND c.relname = ANY(%s)
            ORDER BY c.relname
            """,
            (
                [
                    "feedback_eligible_observations",
                    "feedback_review_retries",
                    "feedback_review_subjects",
                ],
            ),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        privileged = cur.fetchone()
    assert rows == [
        ("feedback_eligible_observations", True, True),
        ("feedback_review_retries", True, True),
        ("feedback_review_subjects", True, True),
    ]
    assert privileged == (False, False)


def test_share_role_has_only_the_new_definer_execute_door(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT has_table_privilege(
                       'toorow_share_reader', 'app.feedback_eligible_observations', 'SELECT'),
                   has_table_privilege(
                       'toorow_share_reader', 'app.feedback_review_subjects', 'SELECT'),
                   has_table_privilege('toorow_share_reader', 'app.feedback_reviews', 'SELECT'),
                   has_function_privilege(
                       'toorow_share_reader',
                       'app.record_render_share_feedback_eligibility_v1(text,text)',
                       'EXECUTE'
                   ),
                   has_function_privilege(
                       'toorow_share_reader',
                       'app.record_render_share_feedback_v1('
                       'text,text,text,text,integer,text,integer,text,text,text,text)',
                       'EXECUTE'
                   ),
                   has_function_privilege(
                       'toorow_share_reader',
                       'app.record_render_share_feedback_legacy_v1('
                       'text,text,text,text,integer,text,integer,text,text,text,text)',
                       'EXECUTE'
                   )
            """
        )
        row = cur.fetchone()
    assert row == (False, False, False, True, True, False)


def test_rolling_writer_allows_drained_legacy_rows_but_enforces_marked_current_rows(
    live_postgres,
):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    template_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    legacy_id = f"fba_{ULID()}"
    legacy_interaction = f"afi_{ULID()}"

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.feedback_annotations
            SELECT (jsonb_populate_record(
                NULL::app.feedback_annotations,
                to_jsonb(a) || jsonb_build_object(
                    'id', %s::text, 'interaction_ref', %s::text,
                    'retry_key_hash', %s::text, 'request_hash', %s::text,
                    'eligibility_schema_version', NULL
                )
            )).* FROM app.feedback_annotations a WHERE a.id = %s
            """,
            (
                legacy_id,
                legacy_interaction,
                secrets.token_hex(32),
                secrets.token_hex(32),
                template_id,
            ),
        )
        cur.execute(
            "SELECT eligibility_schema_version FROM app.feedback_annotations WHERE id = %s",
            (legacy_id,),
        )
        assert cur.fetchone() == (None,)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.feedback_annotations
                SELECT (jsonb_populate_record(
                    NULL::app.feedback_annotations,
                    to_jsonb(a) || jsonb_build_object(
                        'id', %s::text, 'interaction_ref', %s::text,
                        'retry_key_hash', %s::text, 'request_hash', %s::text,
                        'eligibility_schema_version', 'feedback-eligibility.v1'
                    )
                )).* FROM app.feedback_annotations a WHERE a.id = %s
                """,
                (
                    f"fba_{ULID()}",
                    f"afi_{ULID()}",
                    secrets.token_hex(32),
                    secrets.token_hex(32),
                    template_id,
                ),
            )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.feedback_eligible_observations "
            "WHERE project_id = %s AND interaction_ref = %s",
            (project_id, legacy_interaction),
        )
        assert cur.fetchone() == (0,)
    live_postgres.rollback()


def test_authenticated_and_historical_share_sources_keep_their_bytes_when_reviewed(
    live_postgres,
):
    from core import render_shares

    from tests.integration.test_render_shares_postgres import Chain, _bearer_of

    org_id, project_id, result_id = _pg_seed(live_postgres)
    authenticated_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    share_chain = Chain(live_postgres).build()
    created = share_chain.share()
    exchanged = render_shares.exchange_bearer(
        live_postgres,
        bearer=_bearer_of(created),
        ip_hash=None,
        client_class="browser",
    )
    session = render_shares.resolve_session(
        live_postgres, session_value=exchanged.session_value
    )
    frozen = render_shares.load_frozen_render(live_postgres, session)
    share_id = render_shares.record_feedback(
        live_postgres,
        session,
        frozen,
        polarity="helpful",
        comment="Historical Share evidence stays frozen.",
        selected_datum_key=None,
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(a) FROM app.feedback_annotations a WHERE id = %s",
            (authenticated_id,),
        )
        authenticated_before = cur.fetchone()[0]
        cur.execute(
            "SELECT to_jsonb(f) FROM app.render_share_feedback f WHERE id = %s",
            (share_id,),
        )
        share_before = cur.fetchone()[0]
        cur.execute(
            """
            SELECT f.eligibility_schema_version, count(e.id)
              FROM app.render_share_feedback f
              LEFT JOIN app.feedback_eligible_observations e
                ON e.org_id = f.org_id AND e.project_id = f.project_id
               AND e.source = 'anonymous_share'
               AND e.interaction_ref = f.interaction_ref
             WHERE f.id = %s
             GROUP BY f.eligibility_schema_version
            """,
            (share_id,),
        )
        share_eligibility = cur.fetchone()
        cur.execute(
            "SELECT subject_id, authenticated_feedback_id, share_feedback_id "
            "FROM app.feedback_review_subjects WHERE subject_id = ANY(%s) ORDER BY subject_id",
            ([authenticated_id, share_id],),
        )
        subjects = cur.fetchall()
    assert subjects == sorted(
        [
            (authenticated_id, authenticated_id, None),
            (share_id, None, share_id),
        ]
    )
    assert share_eligibility == (None, 0)

    for feedback_id, scope_org, scope_project, retry_key in (
        (authenticated_id, org_id, project_id, "review-auth-source"),
        (share_id, share_chain.org_id, share_chain.project_id, "review-share-source"),
    ):
        append_review_version(
            live_postgres,
            org_id=scope_org,
            project_id=scope_project,
            feedback_id=feedback_id,
            reviewer="source-bytes@example.com",
            payload=_review_command(expected_head=None, retry_key=retry_key),
        )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(a) FROM app.feedback_annotations a WHERE id = %s",
            (authenticated_id,),
        )
        authenticated_after = cur.fetchone()[0]
        cur.execute(
            "SELECT to_jsonb(f) FROM app.render_share_feedback f WHERE id = %s",
            (share_id,),
        )
        share_after = cur.fetchone()[0]
    assert authenticated_after == authenticated_before
    assert share_after == share_before
    live_postgres.rollback()


def test_exact_aggregate_uses_eligible_interactions_and_compatibility_cohorts(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    _pg_annotation(live_postgres, org_id, project_id, result_id)
    now = datetime.now(timezone.utc)

    result = aggregate_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        filters={
            "observed_from": (now - timedelta(minutes=1)).isoformat(),
            "observed_to": (now + timedelta(minutes=1)).isoformat(),
        },
    )

    assert result["schema_version"] == "feedback-aggregate.v1"
    assert result["scope"] == {
        "eligible_interactions": 1,
        "annotated_interactions": 1,
        "annotations": 1,
        "coverage": 1.0,
        "coverage_state": "stated",
        "coverage_reason": None,
    }
    assert sum(len(axis["buckets"]) for axis in result["axes"].values()) == 5
    assert {
        bucket["compatibility_key"]
        for axis in result["axes"].values()
        for bucket in axis["buckets"]
    } == {next(iter(result["axes"]["semantic_view"]["buckets"]))["compatibility_key"]}
    live_postgres.rollback()


def test_real_producer_freezes_complete_snapshots_and_exact_automated_axes(
    live_postgres,
):
    from tests.fixture_generators.feedback_review import build_fixture

    live_postgres.rollback()
    fixture = asyncio.run(build_fixture(live_postgres.info.dsn))
    base_snapshot = {
        "result_id",
        "result_content_hash",
        "query_spec_version_id",
        "query_spec_version_content_hash",
        "semantic_view_id",
        "semantic_view_version_id",
        "semantic_view_version_content_hash",
        "ai_path_id",
        "ai_path_content_hash",
    }
    render_snapshot = {
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
    }
    authenticated = fixture["authenticated_detail"]
    anonymous = fixture["detail"]
    assert base_snapshot <= set(authenticated["visible_versions"])
    assert base_snapshot | render_snapshot <= set(anonymous["visible_versions"])
    assert authenticated["visible_versions_hash_valid"] is True
    assert anonymous["visible_versions_hash_kind"] == "evidence_manifest"
    assert anonymous["actor"] is None
    verdict = anonymous["automated_verdicts"]["items"]
    assert [(item["dimension"], item["verdict"]) for item in verdict] == [
        ("path_quality", "fail")
    ]

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT id, org_id, project_id FROM app.render_share_feedback "
            "WHERE target_schema_version = 'exact-feedback.v1' "
            "ORDER BY submitted_at DESC LIMIT 1"
        )
        share_feedback_id, org_id, project_id = cur.fetchone()
        cur.execute(
            """
            SELECT count(*) FILTER (
                       WHERE eligibility_schema_version = 'feedback-eligibility.v1'
                   ), count(*)
              FROM app.feedback_annotations
             WHERE target_schema_version = 'exact-feedback.v1' AND project_id = %s
            """,
            (project_id,),
        )
        assert cur.fetchone() == (3, 3)
        cur.execute(
            """
            SELECT f.eligibility_schema_version,
                   c.ai_path_id = f.ai_path_id,
                   e.classification->'business_domains'->'versions'
                       @> jsonb_build_array(jsonb_build_object(
                           'id', c.business_domain_id,
                           'version_number', c.business_domain_version_number
                       )),
                   e.classification->'capability'->>'key' = c.capability_key,
                   e.classification->'result_type'->>'value' = c.result_type,
                   e.classification_hash = c.result_classification_hash
              FROM app.render_share_feedback f
              JOIN app.feedback_eligible_observations e
                ON e.org_id = f.org_id AND e.project_id = f.project_id
               AND e.source = 'anonymous_share'
               AND e.interaction_ref = f.interaction_ref
              JOIN app.evaluation_run_cases c
                ON c.org_id = f.org_id AND c.project_id = f.project_id
               AND c.result_id = f.result_id AND c.render_ref = f.render_id
             WHERE f.target_schema_version = 'exact-feedback.v1'
             ORDER BY f.submitted_at DESC LIMIT 1
            """
        )
        assert cur.fetchone() == ("feedback-eligibility.v1", True, True, True, True, True)
        cur.execute(
            """
            SELECT array_agg(v.dimension ORDER BY v.dimension)
              FROM app.render_share_feedback f
              JOIN app.evaluation_run_cases c
                ON c.org_id = f.org_id AND c.project_id = f.project_id
               AND c.result_id = f.result_id AND c.render_ref = f.render_id
              JOIN app.evaluation_case_dimension_verdicts v
                ON v.case_id = c.id AND v.org_id = c.org_id AND v.project_id = c.project_id
             WHERE f.target_schema_version = 'exact-feedback.v1'
               AND f.project_id = %s
            """,
            (project_id,),
        )
        assert cur.fetchone()[0] == ["path_quality", "semantic_correctness"]
        cur.execute(
            """
            SELECT a.visible_versions, qsv.content_hash, svv.content_hash, ap.content_hash
              FROM app.feedback_annotations a
              JOIN app.query_results qr
                ON qr.id = a.result_id AND qr.org_id = a.org_id
               AND qr.project_id = a.project_id
              JOIN app.query_spec_versions qsv
                ON qsv.id = qr.query_spec_version_id AND qsv.org_id = qr.org_id
               AND qsv.project_id = qr.project_id
              JOIN app.semantic_view_versions svv
                ON svv.id = qsv.semantic_view_version_id
               AND svv.view_id = qsv.semantic_view_id
               AND svv.project_id = qsv.project_id
              JOIN app.ai_paths ap
                ON ap.id = qr.ai_path_id AND ap.org_id = qr.org_id
               AND ap.project_id = qr.project_id
             WHERE a.target_schema_version = 'exact-feedback.v1'
               AND a.project_id = %s
             ORDER BY a.observed_at DESC LIMIT 1
            """,
            (project_id,),
        )
        visible, query_spec_hash, semantic_view_hash, ai_path_hash = cur.fetchone()
        assert visible["query_spec_version_content_hash"] == query_spec_hash
        assert visible["semantic_view_version_content_hash"] == semantic_view_hash
        assert visible["ai_path_content_hash"] == ai_path_hash
        assert all(
            isinstance(value, str) and len(value) == 64
            for value in (query_spec_hash, semantic_view_hash, ai_path_hash)
        )
    exact_detail = get_annotation(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=share_feedback_id,
    )
    domain = exact_detail["classification"]["business_domains"]["versions"][0]
    domain_link = next(
        link
        for link in exact_detail["owner_links"]
        if link["object_type"] == "business-domain"
    )
    assert domain_link == {
        "workspace": "governance",
        "section": "master-data",
        "object_type": "business-domain",
        "object_id": domain["id"],
        "version_id": f"{domain['id']}:{domain['version_number']}",
        "tab": "versions",
    }
    live_postgres.rollback()


def _review_command(*, expected_head, retry_key, reason="Pinned evidence is wrong"):
    return {
        "schema_version": "feedback-review-command.v1",
        "expected_head": expected_head,
        "retry_key": retry_key,
        "state": "triaged",
        "affected_dimension": "semantic_correctness",
        "human_verdict": "fail",
        "severity": "critical",
        "reason": reason,
    }


def test_review_retry_receipt_stays_anchored_after_a_later_head(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    first_command = _review_command(expected_head=None, retry_key="review-retry-one")
    first = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        reviewer="reviewer@example.com",
        payload=first_command,
    )
    second = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        reviewer="reviewer@example.com",
        payload=_review_command(
            expected_head=first["review_version_id"], retry_key="review-retry-two"
        ),
    )
    replay = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        reviewer="reviewer@example.com",
        payload=first_command,
    )

    assert second["review_version_id"] != first["review_version_id"]
    assert replay == {**first, "status": "replayed"}
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT retry_key_hash FROM app.feedback_review_retries WHERE subject_id = %s",
            (feedback_id,),
        )
        hashes = [row[0] for row in cur.fetchall()]
    assert len(hashes) == 2
    assert all("review-retry" not in value for value in hashes)
    with pytest.raises(FeedbackRefused, match="head changed") as stale:
        append_review_version(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            reviewer="reviewer@example.com",
            payload=_review_command(expected_head=None, retry_key="review-retry-three"),
        )
    assert stale.value.code == "stale_review_head"
    live_postgres.rollback()


def test_current_review_head_cannot_reference_another_subject_version(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    first_feedback = _pg_annotation(live_postgres, org_id, project_id, result_id)
    second_feedback = _pg_annotation(live_postgres, org_id, project_id, result_id)
    first = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=first_feedback,
        reviewer="head-scope@example.com",
        payload=_review_command(expected_head=None, retry_key="head-scope-first"),
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.feedback_reviews SET current_review_version_id = %s "
                "WHERE feedback_id = %s",
                (first["review_version_id"], second_feedback),
            )
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_review_version_id FROM app.feedback_reviews WHERE feedback_id = %s",
            (second_feedback,),
        )
        assert cur.fetchone() == (None,)
    live_postgres.rollback()


def test_collection_cursor_is_bound_to_filters_and_uses_keyset_order(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    _pg_annotation(live_postgres, org_id, project_id, result_id, polarity="positive")
    _pg_annotation(live_postgres, org_id, project_id, result_id, polarity="positive")
    now = datetime.now(timezone.utc)
    filters = {
        "observed_from": (now - timedelta(minutes=1)).isoformat(),
        "observed_to": (now + timedelta(minutes=1)).isoformat(),
    }
    first = list_annotations(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        filters=filters,
        polarity="positive",
        limit=1,
    )
    second = list_annotations(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        filters=filters,
        polarity="positive",
        cursor=first["next_cursor"],
        limit=1,
    )
    assert first["truncated"] is True
    assert first["normalized_filters"]["polarity"] == "positive"
    assert first["items"][0]["id"] != second["items"][0]["id"]
    with pytest.raises(FeedbackRefused) as mismatch:
        list_annotations(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            filters=filters,
            polarity="negative",
            cursor=first["next_cursor"],
            limit=1,
        )
    assert mismatch.value.code == "invalid_cursor"
    live_postgres.rollback()


def _table_count(conn, table: str, *, project_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM app.{table} WHERE project_id = %s", (project_id,))
        return int(cur.fetchone()[0])


def test_new_append_only_tables_refuse_update_delete_and_truncate(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        reviewer="immutability@example.com",
        payload=_review_command(expected_head=None, retry_key="immutable-review"),
    )
    tables = (
        "feedback_review_subjects",
        "feedback_eligible_observations",
        "feedback_review_retries",
    )
    before = {table: _table_count(live_postgres, table, project_id=project_id) for table in tables}

    for table in tables:
        for statement in (
            f"UPDATE app.{table} SET created_at = created_at WHERE project_id = %s",
            f"DELETE FROM app.{table} WHERE project_id = %s",
        ):
            with pytest.raises(Exception), live_postgres.transaction():
                with live_postgres.cursor() as cur:
                    cur.execute(statement, (project_id,))
        with pytest.raises(Exception), live_postgres.transaction():
            with live_postgres.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE app.{table}")

    after = {table: _table_count(live_postgres, table, project_id=project_id) for table in tables}
    assert before == after == {
        "feedback_review_subjects": 1,
        "feedback_eligible_observations": 1,
        "feedback_review_retries": 1,
    }
    live_postgres.rollback()


def test_audit_failure_rolls_back_review_version_retry_and_head(live_postgres, monkeypatch):
    from core import feedback_review

    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr(feedback_review, "insert_audit_row", fail_audit)
    with pytest.raises(RuntimeError, match="forced audit failure"), live_postgres.transaction():
        append_review_version(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            reviewer="audit@example.com",
            payload=_review_command(expected_head=None, retry_key="audit-must-rollback"),
        )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_review_version_id, current_state FROM app.feedback_reviews "
            "WHERE feedback_id = %s",
            (feedback_id,),
        )
        head = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM app.feedback_review_versions WHERE feedback_id = %s",
            (feedback_id,),
        )
        versions = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM app.feedback_review_retries WHERE subject_id = %s",
            (feedback_id,),
        )
        retries = int(cur.fetchone()[0])
    assert head == (None, "unreviewed")
    assert (versions, retries) == (0, 0)
    live_postgres.rollback()


def test_review_retry_changed_payload_conflicts_and_same_key_is_independent_per_subject(
    live_postgres,
):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    first_feedback = _pg_annotation(live_postgres, org_id, project_id, result_id)
    second_feedback = _pg_annotation(live_postgres, org_id, project_id, result_id)
    command = _review_command(expected_head=None, retry_key="same-human-retry")
    first = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=first_feedback,
        reviewer="reviewer@example.com",
        payload=command,
    )
    replay = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=first_feedback,
        reviewer="reviewer@example.com",
        payload=command,
    )
    with pytest.raises(FeedbackRefused) as changed:
        append_review_version(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            feedback_id=first_feedback,
            reviewer="reviewer@example.com",
            payload={**command, "reason": "Changed retry body"},
        )
    independent = append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=second_feedback,
        reviewer="reviewer@example.com",
        payload=command,
    )

    assert replay == {**first, "status": "replayed"}
    assert changed.value.code == "idempotency_conflict"
    assert independent["status"] == "recorded"
    assert independent["feedback_id"] == second_feedback
    live_postgres.rollback()


def test_concurrent_review_revisions_serialize_one_append_and_one_stale_head(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    live_postgres.commit()
    barrier = threading.Barrier(2)

    def append(retry_key: str) -> str:
        with psycopg.connect(live_postgres.info.dsn) as conn:
            barrier.wait(timeout=5)
            try:
                receipt = append_review_version(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    feedback_id=feedback_id,
                    reviewer="concurrent-reviewer@example.com",
                    payload=_review_command(expected_head=None, retry_key=retry_key),
                )
                conn.commit()
                return receipt["status"]
            except FeedbackRefused as exc:
                conn.rollback()
                return exc.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(
            future.result()
            for future in (
                pool.submit(append, "concurrent-review-a"),
                pool.submit(append, "concurrent-review-b"),
            )
        )
    assert outcomes == ["recorded", "stale_review_head"]


def test_critical_negative_pages_are_byte_bounded_and_resume_without_repeats(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    expected_ids: set[str] = set()
    for index in range(140):
        feedback_id = _pg_annotation(
            live_postgres,
            org_id,
            project_id,
            result_id,
            actor=f"critical-{index}@example.com",
            comment=f"{index:03d}:" + "x" * 1996,
        )
        expected_ids.add(feedback_id)
        append_review_version(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            feedback_id=feedback_id,
            reviewer="critical-reviewer@example.com",
            payload=_review_command(
                expected_head=None,
                retry_key=f"critical-review-{index}",
                reason=f"Critical evidence {index}",
            ),
        )

    first = list_unresolved_critical_negatives(
        live_postgres, org_id=org_id, project_id=project_id, limit=200
    )
    repeated = list_unresolved_critical_negatives(
        live_postgres, org_id=org_id, project_id=project_id, limit=200
    )
    assert len(json.dumps(first, separators=(",", ":")).encode()) <= 262_144
    assert first["truncated"] is True
    assert first["next_cursor"] == repeated["next_cursor"]
    seen: set[str] = set()
    page = first
    for _page_number in range(10):
        page_ids = {item["id"] for item in page["items"]}
        assert page_ids
        assert seen.isdisjoint(page_ids)
        assert len(json.dumps(page, separators=(",", ":")).encode()) <= 262_144
        seen.update(page_ids)
        if page["next_cursor"] is None:
            break
        page = list_unresolved_critical_negatives(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            limit=200,
            cursor=page["next_cursor"],
        )
    else:
        pytest.fail("critical-negative cursor did not terminate")
    assert seen == expected_ids
    live_postgres.rollback()


#: The issuer this suite's fake tokens are signed by. It has to be a real value
#: and it has to be CONFIGURED: canonical resolution keys a person on
#: (issuer, subject), so a token carrying no `iss` resolves to nobody and every
#: request 401s before it reaches the access seam under test.
_ISSUER = "https://feedback-review.test"


class _BearerVerifier:
    def __init__(self, identities: dict[str, str]) -> None:
        self.identities = identities
        self.seen: list[str] = []

    async def verify_token(self, token: str):
        self.seen.append(token)
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


def test_oauth_asgi_uses_two_bearers_and_identity_bound_force_rls_connections(
    live_postgres, monkeypatch
):
    from core import api_auth
    from core.main import build_asgi_app
    from starlette.testclient import TestClient
    from ulid import ULID

    org_id, project_id, result_id = _pg_seed(live_postgres)
    visible_feedback = _pg_annotation(live_postgres, org_id, project_id, result_id)
    _org, foreign_project, foreign_result = _pg_seed(live_postgres)
    foreign_feedback = _pg_annotation(
        live_postgres, org_id, foreign_project, foreign_result, polarity="positive"
    )
    viewer = f"oauth-viewer-{secrets.token_hex(4)}@example.com"
    outsider = f"oauth-outsider-{secrets.token_hex(4)}@example.com"
    viewer_person = _person(live_postgres, viewer)
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
    verifier = _BearerVerifier({"viewer-token": viewer, "outsider-token": outsider})
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)
    client = TestClient(build_asgi_app(), raise_server_exceptions=True)
    base = f"/api/projects/{project_id}/test/feedback"
    visible = client.get(
        f"{base}/{visible_feedback}", headers={"Authorization": "Bearer viewer-token"}
    )
    denied = client.get(
        f"{base}/{visible_feedback}", headers={"Authorization": "Bearer outsider-token"}
    )
    foreign = client.get(
        f"{base}/{foreign_feedback}", headers={"Authorization": "Bearer viewer-token"}
    )
    missing = client.get(
        f"{base}/fba_missing", headers={"Authorization": "Bearer viewer-token"}
    )

    assert visible.status_code == 200, visible.text
    assert set(verifier.seen) == {"viewer-token", "outsider-token"}
    assert denied.status_code == foreign.status_code == missing.status_code == 404
    assert denied.content == foreign.content == missing.content
    for response in (denied, foreign, missing):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["vary"] == "Authorization"
