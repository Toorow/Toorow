"""Story 51.5 -- the User Feedback evidence mode, proved at three layers.

WHY THREE LAYERS IN ONE FILE. Each property below is enforced somewhere
specific, and asserting it anywhere else proves nothing:

* the *vocabulary and refusal* layer is service logic -- a scripted cursor is
  enough, and a live database would only make it slow;
* the *immutability, scoped foreign key, CHECK and RLS* layer is the database.
  A mocked cursor accepts every one of them happily, which is exactly how a
  schema promise turns into a comment. Those tests take `live_postgres` and skip
  when `TEST_POSTGRES_DSN` is unset;
* the *route* layer is the ASGI application. This repository has already carried
  an orphan handler whose own tests were green while every request answered 405,
  so the routes are exercised through `build_asgi_app()` and nothing else.

Every live-Postgres test rolls back. Nothing is left behind even on a disposable
database.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.ai_paths import NO_AI_PATH
from core.feedback_review import (
    _ANNOTATION_FIELDS,
    _ANNOTATION_SELECT,
    ACTION_FEEDBACK_ANNOTATION_CREATED,
    AFFECTED_DIMENSIONS,
    HUMAN_VERDICTS,
    MCP_APP_BEHAVIOR_ABSENT,
    RENDER_ABSENT,
    FeedbackNotFound,
    FeedbackRefused,
    _aggregate_axis_rows,
    _aggregate_cursor,
    _aggregate_feedback_story_51,
    _annotation_read_model,
    _automated_verdicts,
    _decode_aggregate_cursor,
    _exact_feedback_authority,
    _normalize_aggregate_filters,
    _require_known_filter_owners,
    _version_divergence,
    _visible_versions_snapshot,
    append_review_version,
    canonical_hash,
    decode_review_cursor,
    encode_review_cursor,
    freeze_result_classification,
    get_annotation,
    is_exact_version_pin,
    list_unresolved_critical_negatives,
    read_actor_path_step_reactions,
    record_feedback_eligibility,
    submit_ai_path_step_feedback,
)
from core.feedback_review import (
    _create_annotation_legacy_contract_for_tests as create_annotation,
)
from core.feedback_review_api import _create_ai_path_step_feedback
from ulid import ULID

from tests.conftest import TEST_ORG_ID

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_MODULE_PATH = Path(__file__).resolve().parents[2] / "core" / "feedback_review.py"
_API_PATH = Path(__file__).resolve().parents[2] / "core" / "feedback_review_api.py"

ORG = "org_EXAMPLE"
PROJECT = "proj_EXAMPLE"

_RESULT_ROW = ("qr_1", "success", "qsv_1", None, NO_AI_PATH, "sv_1", "svv_1", "qs_1")

_ANNOTATION_ROW = (
    "fba_1",
    "negative",
    "the number disagrees with the invoice",
    "person-1",
    "user",
    "console",
    None,
    None,
    "qr_1",
    None,
    NO_AI_PATH,
    None,
    None,
    None,
    None,
    # exact-feedback.v1 columns (historical fixture remains NULL)
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    {},
    "0" * 64,
    datetime(2026, 7, 31, tzinfo=timezone.utc),
    datetime(2026, 7, 31, tzinfo=timezone.utc),
    "success",
    "qsv_1",
    "qs_1",
    "sv_1",
    "svv_1",
    "unreviewed",
    None,
    "authenticated",
    None,
)


# ---------------------------------------------------------------------------
# A scripted connection. It answers by which query was asked, so a handler that
# reads eight columns from one statement and twenty-five from another is never
# silently fed the wrong arity -- which is how a fixture invents a failure the
# code never had.
# ---------------------------------------------------------------------------


class _Script:
    def __init__(self, **overrides):
        self.statements: list[tuple[str, object]] = []
        self.result_row = overrides.get("result_row", _RESULT_ROW)
        self.annotation_row = overrides.get("annotation_row", _ANNOTATION_ROW)
        self.connector_clash = overrides.get("connector_clash", None)
        self.domain_row = overrides.get("domain_row", (1,))
        self.ai_path_lifecycle = overrides.get("ai_path_lifecycle", ("finalized",))
        self.review_head = overrides.get("review_head", (None,))
        self.max_version = overrides.get("max_version", (0,))
        self.golden_question = overrides.get("golden_question", None)
        self.review_versions = overrides.get("review_versions", [])
        self.aggregate_rows = overrides.get("aggregate_rows", [])
        self.eligible_rows = overrides.get("eligible_rows", [])
        self.skill_rows = overrides.get("skill_rows", [])

    def one(self, sql: str):
        if "a.visible_versions_hash" in sql:
            return self.annotation_row
        if "qr.ai_path_absent_literal" in sql:
            return self.result_row
        if "app.project_modules" in sql:
            return self.connector_clash
        if "app.mdm_business_domain_versions v" in sql:
            return self.domain_row
        if "FROM app.ai_paths" in sql:
            return self.ai_path_lifecycle
        if "FOR UPDATE" in sql:
            return self.review_head
        if "COALESCE(MAX(version_number)" in sql:
            return self.max_version
        if "app.golden_questions" in sql:
            return self.golden_question
        return None

    def many(self, sql: str):
        if "s.skill_version_id IS NOT NULL" in sql:
            return self.skill_rows
        if "GROUP BY 1, 2" in sql:
            return self.eligible_rows
        if "rv.severity" in sql and "a.visible_versions_hash" not in sql:
            return self.aggregate_rows
        if "FROM app.feedback_review_versions" in sql:
            return self.review_versions
        return []


class _Cursor:
    def __init__(self, script: _Script):
        self._script = script
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._sql = sql
        self._script.statements.append((sql, params))

    def fetchone(self):
        return self._script.one(self._sql)

    def fetchall(self):
        return self._script.many(self._sql)


def _connection(script: _Script):
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _Cursor(script))
    return conn


def _valid_payload(**overrides):
    payload = {
        "polarity": "negative",
        "observed_surface": "console",
        "result_id": "qr_1",
        "comment": "the number disagrees with the invoice",
    }
    payload.update(overrides)
    return payload


def _create(script: _Script, **overrides):
    return create_annotation(
        _connection(script), org_id=ORG, project_id=PROJECT, actor="person-1",
        payload=_valid_payload(**overrides),
    )


# ---------------------------------------------------------------------------
# Story 65.8 -- server-owned classification and delivery eligibility.
# ---------------------------------------------------------------------------


class _ClassificationScript(_Script):
    def one(self, sql: str):
        if "FROM app.query_spec_versions qsv" in sql:
            return (
                "sv_1",
                "svv_1",
                {
                    "result_shape": "tabular_v1",
                    "required_capability": {"key": "country"},
                },
                ["bdom_1"],
            )
        if "FROM app.project_capabilities" in sql:
            return ("cfgv_1",)
        if "FROM app.ai_paths" in sql and "lifecycle" in sql:
            return ("finalized",)
        return super().one(sql)

    def many(self, sql: str):
        if "FROM app.mdm_business_domain_versions" in sql:
            return [("bdom_1", 3)]
        if "SELECT DISTINCT skill_version_id" in sql:
            return [("skv_2",), ("skv_1",)]
        return super().many(sql)


def test_story_65_8_classification_is_server_owned_sorted_and_closed():
    document = freeze_result_classification(
        _connection(_ClassificationScript()),
        org_id=ORG,
        project_id=PROJECT,
        query_spec_version_id="qsv_1",
        outcome="success",
        ai_path_id="aip_1",
    )
    expected = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "attributed", "id": "sv_1", "version_id": "svv_1"},
        "business_domains": {
            "state": "attributed",
            "versions": [{"id": "bdom_1", "version_number": 3}],
        },
        "skills": {"state": "attributed", "versions": ["skv_1", "skv_2"]},
        "capability": {"state": "attributed", "key": "country", "version_id": "cfgv_1"},
        "result_type": {"state": "attributed", "value": "table"},
    }
    assert document == {**expected, "classification_hash": canonical_hash(expected)}


def test_story_65_8_unknown_result_shape_is_explicitly_unavailable():
    script = _ClassificationScript()
    original = script.one

    def one(sql: str):
        row = original(sql)
        if "FROM app.query_spec_versions qsv" in sql and row is not None:
            return row[0], row[1], {"result_shape": "future_shape_v9"}, row[3]
        return row

    script.one = one
    document = freeze_result_classification(
        _connection(script),
        org_id=ORG,
        project_id=PROJECT,
        query_spec_version_id="qsv_1",
        outcome="success",
        ai_path_id=None,
    )
    assert document["result_type"] == {
        "state": "unavailable",
        "reason": "result_type_not_published",
    }


def test_story_65_8_refusal_wins_even_when_query_spec_owner_is_unavailable():
    document = freeze_result_classification(
        _connection(_Script(result_row=None)),
        org_id=ORG,
        project_id=PROJECT,
        query_spec_version_id="qsv_missing",
        outcome="refused",
        ai_path_id=None,
    )
    assert document["result_type"] == {"state": "attributed", "value": "refusal"}


def test_story_65_8_public_filters_refuse_moving_version_aliases():
    with pytest.raises(FeedbackRefused) as raised:
        _normalize_aggregate_filters(
            {
                "observed_from": "2026-08-01T00:00:00Z",
                "observed_to": "2026-08-02T00:00:00Z",
                "semantic_view_version_id": "latest",
            }
        )
    assert raised.value.code == "unqualified_version_pin"


def test_story_65_8_divergence_covers_the_full_render_build_tuple():
    row = {
        "visible_versions": {
            "visualization_spec_version_id": "vsv_visible",
            "renderer_build_id": "renderer-visible",
            "runtime_build_id": "runtime-visible",
            "theme_version": "theme-visible",
            "formatter_version": "formatter-visible",
        },
        "visualization_spec_version_id": "vsv_pinned",
        "renderer_build_id": "renderer-pinned",
        "runtime_build_id": "runtime-pinned",
        "theme_version": "theme-pinned",
        "formatter_version": "formatter-pinned",
    }
    assert {item["field"] for item in _version_divergence(row)} == {
        "visualization_spec_version_id",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
    }


def test_story_65_8_exact_writer_hashes_the_complete_visible_owner_snapshot():
    snapshot = _visible_versions_snapshot(
        result={
            "result_id": "qr_1",
            "content_hash": "a" * 64,
            "query_spec_version_id": "qsv_1",
            "query_spec_version_content_hash": "b" * 64,
            "semantic_view_id": "sv_1",
            "semantic_view_version_id": "svv_1",
            "semantic_view_version_content_hash": "c" * 64,
            "ai_path_id": "aip_1",
            "ai_path_content_hash": "d" * 64,
            "ai_path_absent_literal": None,
        },
        claims={
            "render_id": "rnd_1",
            "visualization_spec_version_id": "vsv_1",
            "renderer_build_id": "renderer_1",
            "runtime_build_id": "runtime_1",
            "theme_version": "theme_1",
            "formatter_version": "formatter_1",
        },
    )
    assert snapshot == {
        "result_id": "qr_1",
        "result_content_hash": "a" * 64,
        "query_spec_version_id": "qsv_1",
        "query_spec_version_content_hash": "b" * 64,
        "semantic_view_id": "sv_1",
        "semantic_view_version_id": "svv_1",
        "semantic_view_version_content_hash": "c" * 64,
        "ai_path_id": "aip_1",
        "ai_path_content_hash": "d" * 64,
        "render_id": "rnd_1",
        "visualization_spec_version_id": "vsv_1",
        "renderer_build_id": "renderer_1",
        "runtime_build_id": "runtime_1",
        "theme_version": "theme_1",
        "formatter_version": "formatter_1",
    }
    assert canonical_hash(snapshot) == canonical_hash(dict(reversed(snapshot.items())))


def test_story_65_8_exact_authority_resolves_each_owner_hash_from_its_scoped_row():
    script = _Script(
        result_row=(
            "qr_1",
            "a" * 64,
            None,
            NO_AI_PATH,
            {},
            [],
            {},
            None,
            "qsv_1",
            "sv_1",
            "svv_1",
            "b" * 64,
            "c" * 64,
            None,
        )
    )
    authority = _exact_feedback_authority(
        _connection(script),
        org_id=ORG,
        project_id=PROJECT,
        claims={
            "result_id": "qr_1",
            "result_content_hash": "a" * 64,
            "ai_path_id": None,
            "delivered_rows": {"start": 0, "count": 0},
        },
        target={"kind": "result"},
    )
    assert authority["query_spec_version_content_hash"] == "b" * 64
    assert authority["semantic_view_version_content_hash"] == "c" * 64
    assert authority["ai_path_content_hash"] is None
    sql, _ = next(
        statement
        for statement in script.statements
        if "FROM app.query_results qr" in statement[0]
    )
    assert "JOIN app.semantic_view_versions svv" in sql
    assert "svv.content_hash" in sql
    assert "ap.content_hash" in sql


def test_story_65_8_visible_snapshot_declares_absent_ai_path_hash_unavailable():
    snapshot = _visible_versions_snapshot(
        result={
            "result_id": "qr_1",
            "content_hash": "a" * 64,
            "query_spec_version_id": "qsv_1",
            "query_spec_version_content_hash": "b" * 64,
            "semantic_view_id": "sv_1",
            "semantic_view_version_id": "svv_1",
            "semantic_view_version_content_hash": "c" * 64,
            "ai_path_id": None,
            "ai_path_content_hash": None,
            "ai_path_absent_literal": NO_AI_PATH,
        },
        claims={},
    )
    assert snapshot["ai_path_content_hash"] == {
        "state": "unavailable",
        "reason": "result_has_no_ai_path",
    }


def test_story_65_8_share_projection_completes_the_frozen_visible_owner_snapshot():
    for key in (
        "query_spec_version_id",
        "query_spec_version_content_hash",
        "semantic_view_id",
        "semantic_view_version_id",
        "semantic_view_version_content_hash",
        "ai_path_id",
        "ai_path_content_hash",
        "render_id",
    ):
        assert f"'{key}'" in _ANNOTATION_SELECT


def test_story_65_8_eligibility_failure_rolls_back_its_savepoint_and_returns_none():
    script = _Script(result_row=None)
    result = record_feedback_eligibility(
        _connection(script),
        claims={
            "schema_version": "exact-feedback.v1",
            "org_id": ORG,
            "project_id": PROJECT,
            "surface": "console",
            "interaction_ref": "afi_1",
            "result_id": "qr_missing",
            "result_content_hash": "a" * 64,
            "delivered_rows": {"start": 0, "count": 1, "field_count": 1, "fields_hash": "b" * 64},
        },
    )
    assert result is None
    statements = [sql for sql, _ in script.statements]
    assert any("SAVEPOINT feedback_eligibility" in sql for sql in statements)
    assert any("ROLLBACK TO SAVEPOINT feedback_eligibility" in sql for sql in statements)


def test_story_65_8_unknown_scoped_filter_owner_is_a_refusal_not_an_empty_page():
    script = _Script()
    with pytest.raises(FeedbackRefused) as raised:
        _require_known_filter_owners(
            _connection(script),
            org_id=ORG,
            project_id=PROJECT,
            filters={"semantic_view_version_id": "svv_missing"},
        )
    assert raised.value.code == "unknown_filter_owner"
    authority_sql, params = script.statements[-1]
    assert "semantic_view_versions" in authority_sql
    assert "org_id" not in authority_sql
    assert params == ("svv_missing", PROJECT)


def test_story_65_8_target_kind_joins_only_matching_annotations_at_effective_time():
    script = _Script()
    _aggregate_axis_rows(
        _connection(script),
        org_id=ORG,
        project_id=PROJECT,
        filters={
            "observed_from": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "observed_to": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "target_kind": "datum",
        },
        axis_name="semantic_view",
    )
    sql, params = script.statements[-1]
    assert "AND f.target_kind = %s" in sql
    assert "COALESCE(f.observed_at, e.observed_at) >= %s" in sql
    assert params.count("datum") == 2


def test_story_65_8_aggregate_cursor_is_bound_to_org_project_and_filters():
    filters = {
        "observed_from": "2026-08-01T00:00:00Z",
        "observed_to": "2026-08-02T00:00:00Z",
    }
    cursor = _aggregate_cursor(
        ("semantic_view", "svv_1", "a" * 64),
        org_id=ORG,
        project_id=PROJECT,
        filters=filters,
    )
    assert _decode_aggregate_cursor(
        cursor, org_id=ORG, project_id=PROJECT, filters=filters
    ) == ("semantic_view", "svv_1", "a" * 64)
    with pytest.raises(FeedbackRefused):
        _decode_aggregate_cursor(
            cursor, org_id="org_OTHER", project_id=PROJECT, filters=filters
        )


def test_story_65_8_review_cursor_is_opaque_and_bound_to_its_subject():
    cursor = encode_review_cursor(
        project_id=PROJECT, feedback_id="fba_1", version_number=7
    )
    assert cursor != "7"
    assert decode_review_cursor(
        cursor, project_id=PROJECT, feedback_id="fba_1"
    ) == 7
    with pytest.raises(FeedbackRefused):
        decode_review_cursor(cursor, project_id=PROJECT, feedback_id="fba_2")


def test_story_65_8_detail_verifies_visible_hash_and_links_every_exact_owner():
    classification = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "attributed", "id": "sv_1", "version_id": "svv_1"},
        "business_domains": {
            "state": "attributed",
            "versions": [{"id": "bdom_1", "version_number": 3}],
        },
        "skills": {"state": "attributed", "versions": ["skv_1"]},
        "capability": {"state": "attributed", "key": "country", "version_id": "cfgv_1"},
        "result_type": {"state": "attributed", "value": "table"},
    }
    classification["classification_hash"] = canonical_hash(classification)
    row = dict(zip(_ANNOTATION_FIELDS, _ANNOTATION_ROW))
    visible = {
        "result_id": "qr_1",
        "query_spec_version_id": "qsv_1",
        "visualization_spec_version_id": "vsv_1",
    }
    row.update(
        ai_path_id="aip_1",
        target_schema_version="exact-feedback.v1",
        render_ref="rnd_1",
        visualization_spec_version_id="vsv_1",
        renderer_build_id="renderer_1",
        runtime_build_id="runtime_1",
        theme_version="theme_1",
        formatter_version="formatter_1",
        visible_versions=visible,
        visible_versions_hash=canonical_hash(visible),
        classification=classification,
    )
    model = _annotation_read_model(row)
    assert model["visible_versions_hash_valid"] is True
    owners = {
        (link["workspace"], link["section"], link["object_type"], link.get("tab"))
        for link in model["owner_links"]
    }
    assert owners == {
        ("analyze", "explore", "result", None),
        ("analyze", "explore", "query-spec", "query"),
        ("governance", "semantic-model", "semantic-view", "versions"),
        ("governance", "master-data", "business-domain", "versions"),
        ("analyze", "renders", "render", None),
        ("test", "regression-runs", "trace-observation", "timeline"),
    }
    unavailable = {entry["target"] for entry in model["unavailable_owner_links"]}
    assert {"capability", "skill-versions", "visualization-spec-version"} <= unavailable
    domain_owner = next(
        link for link in model["owner_links"] if link["object_type"] == "business-domain"
    )
    assert domain_owner["object_id"] == "bdom_1"
    assert domain_owner["version_id"] == "bdom_1:3"


def test_story_65_8_divergence_verifies_each_visible_immutable_owner_hash():
    row = {
        "visible_versions": {
            "query_spec_version_content_hash": "visible-qsv",
            "semantic_view_version_content_hash": "visible-svv",
            "ai_path_content_hash": "visible-path",
        },
        "query_spec_version_content_hash": "pinned-qsv",
        "semantic_view_version_content_hash": "pinned-svv",
        "ai_path_content_hash": "pinned-path",
    }
    assert {item["field"] for item in _version_divergence(row)} == {
        "query_spec_version_content_hash",
        "semantic_view_version_content_hash",
        "ai_path_content_hash",
    }


def test_story_65_8_automated_verdict_join_reverifies_classification_and_render_tuple():
    classification = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "attributed", "id": "sv_1", "version_id": "svv_1"},
        "business_domains": {
            "state": "attributed",
            "versions": [{"id": "bdom_1", "version_number": 3}],
        },
        "skills": {"state": "attributed", "versions": []},
        "capability": {
            "state": "attributed",
            "key": "country",
            "version_id": "cfgv_1",
        },
        "result_type": {"state": "attributed", "value": "table"},
    }
    classification["classification_hash"] = canonical_hash(classification)
    row = {
        "id": "fba_1",
        "target_schema_version": "exact-feedback.v1",
        "result_id": "qr_1",
        "result_content_hash": "a" * 64,
        "semantic_view_id": "sv_1",
        "semantic_view_version_id": "svv_1",
        "ai_path_id": "aip_1",
        "ai_path_absent_literal": None,
        "render_ref": "rnd_1",
        "visualization_spec_version_id": "vsv_1",
        "renderer_build_id": "renderer_1",
        "runtime_build_id": "runtime_1",
        "theme_version": "theme_1",
        "formatter_version": "formatter_1",
        "classification": classification,
        "current_review_version_id": "fbrv_1",
    }
    class VerdictScript(_Script):
        def many(self, sql: str):
            if "evaluation_case_dimension_verdicts" in sql:
                return [
                    (
                        "erun_1",
                        "ecase_1",
                        "path_quality",
                        "fail",
                        "path_mismatch",
                        {},
                        datetime(2026, 8, 1, tzinfo=timezone.utc),
                    )
                ]
            return super().many(sql)

    script = VerdictScript()
    verdicts = _automated_verdicts(
        _connection(script), org_id=ORG, project_id=PROJECT, row=row
    )
    assert verdicts["state"] == "available"
    assert verdicts["items"][0]["owner_link"] == {
        "workspace": "test",
        "section": "regression-runs",
        "object_type": "evaluation-run",
        "object_id": "erun_1",
        "version_id": None,
        "tab": None,
    }
    sql, params = script.statements[-1]
    assert "app.feedback_canonical_json" in sql
    assert "LEFT JOIN app.renders pinned_render" in sql
    assert "%s::text IS NULL AND c.render_ref IS NULL" in sql
    assert "pinned_render.formatter_version = %s" in sql
    assert "c.ai_path_id IS NOT DISTINCT FROM %s" in sql
    assert "c.ai_path_absent_literal IS NOT DISTINCT FROM %s" in sql
    assert "domain->>'id' = c.business_domain_id" in sql
    assert "domain->>'version_number'" in sql
    assert "->'capability'->>'key'" in sql and "= c.capability_key" in sql
    assert "->'result_type'->>'value'" in sql and "= c.result_type" in sql
    assert "human_review.id = %s" in sql
    assert "human_review.feedback_id = %s" in sql
    assert "v.dimension = human_review.affected_dimension" in sql
    assert params[:4] == ("fbrv_1", "fba_1", ORG, PROJECT)
    assert params[-5:] == ("vsv_1", "renderer_1", "runtime_1", "theme_1", "formatter_1")


def test_story_65_8_automated_verdicts_are_unavailable_without_current_human_review():
    classification = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "attributed", "id": "sv_1", "version_id": "svv_1"},
        "business_domains": {"state": "attributed", "versions": []},
        "skills": {"state": "attributed", "versions": []},
        "capability": {"state": "attributed", "key": "country", "version_id": "cfgv_1"},
        "result_type": {"state": "attributed", "value": "table"},
    }
    classification["classification_hash"] = canonical_hash(classification)
    script = _Script()
    verdicts = _automated_verdicts(
        _connection(script),
        org_id=ORG,
        project_id=PROJECT,
        row={
            "id": "fba_1",
            "target_schema_version": "exact-feedback.v1",
            "classification": classification,
            "current_review_version_id": None,
        },
    )
    assert verdicts == {
        "state": "unavailable",
        "reason": "current_objective_review_unavailable",
        "items": [],
        "truncated": False,
    }
    assert not any("evaluation_case_dimension_verdicts" in sql for sql, _ in script.statements)


def test_story_65_8_critical_cursor_is_opaque_and_keyset_scoped():
    first = list(_ANNOTATION_ROW)
    second = list(_ANNOTATION_ROW)
    first[0], second[0] = "fba_2", "fba_1"

    class CriticalScript(_Script):
        def many(self, sql: str):
            if "a.polarity = 'negative'" in sql:
                return [tuple(first), tuple(second)]
            return super().many(sql)

    page = list_unresolved_critical_negatives(
        _connection(CriticalScript()),
        org_id=ORG,
        project_id=PROJECT,
        limit=1,
    )
    assert page["truncated"] is True
    assert page["next_cursor"] and page["next_cursor"] != "fba_2"


def test_story_65_8_critical_page_byte_trim_keeps_a_stable_continuation_cursor():
    first = list(_ANNOTATION_ROW)
    second = list(_ANNOTATION_ROW)
    first[0], second[0] = "fba_2", "fba_1"
    first[2] = "a" * 140_000
    second[2] = "b" * 140_000

    class CriticalScript(_Script):
        def many(self, sql: str):
            if "a.polarity = 'negative'" in sql:
                return [tuple(first), tuple(second)]
            return super().many(sql)

    page = list_unresolved_critical_negatives(
        _connection(CriticalScript()),
        org_id=ORG,
        project_id=PROJECT,
        limit=2,
    )
    assert [item["id"] for item in page["items"]] == ["fba_2"]
    assert page["truncated"] is True
    assert page["next_cursor"]
    assert len(json.dumps(page, separators=(",", ":")).encode("utf-8")) <= 250_000


def test_story_65_8_http_query_keys_are_closed_and_duplicates_are_malformed():
    from core.feedback_review_api import _query_filters
    from starlette.requests import Request

    unknown = Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"typo=1", "headers": []}
    )
    with pytest.raises(FeedbackRefused) as raised:
        _query_filters(unknown)
    assert raised.value.code == "unknown_query_parameter"

    duplicate = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"limit=1&limit=2",
            "headers": [],
        }
    )
    with pytest.raises(FeedbackRefused) as raised:
        _query_filters(duplicate, allowed_extra=("limit",))
    assert raised.value.code == "malformed_query_parameter"


def test_story_65_8_migration_preserves_history_and_enforces_same_subject_lineage():
    migration = (
        Path(__file__).resolve().parents[3]
        / "infra"
        / "nango"
        / "migrations"
        / "251_feedback_review_exact_cohorts.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER COLUMN observed_surface DROP NOT NULL" in migration
    assert "UPDATE app.render_share_feedback" not in migration
    assert "NEW.observed_surface := 'share'" in migration
    assert "(predecessor_version_id, feedback_id, org_id, project_id)" in migration
    assert "(review_version_id, subject_id, org_id, project_id)" in migration
    assert "(current_review_version_id, feedback_id, org_id, project_id)" in migration
    assert "eligibility_schema_version" in migration
    assert "record_render_share_feedback_legacy_v1" in migration


# ---------------------------------------------------------------------------
# 1. The single writer, and the vocabularies (AC1, AC2, AC5, AC6).
# ---------------------------------------------------------------------------


def test_the_service_never_writes_the_legacy_unpinnable_feedback_table():
    """AC1: `app.feedback` (migration 012) pins no Result and stops receiving rows."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"INSERT\s+INTO\s+app\.feedback\b", source), (
        "the legacy store cannot pin a Result, an AI Path or an interaction"
    )
    assert "app.feedback_annotations" in source


