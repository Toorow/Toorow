"""Story 65.3 -- real PostgreSQL/FastMCP proof for frozen Result windows."""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets

import psycopg
import pytest
from core import analyze_render_mcp as adapter
from core import query_specs_api
from core.analyze_feedback import verify_feedback_context
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport

pytestmark = pytest.mark.anyio

_HASH = "c" * 64
_SUFFIX = "S653" + secrets.token_hex(4).upper()
_IDENTITY = f"story-65-3-{_SUFFIX.lower()}@example.com"
_HANDLE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _seed_large_result(conn):
    rows = [{"ordinal": index, "value": f"row-{index:03d}"} for index in range(205)]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.view_id, v.project_id, p.org_id
            FROM app.semantic_view_versions v
            JOIN app.projects p ON p.id = v.project_id
            LIMIT 1
            """
        )
        found = cur.fetchone()
        if found is None:
            pytest.skip("no Semantic View version available for the Result fixture")
        version_id, view_id, project_id, org_id = found
        cur.execute(
            """
            INSERT INTO app.org_members
                (id, org_id, identity, role, status, joined_at)
            VALUES (%s, %s, %s, 'owner', 'active', NOW())
            ON CONFLICT (org_id, identity) DO UPDATE
            SET role = 'owner', status = 'active'
            """,
            (f"omem_{_SUFFIX}", org_id, _IDENTITY),
        )
        cur.execute(
            "INSERT INTO app.query_specs "
            "(id, org_id, project_id, semantic_view_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'test')",
            (f"qs_{_SUFFIX}", org_id, project_id, view_id),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
            """,
            (
                f"qsv_{_SUFFIX}",
                f"qs_{_SUFFIX}",
                org_id,
                project_id,
                view_id,
                version_id,
                "a" * 64,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, 'test')
            """,
            (
                f"qea_{_SUFFIX}",
                org_id,
                project_id,
                f"qsv_{_SUFFIX}",
                f"qr_{_SUFFIX}",
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, row_count, truncated,
                 started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, 'success', 'No AI path', %s, %s, FALSE,
                    NOW(), NOW())
            """,
            (
                f"qr_{_SUFFIX}",
                org_id,
                project_id,
                f"qea_{_SUFFIX}",
                f"qsv_{_SUFFIX}",
                _HASH,
                len(rows),
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash,
                 result_schema, manifest, rows_chunk)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
            """,
            (
                f"qr_{_SUFFIX}",
                org_id,
                project_id,
                _HASH,
                json.dumps({"fields": [{"name": "ordinal"}, {"name": "value"}]}),
                json.dumps({"grain": "row", "relation": "private.raw_table"}),
                json.dumps(rows),
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_execution_attempts
                (id, org_id, project_id, query_spec_version_id, result_id, requested_by)
            VALUES (%s, %s, %s, %s, %s, 'test')
            """,
            (
                f"qea_{_SUFFIX}_BAD",
                org_id,
                project_id,
                f"qsv_{_SUFFIX}",
                f"qr_{_SUFFIX}_BAD",
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_results
                (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                 ai_path_absent_literal, content_hash, row_count, truncated,
                 started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, 'success', 'No AI path', %s, 1, FALSE,
                    NOW(), NOW())
            """,
            (
                f"qr_{_SUFFIX}_BAD",
                org_id,
                project_id,
                f"qea_{_SUFFIX}_BAD",
                f"qsv_{_SUFFIX}",
                _HASH,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash,
                 result_schema, manifest, rows_chunk)
            VALUES (%s, %s, %s, %s, %s::jsonb, '{}'::jsonb, %s::jsonb)
            """,
            (
                f"qr_{_SUFFIX}_BAD",
                org_id,
                project_id,
                "e" * 64,
                json.dumps({"fields": [{"name": "ordinal"}, {"name": "value"}]}),
                json.dumps(rows[:1]),
            ),
        )
        visualization_id = f"viz_{_SUFFIX}"
        visualization_version_id = f"vsv_{_SUFFIX}"
        cur.execute(
            """
            INSERT INTO app.visualizations
                (id, org_id, project_id, query_spec_id, name, created_by)
            VALUES (%s, %s, %s, %s, 'Story 65.3 fixture', 'test')
            """,
            (visualization_id, org_id, project_id, f"qs_{_SUFFIX}"),
        )
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number,
                 query_spec_id, query_spec_version_id, spec_contract_version,
                 schema_version, family, spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, 'visualization-spec.v1', 1,
                    'table', %s::jsonb, %s, 'test')
            """,
            (
                visualization_version_id,
                visualization_id,
                org_id,
                project_id,
                f"qs_{_SUFFIX}",
                f"qsv_{_SUFFIX}",
                json.dumps(
                    {
                        "spec_contract_version": "visualization-spec.v1",
                        "schema_version": 1,
                        "family": "table",
                        "bindings": {
                            "dimension": ["ordinal"],
                            "measure": ["value"],
                        },
                        "accessibility": {"table_fallback": "required"},
                    }
                ),
                "d" * 64,
            ),
        )
    return str(org_id), str(project_id), visualization_version_id, rows, _IDENTITY


def _write_runtime(tmp_path):
    bundle = tmp_path / "mcp-app.html"
    bundle.write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "runtime_build": "runtime@story-65.3",
        "theme_version": "theme@story-65.3",
        "formatter_version": "formatters@story-65.3",
        "renderers": {
            "table": {
                "renderer_build": "table@story-65.3",
                "schema_versions": {"min": 1, "max": 1},
                "profiles": ["mcp-inline"],
            }
        },
    }
    bundle.with_name("runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return bundle


def _body(result):
    return result.structured_content or result.data


def _error_text(result) -> str:
    return "\n".join(
        block.text
        for block in (result.content or [])
        if isinstance(getattr(block, "text", None), str)
    )


async def test_real_app_tool_pages_two_frozen_windows_and_refuses_without_warehouse(
    live_postgres, monkeypatch, tmp_path
):
    org_id, project_id, visualization_version_id, rows, owner_identity = _seed_large_result(
        live_postgres
    )
    live_postgres.commit()
    bundle = _write_runtime(tmp_path)
    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", str(bundle))
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv(
        "TOOROW_FEEDBACK_CONTEXT_SECRET", "story-65-3-test-secret-at-least-32-bytes"
    )
    identity = {"value": owner_identity}
    test_dsn = live_postgres.info.dsn

    @contextlib.contextmanager
    def connection(_identity):
        # A distinct real transaction per tool/guard call. psycopg commits on
        # success and rolls back on error, exactly like production's request
        # connection; no commit-swallowing proxy can make atomicity vacuous.
        with psycopg.connect(test_dsn) as conn:
            yield conn

    def warehouse_tripwire(*args, **kwargs):
        raise AssertionError("a frozen Result slice reached the warehouse")

    from core import db, query_execution, render_app_payload, warehouse

    monkeypatch.setattr(adapter, "_identity", lambda: identity["value"])
    monkeypatch.setattr(query_specs_api, "analyze_connection", connection)
    monkeypatch.setattr(db, "request_connection", connection)
    monkeypatch.setattr(db, "get_warehouse_connection", warehouse_tripwire)
    monkeypatch.setattr(query_execution, "build_sql", warehouse_tripwire)
    monkeypatch.setattr(query_execution, "run_execution", warehouse_tripwire)
    monkeypatch.setattr(warehouse, "_query_duckdb", warehouse_tripwire)
    monkeypatch.setattr(warehouse, "_query_bigquery", warehouse_tripwire)

    expired_handle = "rh_" + "".join(secrets.choice(_HANDLE_ALPHABET) for _ in range(26))
    idle_handle = "rh_" + "".join(secrets.choice(_HANDLE_ALPHABET) for _ in range(26))
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, issued_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s,
                    ARRAY['ordinal', 'value'], NOW() - INTERVAL '2 hours',
                    NOW() - INTERVAL '1 hour')
            """,
            (expired_handle, org_id, project_id, f"qr_{_SUFFIX}", _HASH, owner_identity),
        )
        cur.execute(
            """
            INSERT INTO app.result_app_grants
                (handle_id, org_id, project_id, result_id, content_hash,
                 issued_to_identity, allowed_columns, issued_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s,
                    ARRAY['ordinal', 'value'], NOW() - INTERVAL '31 minutes',
                    NOW() + INTERVAL '7 hours')
            """,
            (idle_handle, org_id, project_id, f"qr_{_SUFFIX}", _HASH, owner_identity),
        )
    live_postgres.commit()

    target = FastMCP("story-65-3-result-slices")
    adapter.register(target)
    async with Client(FastMCPTransport(target)) as client:
        refused_render = await client.call_tool(
            "render_analyze_result",
            {
                "project_id": project_id,
                "result_id": f"qr_{_SUFFIX}",
                "visualization_spec_version_id": f"vsv_{_SUFFIX}_missing",
            },
            raise_on_error=False,
        )
        assert refused_render.is_error
        with live_postgres.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.result_app_grants WHERE result_id = %s",
                (f"qr_{_SUFFIX}",),
            )
            # The failed Render rolled its newly issued grant back.
            assert cur.fetchone()[0] == 2  # only the two explicit expired fixtures

        hash_mismatch = await client.call_tool(
            "render_analyze_result",
            {
                "project_id": project_id,
                "result_id": f"qr_{_SUFFIX}_BAD",
                "visualization_spec_version_id": visualization_version_id,
            },
            raise_on_error=False,
        )
        assert hash_mismatch.is_error
        assert "render_result_identity_mismatch" in _error_text(hash_mismatch)
        with live_postgres.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.result_app_grants WHERE result_id = %s",
                (f"qr_{_SUFFIX}_BAD",),
            )
            assert cur.fetchone()[0] == 0

        real_compose = render_app_payload.compose_render_app_payload
        original_budget = render_app_payload.MAX_RENDER_TOOL_META_BYTES
        final_meta_seen = {"value": False}

        def refuse_the_final_aggregate(*, render_input, moved=None, existing_meta=None):
            assert existing_meta is not None and "toorow.feedback" in existing_meta
            final_meta_seen["value"] = True
            candidate = {
                "schema_version": 1,
                "kind": "render",
                "render_input": render_input,
            }
            if moved is not None:
                candidate["moved"] = moved
            measured = render_app_payload.serialized_bytes(
                {**existing_meta, render_app_payload.APP_PAYLOAD_META_KEY: candidate}
            )
            monkeypatch.setattr(render_app_payload, "MAX_RENDER_TOOL_META_BYTES", measured - 1)
            return real_compose(
                render_input=render_input, moved=moved, existing_meta=existing_meta
            )

        monkeypatch.setattr(
            render_app_payload, "compose_render_app_payload", refuse_the_final_aggregate
        )
        budget_refusal = await client.call_tool(
            "render_analyze_result",
            {
                "project_id": project_id,
                "result_id": f"qr_{_SUFFIX}",
                "visualization_spec_version_id": visualization_version_id,
            },
            raise_on_error=False,
        )
        assert budget_refusal.is_error
        assert final_meta_seen["value"] is True
        assert "render_payload_over_budget" in _error_text(budget_refusal)
        with live_postgres.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.result_app_grants WHERE result_id = %s",
                (f"qr_{_SUFFIX}",),
            )
            assert cur.fetchone()[0] == 2
        monkeypatch.setattr(render_app_payload, "compose_render_app_payload", real_compose)
        monkeypatch.setattr(
            render_app_payload, "MAX_RENDER_TOOL_META_BYTES", original_budget
        )

        answer = await client.call_tool(
            "render_analyze_result",
            {
                "project_id": project_id,
                "result_id": f"qr_{_SUFFIX}",
                "visualization_spec_version_id": visualization_version_id,
            },
        )
        assert not answer.is_error, _error_text(answer)
        assert '"rows"' not in json.dumps(answer.structured_content)
        result_meta = answer.meta["toorow.result"]
        assert result_meta["projection_size"] == "large"
        assert "relation" not in json.dumps(result_meta)
        render_result = answer.meta["toorow.app_payload"]["render_input"]["result"]
        assert render_result["rows"] == result_meta["initial_projection"]
        assert render_result["row_count"] == len(rows)
        data_answer = await client.call_tool(
            "analyze_result", {"project_id": project_id, "result_id": f"qr_{_SUFFIX}"}
        )
        assert json.dumps(answer.structured_content, separators=(",", ":")) == json.dumps(
            data_answer.structured_content, separators=(",", ":")
        )
        handle = result_meta["result_handle"]
        initial_feedback = answer.meta["toorow.feedback"]

        legacy_result = await client.call_tool(
            "app_read_result_slice",
            {"project_id": project_id, "handle": handle, "offset": 0, "limit": 1},
        )
        assert _body(legacy_result)["rows"] == rows[:1]
        assert not legacy_result.meta or "toorow.feedback" not in legacy_result.meta

        damaged_feedback = {
            **initial_feedback,
            "token": initial_feedback["token"][:-1]
            + ("A" if initial_feedback["token"][-1] != "A" else "B"),
        }
        damaged_page = await client.call_tool(
            "app_read_result_slice",
            {
                "project_id": project_id,
                "handle": handle,
                "offset": 200,
                "limit": 5,
                "feedback_context": damaged_feedback,
            },
            raise_on_error=False,
        )
        assert damaged_page.is_error
        assert "not_found" in _error_text(damaged_page)

        first_result = await client.call_tool(
            "app_read_result_slice",
            {
                "project_id": project_id,
                "handle": handle,
                "offset": 0,
                "limit": 100,
                "feedback_context": initial_feedback,
            },
        )
        first = _body(first_result)
        first_feedback = first_result.meta["toorow.feedback"]
        second_result = await client.call_tool(
            "app_read_result_slice",
            {
                "project_id": project_id,
                "handle": handle,
                "cursor": first["next_cursor"],
                "limit": 100,
                "feedback_context": first_feedback,
            },
        )
        second = _body(second_result)
        second_feedback = second_result.meta["toorow.feedback"]
        third_result = await client.call_tool(
            "app_read_result_slice",
            {
                "project_id": project_id,
                "handle": handle,
                "cursor": second["next_cursor"],
                "limit": 100,
                "feedback_context": second_feedback,
            },
        )
        third = _body(third_result)
        third_feedback = third_result.meta["toorow.feedback"]
        assert first["rows"] == rows[:100]
        assert second["rows"] == rows[100:200]
        assert third["rows"] == rows[200:]
        assert first["total_rows"] == second["total_rows"] == len(rows)
        first_claims = verify_feedback_context(first_feedback)
        second_claims = verify_feedback_context(second_feedback)
        third_claims = verify_feedback_context(third_feedback)
        assert first_claims["delivered_rows"]["start"] == 0
        assert first_claims["delivered_rows"]["count"] == 100
        assert second_claims["delivered_rows"]["start"] == 100
        assert second_claims["delivered_rows"]["count"] == 100
        assert third_claims["delivered_rows"]["start"] == 200
        assert third_claims["delivered_rows"]["count"] == 5
        for field in (
            "result_id",
            "result_content_hash",
            "surface",
            "visualization_spec_version_id",
            "renderer_build_id",
            "runtime_build_id",
            "theme_version",
            "formatter_version",
        ):
            assert (
                first_claims.get(field)
                == second_claims.get(field)
                == third_claims.get(field)
                == verify_feedback_context(initial_feedback).get(field)
            )

        outside_initial = await client.call_tool(
            "submit_analyze_feedback",
            {
                "context": initial_feedback,
                "target": {"kind": "datum", "row_index": 204, "field": "value"},
                "polarity": "negative",
                "comment": None,
                "retry_key": f"slice-initial-refusal-{_SUFFIX}",
            },
            raise_on_error=False,
        )
        assert outside_initial.is_error

        recorded = await client.call_tool(
            "submit_analyze_feedback",
            {
                "context": third_feedback,
                "target": {"kind": "datum", "row_index": 204, "field": "value"},
                "polarity": "negative",
                "comment": "The paged value looks surprising.",
                "retry_key": f"slice-window-{_SUFFIX}",
            },
        )
        assert _body(recorded)["status"] == "recorded"
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                SELECT datum_row_index, datum_field
                FROM app.feedback_annotations
                WHERE result_id = %s AND actor = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (f"qr_{_SUFFIX}", owner_identity),
            )
            assert cur.fetchone() == (204, "value")
        over_limit = await client.call_tool(
            "app_read_result_slice",
            {"project_id": project_id, "handle": handle, "offset": 0, "limit": 501},
            raise_on_error=False,
        )
        assert over_limit.is_error
        assert "limit_over_bound" in _error_text(over_limit)

        with live_postgres.cursor() as cur:
            cur.execute(
                "SELECT handle_id, last_read_at FROM app.result_app_grants "
                "WHERE handle_id = ANY(%s)",
                ([handle, expired_handle, idle_handle],),
            )
            timestamps_before_refusals = dict(cur.fetchall())

        refusal_args = {"project_id": project_id, "offset": 0, "limit": 1}
        expired = await client.call_tool(
            "app_read_result_slice",
            {**refusal_args, "handle": expired_handle},
            raise_on_error=False,
        )
        idle = await client.call_tool(
            "app_read_result_slice",
            {**refusal_args, "handle": idle_handle},
            raise_on_error=False,
        )
        malformed = await client.call_tool(
            "app_read_result_slice",
            {**refusal_args, "handle": "not-a-handle"},
            raise_on_error=False,
        )
        missing = await client.call_tool(
            "app_read_result_slice",
            {**refusal_args, "handle": "rh_" + "3" * 26},
            raise_on_error=False,
        )
        cross_project = await client.call_tool(
            "app_read_result_slice",
            {
                "project_id": f"{project_id}_foreign",
                "handle": handle,
                "offset": 0,
                "limit": 1,
            },
            raise_on_error=False,
        )
        identity["value"] = "foreign@example.com"
        foreign_identity = await client.call_tool(
            "app_read_result_slice",
            {**refusal_args, "handle": handle},
            raise_on_error=False,
        )
        identity["value"] = owner_identity
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.result_app_grants SET revoked_at = NOW() WHERE handle_id = %s",
                (handle,),
            )
        live_postgres.commit()
        revoked = await client.call_tool(
            "app_read_result_slice",
            {**refusal_args, "handle": handle},
            raise_on_error=False,
        )
        refused = [
            expired,
            idle,
            malformed,
            missing,
            cross_project,
            foreign_identity,
            revoked,
        ]
        assert all(result.is_error for result in refused)
        assert len({_error_text(result) for result in refused}) == 1
        assert "not_found" in _error_text(refused[0])
        with live_postgres.cursor() as cur:
            cur.execute(
                "SELECT handle_id, last_read_at FROM app.result_app_grants "
                "WHERE handle_id = ANY(%s)",
                ([handle, expired_handle, idle_handle],),
            )
            assert dict(cur.fetchall()) == timestamps_before_refusals
