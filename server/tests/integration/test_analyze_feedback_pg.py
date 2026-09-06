from __future__ import annotations

import concurrent.futures
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from core import analyze_render_mcp as adapter
from core.analyze_feedback import mint_feedback_context, verify_feedback_context
from core.feedback_review import FeedbackRefused, submit_exact_feedback
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ulid import ULID

from tests.core.test_feedback_review import _pg_seed

pytestmark = pytest.mark.pg

SECRET = b"feedback-test-secret-32-bytes-long"


def _seed_result(conn):
    org_id, project_id, result_id = _pg_seed(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
            VALUES (%s, %s, %s, %s, %s::jsonb, '{}'::jsonb, '[]'::jsonb)
            """,
            (result_id, org_id, project_id, "b" * 64, '{"fields": []}'),
        )
    return org_id, project_id, result_id


def _context(conn, org_id, project_id, result_id, *, now=None):
    from core.feedback_review import record_feedback_eligibility

    sidecar = mint_feedback_context(
        {
            "org_id": org_id,
            "project_id": project_id,
            "surface": "mcp_app",
            "interaction_ref": "afi_01K00000000000000000000000",
            "result_id": result_id,
            "result_content_hash": "b" * 64,
            "delivered_rows": {"start": 0, "count": 0, "fields": []},
        },
        secret=SECRET,
        now=now,
    )
    claims = verify_feedback_context(sidecar, secret=SECRET, now=now)
    assert record_feedback_eligibility(conn, claims=claims) is not None
    return sidecar


def _payload(context, *, retry_key="retry-1", comment="This answer helped."):
    return {
        "context": context,
        "target": {"kind": "answer"},
        "polarity": "positive",
        "comment": comment,
        "retry_key": retry_key,
    }


def test_exact_feedback_commits_once_and_replays_under_the_same_retry_hash(live_postgres):
    org_id, project_id, result_id = _seed_result(live_postgres)
    payload = _payload(_context(live_postgres, org_id, project_id, result_id))

    accepted = submit_exact_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person@example.com",
        payload=payload,
        secret=SECRET,
    )
    replayed = submit_exact_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person@example.com",
        payload=payload,
        secret=SECRET,
    )

    assert accepted["status"] == "recorded"
    assert replayed == {**accepted, "status": "replayed"}
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), min(target_schema_version), min(result_content_hash),
                   min(target_kind), min(length(comment))
            FROM app.feedback_annotations WHERE id = %s
            """,
            (accepted["feedback_id"],),
        )
        assert cur.fetchone() == (1, "exact-feedback.v1", "b" * 64, "answer", 19)
        cur.execute(
            "SELECT count(*) FROM app.feedback_reviews WHERE feedback_id = %s",
            (accepted["feedback_id"],),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM app.audit_log WHERE metadata ->> 'feedback_id' = %s",
            (accepted["feedback_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_same_retry_key_with_changed_request_refuses_without_a_second_row(live_postgres):
    org_id, project_id, result_id = _seed_result(live_postgres)
    context = _context(live_postgres, org_id, project_id, result_id)
    submit_exact_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person@example.com",
        payload=_payload(context),
        secret=SECRET,
    )

    with pytest.raises(FeedbackRefused, match="already used"):
        submit_exact_feedback(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            actor="person@example.com",
            payload=_payload(context, comment="Changed"),
            secret=SECRET,
        )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.feedback_annotations "
            "WHERE target_schema_version = 'exact-feedback.v1' AND project_id = %s",
            (project_id,),
        )
        assert cur.fetchone()[0] == 1


def test_expired_context_is_reissued_after_scope_check_and_writes_nothing(live_postgres):
    org_id, project_id, result_id = _seed_result(live_postgres)
    issued = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

    receipt = submit_exact_feedback(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        actor="person@example.com",
        payload=_payload(_context(live_postgres, org_id, project_id, result_id, now=issued)),
        secret=SECRET,
        now=issued + timedelta(minutes=16),
    )

    assert receipt["status"] == "refresh_required"
    assert receipt["code"] == "feedback_context_expired"
    assert receipt["feedback_context"]["schema_version"] == "exact-feedback.v1"
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.feedback_annotations "
            "WHERE target_schema_version = 'exact-feedback.v1' AND project_id = %s",
            (project_id,),
        )
        assert cur.fetchone()[0] == 0