def test_a_single_writer_owns_the_annotation_row():
    """No second insert path: the API module contains no SQL at all."""
    api_source = _API_PATH.read_text(encoding="utf-8")
    assert "INSERT INTO" not in api_source
    assert "app.feedback_annotations" not in api_source


def test_a_pinned_result_is_resolved_inside_the_callers_project():
    script = _Script()
    _create(script)
    resolution = [s for s, _ in script.statements if "qr.ai_path_absent_literal" in s][0]
    assert "qr.org_id = %s" in resolution and "qr.project_id = %s" in resolution


def test_a_foreign_or_absent_result_answers_the_same_not_found():
    script = _Script(result_row=None)
    with pytest.raises(FeedbackNotFound):
        _create(script)


def test_the_semantic_view_pins_are_derived_not_copied_into_the_annotation():
    """AC2: the annotation stores no Semantic View column; it is read from the Result."""
    script = _Script()
    _create(script)
    insert = [s for s, _ in script.statements if "INSERT INTO app.feedback_annotations" in s][0]
    assert "semantic_view_version_id" not in insert
    assert "query_spec_version_id" not in insert


@pytest.mark.parametrize("field,value", [
    ("polarity", "neutral"),
    ("observed_surface", "share"),
    ("actor_source", "machine"),
    ("result_type", "chart"),
])
def test_an_invented_vocabulary_value_is_refused(field, value):
    """`share` is Story 50.7's to add; until then a caller cannot pin that surface."""
    with pytest.raises(FeedbackRefused) as raised:
        _create(_Script(), **{field: value})
    assert raised.value.code in {"invalid_vocabulary", "invalid_field"}


def test_the_result_type_vocabulary_is_the_ratified_closed_list():
    for value in ("scalar", "series", "breakdown", "comparison", "table", "narrative", "refusal"):
        assert _create(_Script(), result_type=value)["id"] == "fba_1"


def test_a_capability_that_is_an_installed_connector_name_is_refused():
    """`analyze-and-test.md:237` -- capability is not a connector name."""
    with pytest.raises(FeedbackRefused) as raised:
        _create(_Script(connector_clash=(1,)), capability="meta-ads")
    assert raised.value.code == "capability_is_a_connector_name"


def test_a_capability_that_is_not_a_connector_is_accepted():
    assert _create(_Script(connector_clash=None), capability="daily-briefing")


def test_half_a_business_domain_pin_is_refused():
    with pytest.raises(FeedbackRefused) as raised:
        _create(_Script(), business_domain_id="bdom_1")
    assert raised.value.code == "incomplete_version_pin"


def test_a_business_domain_from_another_organization_is_not_found():
    with pytest.raises(FeedbackNotFound):
        _create(
            _Script(domain_row=None),
            business_domain_id="bdom_foreign",
            business_domain_version_number=2,
        )


def test_an_invalid_trace_id_is_refused_and_the_all_zero_one_too():
    for candidate in ("nothex", "a" * 31, "0" * 32):
        with pytest.raises(FeedbackRefused) as raised:
            _create(_Script(), w3c_trace_id=candidate)
        assert raised.value.code == "invalid_trace_id"


def test_a_valid_trace_id_is_stored_lowercased():
    script = _Script()
    _create(script, w3c_trace_id="A" * 32)
    insert = [p for s, p in script.statements if "INSERT INTO app.feedback_annotations" in s][0]
    assert "a" * 32 in insert


# ---------------------------------------------------------------------------
# 2. `latest` is not a version pin (AC5, AC8).
# ---------------------------------------------------------------------------


def test_latest_is_never_an_exact_version_pin():
    for word in ("latest", "current", "head", "LATEST", "  latest  ", "", None):
        assert not is_exact_version_pin(word)
    assert is_exact_version_pin("svv_01J")


def test_a_visible_version_snapshot_containing_latest_is_refused():
    with pytest.raises(FeedbackRefused) as raised:
        _create(_Script(), visible_versions={"semantic_view_version_id": "latest"})
    assert raised.value.code == "unqualified_version_pin"
    assert raised.value.refusals[0].subject == "visible_versions.semantic_view_version_id"


def test_a_nested_latest_is_refused_too():
    """A rule that only inspected known keys would pass the day someone nests one."""
    with pytest.raises(FeedbackRefused):
        _create(_Script(), visible_versions={"context": {"pins": ["svv_1", "latest"]}})


def test_an_aggregate_filter_that_says_latest_is_refused_not_answered():
    with pytest.raises(FeedbackRefused) as raised:
        _aggregate_feedback_story_51(
            _connection(_Script()),
            org_id=ORG,
            project_id=PROJECT,
            filters={"semantic_view_version_id": "latest"},
        )
    assert raised.value.code == "unqualified_version_pin"


def test_an_aggregate_cannot_state_half_a_business_domain_filter():
    with pytest.raises(FeedbackRefused) as raised:
        _aggregate_feedback_story_51(
            _connection(_Script()),
            org_id=ORG,
            project_id=PROJECT,
            filters={"business_domain_id": "bdom_1"},
        )
    assert raised.value.code == "incomplete_version_pin"


# ---------------------------------------------------------------------------
# 3. The AI Path pin (AC3).
# ---------------------------------------------------------------------------