def test_database_refuses_every_future_non_v1_annotation(live_postgres):
    org_id, project_id, result_id = _seed_result(live_postgres)
    with live_postgres.cursor() as cur, pytest.raises(Exception, match="exact-feedback.v1"):
        cur.execute(
            """
            INSERT INTO app.feedback_annotations
                (id, org_id, project_id, polarity, actor, actor_source, observed_surface,
                 result_id, ai_path_absent_literal, visible_versions_hash, observed_at)
            VALUES ('fba_01K00000000000000000000000', %s, %s, 'negative', 'person',
                    'user', 'console', %s, 'No AI path', %s, NOW())
            """,
            (org_id, project_id, result_id, "c" * 64),
        )


@pytest.mark.anyio
async def test_real_fastmcp_mints_then_submits_with_rls_and_rolls_back_failure(
    live_postgres, monkeypatch, tmp_path
):
    from core import feedback_review

    from tests.integration import test_render_shares_postgres as share_fixture
    from tests.integration.test_analyze_result_slices_pg import _write_runtime
    from tests.integration.test_render_shares_postgres import Chain, _uid

    inline = Chain(live_postgres).build()
    identity = f"story-65-5-inline-{secrets.token_hex(4)}@example.com"
    project_id = inline.project_id
    spec_id, large_result_id = inline.spec_version_id, inline.result_id
    retained_schema = {
        "fields": [*share_fixture.RESULT_SCHEMA["fields"], {"name": "safe_unbound"}]
    }
    retained_rows = [
        {**row, "safe_unbound": index}
        for index, row in enumerate(share_fixture.RESULT_ROWS)
    ]
    with (
        patch.object(share_fixture, "RESULT_SCHEMA", retained_schema),
        patch.object(share_fixture, "RESULT_ROWS", retained_rows),
    ):
        retained = Chain(live_postgres, with_ai_path=True).build()
    with patch("core.ai_paths.finalize_path", return_value={}):
        recording = Chain(live_postgres, with_ai_path=True).build()
    retained_identity = f"story-65-5-retained-{secrets.token_hex(4)}@example.com"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
            (_uid("omem"), inline.org_id, identity),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
            (_uid("omem"), retained.org_id, retained_identity),
        )
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
            (_uid("omem"), recording.org_id, retained_identity),
        )
    live_postgres.commit()
    bundle = _write_runtime(tmp_path)
    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", str(bundle))
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_FEEDBACK_CONTEXT_SECRET", SECRET.decode())
    monkeypatch.setenv("PLATFORM_DB_URL", live_postgres.info.dsn)
    current_identity = {"value": identity}
    monkeypatch.setattr(adapter, "_identity", lambda: current_identity["value"])

    target = FastMCP("story-65-5-exact-feedback")
    adapter.register(target)
    async with Client(FastMCPTransport(target)) as mcp_client:
        tools = {tool.name: tool for tool in await mcp_client.list_tools()}
        assert tools["submit_analyze_feedback"].meta["ui"]["visibility"] == ["app"]
        rendered = await mcp_client.call_tool(
            "render_analyze_result",
            {
                "project_id": project_id,
                "result_id": large_result_id,
                "visualization_spec_version_id": spec_id,
            },
        )
        context = rendered.meta["toorow.feedback"]
        claims_body = context["token"].split(".", 1)[0]
        assert "rows" not in json.loads(_decode(claims_body))

        recorded = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": context,
                "target": {"kind": "answer"},
                "polarity": "negative",
                "comment": "The last visible value needs review.",
                "retry_key": "fastmcp-recorded",
            },
        )
        receipt = recorded.structured_content or recorded.data
        assert receipt["status"] == "recorded"

        outside_window = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": context,
                "target": {"kind": "datum", "row_index": 3, "field": "sessions"},
                "polarity": "negative",
                "comment": None,
                "retry_key": "not-delivered",
            },
            raise_on_error=False,
        )
        assert outside_window.is_error and "not_found" in _tool_error_text(outside_window)

        current_identity["value"] = "foreign@example.com"
        denied = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": context,
                "target": {"kind": "answer"},
                "polarity": "positive",
                "comment": None,
                "retry_key": "foreign",
            },
            raise_on_error=False,
        )
        assert denied.is_error and "not_found" in _tool_error_text(denied)

        current_identity["value"] = identity
        original_audit = feedback_review.insert_audit_row

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("audit tripwire")

        monkeypatch.setattr(feedback_review, "insert_audit_row", fail_audit)
        failed = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": context,
                "target": {"kind": "answer"},
                "polarity": "negative",
                "comment": None,
                "retry_key": "must-roll-back",
            },
            raise_on_error=False,
        )
        assert failed.is_error
        monkeypatch.setattr(feedback_review, "insert_audit_row", original_audit)

        from core import query_specs_api
        from core.analyze_feedback import verify_feedback_context

        app = Starlette(routes=query_specs_api.query_spec_routes)
        with (
            patch.object(
                query_specs_api,
                "_authorize",
                new=AsyncMock(return_value=(retained_identity, retained.org_id)),
            ),
            TestClient(app) as http_client,
        ):
            evidence = http_client.get(
                f"/api/projects/{retained.project_id}/analyze/results/"
                f"{retained.result_id}/evidence?render_id={retained.render_id}"
            )
            missing_render = http_client.get(
                f"/api/projects/{retained.project_id}/analyze/results/"
                f"{retained.result_id}/evidence?render_id=rnd_missing"
            )
        with (
            patch.object(
                query_specs_api,
                "_authorize",
                new=AsyncMock(return_value=(retained_identity, recording.org_id)),
            ),
            TestClient(app) as http_client,
        ):
            recording_evidence = http_client.get(
                f"/api/projects/{recording.project_id}/analyze/results/"
                f"{recording.result_id}/evidence?render_id={recording.render_id}"
            )
        assert evidence.status_code == 200
        assert missing_render.status_code == 404
        assert recording_evidence.status_code == 200
        retained_context = evidence.json()["feedback_context"]
        retained_claims = verify_feedback_context(retained_context, secret=SECRET)
        assert retained_claims["render_id"] == retained.render_id
        assert retained_claims["result_id"] == retained.result_id
        assert retained_claims["path_step_ordinals"] == [0]
        assert retained_claims["delivered_rows"]["count"] == 3

        current_identity["value"] = retained_identity
        datum_receipt_result = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": retained_context,
                "target": {"kind": "datum", "row_index": 2, "field": "sessions"},
                "polarity": "negative",
                "comment": "This visible value needs review.",
                "retry_key": "retained-datum",
            },
        )
        datum_receipt = datum_receipt_result.structured_content or datum_receipt_result.data
        assert datum_receipt["status"] == "recorded"
        unbound_field = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": retained_context,
                "target": {"kind": "datum", "row_index": 2, "field": "safe_unbound"},
                "polarity": "negative",
                "comment": None,
                "retry_key": "unbound-field",
            },
            raise_on_error=False,
        )
        assert unbound_field.is_error and "not_found" in _tool_error_text(unbound_field)
        path_receipt_result = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": retained_context,
                "target": {"kind": "path_step", "ordinal": 0},
                "polarity": "positive",
                "comment": "This observed step explains the answer.",
                "retry_key": "retained-path",
            },
        )
        path_receipt = path_receipt_result.structured_content or path_receipt_result.data
        assert path_receipt["status"] == "recorded"
        wrong_step = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": retained_context,
                "target": {"kind": "path_step", "ordinal": 1},
                "polarity": "negative",
                "comment": None,
                "retry_key": "wrong-step",
            },
            raise_on_error=False,
        )
        assert wrong_step.is_error and "not_found" in _tool_error_text(wrong_step)

        tampered_claims = dict(retained_claims)
        tampered_claims["runtime_build_id"] = "runtime@different"
        tampered = mint_feedback_context(tampered_claims, secret=SECRET)
        wrong_pin = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": tampered,
                "target": {"kind": "answer"},
                "polarity": "negative",
                "comment": None,
                "retry_key": "wrong-render-pin",
            },
            raise_on_error=False,
        )
        assert wrong_pin.is_error and "not_found" in _tool_error_text(wrong_pin)
        recording_context = recording_evidence.json()["feedback_context"]
        recording_path = await mcp_client.call_tool(
            "submit_analyze_feedback",
            {
                "context": recording_context,
                "target": {"kind": "answer"},
                "polarity": "negative",
                "comment": None,
                "retry_key": "recording-path",
            },
            raise_on_error=False,
        )
        assert recording_path.is_error and "not_found" in _tool_error_text(recording_path)

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.feedback_annotations "
            "WHERE project_id = %s AND result_id = %s "
            "AND target_schema_version = 'exact-feedback.v1'",
            (project_id, large_result_id),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT render_ref, visualization_spec_version_id, renderer_build_id,
                   runtime_build_id, theme_version, formatter_version, target_kind,
                   path_step_ordinal
            FROM app.feedback_annotations
            WHERE project_id = %s AND target_schema_version = 'exact-feedback.v1'
              AND target_kind = 'path_step'
            """,
            (retained.project_id,),
        )
        assert cur.fetchone() == (
            retained_claims["render_id"],
            retained_claims["visualization_spec_version_id"],
            retained_claims["renderer_build_id"],
            retained_claims["runtime_build_id"],
            retained_claims["theme_version"],
            retained_claims["formatter_version"],
            "path_step",
            0,
        )


def test_committed_concurrent_retry_records_once_and_replays_once(live_postgres, monkeypatch):
    from core.db import request_connection

    org_id, project_id, result_id = _seed_result(live_postgres)
    suffix = secrets.token_hex(6)
    identity = f"concurrent-feedback-{suffix}@example.com"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.org_members (id, org_id, identity, role, status, joined_at) "
            "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
            (f"omem_concurrent_{suffix}", org_id, identity),
        )
    live_postgres.commit()
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("PLATFORM_DB_URL", live_postgres.info.dsn)
    payload = _payload(
        _context(live_postgres, org_id, project_id, result_id), retry_key="concurrent"
    )
    live_postgres.commit()
    barrier = threading.Barrier(2)

    def submit_and_commit():
        barrier.wait(timeout=5)
        with request_connection(identity) as conn:
            receipt = submit_exact_feedback(
                conn,
                org_id=org_id,
                project_id=project_id,
                actor=identity,
                payload=payload,
                secret=SECRET,
            )
            conn.commit()
            return receipt["status"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit_and_commit) for _ in range(2)]
        statuses = sorted(future.result() for future in futures)

    assert statuses == ["recorded", "replayed"]


def test_eligibility_guard_refuses_placeholder_v1_render_pins(live_postgres):
    org_id, project_id, result_id = _seed_result(live_postgres)
    with pytest.raises(
        Exception, match="feedback interaction was not delivered as eligible"
    ), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.feedback_annotations
                    (id, org_id, project_id, polarity, actor, actor_source,
                     observed_surface, interaction_ref, result_id, result_content_hash,
                     ai_path_absent_literal, visualization_spec_version_id,
                     renderer_build_id, runtime_build_id, theme_version, formatter_version,
                     target_schema_version, eligibility_schema_version, target_kind,
                     retry_key_hash, request_hash,
                     visible_versions_hash, observed_at)
                VALUES (%s, %s, %s, 'negative', 'person', 'user', 'mcp_app', %s,
                        %s, %s, 'No AI path', 'latest', 'latest', 'latest', 'latest',
                        'latest', 'exact-feedback.v1', 'feedback-eligibility.v1',
                        'answer', %s, %s, %s, NOW())
                """,
                (
                    f"fba_{secrets.token_hex(12)}",
                    org_id,
                    project_id,
                    f"afi_{secrets.token_hex(12)}",
                    result_id,
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                ),
            )