def test_the_absent_ai_path_literal_has_exactly_one_source_in_the_repository():
    """AC3: the literal is `ai_paths.NO_AI_PATH`, never a string written here."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "No AI path" not in source
    assert "from core.ai_paths import" in source


def test_an_annotation_with_no_path_carries_the_exact_literal_not_null():
    script = _Script()
    _create(script)
    params = [p for s, p in script.statements if "INSERT INTO app.feedback_annotations" in s][0]
    assert NO_AI_PATH in params
    assert params.count(None) >= 1  # ai_path_id stays NULL, and only one of the two is set


def test_an_annotation_inherits_the_paths_the_result_itself_pinned():
    script = _Script(
        result_row=("qr_1", "success", "qsv_1", "aip_1", None, "sv_1", "svv_1", "qs_1")
    )
    _create(script)
    params = [p for s, p in script.statements if "INSERT INTO app.feedback_annotations" in s][0]
    assert "aip_1" in params
    assert NO_AI_PATH not in params


def test_an_unfinalized_ai_path_cannot_be_pinned_as_evidence():
    """A recording path can still grow steps; pinning it would describe moving evidence."""
    script = _Script(ai_path_lifecycle=("recording",))
    with pytest.raises(FeedbackRefused) as raised:
        _create(script, ai_path_id="aip_recording")
    assert raised.value.code == "invalid_ai_path_pin"


def test_an_unknown_ai_path_is_not_found():
    with pytest.raises(FeedbackRefused):
        _create(_Script(ai_path_lifecycle=None), ai_path_id="aip_ghost")


@pytest.mark.parametrize("bogus", ["", "   ", "deferred"])
def test_a_reworded_or_empty_ai_path_pin_is_refused(bogus):
    script = _Script(ai_path_lifecycle=None)
    with pytest.raises(FeedbackRefused):
        _create(script, ai_path_id=bogus)


# ---------------------------------------------------------------------------
# 4. Render and datum/mark: declared, unset, `Unverifiable` (AC4).
# ---------------------------------------------------------------------------


def test_no_write_path_names_the_render_or_datum_columns():
    source = _MODULE_PATH.read_text(encoding="utf-8").split(
        "def _exact_feedback_authority", maxsplit=1
    )[0]
    for statement in re.findall(
        r"INSERT INTO app\.feedback_annotations\s*\(([^)]*)\)", source
    ):
        assert "render" not in statement
        assert "datum_mark" not in statement


@pytest.mark.parametrize("key", ["render_id", "render_ref", "datum_mark"])
def test_a_caller_that_supplies_an_undeliverable_pin_is_refused_not_silently_dropped(key):
    """Dropping it silently would let a reader believe render evidence was recorded."""
    with pytest.raises(FeedbackRefused) as raised:
        _create(_Script(), **{key: "anything"})
    assert raised.value.code == "pin_owner_not_delivered"
    assert raised.value.refusals[0].code in {
        RENDER_ABSENT.reason,
        "visualization_spec_owner_not_delivered",
    }


def test_the_render_lens_is_unverifiable_with_its_exact_reason_and_owner():
    lens = get_annotation(
        _connection(_Script()), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )["evidence_lenses"]["render"]
    assert lens["state"] == "unverifiable"
    assert lens["reason"] == "render_owner_not_delivered"
    assert lens["owner_stories"] == ["50.4", "50.5", "50.7"]


def test_render_unverifiable_is_never_pass_never_fail_and_never_a_zero():
    model = get_annotation(
        _connection(_Script()), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )
    for lens in model["evidence_lenses"].values():
        assert lens["state"] in {"unverifiable", "observed"}
        assert lens["state"] not in {"pass", "fail"}
    serialized = json.dumps(model)
    assert '"render": 0' not in serialized


def test_the_datum_mark_lens_names_its_own_owner_story():
    model = get_annotation(
        _connection(_Script()), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )
    assert model["evidence_lenses"]["datum_mark"]["owner_stories"] == ["50.4"]


def test_no_absence_is_reported_with_a_placeholder_owner():
    """Story 49.6 was rejected for exactly this: an absence nobody owns."""
    model = get_annotation(
        _connection(_Script()), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )
    serialized = json.dumps(model)
    for placeholder in ("future_owner", "TBD", "tbd", "someone", "unknown_owner"):
        assert placeholder not in serialized


def test_an_annotation_without_a_recorded_path_reports_path_evidence_unverifiable():
    model = get_annotation(
        _connection(_Script()), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )
    lens = model["evidence_lenses"]["server_owned_path_evidence"]
    assert lens["state"] == "unverifiable"
    assert lens["reason"] == "server_owned_path_evidence_absent"


# ---------------------------------------------------------------------------
# 5. Interaction, visible versions and divergence (AC5).
# ---------------------------------------------------------------------------


def test_the_visible_version_snapshot_carries_its_own_content_hash():
    script = _Script()
    _create(script, visible_versions={"semantic_view_version_id": "svv_1"})
    params = [p for s, p in script.statements if "INSERT INTO app.feedback_annotations" in s][0]
    hashes = [p for p in params if isinstance(p, str) and re.fullmatch(r"[0-9a-f]{64}", p)]
    assert hashes, "visible_versions_hash must be computed, never supplied by the caller"


def test_a_divergence_between_what_was_seen_and_what_is_pinned_is_disclosed():
    """Disclosed, never reconciled: the two disagreeing IS the fact worth keeping."""
    row = list(_ANNOTATION_ROW)
    row[27] = {"semantic_view_version_id": "svv_older"}
    model = get_annotation(
        _connection(_Script(annotation_row=tuple(row))),
        org_id=ORG,
        project_id=PROJECT,
        feedback_id="fba_1",
    )
    divergence = model["version_divergence"]
    assert divergence == [
        {"field": "semantic_view_version_id", "visible": "svv_older", "pinned": "svv_1"}
    ]


def test_an_agreeing_snapshot_produces_no_divergence():
    row = list(_ANNOTATION_ROW)
    row[15] = {"semantic_view_version_id": "svv_1", "result_id": "qr_1"}
    model = get_annotation(
        _connection(_Script(annotation_row=tuple(row))),
        org_id=ORG,
        project_id=PROJECT,
        feedback_id="fba_1",
    )
    assert model["version_divergence"] == []


# ---------------------------------------------------------------------------
# 6. Review: one dimension, one human verdict, nothing machine-made (AC6, AC7).
# ---------------------------------------------------------------------------


def _review(script: _Script, **overrides):
    payload = {
        "schema_version": "feedback-review-command.v1",
        "expected_head": script.review_head[0] if script.review_head else None,
        "retry_key": "retry-review-1",
        "state": "triaged",
        "affected_dimension": "semantic_correctness",
        "human_verdict": "fail",
        "severity": "critical",
        "reason": "the figure contradicts the pinned Semantic View definition",
    }
    payload.update(overrides)
    return append_review_version(
        _connection(script),
        org_id=ORG,
        project_id=PROJECT,
        feedback_id="fba_1",
        reviewer="reviewer-1",
        payload=payload,
    )


def test_the_dimension_vocabulary_is_the_ratified_six_plus_not_applicable():
    assert set(AFFECTED_DIMENSIONS) == {
        "semantic_correctness",
        "provenance_correctness",
        "context_adherence",
        "path_quality",
        "dq_handling",
        "mcp_app_behavior",
        "not_applicable",
    }
    for dimension in AFFECTED_DIMENSIONS:
        assert _review(_Script(), affected_dimension=dimension)


@pytest.mark.parametrize(
    "invented", ["tone", "fluency", "layout_taste", "eloquence", "helpfulness"]
)
def test_no_seventh_dimension_and_no_stylistic_one_is_accepted(invented):
    with pytest.raises(FeedbackRefused) as raised:
        _review(_Script(), affected_dimension=invented)
    assert raised.value.code == "invalid_vocabulary"


def test_the_human_verdict_vocabulary_keeps_unverifiable_as_a_real_verdict():
    assert set(HUMAN_VERDICTS) == {"pass", "fail", "unverifiable", "not_applicable"}
    with pytest.raises(FeedbackRefused):
        _review(_Script(), human_verdict="partial")


def test_this_story_owns_no_machine_verdict_field_anywhere():
    """AC7, checked on the source rather than on one response shape."""
    forbidden = re.compile(r"combined.verdict|overall.score|trust.score", re.I)
    for path in (_MODULE_PATH, _API_PATH):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path


def test_a_human_verdict_and_machine_evidence_are_returned_under_different_names():
    script = _Script(
        review_versions=[(
            "fbrv_1", 1, "triaged", "mcp_app_behavior", "fail", "critical",
            "the widget showed a stale figure", "reviewer-1",
            None, None, None, None, None, datetime(2026, 7, 31, tzinfo=timezone.utc),
        )]
    )
    versions = get_annotation(
        _connection(script), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )["review"]["versions"]
    assert versions[0]["human_verdict"] == "fail"
    assert versions[0]["machine_evidence"]["state"] == "unverifiable"
    assert versions[0]["machine_evidence"]["reason"] == MCP_APP_BEHAVIOR_ABSENT.reason
    # Two fields, two names, never one merged value.
    assert "verdict" not in versions[0]["machine_evidence"]


def test_the_review_version_advances_the_head_in_the_same_transaction():
    script = _Script()
    _review(script)
    statements = [s for s, _ in script.statements]
    assert any("INSERT INTO app.feedback_review_versions" in s for s in statements)
    assert any("UPDATE app.feedback_reviews" in s for s in statements)


def test_a_review_on_a_feedback_the_caller_cannot_see_is_not_found():
    with pytest.raises(FeedbackNotFound):
        _review(_Script(review_head=None))


def test_a_revision_keeps_its_lineage():
    script = _Script(review_head=("fbrv_1",), max_version=(1,))
    _review(script)
    params = [p for s, p in script.statements if "INSERT INTO app.feedback_review_versions" in s][0]
    assert 2 in params and "fbrv_1" in params


# ---------------------------------------------------------------------------
# 7. Regression-case creation is the next story, never this command.
# ---------------------------------------------------------------------------


def test_a_review_command_cannot_create_a_regression_seed():
    script = _Script()
    with pytest.raises(FeedbackRefused) as raised:
        _review(script, seed_requested=True, seed_reason="reproduce this offline before shipping")
    assert raised.value.code == "invalid_review_command"
    writes = [
        s for s, _ in script.statements
        if s.strip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    tables = {
        m for s in writes for m in re.findall(r"(?:INSERT INTO|UPDATE)\s+(app\.\w+)", s)
    }
    assert tables == set()
    for forbidden in (
        "app.golden_questions",
        "app.golden_question_versions",
        "app.context_topics",
        "app.semantic_views",
        "app.mdm_business_domains",
        "app.evaluation_runs",
    ):
        assert forbidden not in tables


def test_a_seed_without_a_reason_is_refused():
    with pytest.raises(FeedbackRefused) as raised:
        _review(_Script(), seed_requested=True)
    assert raised.value.code == "invalid_review_command"


def test_seed_fields_without_a_request_are_refused():
    with pytest.raises(FeedbackRefused):
        _review(_Script(), seed_reason="orphan reason")


def test_a_seed_target_that_does_not_exist_is_not_found():
    with pytest.raises(FeedbackRefused) as raised:
        _review(
            _Script(golden_question=None),
            seed_requested=True,
            seed_reason="reproduce offline",
            seed_target_id="gq_ghost",
        )
    assert raised.value.code == "invalid_review_command"


def test_a_seed_with_no_target_reports_the_owner_that_is_absent():
    script = _Script(
        review_versions=[(
            "fbrv_1", 1, "triaged", "semantic_correctness", "fail", "critical",
            "wrong figure", "reviewer-1",
            datetime(2026, 7, 31, tzinfo=timezone.utc), "reproduce offline",
            "golden_question", None, None, datetime(2026, 7, 31, tzinfo=timezone.utc),
        )]
    )
    seed = get_annotation(
        _connection(script), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )["review"]["versions"][0]["seed_request"]
    assert seed["target_id"] is None
    assert seed["target_state"] == "requested, target owner absent"
    assert seed["target_owner_story"] == "51.1"


def test_a_critical_negative_deep_links_to_the_owners_it_actually_pinned():
    row = list(_ANNOTATION_ROW)
    row[11], row[12] = "bdom_1", 3
    model = get_annotation(
        _connection(_Script(annotation_row=tuple(row))),
        org_id=ORG,
        project_id=PROJECT,
        feedback_id="fba_1",
    )
    targets = {(link["workspace"], link["object_type"]) for link in model["owner_links"]}
    assert ("analyze", "result") in targets
    assert ("governance", "semantic-view") in targets
    assert ("governance", "business-domain") in targets
    # The server emits a structured pointer, never a URL: the shell owns the
    # address grammar and a second one here would drift.
    assert all("href" not in link and "url" not in link for link in model["owner_links"])


def test_a_link_that_cannot_be_built_is_named_with_its_owner_rather_than_omitted():
    model = get_annotation(
        _connection(_Script()), org_id=ORG, project_id=PROJECT, feedback_id="fba_1"
    )
    unavailable = {entry["target"]: entry for entry in model["unavailable_owner_links"]}
    assert unavailable["render"]["reason"] == "render_owner_not_delivered"
    assert unavailable["context-hub"]["owner_stories"] == ["51.2"]


def test_exact_v1_read_model_exposes_render_target_and_no_false_render_absence():
    row = list(_ANNOTATION_ROW)
    row[15:27] = [
        "exact-feedback.v1",
        "a" * 64,
        "rnd_1",
        "vsv_1",
        "renderer_1",
        "runtime_1",
        "theme_1",
        "formatter_1",
        "datum",
        7,
        "running_total_micros",
        None,
    ]
    model = get_annotation(
        _connection(_Script(annotation_row=tuple(row))),
        org_id=ORG,
        project_id=PROJECT,
        feedback_id="fba_1",
    )

    assert model["target_schema_version"] == "exact-feedback.v1"
    assert model["result"]["content_hash"] == "a" * 64
    assert model["target"] == {
        "kind": "datum",
        "row_index": 7,
        "field": "running_total_micros",
    }
    assert model["render"]["render_id"] == "rnd_1"
    assert model["evidence_lenses"]["render"]["state"] == "observed"
    assert "render" not in {
        item["target"] for item in model["unavailable_owner_links"]
    }
    assert any(link["object_type"] == "render" for link in model["owner_links"])


# ---------------------------------------------------------------------------
# 8. Aggregates: denominators, coverage, `unattributed` (AC8, AC9).
# ---------------------------------------------------------------------------


def _aggregate_row(**overrides):
    row = {
        "id": "fba_1",
        "polarity": "negative",
        "result_id": "qr_1",
        "ai_path_id": None,
        "business_domain_id": None,
        "business_domain_version_number": None,
        "capability": None,
        "result_type": None,
        "semantic_view_id": "sv_1",
        "semantic_view_version_id": "svv_1",
        "current_state": None,
        "severity": None,
    }
    row.update(overrides)
    return tuple(row.values())


def _aggregate(**overrides):
    script = _Script(**overrides)
    return _aggregate_feedback_story_51(
        _connection(script), org_id=ORG, project_id=PROJECT, filters={}
    )


def test_an_aggregate_echoes_the_exact_version_filters_that_produced_it():
    answer = _aggregate_feedback_story_51(
        _connection(_Script(aggregate_rows=[_aggregate_row()])),
        org_id=ORG,
        project_id=PROJECT,
        filters={"semantic_view_version_id": "svv_1", "capability": None},
    )
    assert answer["version_filters"] == {"semantic_view_version_id": "svv_1"}
    for axis in answer["axes"].values():
        for bucket in axis["buckets"]:
            assert bucket["version_filters"] == {"semantic_view_version_id": "svv_1"}


def test_the_five_ratified_axes_are_all_present():
    answer = _aggregate()
    assert set(answer["axes"]) == {
        "business_domain", "skill", "semantic_view", "capability", "result_type"
    }


def test_an_unattributed_annotation_gets_its_own_bucket_and_stays_in_the_denominator():
    answer = _aggregate(aggregate_rows=[_aggregate_row(), _aggregate_row(id="fba_2")])
    capability = answer["axes"]["capability"]["buckets"]
    unattributed = [b for b in capability if not b["attributed"]]
    assert len(unattributed) == 1
    assert unattributed[0]["annotated"] == 2
    assert unattributed[0]["unattributed_reason"] == "annotation_states_no_capability"
    assert answer["scope"]["annotations"] == 2


def test_an_unattributed_annotation_is_never_assigned_to_a_default_group():
    answer = _aggregate(
        aggregate_rows=[_aggregate_row(capability="daily-briefing"), _aggregate_row(id="fba_2")]
    )
    attributed = [b for b in answer["axes"]["capability"]["buckets"] if b["attributed"]]
    assert len(attributed) == 1
    assert attributed[0]["annotated"] == 1


def test_the_unattributed_bucket_is_emitted_even_when_empty():
    """"Nothing was unattributed" and "the axis never reported it" are not the same."""
    answer = _aggregate(aggregate_rows=[_aggregate_row(capability="daily-briefing")])
    assert any(not b["attributed"] for b in answer["axes"]["capability"]["buckets"])


def test_coverage_is_stated_where_the_denominator_is_derivable_and_unverifiable_elsewhere():
    answer = _aggregate(
        aggregate_rows=[_aggregate_row()],
        eligible_rows=[("sv_1", "svv_1", 4)],
    )
    view_bucket = [b for b in answer["axes"]["semantic_view"]["buckets"] if b["attributed"]][0]
    assert view_bucket["eligible_results"] == 4
    assert view_bucket["coverage"] == 0.25
    assert view_bucket["coverage_state"] == "stated"

    capability_bucket = answer["axes"]["capability"]["buckets"][0]
    assert capability_bucket["coverage"] is None
    assert capability_bucket["coverage_state"] == "unverifiable"
    assert capability_bucket["coverage_reason"] == "eligible_denominator_not_derivable_from_result"


def test_no_endpoint_returns_a_raw_thumbs_up_percentage_as_correctness():
    """`analyze-and-test.md:313` forbids it by name; the absence is the proof."""
    answer = _aggregate(
        aggregate_rows=[
            _aggregate_row(polarity="positive"),
            _aggregate_row(id="fba_2", polarity="negative"),
        ],
        eligible_rows=[("sv_1", "svv_1", 2)],
    )
    serialized = json.dumps(answer)
    for banned in ("positive_pct", "positivePct", "satisfaction", "score", "pass_rate"):
        assert banned not in serialized
    bucket = [b for b in answer["axes"]["semantic_view"]["buckets"] if b["attributed"]][0]
    assert bucket["positive"] == 1 and bucket["negative"] == 1


def test_unresolved_critical_negatives_are_counted_and_a_closed_one_is_not():
    answer = _aggregate(
        aggregate_rows=[
            _aggregate_row(current_state="triaged", severity="critical"),
            _aggregate_row(id="fba_2", current_state="resolved", severity="critical"),
            _aggregate_row(id="fba_3", current_state="rejected", severity="critical"),
        ],
    )
    bucket = [b for b in answer["axes"]["semantic_view"]["buckets"] if b["attributed"]][0]
    assert bucket["unresolved_critical_negatives"] == 1


def test_a_negative_nobody_reviewed_is_its_own_fact_not_a_minor_one():
    answer = _aggregate(aggregate_rows=[_aggregate_row()])
    bucket = [b for b in answer["axes"]["semantic_view"]["buckets"] if b["attributed"]][0]
    assert bucket["negatives_without_review"] == 1
    assert bucket["unresolved_critical_negatives"] == 0


def test_the_result_type_axis_declares_that_its_own_owner_is_absent():
    answer = _aggregate(aggregate_rows=[_aggregate_row(result_type="scalar")])
    axis = answer["axes"]["result_type"]
    assert axis["axis_evidence"]["state"] == "unverifiable"
    assert axis["axis_evidence"]["reason"] == "result_type_contract_owner_not_delivered"
    assert axis["axis_evidence"]["owner_stories"] == ["50.4"]


def test_the_skill_axis_says_its_buckets_must_not_be_summed():
    answer = _aggregate(
        aggregate_rows=[_aggregate_row(ai_path_id="aip_1")],
        skill_rows=[("fba_1", "skv_1"), ("fba_1", "skv_2")],
    )
    axis = answer["axes"]["skill"]
    assert axis["buckets_are_exclusive"] is False
    attributed = [b for b in axis["buckets"] if b["attributed"]]
    assert {tuple(b["key"].values())[0] for b in attributed} == {"skv_1", "skv_2"}


def test_an_annotation_whose_path_records_no_skill_lands_in_the_unattributed_bucket():
    answer = _aggregate(aggregate_rows=[_aggregate_row()], skill_rows=[])
    unattributed = [b for b in answer["axes"]["skill"]["buckets"] if not b["attributed"]][0]
    assert unattributed["annotated"] == 1
    assert unattributed["unattributed_reason"] == "pinned_ai_path_records_no_skill_step"


def test_the_aggregate_restates_that_feedback_never_proves_correctness():
    assert "never proves correctness" in _aggregate()["blocking_use"]


# ---------------------------------------------------------------------------
# 9. Live PostgreSQL: immutability, scoped foreign keys, CHECKs and RLS (AC11).
# ---------------------------------------------------------------------------


def _pg_seed(conn):
    """One Project-scoped Result to pin, built whole inside the caller's transaction.

    The chain is created rather than borrowed. Reusing "whatever Semantic View
    version this database happens to have" makes the test pass or skip depending
    on someone else's fixtures, and a disposable database has none -- which is
    how a pg-gated file reports 15 skips and looks like it ran.
    """
    org_id = TEST_ORG_ID
    project_id = f"proj_{ULID()}"
    view_id, version_id = f"sv_{ULID()}", f"svv_{ULID()}"
    spec_id, spec_version_id = f"qs_{ULID()}", f"qsv_{ULID()}"
    attempt_id, result_id = f"qea_{ULID()}", f"qr_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, 'Feedback fixture', %s, 'test')",
            (project_id, org_id, f"feedback-fixture-{project_id[-8:].lower()}"),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
            "VALUES (%s, %s, 'feedback_fixture_view', 'test')",
            (view_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'feedback_fixture_view',
                    'Feedback fixture view', %s, %s, 'test')
            """,
            (version_id, view_id, project_id, "d" * 64, "e" * 64),
        )
        cur.execute(
            "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'test')",
            (spec_id, org_id, project_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (spec_version_id, spec_id, org_id, project_id, view_id, version_id, "a" * 64),
        )
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, 'test')
            """,
            (attempt_id, org_id, project_id, spec_version_id, result_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, 'success', %s, %s, NOW(), NOW())
            """,
            (result_id, org_id, project_id, attempt_id, spec_version_id, NO_AI_PATH, "b" * 64),
        )
    return org_id, project_id, result_id


def _pg_annotation(conn, org_id, project_id, result_id, **overrides):
    feedback_id = f"fba_{ULID()}"
    retry_hash = hashlib.sha256(feedback_id.encode()).hexdigest()
    interaction_ref = f"afi_{ULID()}"
    columns = {
        "id": feedback_id,
        "org_id": org_id,
        "project_id": project_id,
        "polarity": "negative",
        "actor": "person-1",
        "actor_source": "user",
        "observed_surface": "console",
        "interaction_ref": interaction_ref,
        "result_id": result_id,
        "result_content_hash": "b" * 64,
        "ai_path_absent_literal": NO_AI_PATH,
        "target_schema_version": "exact-feedback.v1",
        "target_kind": "answer",
        "retry_key_hash": retry_hash,
        "request_hash": "d" * 64,
        "visible_versions_hash": "c" * 64,
        "observed_at": datetime.now(timezone.utc),
    }
    columns.update(overrides)
    classification = {
        "schema_version": "evaluation-classification.v1",
        "semantic_view": {"state": "unavailable", "reason": "historical_result_unclassified"},
        "business_domains": {
            "state": "unavailable",
            "reason": "historical_result_unclassified",
        },
        "skills": {"state": "unavailable", "reason": "historical_result_unclassified"},
        "capability": {"state": "unavailable", "reason": "historical_result_unclassified"},
        "result_type": {"state": "unavailable", "reason": "historical_result_unclassified"},
    }
    classification["classification_hash"] = canonical_hash(classification)
    authority = {
        "delivered_rows": None,
        "ai_path_id": None,
        "path_step_ordinals": None,
        "target_kinds": ["answer"],
    }
    preimage = {
        "schema_version": "feedback-compatibility.v1",
        "target_schema_version": "exact-feedback.v1",
        "source": "authenticated",
        "surface": columns["observed_surface"],
        "query_spec_version_id": None,
        "semantic_view": classification["semantic_view"],
        "business_domains": classification["business_domains"],
        "skills": classification["skills"],
        "capability": classification["capability"],
        "result_type": classification["result_type"],
        "render": {
            key: columns.get(key)
            for key in (
                "visualization_spec_version_id",
                "renderer_build_id",
                "runtime_build_id",
                "theme_version",
                "formatter_version",
            )
        },
    }
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.feedback_eligible_observations
                (id, org_id, project_id, source, observed_surface, interaction_ref,
                 result_id, result_content_hash, authority, classification,
                 classification_hash, compatibility_preimage, compatibility_key)
            VALUES (%s, %s, %s, 'authenticated', %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s::jsonb, %s)
            """,
            (
                f"fbe_{ULID()}",
                org_id,
                project_id,
                columns["observed_surface"],
                interaction_ref,
                result_id,
                columns["result_content_hash"],
                json.dumps(authority),
                json.dumps(classification),
                classification["classification_hash"],
                json.dumps(preimage),
                canonical_hash(preimage),
            ),
        )
        cur.execute(
            f"INSERT INTO app.feedback_annotations ({names}) VALUES ({placeholders})",
            tuple(columns.values()),
        )
    return feedback_id


def _pg_walk(conn, org_id, project_id, *, steps=2, lifecycle="finalized"):
    """One observed walk with `steps` steps, and the Result it delivered.

    Built whole rather than borrowed, for the reason `_pg_seed` gives: a
    disposable database has no walks, and a test that skips when it finds none
    proves nothing on the day the constraint is dropped.
    """
    path_id = f"aip_{ULID()}"
    with conn.cursor() as cur:
        # Recorded in the order the owner records it: `reject_step_on_finalized_path`
        # refuses a step appended after the walk closed, which is the whole point
        # of a finalized path being evidence.
        cur.execute(
            """
            INSERT INTO app.ai_paths
                (id, org_id, project_id, lifecycle, actor,
                 policy_snapshot_hash, w3c_trace_id)
            VALUES (%s, %s, %s, 'recording', 'person-1', %s, %s)
            """,
            (path_id, org_id, project_id, "f" * 64, "4bf92f3577b34da6a3ce929d0e0e4736"),
        )
        for ordinal in range(steps):
            cur.execute(
                """
                INSERT INTO app.ai_path_steps
                    (id, path_id, org_id, project_id, ordinal, step_kind, outcome)
                VALUES (%s, %s, %s, %s, %s, 'tool_call', 'succeeded')
                """,
                (f"aps_{ULID()}", path_id, org_id, project_id, ordinal),
            )
        if lifecycle == "finalized":
            cur.execute(
                """
                UPDATE app.ai_paths
                SET lifecycle = 'finalized', outcome = 'succeeded',
                    ended_at = NOW(), content_hash = %s
                WHERE id = %s
                """,
                ("a" * 64, path_id),
            )

        # A SECOND Result, this one naming the walk. `app.query_results` rows are
        # append-only, so the seeded one is left alone rather than updated.
        cur.execute(
            "SELECT query_spec_version_id FROM app.query_results WHERE project_id = %s LIMIT 1",
            (project_id,),
        )
        spec_version_id = cur.fetchone()[0]
        attempt_id, result_id = f"qea_{ULID()}", f"qr_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, 'test')
            """,
            (attempt_id, org_id, project_id, spec_version_id, result_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_id, content_hash, started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, 'success', %s, %s, NOW(), NOW())
            """,
            (result_id, org_id, project_id, attempt_id, spec_version_id, path_id, "b" * 64),
        )
        # The payload the delivery fact is read from: migration 251's trigger
        # requires an eligible observation, and the observation is classified
        # from the Result manifest.
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash, result_schema, manifest)
            VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb)
            """,
            (result_id, org_id, project_id, "b" * 64),
        )
    return path_id, result_id