@pytest.mark.parametrize(
    ("pins", "target", "constraint", "record_eligibility"),
    [
        (("vsv_partial@1", None, None, None, None), ("answer", None, None),
         "feedback interaction was not delivered as eligible", False),
        ((None, None, None, None, None), ("datum", None, None),
         "ck_feedback_annotations_target", True),
    ],
)
def test_database_refuses_partial_pins_and_incomplete_targets(
    live_postgres, pins, target, constraint, record_eligibility
):
    org_id, project_id, result_id = _seed_result(live_postgres)
    interaction_ref = f"afi_{ULID()}"
    if record_eligibility:
        context = _context(live_postgres, org_id, project_id, result_id)
        interaction_ref = verify_feedback_context(
            context, secret=SECRET
        )["interaction_ref"]
    with pytest.raises(Exception, match=constraint), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.feedback_annotations
                    (id, org_id, project_id, polarity, actor, actor_source,
                     observed_surface, interaction_ref, result_id, result_content_hash,
                     ai_path_absent_literal, visualization_spec_version_id,
                     renderer_build_id, runtime_build_id, theme_version, formatter_version,
                     target_schema_version, eligibility_schema_version, target_kind,
                     datum_row_index, datum_field,
                     retry_key_hash, request_hash, visible_versions_hash, observed_at)
                VALUES (%s, %s, %s, 'negative', 'person', 'user', 'mcp_app', %s,
                        %s, %s, 'No AI path', %s, %s, %s, %s, %s,
                        'exact-feedback.v1', 'feedback-eligibility.v1',
                        %s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    f"fba_{ULID()}",
                    org_id,
                    project_id,
                    interaction_ref,
                    result_id,
                    "b" * 64,
                    *pins,
                    *target,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                ),
            )