def test_pg_ac8_a_step_annotation_lands_on_the_exact_path_step(live_postgres):
    """The exact target migration 249 opened, written end to end by its command."""
    org_id, project_id, _seeded = _pg_seed(live_postgres)
    path_id, result_id = _pg_walk(live_postgres, org_id, project_id)

    receipt = submit_ai_path_step_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person-1",
        path_id=path_id,
        step_ordinal=1,
        polarity="negative",
        comment="this step read the wrong Skill version",
        retry_key="k-1",
    )
    assert receipt["status"] == "recorded"

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT target_kind, ai_path_id, path_step_ordinal, result_id,
                   observed_surface, polarity, target_schema_version, w3c_trace_id
            FROM app.feedback_annotations WHERE id = %s
            """,
            (receipt["feedback_id"],),
        )
        row = cur.fetchone()
    assert row == (
        "path_step",
        path_id,
        1,
        result_id,
        "console",
        "negative",
        "exact-feedback.v1",
        "4bf92f3577b34da6a3ce929d0e0e4736",
    )

    # The review head exists from the first moment, and the audit row committed
    # in the SAME transaction as the annotation.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT current_state FROM app.feedback_reviews WHERE feedback_id = %s",
            (receipt["feedback_id"],),
        )
        assert cur.fetchone() == ("unreviewed",)
        cur.execute(
            "SELECT COUNT(*) FROM app.audit_log WHERE metadata->>'feedback_id' = %s",
            (receipt["feedback_id"],),
        )
        assert cur.fetchone()[0] == 1
    live_postgres.rollback()


def test_pg_ac8_the_same_key_is_recorded_once(live_postgres):
    org_id, project_id, _seeded = _pg_seed(live_postgres)
    path_id, _result_id = _pg_walk(live_postgres, org_id, project_id)

    kwargs = dict(
        org_id=org_id,
        project_id=project_id,
        actor="person-1",
        path_id=path_id,
        step_ordinal=0,
        polarity="positive",
        comment=None,
        retry_key="k-same",
    )
    first = submit_ai_path_step_feedback(live_postgres, **kwargs)
    second = submit_ai_path_step_feedback(live_postgres, **kwargs)

    assert first["status"] == "recorded"
    assert second["status"] == "replayed"
    assert second["feedback_id"] == first["feedback_id"]
    assert second["polarity"] == "positive"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.feedback_annotations WHERE ai_path_id = %s", (path_id,)
        )
        assert cur.fetchone()[0] == 1
    live_postgres.rollback()


def test_pg_ac8_the_detail_read_gives_the_caller_back_their_own_reaction(live_postgres):
    """AC8, after a reload. The screen does not ask twice what this person already
    answered (`context-hub.md`, amendment of 2026-08-30, corrected the same day:
    the standing reaction is the LATEST row), and it could only honour that if
    the detail read tells it what this person already recorded.

    The composer the HTTP read calls is exercised here, on the real table, so
    `my_feedback` is proved end to end rather than as a shape.
    """
    from core.ai_paths_api import _my_feedback, _my_step_reactions

    org_id, project_id, _seeded = _pg_seed(live_postgres)
    path_id, _result_id = _pg_walk(live_postgres, org_id, project_id, steps=2)

    submit_ai_path_step_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person-1",
        path_id=path_id,
        step_ordinal=1,
        polarity="negative",
        comment="this step read the wrong Skill version",
        retry_key="ai-path-step-k",
    )

    reactions = read_actor_path_step_reactions(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person-1",
        path_id=path_id,
    )
    assert set(reactions) == {1}
    assert reactions[1]["polarity"] == "negative"
    assert reactions[1]["recorded_at"] is not None

    # ... keyed on the step's OWN ordinal, which is what the projection carries.
    composed = _my_step_reactions(
        live_postgres, org_id=org_id, project_id=project_id, actor="person-1", path_id=path_id
    )
    assert _my_feedback({"step_order": 1}, composed)["my_feedback"]["polarity"] == "negative"
    assert _my_feedback({"step_order": 0}, composed) == {"my_feedback": None}

    # SOMEBODY ELSE'S reaction is not this person's answer. `my_feedback` says
    # what I recorded, never what anyone recorded.
    assert (
        read_actor_path_step_reactions(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            actor="person-2",
            path_id=path_id,
        )
        == {}
    )
    live_postgres.rollback()


def test_pg_ac8_the_reader_takes_the_latest_reaction_comment_included(live_postgres):
    """Review of 2026-08-30 (round 2, B1): the store is insert-once, so a person
    who changes their mind writes a NEW row under a new key. The standing
    reaction on a step is therefore the LATEST row -- the reader used to take the
    first by `observed_at`, i.e. the superseded one -- and the sentence they
    wrote travels with it, so a reload forgets nothing."""
    from core.ai_paths_api import _my_feedback

    org_id, project_id, _seeded = _pg_seed(live_postgres)
    path_id, _result_id = _pg_walk(live_postgres, org_id, project_id, steps=2)

    for retry_key, polarity, comment in (
        ("ai-path-step-first", "negative", "first thought"),
        ("ai-path-step-second", "positive", "on reflection it read the right version"),
    ):
        submit_ai_path_step_feedback(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            actor="person-1",
            path_id=path_id,
            step_ordinal=1,
            polarity=polarity,
            comment=comment,
            retry_key=retry_key,
        )

    reactions = read_actor_path_step_reactions(
        live_postgres, org_id=org_id, project_id=project_id, actor="person-1", path_id=path_id
    )
    assert reactions[1]["polarity"] == "positive"
    assert reactions[1]["comment"] == "on reflection it read the right version"
    composed = _my_feedback({"step_order": 1}, reactions)["my_feedback"]
    assert composed["polarity"] == "positive"
    assert composed["comment"] == "on reflection it read the right version"
    live_postgres.rollback()


def test_pg_ac8_a_replay_returns_the_same_receipt_and_the_table_keeps_one_row(live_postgres):
    """The console's key is deterministic on (path, step), so the SECOND arrival
    of the same reaction is this replay -- not a second row an aggregate would
    count twice. A different polarity on that key is the conflict below it, and
    it leaves nothing behind (N1)."""
    org_id, project_id, _seeded = _pg_seed(live_postgres)
    path_id, _result_id = _pg_walk(live_postgres, org_id, project_id, steps=2)

    body = dict(
        org_id=org_id,
        project_id=project_id,
        actor="person-1",
        path_id=path_id,
        step_ordinal=0,
        polarity="positive",
        comment="clear",
        retry_key=f"ai-path-step-{path_id}-0",
    )
    first = submit_ai_path_step_feedback(live_postgres, **body)
    second = submit_ai_path_step_feedback(live_postgres, **body)

    assert first["status"] == "recorded"
    assert second["status"] == "replayed"
    assert second["feedback_id"] == first["feedback_id"]
    assert second["polarity"] == first["polarity"] == "positive"
    assert second["target"] == {"kind": "path_step", "ordinal": 0}

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.feedback_annotations WHERE ai_path_id = %s", (path_id,)
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT COUNT(*) FROM app.feedback_eligible_observations
            WHERE org_id = %s AND project_id = %s AND interaction_ref = %s
            """,
            (org_id, project_id, f"context-hub:ai-path:{path_id}:step:0"),
        )
        eligibility_rows = cur.fetchone()[0]
    assert eligibility_rows == 1

    # THE SAME KEY, A DIFFERENT REACTION: refused, and nothing is added.
    with pytest.raises(FeedbackRefused) as raised:
        submit_ai_path_step_feedback(live_postgres, **{**body, "polarity": "negative"})
    assert raised.value.code == "idempotency_conflict"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.feedback_annotations WHERE ai_path_id = %s", (path_id,)
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT COUNT(*) FROM app.feedback_eligible_observations
            WHERE org_id = %s AND project_id = %s AND interaction_ref = %s
            """,
            (org_id, project_id, f"context-hub:ai-path:{path_id}:step:0"),
        )
        assert cur.fetchone()[0] == eligibility_rows
    live_postgres.rollback()


def test_pg_ac8_a_step_the_walk_never_had_is_refused_by_the_foreign_key(live_postgres):
    """The database, not only the service: `fk_feedback_annotations_path_step`
    is what makes an invented ordinal impossible for ANY future writer."""
    org_id, project_id, result_id = _pg_seed(live_postgres)
    path_id, _delivered = _pg_walk(live_postgres, org_id, project_id, steps=2)

    with pytest.raises(Exception, match="fk_feedback_annotations_path_step"):
        _pg_annotation(
            live_postgres,
            org_id,
            project_id,
            result_id,
            target_kind="path_step",
            ai_path_id=path_id,
            ai_path_absent_literal=None,
            path_step_ordinal=99,
        )
    live_postgres.rollback()


def test_pg_ac8_a_walk_still_recording_is_refused_before_anything_is_written(live_postgres):
    org_id, project_id, _seeded = _pg_seed(live_postgres)
    path_id, _result_id = _pg_walk(live_postgres, org_id, project_id, lifecycle="recording")

    with pytest.raises(FeedbackRefused) as raised:
        submit_ai_path_step_feedback(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            actor="person-1",
            path_id=path_id,
            step_ordinal=0,
            polarity="positive",
            comment=None,
            retry_key="k-rec",
        )
    assert raised.value.code == "ai_path_still_recording"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.feedback_annotations WHERE ai_path_id = %s", (path_id,)
        )
        assert cur.fetchone()[0] == 0
    live_postgres.rollback()


def test_pg_an_annotation_cannot_be_updated_or_deleted(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    # The message is asserted, not only the failure: a generic error would let a
    # missing trigger pass for an enforced one.
    with live_postgres.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.feedback_annotations SET polarity = 'positive' WHERE id = %s",
            (feedback_id,),
        )
    live_postgres.rollback()

    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    with live_postgres.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute("DELETE FROM app.feedback_annotations WHERE id = %s", (feedback_id,))
    live_postgres.rollback()


def test_pg_an_annotation_cannot_pin_a_result_from_another_project(live_postgres):
    """The composite scoped foreign key, independent of any service check."""
    org_id, project_id, _result_id = _pg_seed(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.feedback_annotations
                (id, org_id, project_id, polarity, actor, actor_source, observed_surface,
                 result_id, ai_path_absent_literal, visible_versions_hash, observed_at)
            VALUES (%s, %s, %s, 'negative', 'person-1', 'user', 'console',
                    'qr_from_another_project', %s, %s, NOW())
            """,
            (f"fba_{ULID()}", org_id, project_id, NO_AI_PATH, "c" * 64),
        )
    live_postgres.rollback()


@pytest.mark.parametrize("literal", [None, "deferred", "", "no ai path"])
def test_pg_the_ai_path_pin_has_exactly_two_legal_states(live_postgres, literal):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.feedback_annotations
                (id, org_id, project_id, polarity, actor, actor_source, observed_surface,
                 result_id, ai_path_absent_literal, visible_versions_hash, observed_at)
            VALUES (%s, %s, %s, 'negative', 'person-1', 'user', 'console', %s, %s, %s, NOW())
            """,
            (f"fba_{ULID()}", org_id, project_id, result_id, literal, "c" * 64),
        )
    live_postgres.rollback()


@pytest.mark.parametrize("column,value", [("render_ref", "rnd_1"), ("datum_mark", '{"x": 1}')])
def test_pg_the_declared_but_unowned_pins_cannot_be_written(live_postgres, column, value):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    with pytest.raises(Exception):
        _pg_annotation(live_postgres, org_id, project_id, result_id, **{column: value})
    live_postgres.rollback()


def test_pg_the_surface_enum_does_not_yet_contain_share(live_postgres):
    """`share` is Story 50.7's to add. Until then it cannot be recorded."""
    org_id, project_id, result_id = _pg_seed(live_postgres)
    with pytest.raises(Exception):
        _pg_annotation(live_postgres, org_id, project_id, result_id, observed_surface="share")
    live_postgres.rollback()


def test_pg_a_review_version_is_insert_once(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    version_id = f"fbrv_{ULID()}"
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.feedback_review_versions
                (id, feedback_id, org_id, project_id, version_number, review_state,
                 affected_dimension, human_verdict, severity, reason, reviewer)
            VALUES (%s, %s, %s, %s, 1, 'triaged', 'semantic_correctness', 'fail',
                    'critical', 'wrong figure', 'reviewer-1')
            """,
            (version_id, feedback_id, org_id, project_id),
        )
    with live_postgres.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.feedback_review_versions SET human_verdict = 'pass' WHERE id = %s",
            (version_id,),
        )
    live_postgres.rollback()


@pytest.mark.parametrize("dimension", ["tone", "fluency", "layout_taste"])
def test_pg_an_invented_dimension_is_refused_by_the_database(live_postgres, dimension):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    with live_postgres.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.feedback_review_versions
                (id, feedback_id, org_id, project_id, version_number, review_state,
                 affected_dimension, human_verdict, severity, reason, reviewer)
            VALUES (%s, %s, %s, %s, 1, 'triaged', %s, 'fail', 'critical', 'x', 'reviewer-1')
            """,
            (f"fbrv_{ULID()}", feedback_id, org_id, project_id, dimension),
        )
    live_postgres.rollback()


def test_pg_the_exact_review_command_does_not_create_a_regression_case(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.golden_questions WHERE project_id = %s", (project_id,)
        )
        before = cur.fetchone()[0]

    append_review_version(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        feedback_id=feedback_id,
        reviewer="reviewer-1",
        payload={
            "schema_version": "feedback-review-command.v1",
            "expected_head": None,
            "retry_key": "retry-review-pg-1",
            "state": "triaged",
            "affected_dimension": "semantic_correctness",
            "human_verdict": "fail",
            "severity": "critical",
            "reason": "the figure contradicts the pinned definition",
        },
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.golden_questions WHERE project_id = %s", (project_id,)
        )
        assert cur.fetchone()[0] == before, "a seed is a request; it creates no Golden Question"
        cur.execute(
            "SELECT COUNT(*) FROM app.feedback_review_versions WHERE feedback_id = %s",
            (feedback_id,),
        )
        assert cur.fetchone()[0] == 1
    live_postgres.rollback()