def test_database_refuses_a_well_shaped_foreign_result_identity(live_postgres):
    org_id, project_id, result_id = _seed_result(live_postgres)
    with pytest.raises(
        Exception, match="feedback interaction was not delivered as eligible"
    ), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.feedback_annotations
                    (id, org_id, project_id, polarity, actor, actor_source,
                     observed_surface, interaction_ref, result_id, result_content_hash,
                     ai_path_absent_literal, target_schema_version,
                     eligibility_schema_version, target_kind,
                     retry_key_hash, request_hash, visible_versions_hash, observed_at)
                VALUES (%s, %s, %s, 'negative', 'person', 'user', 'console', %s,
                        %s, %s, 'No AI path', 'exact-feedback.v1',
                        'feedback-eligibility.v1', 'answer',
                        %s, %s, %s, NOW())
                """,
                (
                    f"fba_{ULID()}",
                    org_id,
                    project_id,
                    f"afi_{ULID()}",
                    result_id,
                    "f" * 64,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                ),
            )


def _decode(value: str) -> str:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _tool_error_text(result) -> str:
    return "\n".join(
        block.text
        for block in (result.content or [])
        if isinstance(getattr(block, "text", None), str)
    )


@pytest.mark.anyio
async def test_one_real_result_feedback_target_crosses_mcp_console_and_share_golden(
    live_postgres,
):
    from tests.fixture_generators.analyze_feedback import build_fixture

    fixture = await build_fixture(live_postgres.info.dsn)
    golden = (
        Path(__file__).resolve().parents[3]
        / "ui/cards/shell/src/viz/__tests__/fixtures/analyzeFeedbackTargets.json"
    )
    assert fixture == json.loads(golden.read_text(encoding="utf-8"))
    assert [
        fixture["surfaces"]["mcp"]["target"]["kind"],
        fixture["surfaces"]["console"]["interactions"]["visualization"]["target"]["kind"],
        fixture["surfaces"]["console"]["interactions"]["workbench"]["target"]["kind"],
        fixture["surfaces"]["share"]["target"]["kind"],
    ] == ["answer", "datum", "datum", "path_step"]
    assert len({json.dumps(item, sort_keys=True) for item in (
        fixture["pins"],
        {
            key: fixture["surfaces"]["mcp"]["authority"][key]
            for key in fixture["pins"]
        },
        {
            key: fixture["surfaces"]["console"]["authority"][key]
            for key in fixture["pins"]
        },
        {
            key: fixture["surfaces"]["share"]["authority"][key]
            for key in fixture["pins"]
        },
    )}) == 1
    render_inputs = [
        fixture["surfaces"]["mcp"]["delivery"]["_meta"]["toorow.app_payload"][
            "render_input"
        ],
        fixture["surfaces"]["console"]["delivery"]["render_input"],
        fixture["surfaces"]["share"]["delivery"]["render"],
    ]
    for delivered in render_inputs:
        assert delivered["result"]["result_id"] == fixture["result"]["result_id"]
        assert delivered["result"]["content_hash"] == fixture["result"]["content_hash"]
        assert delivered["result"]["row_count"] == fixture["result"]["row_count"]
        assert delivered["spec"]["visualization_spec_version_id"] == (
            fixture["pins"]["visualization_spec_version_id"]
        )
    interactions = fixture["surfaces"]["console"]["interactions"]
    assert interactions["visualization"]["context"]["interaction_ref"] != (
        interactions["workbench"]["context"]["interaction_ref"]
    )