def test_pg_rls_is_enabled_and_forced_on_the_three_tables(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE relnamespace = 'app'::regnamespace
              AND relname IN ('feedback_annotations', 'feedback_reviews',
                              'feedback_review_versions')
            ORDER BY relname
            """
        )
        rows = cur.fetchall()
    assert len(rows) == 3, "migration 153 has not been applied to this database"
    for name, enabled, forced in rows:
        assert enabled, f"{name} has no row level security"
        # FORCE matters: without it the table owner is silently exempt, and a
        # deployment that connects as owner reads every Project.
        assert forced, f"{name} does not FORCE row level security"


def test_pg_a_foreign_project_reads_nothing_under_enforcement(live_postgres):
    org_id, project_id, result_id = _pg_seed(live_postgres)
    feedback_id = _pg_annotation(live_postgres, org_id, project_id, result_id)
    with live_postgres.cursor() as cur:
        cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
        row = cur.fetchone()
    if row and row[0]:
        pytest.skip(
            "connected as a superuser: row level security is not applied, so this "
            "assertion would pass while proving nothing"
        )
    with live_postgres.cursor() as cur:
        cur.execute("SET LOCAL toorow.enforce_epic36 = 'on'")
        cur.execute("SELECT COUNT(*) FROM app.feedback_annotations WHERE id = %s", (feedback_id,))
        visible = cur.fetchone()[0]
    assert visible == 0, "an identity with no Project grant must read nothing"
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# 10. The routes, through the real ASGI application (AC13).
#
# `server/core/admin_api.py` belongs to another session, so this story delivers
# `feedback_review_routes` and the orchestrator adds the two mount lines. Until
# it does, `test_seam_the_feedback_routes_are_actually_mounted` FAILS and the
# behaviour seams below skip with that exact reason. One red, stated once, is
# the honest shape: a handler test alone would stay green while every request
# answered 404, which has already happened in this repository.
#
# Asserting "not 405" is not a mount proof either -- an unmounted path answers
# 404, not 405 -- so the proof reads the built application's own route table.
# ---------------------------------------------------------------------------

_BASE = f"/api/projects/{PROJECT}/test"


def _client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


#: One probe per (method, route). A route is "mounted" when an unauthenticated
#: request reaches its handler and is refused with 401; an UNmounted path never
#: reaches a handler and answers Starlette's bare 404 instead. Reading the
#: application's own route table is not an option here: `build_asgi_app()`
#: returns a middleware wrapping a closure, so the routers are not reachable as
#: attributes -- and a collector that silently finds nothing would report every
#: route as missing forever.
_ROUTE_PROBES = (
    ("POST", "/feedback"),
    ("POST", "/feedback/ai-path-steps"),
    ("GET", "/feedback"),
    ("GET", "/feedback/aggregates"),
    ("GET", "/feedback/critical-negatives"),
    ("GET", "/feedback/fba_1"),
    ("POST", "/feedback/fba_1/reviews"),
    ("GET", "/feedback/fba_1/reviews"),
)

#: A route this repository already mounts (Story 50.1). If the probe cannot see
#: THIS one either, the instrument is broken and the absence it reports is its
#: own, not the code's.
_KNOWN_MOUNTED_PROBE = ("GET", f"/api/projects/{PROJECT}/analyze/results/qr_probe")


def _unreachable_routes() -> list[tuple[str, str]]:
    client = _client()
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))):
        control = client.request(*_KNOWN_MOUNTED_PROBE)
        assert control.status_code == 401, (
            "the mount probe itself is broken: an already-mounted route did not "
            f"answer 401 but {control.status_code}"
        )
        return [
            (method, suffix)
            for method, suffix in _ROUTE_PROBES
            if client.request(method, f"{_BASE}{suffix}").status_code != 401
        ]


def _skip_unless_mounted() -> None:
    if _unreachable_routes():
        pytest.skip(
            "feedback_review_routes is not mounted in admin_api.py yet -- see "
            "test_seam_the_feedback_routes_are_actually_mounted"
        )


class _SeamCursor:
    def __init__(self):
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchone(self):
        if "app.projects" in self._sql:
            return (ORG,)
        return None

    def fetchall(self):
        return []


def _seam_connection():
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _SeamCursor())
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _authed(role_ok=True):
    from starlette.responses import JSONResponse

    denial = None if role_ok else JSONResponse({"code": "not_found"}, 404)
    return (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.admin_api._require_datastream_role", return_value=denial),
        patch("core.db.request_connection", return_value=_seam_connection()),
    )


def test_seam_the_feedback_routes_are_actually_mounted():
    missing = _unreachable_routes()
    assert not missing, (
        "these routes exist as handlers but no request can reach them; "
        f"admin_api.py must spread feedback_review_routes: {missing}"
    )


def test_seam_an_unauthenticated_caller_is_refused_before_any_work():
    _skip_unless_mounted()
    with patch("core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))):
        response = _client().get(f"{_BASE}/feedback/fba_anything")
    assert response.status_code == 401


def test_seam_a_denied_project_answers_the_same_envelope_as_a_missing_one():
    _skip_unless_mounted()
    auth, role, db = _authed(role_ok=False)
    with auth, role, db:
        denied = _client().get(f"{_BASE}/feedback/fba_secret")
    assert denied.status_code == 404
    assert denied.json()["code"] == "not_found"
    assert "fba_secret" not in denied.text


def test_seam_aggregates_is_never_captured_as_a_feedback_id():
    from core.feedback_review_api import feedback_review_routes

    paths = [r.path for r in feedback_review_routes]
    template = "/api/projects/{project_id}/test"
    literal = paths.index(f"{template}/feedback/aggregates")
    parameterized = paths.index(f"{template}/feedback/{{feedback_id}}")
    assert literal < parameterized, "Starlette matches in declaration order"

    # And at the transport level: the two paths must not return the same shape.
    _skip_unless_mounted()
    auth, role, db = _authed()
    with auth, role, db:
        aggregates = _client().get(
            f"{_BASE}/feedback/aggregates",
            params={"observed_from": "2026-08-01", "observed_to": "2026-08-02"},
        )
        one = _client().get(f"{_BASE}/feedback/fba_1")
    assert aggregates.status_code == 200
    assert "axes" in aggregates.json()
    assert one.status_code == 404


def test_seam_ac8_the_step_door_is_never_captured_as_a_feedback_id():
    """`/feedback/ai-path-steps` before `/feedback/{feedback_id}`: Starlette
    matches the PATH before the method, so the reversed order answers 405 on a
    POST to a literal segment that exists."""
    from core.feedback_review_api import feedback_review_routes

    paths = [r.path for r in feedback_review_routes]
    template = "/api/projects/{project_id}/test"
    literal = paths.index(f"{template}/feedback/ai-path-steps")
    parameterized = paths.index(f"{template}/feedback/{{feedback_id}}")
    assert literal < parameterized


def test_seam_ac8_the_step_door_refuses_a_write_without_an_idempotency_key():
    """One reaction, one row. A thumbs-down that double-writes on a slow network
    would be counted twice by every aggregate that reads it."""
    _skip_unless_mounted()
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().post(
            f"{_BASE}/feedback/ai-path-steps",
            json={"ai_path_id": "aip_1", "step_ordinal": 0, "polarity": "positive"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "missing_idempotency_key"


def test_seam_ac8_an_unknown_or_foreign_walk_answers_the_non_disclosing_404():
    _skip_unless_mounted()
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().post(
            f"{_BASE}/feedback/ai-path-steps",
            headers={"Idempotency-Key": "k-1"},
            json={"ai_path_id": "aip_secret", "step_ordinal": 0, "polarity": "negative"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "aip_secret" not in response.text


def test_seam_ac8_the_step_door_accepts_only_the_four_fields_of_the_command():
    """A caller cannot slip a Result, a Render pin or a surface into the row:
    every owner is re-resolved server-side, so an unknown field is a refusal."""
    _skip_unless_mounted()
    auth, role, db = _authed()
    with auth, role, db:
        response = _client().post(
            f"{_BASE}/feedback/ai-path-steps",
            headers={"Idempotency-Key": "k-2"},
            json={
                "ai_path_id": "aip_1",
                "step_ordinal": 0,
                "polarity": "positive",
                "result_id": "qr_forged",
            },
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_body"


def test_seam_ac8_the_step_door_holds_the_same_grant_as_the_slice_annotation():
    """Filing a reaction is a reader's right (`viewer`); JUDGING one is
    `_create_review`'s `member`. A third answer to one authorization question is
    how the two drift."""
    source = inspect.getsource(_create_ai_path_step_feedback)
    assert '_authorized_connection(request, "viewer")' in source
    assert "submit_ai_path_step_feedback(" in source
    # The door translates HTTP and nothing else -- the module's own contract.
    assert "INSERT INTO" not in source


# ---------------------------------------------------------------------------
# AC8 -- THE GUARD IS EXECUTED, not read.
#
# Every assertion above about authorization was either an `inspect.getsource`
# substring or a run with `_require_datastream_role` patched away, so all of them
# stayed green with the guard neutered: they proved the door NAMES a check, never
# that a refused caller is refused. The two below run the real
# `core.admin_api._require_datastream_role`.
# ---------------------------------------------------------------------------


class _RecordingSeamCursor(_SeamCursor):
    """A `_SeamCursor` that keeps every statement, so "zero rows" is measurable."""

    def __init__(self, log):
        super().__init__()
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(sql)
        super().execute(sql, params)


def _recording_seam_connection(log):
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _RecordingSeamCursor(log))
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_seam_ac8_a_caller_the_real_role_check_refuses_is_refused_and_writes_nothing():
    """The guard RUNS here: only its own dependency (`identity_has_project_role`)
    is stubbed, so `_require_datastream_role` decides, audits and answers.

    The refusal is the one `_authorized_connection` gives -- the same 404
    envelope a missing Project gets, because a distinct 403 would tell a stranger
    that this Project exists.
    """
    _skip_unless_mounted()
    statements: list[str] = []
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.request_connection", return_value=_recording_seam_connection(statements)),
        patch("core.project_access.identity_has_project_role", return_value=False) as role_check,
        patch("core.admin_api.write_audit_row") as audited,
        patch("core.feedback_review_api.submit_ai_path_step_feedback") as writer,
    ):
        response = _client().post(
            f"{_BASE}/feedback/ai-path-steps",
            headers={"Idempotency-Key": "k-denied"},
            json={"ai_path_id": "aip_1", "step_ordinal": 0, "polarity": "positive"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    # The guard was actually consulted, and it actually refused.
    assert role_check.call_count == 1
    assert audited.call_count == 1
    # ZERO ROWS. Not "the writer says it refused": the command was never called
    # and no INSERT of any kind reached the connection.
    assert writer.call_count == 0
    assert not [sql for sql in statements if "INSERT" in sql.upper()]


def test_seam_ac8_the_role_this_route_asks_for_is_viewer():
    """Filing a reaction is a reader's right. `_create_review`, which JUDGES one,
    is the `member` route.

    Recorded from the CALL, not from the source: a substring assertion on
    `inspect.getsource` stays green when the argument is passed and then ignored,
    and it was the only proof this route had.
    """
    _skip_unless_mounted()
    from core.admin_api import _require_datastream_role as real_guard

    recorder = MagicMock(side_effect=real_guard)
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "person-1"))),
        patch("core.db.request_connection", return_value=_seam_connection()),
        patch("core.admin_api._require_datastream_role", new=recorder),
        patch("core.project_access.identity_has_project_role", return_value=True) as role_check,
    ):
        _client().post(
            f"{_BASE}/feedback/ai-path-steps",
            headers={"Idempotency-Key": "k-viewer"},
            json={"ai_path_id": "aip_1", "step_ordinal": 0, "polarity": "positive"},
        )

    assert recorder.call_count == 1
    positional = recorder.call_args.args
    assert positional[0] == PROJECT
    assert positional[1] == "person-1"
    assert positional[2] == "viewer"
    # And the guard did not merely receive the word: it asked the access owner
    # for exactly that role.
    assert role_check.call_args.args[2] == "viewer"


def test_seam_the_legacy_feedback_route_is_not_shadowed_by_this_namespace():
    """AC1: `/api/feedback` (Story 5.5) stays a different, unpinned read."""
    from core.feedback_review_api import feedback_review_routes

    assert all(r.path.startswith("/api/projects/") for r in feedback_review_routes)
    assert all(r.path != "/api/feedback" for r in feedback_review_routes)


def test_exact_feedback_http_scores_only_after_the_write_commit(monkeypatch):
    from core.analyze_feedback import mint_feedback_context

    secret = "feedback-api-test-secret-32-bytes"
    monkeypatch.setenv("TOOROW_FEEDBACK_CONTEXT_SECRET", secret)
    context = mint_feedback_context(
        {
            "org_id": ORG,
            "project_id": PROJECT,
            "surface": "console",
            "interaction_ref": "afi_01K00000000000000000000000",
            "result_id": "res_01K00000000000000000000000",
            "result_content_hash": "a" * 64,
            "delivered_rows": {"start": 0, "count": 0, "fields": []},
            "w3c_trace_id": "b" * 32,
        },
        secret=secret.encode(),
    )
    events = []
    connection = MagicMock()
    connection.cursor = MagicMock(side_effect=lambda: _SeamCursor())
    connection.commit.side_effect = lambda: events.append("commit")
    connection.rollback.side_effect = lambda: events.append("rollback")
    manager = MagicMock()
    manager.__enter__ = MagicMock(return_value=connection)
    manager.__exit__ = MagicMock(return_value=False)

    def score(**_kwargs):
        assert events == ["commit"]
        events.append("score")

    auth, role, db = _authed()
    with (
        auth,
        role,
        db,
        patch("core.db.request_connection", return_value=manager),
        patch(
            "core.feedback_review_api.submit_exact_feedback",
            return_value={
                "schema_version": "exact-feedback-receipt.v1",
                "status": "recorded",
                "feedback_id": "fba_1",
                "interaction_ref": context["interaction_ref"],
                "target": {"kind": "answer"},
            },
        ),
        patch("core.analyze_feedback.score_feedback_after_commit", side_effect=score),
    ):
        response = _client().post(
            f"{_BASE}/feedback",
            json={
                "context": context,
                "target": {"kind": "answer"},
                "polarity": "positive",
                "comment": None,
                "retry_key": "api-commit-order",
            },
        )

    assert response.status_code == 201
    assert events == ["commit", "score"]


# ---------------------------------------------------------------------------
# Story 49.6 AC8 -- the +/- annotation on an exact observed AI Path step.
#
# The store was already shaped for this: migration 249 carries
# `(ai_path_id, path_step_ordinal) -> app.ai_path_steps` and the
# `target_kind = 'path_step'` branch of `ck_feedback_annotations_target`, and
# until now only a delivered Result slice could reach them. These assert the
# third entry point -- what it re-resolves, what it refuses, and that it writes
# through the SAME row writer as the slice, so the two cannot drift.
# ---------------------------------------------------------------------------

_PATH_ID = "aip_01J0000000000000000000000A"
_ELIGIBLE = {"schema_version": "feedback-eligibility.v1"}
_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


class _StepScript:
    """The three owner reads the step writer performs, in order."""

    def __init__(self, **overrides):
        self.statements: list[tuple[str, object]] = []
        self.path_row = overrides.get("path_row", ("finalized", "a" * 64, _TRACE))
        self.step_row = overrides.get("step_row", (1,))
        self.result_row = overrides.get(
            "result_row",
            ("qr_1", "b" * 64, "qsv_1", "sv_1", "svv_1", "c" * 64, "d" * 64),
        )
        self.existing = overrides.get("existing", None)

    def one(self, sql: str):
        if "FROM app.ai_paths" in sql:
            return self.path_row
        if "FROM app.ai_path_steps" in sql:
            return self.step_row
        if "FROM app.query_results qr" in sql:
            return self.result_row
        if "FROM app.feedback_annotations" in sql and "retry_key_hash = %s" in sql:
            return self.existing
        return None

    def many(self, sql: str):
        return []


def _step_connection(script: _StepScript):
    conn = MagicMock()
    conn.cursor = MagicMock(side_effect=lambda: _Cursor(script))
    return conn


def _submit_step(script: _StepScript, *, eligibility=_ELIGIBLE, **overrides):
    """Drive the command with the delivery fact stubbed.

    `record_feedback_eligibility` reads the Result payload manifest and replays
    its own row; faking that with a scripted cursor would assert the fake. Its
    presence in the command is proved by
    `test_ac8_an_unavailable_delivery_fact_is_refused_rather_than_crashed`, and
    its REAL behaviour by the live-Postgres tests, which run the trigger
    (`require_feedback_eligible_observation`) that makes it mandatory.
    """
    kwargs = {
        "org_id": ORG,
        "project_id": PROJECT,
        "actor": "person-1",
        "path_id": _PATH_ID,
        "step_ordinal": 2,
        "polarity": "negative",
        "comment": "this retrieval did not answer the question",
        "retry_key": "key-1",
    }
    kwargs.update(overrides)
    with patch(
        "core.feedback_review.record_feedback_eligibility", return_value=eligibility
    ), patch("core.feedback_review.require_feedback_eligibility", return_value={}):
        return submit_ai_path_step_feedback(_step_connection(script), **kwargs)


def _annotation_insert(script: _StepScript):
    return next(
        (s, p) for s, p in script.statements if "INSERT INTO app.feedback_annotations" in s
    )


def test_ac8_a_step_target_is_recorded_as_an_exact_path_step_annotation():
    script = _StepScript()
    receipt = _submit_step(script)

    assert receipt["status"] == "recorded"
    assert receipt["target"] == {"kind": "path_step", "ordinal": 2}
    assert receipt["polarity"] == "negative"
    assert receipt["ai_path_id"] == _PATH_ID
    assert receipt["feedback_id"].startswith("fba_")

    sql, params = _annotation_insert(script)
    assert "path_step_ordinal" in sql
    assert "path_step" in params and 2 in params
    assert _PATH_ID in params
    # `ck_feedback_annotations_surface` knows two words; a Context Hub screen is
    # the console, and no call site spells it.
    assert "console" in params
    # Nothing about a Render: a walk read in Context Hub was drawn by no Render,
    # and `ck_feedback_annotations_render_pins_all_or_none` refuses half a tuple.
    assert params[13] is None
    assert params[14:19] == (None, None, None, None, None)
    # The trace the walk carries travels with the annotation.
    assert _TRACE in params


def test_ac8_the_walk_the_step_and_the_result_are_all_re_resolved_in_scope():
    script = _StepScript()
    _submit_step(script)
    reads = [s for s, _ in script.statements if "SELECT" in s]

    path_read = next(s for s in reads if "FROM app.ai_paths" in s)
    step_read = next(s for s in reads if "FROM app.ai_path_steps" in s)
    result_read = next(s for s in reads if "FROM app.query_results qr" in s)
    for read in (path_read, step_read, result_read):
        assert "org_id = %s" in read and "project_id = %s" in read


def test_ac8_an_unknown_or_foreign_walk_is_the_same_not_found_as_the_read():
    with pytest.raises(FeedbackNotFound):
        _submit_step(_StepScript(path_row=None))


def test_ac8_a_step_this_walk_does_not_carry_is_the_same_not_found():
    with pytest.raises(FeedbackNotFound):
        _submit_step(_StepScript(step_row=None))


def test_ac8_a_recording_walk_is_named_rather_than_hidden():
    """The reader has the path open; a 404 would deny what the screen shows."""
    with pytest.raises(FeedbackRefused) as raised:
        _submit_step(_StepScript(path_row=("recording", "a" * 64, _TRACE)))
    assert raised.value.code == "ai_path_still_recording"


def test_ac8_a_walk_that_delivered_no_result_says_so_instead_of_failing_on_a_check():
    """`app.feedback_annotations.result_id` is NOT NULL (migration 153) and
    exact-feedback.v1 requires its content hash (249). The refusal is named
    before the INSERT rather than surfacing as an integrity error."""
    script = _StepScript(result_row=None)
    with pytest.raises(FeedbackRefused) as raised:
        _submit_step(script)
    assert raised.value.code == "ai_path_delivered_no_result"
    assert not [s for s, _ in script.statements if "INSERT INTO" in s]


@pytest.mark.parametrize(
    "field,value",
    [("polarity", "neutral"), ("step_ordinal", -1), ("step_ordinal", "2"), ("retry_key", "")],
)
def test_ac8_an_invented_target_or_vocabulary_is_refused_by_name(field, value):
    with pytest.raises(FeedbackRefused) as raised:
        _submit_step(_StepScript(), **{field: value})
    assert raised.value.code in {"invalid_field", "invalid_vocabulary", "missing_field"}


def test_ac8_an_unavailable_delivery_fact_is_refused_rather_than_crashed():
    """Migration 251 puts `require_feedback_eligible_observation` on the table.
    Without the delivery fact the INSERT dies on a raw integrity error, so the
    command asks for it first and names the refusal."""
    script = _StepScript()
    with pytest.raises(FeedbackRefused) as raised:
        _submit_step(script, eligibility=None)
    assert raised.value.code == "feedback_eligibility_unavailable"
    assert not [s for s, _ in script.statements if "INSERT INTO app.feedback_annotations" in s]


def test_ac8_every_step_annotation_writes_the_same_audit_row_as_the_slice():
    script = _StepScript()
    with patch("core.feedback_review.insert_audit_row") as audited:
        _submit_step(script)
    assert audited.call_count == 1
    assert audited.call_args.kwargs["action"] == ACTION_FEEDBACK_ANNOTATION_CREATED
    assert audited.call_args.kwargs["identity"] == "person-1"
    assert audited.call_args.kwargs["metadata"]["target_kind"] == "path_step"


def test_ac8_the_same_key_replays_the_stored_verdict_instead_of_writing_twice():
    """The verdict a surface displays after a retry is the STORED one."""
    probe = _StepScript()
    _submit_step(probe)
    # Column order ends: ..., retry_key_hash, request_hash, visible_versions, hash.
    stored_request_hash = _annotation_insert(probe)[1][-3]

    replayed = _StepScript(
        existing=("fba_existing", stored_request_hash, "ir", 2, "positive", "kept")
    )
    receipt = _submit_step(replayed)

    assert receipt["status"] == "replayed"
    assert receipt["feedback_id"] == "fba_existing"
    # The polarity SENT was negative; the polarity SHOWN is the one on the row.
    assert receipt["polarity"] == "positive"
    assert receipt["comment"] == "kept"
    assert not [s for s, _ in replayed.statements if "INSERT INTO" in s]


def test_ac8_the_same_key_carrying_different_feedback_is_a_conflict():
    conflicting = _StepScript(existing=("fba_existing", "0" * 64, "ir", 2, "positive", None))
    with pytest.raises(FeedbackRefused) as raised:
        _submit_step(conflicting)
    assert raised.value.code == "idempotency_conflict"


_ELIGIBILITY_MARK = "-- record_feedback_eligibility"


def _submit_step_recording_order(script: _StepScript, **overrides):
    """Drive the command with the delivery-fact write MARKED in the statement log.

    N1 is an ORDER, so it is proved by an order and not by a mock's `called`
    flag: the spy appends its own line to the same `script.statements` the
    scripted cursor writes, and the assertions read the sequence.
    """
    kwargs = {
        "org_id": ORG,
        "project_id": PROJECT,
        "actor": "person-1",
        "path_id": _PATH_ID,
        "step_ordinal": 2,
        "polarity": "negative",
        "comment": None,
        "retry_key": "key-1",
    }
    kwargs.update(overrides)

    def _spy(conn, *, claims, source="authenticated"):
        script.statements.append((_ELIGIBILITY_MARK, claims))
        return _ELIGIBLE

    with patch("core.feedback_review.record_feedback_eligibility", new=_spy), patch(
        "core.feedback_review.require_feedback_eligibility", return_value={}
    ):
        return submit_ai_path_step_feedback(_step_connection(script), **kwargs)


def _statement_index(script: _StepScript, needle: str) -> int:
    for position, (sql, _params) in enumerate(script.statements):
        if needle in sql:
            return position
    raise AssertionError(f"no statement contains {needle!r}")


def test_n1_a_refused_reaction_writes_no_delivery_fact():
    """The delivery fact used to be recorded BEFORE the retry key was read back.

    So a second, different polarity on the same key answered
    `idempotency_conflict` -- nothing was annotated -- and
    `app.feedback_eligible_observations` had grown all the same. A refusal that
    leaves a row is a refusal that half happened, and the row it leaves is the
    one that says what a person was DELIVERED as annotatable.
    """
    conflicting = _StepScript(existing=("fba_existing", "0" * 64, "ir", 2, "positive", None))

    with pytest.raises(FeedbackRefused) as raised:
        _submit_step_recording_order(conflicting)

    assert raised.value.code == "idempotency_conflict"
    written = [sql for sql, _ in conflicting.statements]
    assert _ELIGIBILITY_MARK not in written
    assert not any("INSERT INTO app.feedback_annotations" in sql for sql in written)


def test_n1_the_delivery_fact_is_written_between_the_replay_check_and_the_append():
    """And on the accepted path it still happens, in the only order that holds:
    after everything that can refuse, before the row it is required by."""
    script = _StepScript()
    receipt = _submit_step_recording_order(script)

    assert receipt["status"] == "recorded"
    replay_check = _statement_index(script, "retry_key_hash = %s")
    delivery_fact = _statement_index(script, _ELIGIBILITY_MARK)
    append = _statement_index(script, "INSERT INTO app.feedback_annotations")
    assert replay_check < delivery_fact < append


def test_ac8_one_writer_still_owns_the_annotation_row():
    """Both exact entry points go through `_append_exact_annotation`: a second
    INSERT would answer differently the first time one of them was fixed."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    exact = source.split("def _append_exact_annotation")[1]
    assert exact.count("INSERT INTO app.feedback_annotations") == 1
    assert "def submit_exact_feedback" in exact and "def submit_ai_path_step_feedback" in exact


def test_ac8_the_existing_write_ack_tool_keeps_its_signature():
    """AC8: "Existing MCP `submit_feedback` behavior remains compatible"."""
    import inspect as _inspect

    from core.feedback_mcp import submit_feedback

    assert list(_inspect.signature(submit_feedback).parameters) == [
        "project_id",
        "rating",
        "trace_id",
        "comment",
        "report_ref",
        "connector",
        # Added 2026-08-30 (mcp-tool-surface amendment): the append is refused
        # unless the call carries the server-minted handle from the report's
        # `_meta`. Optional ("") so every pre-existing call SHAPE still binds;
        # the REFUSAL of a missing handle is the new behaviour, tested in
        # test_submit_feedback.py.
        "handle",
    ]
