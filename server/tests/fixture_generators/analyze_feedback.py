"""Real-PG producer for one exact feedback target through every Analytics door."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import psycopg
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from starlette.applications import Starlette
from starlette.testclient import TestClient
from ulid import ULID

SCHEMA_VERSION = "analyze-feedback-targets.v1"
SECRET = "story-65-5-golden-feedback-secret-32-bytes"


def _write_runtime(directory: Path) -> Path:
    from tests.integration.test_render_shares_postgres import (
        FORMATTER_VERSION,
        RENDERER_BUILD,
        RUNTIME_BUILD,
        THEME_VERSION,
    )

    bundle = directory / "mcp-app.html"
    bundle.write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    bundle.with_name("runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                "runtime_build": RUNTIME_BUILD,
                "theme_version": THEME_VERSION,
                "formatter_version": FORMATTER_VERSION,
                "renderers": {
                    "table": {
                        "renderer_build": RENDERER_BUILD,
                        "schema_versions": {"min": 1, "max": 1},
                        "profiles": ["mcp-inline"],
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return bundle


def _context_fixture(sidecar: dict, label: str) -> dict:
    if set(sidecar) != {"schema_version", "token", "interaction_ref", "expires_at"}:
        raise AssertionError(f"{label} emitted a non-canonical feedback context")
    return {
        "schema_version": sidecar["schema_version"],
        "token": f"SIGNED_CONTEXT_{label.upper()}",
        "interaction_ref": f"afi_{label.upper()}_FIXTURE",
        "expires_at": "2026-01-01T00:15:00Z",
    }


_TIMESTAMP_KEYS = {
    "created_at",
    "started_at",
    "ended_at",
    "observed_at",
    "shared_on",
    "expires_at",
}


def _normalize_delivery(value: Any, replacements: dict[str, str], label: str) -> Any:
    """Normalize volatile identities only after the real delivery was validated."""
    if isinstance(value, dict):
        if set(value) == {"schema_version", "token", "interaction_ref", "expires_at"}:
            return _context_fixture(value, label)
        normalized = {
            key: _normalize_delivery(item, replacements, label) for key, item in value.items()
        }
        for key in normalized:
            if isinstance(normalized.get(key), str) and (
                key in _TIMESTAMP_KEYS or key.endswith("_at")
            ):
                normalized[key] = "2026-01-01T00:00:00Z"
        for fact in normalized.get("facts") or []:
            if isinstance(fact, dict) and fact.get("label") == "Shared on":
                fact["value"] = "FIXTURE_DATE"
        return normalized
    if isinstance(value, list):
        return [_normalize_delivery(item, replacements, label) for item in value]
    if isinstance(value, str):
        normalized = value
        for actual, stable in replacements.items():
            normalized = normalized.replace(actual, stable)
        return normalized
    return value


def _receipt_fixture(
    receipt: dict,
    label: str,
    target: dict,
    *,
    expected_interaction_ref: str,
) -> dict:
    if (
        receipt.get("status") != "recorded"
        or receipt.get("target") != target
        or receipt.get("interaction_ref") != expected_interaction_ref
    ):
        raise AssertionError(f"{label} did not acknowledge the exact selected target")
    return {
        "schema_version": receipt["schema_version"],
        "status": receipt["status"],
        "feedback_id": f"feedback_{label.upper()}_FIXTURE",
        "interaction_ref": f"afi_{label.upper()}_FIXTURE",
        "target": target,
    }


def _authority_fixture(claims: dict, label: str, *, owns_render: bool) -> dict:
    expected_render = "rnd_FIXTURE" if owns_render else None
    if claims.get("render_id") != expected_render and expected_render is None:
        # The comparison to the real id happens before normalization in build_fixture.
        if claims.get("render_id") is not None:
            raise AssertionError(f"{label} unexpectedly claims a Render")
    authority = {
        "surface": claims["surface"],
        "interaction_ref": f"afi_{label.upper()}_FIXTURE",
        "result_id": "qr_FIXTURE",
        "result_content_hash": "sha256_RESULT_FIXTURE",
        "ai_path_id": "aip_FIXTURE",
        "path_step_ordinals": claims["path_step_ordinals"],
        "delivered_rows": claims["delivered_rows"],
    }
    if claims.get("visualization_spec_version_id") is not None:
        authority.update(
            {
                "visualization_spec_version_id": "vsv_FIXTURE",
                "renderer_build_id": claims["renderer_build_id"],
                "runtime_build_id": claims["runtime_build_id"],
                "theme_version": claims["theme_version"],
                "formatter_version": claims["formatter_version"],
            }
        )
    if owns_render:
        authority["render_id"] = "rnd_FIXTURE"
    return authority


def _stored_fixture(
    row: tuple,
    label: str,
    *,
    expected_result_id: str,
    expected_result_hash: str,
) -> dict:
    (
        surface,
        target_kind,
        row_index,
        field,
        ordinal,
        polarity,
        render_id,
        spec_id,
        renderer,
        runtime,
        theme,
        formatter,
        path_id,
        actor,
        result_id,
        result_hash,
    ) = row
    if (result_id, result_hash) != (expected_result_id, expected_result_hash):
        raise AssertionError(f"{label} changed the governed Result identity")
    return {
        "store": "render_share_feedback" if label == "share" else "feedback_annotations",
        "surface": surface,
        "target": {
            "kind": target_kind,
            **({"row_index": row_index, "field": field} if target_kind == "datum" else {}),
            **({"ordinal": ordinal} if target_kind == "path_step" else {}),
        },
        "polarity": polarity,
        "actor": "actor_FIXTURE" if actor is not None else None,
        "result_id": "qr_FIXTURE",
        "result_content_hash": "sha256_RESULT_FIXTURE",
        "render_id": "rnd_FIXTURE" if render_id is not None else None,
        "visualization_spec_version_id": "vsv_FIXTURE" if spec_id is not None else None,
        "renderer_build_id": renderer,
        "runtime_build_id": runtime,
        "theme_version": theme,
        "formatter_version": formatter,
        "ai_path_id": "aip_FIXTURE" if path_id is not None else None,
    }


def _assert_stored_authority(row: tuple, claims: dict, label: str) -> None:
    expected = (
        claims.get("render_id"),
        claims.get("visualization_spec_version_id"),
        claims.get("renderer_build_id"),
        claims.get("runtime_build_id"),
        claims.get("theme_version"),
        claims.get("formatter_version"),
        claims.get("ai_path_id"),
    )
    if tuple(row[6:13]) != expected:
        raise AssertionError(f"{label} did not persist its signed authority pins")


def _prepare_classified_chain(conn, chain) -> None:
    """Publish governed Domain/capability owners before the Result is frozen."""
    domain_id = f"bd_{ULID()}"
    config_id = f"pcv_{ULID()}"
    view_version_id = f"svv_{ULID()}"
    query_spec_version_id = f"qsv_{ULID()}"
    visualization_spec_version_id = f"vsv_{ULID()}"
    hashes = [hashlib.sha256(f"feedback-classification-{index}".encode()).hexdigest()
              for index in range(4)]
    domain_slug = f"feedback-fixture-{str(domain_id)[-8:].lower()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.mdm_business_domains "
            "(id, org_id, slug, name, created_by) VALUES (%s, %s, %s, %s, 'fixture')",
            (domain_id, chain.org_id, domain_slug, "Feedback fixture domain"),
        )
        cur.execute(
            """
            INSERT INTO app.mdm_business_domain_versions
                (domain_id, version_number, org_id, slug, name, status, created_by,
                 created_at, updated_at, change_kind, changed_by)
            VALUES (%s, 1, %s, %s, 'Feedback fixture domain', 'active', 'fixture',
                    NOW(), NOW(), 'created', 'fixture')
            """,
            (domain_id, chain.org_id, domain_slug),
        )
        cur.execute(
            """
            INSERT INTO app.project_configuration_versions
                (id, project_id, version_number, posture, dependency_fingerprint,
                 content_hash, activated_by)
            VALUES (%s, %s, 1, '{}'::jsonb, %s, %s, 'fixture')
            """,
            (config_id, chain.project_id, hashes[0], hashes[1]),
        )
        cur.execute(
            "UPDATE app.project_capabilities SET state = 'ready', active_version_id = %s "
            "WHERE project_id = %s AND capability_key = 'country'",
            (config_id, chain.project_id),
        )
        cur.execute(
            "UPDATE app.projects SET active_configuration_version_id = %s WHERE id = %s",
            (config_id, chain.project_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, business_domain_refs, created_by)
            VALUES (%s, %s, %s, 2, 'published', 'fixture_view', 'Fixture classified',
                    %s, %s, %s::jsonb, 'fixture')
            """,
            (
                view_version_id,
                chain.view_id,
                chain.project_id,
                hashes[2],
                hashes[2],
                json.dumps([domain_id]),
            ),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_compiled_artifacts
                (id, project_id, view_version_id, compiler_version, content_hash,
                 queryability_matrix, ossie_projection, ossie_spec_version,
                 toorow_extension_version)
            VALUES (%s, %s, %s, 'test', %s, '{}'::jsonb, '{}'::jsonb, '1', '1')
            """,
            (f"sca_{ULID()}", chain.project_id, view_version_id, hashes[2]),
        )
        cur.execute(
            """
            INSERT INTO app.query_spec_versions
                (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                 semantic_view_version_id, spec, content_hash, predecessor_version_id,
                 created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s::jsonb, %s, %s, 'fixture')
            """,
            (
                query_spec_version_id,
                chain.query_spec_id,
                chain.org_id,
                chain.project_id,
                chain.view_id,
                view_version_id,
                json.dumps(
                    {"required_capability": {"key": "country"}, "result_type": "table"}
                ),
                hashes[3],
                chain.query_spec_version_id,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family,
                 spec, content_hash, predecessor_version_id, created_by)
            SELECT %s, visualization_id, org_id, project_id, 2, query_spec_id, %s,
                   spec_contract_version, schema_version, family, spec, %s, %s, 'fixture'
              FROM app.visualization_spec_versions WHERE id = %s
            """,
            (
                visualization_spec_version_id,
                query_spec_version_id,
                hashes[3],
                chain.spec_version_id,
                chain.spec_version_id,
            ),
        )
    chain.view_version_id = view_version_id
    chain.query_spec_version_id = query_spec_version_id
    chain.spec_version_id = visualization_spec_version_id


async def build_fixture(dsn: str) -> dict:
    """Execute once, then submit answer/datum/path targets through real adapters."""
    from core import (
        admin_api,
        analyze_workbench_api,
        db,
        feedback_review_api,
        query_execution,
        query_specs_api,
        render_shares,
        render_shares_api,
        warehouse,
    )
    from core import (
        analyze_render_mcp as adapter,
    )
    from core.analyze_feedback import SIDECAR_META_KEY, verify_feedback_context

    from tests.integration.test_render_shares_postgres import (
        RESULT_ROWS,
        Chain,
        _uid,
    )

    identity = f"story-65-5-golden-{ULID()}@example.com"
    with psycopg.connect(dsn) as seed_conn:
        chain = Chain(seed_conn).build()
        _prepare_classified_chain(seed_conn, chain)
        with seed_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.org_members "
                "(id, org_id, identity, role, status, joined_at) "
                "VALUES (%s, %s, %s, 'owner', 'active', NOW())",
                (_uid("omem"), chain.org_id, identity),
            )
        seed_conn.commit()

    @contextlib.contextmanager
    def connection(_identity):
        with psycopg.connect(dsn) as conn:
            yield conn

    physical_plan = {
        "relation": "fixture_rows",
        "columns": {"day": "day", "sessions": "sessions"},
    }
    evidence = {
        "provenance": {
            "values": [
                {"member_id": "day", "source_field": "day"},
                {"member_id": "sessions", "source_field": "sessions"},
            ]
        },
        "freshness": {"output_created_at": "2026-01-01T00:00:00Z"},
        "dq_evaluation_ids": [],
        "dq_unavailable_reason": None,
    }
    runtime_dir = Path(__file__).resolve().parents[3] / ".codex-tmp/analyze-feedback-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.nullcontext(runtime_dir) as temp_dir:
        runtime = _write_runtime(Path(temp_dir))
        environment = {
            "PLATFORM_DB_URL": dsn,
            "TOOROW_AUTH_MODE": "oauth",
            "TOOROW_FEEDBACK_CONTEXT_SECRET": SECRET,
            "TOOROW_RENDER_SHARE_PEPPER": "story-65-5-golden-share-pepper-32-bytes",
            "TOOROW_RENDER_SHARE_ORIGIN": "https://share.example.com",
            "TOOROW_VISUALIZATION_RUNTIME_DIST": str(runtime),
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(adapter, "_identity", return_value=identity),
            patch.object(query_specs_api, "analyze_connection", connection),
            patch.object(query_execution, "resolve_physical_plan", return_value=physical_plan),
            patch.object(query_execution, "capture_evidence", return_value=evidence),
            patch.object(warehouse, "_db_mode", return_value="duckdb"),
            patch.object(warehouse, "_query_duckdb", return_value=RESULT_ROWS),
        ):
            mcp = FastMCP("story-65-5-feedback-golden")
            adapter.register(mcp)
            async with Client(FastMCPTransport(mcp)) as client:
                produced = await client.call_tool(
                    "execute_analyze_query_spec",
                    {
                        "project_id": chain.project_id,
                        "query_spec_version_id": chain.query_spec_version_id,
                    },
                )
                result = produced.structured_content["result"]
                result_id = result["result_id"]
                path_id = produced.structured_content["ai_path"]
                with psycopg.connect(dsn) as result_conn:
                    result_truth = result_conn.execute(
                        "SELECT outcome, row_count, content_hash FROM app.query_results "
                        "WHERE id = %s AND project_id = %s",
                        (result_id, chain.project_id),
                    ).fetchone()
                if result_truth != ("success", 3, result["content_hash"]):
                    raise AssertionError(
                        f"FastMCP execute did not create the expected real Result: {result_truth!r}"
                    )
                rendered = await client.call_tool(
                    "render_analyze_result",
                    {
                        "project_id": chain.project_id,
                        "result_id": result_id,
                        "visualization_spec_version_id": chain.spec_version_id,
                    },
                )
                mcp_delivery = {
                    "content": [block.model_dump(exclude_none=True) for block in rendered.content],
                    "structuredContent": rendered.structured_content,
                    "_meta": rendered.meta,
                    "isError": rendered.is_error,
                }
                mcp_context = rendered.meta[SIDECAR_META_KEY]
                mcp_target = {"kind": "answer"}
                mcp_call = await client.call_tool(
                    "submit_analyze_feedback",
                    {
                        "context": mcp_context,
                        "target": mcp_target,
                        "polarity": "negative",
                        "comment": "The answer needs clarification.",
                        "retry_key": "golden-mcp-answer",
                    },
                )
                mcp_receipt = mcp_call.structured_content or mcp_call.data

            with psycopg.connect(dsn) as owner_conn:
                with owner_conn.cursor() as cur:
                    render_id = _uid("rnd")
                    cur.execute(
                        """
                        INSERT INTO app.renders
                            (id, org_id, project_id, result_id, result_content_hash,
                             visualization_spec_version_id, renderer_adapter,
                             renderer_build_id, runtime_build_id, theme_version,
                             formatter_version, responsive_profile, display_state,
                             evidence_manifest, creation_surface, origin_kind,
                             content_hash, created_by)
                        SELECT %s, org_id, project_id, %s, %s,
                               %s, renderer_adapter,
                               renderer_build_id, runtime_build_id, theme_version,
                               formatter_version, responsive_profile, display_state,
                               evidence_manifest, creation_surface, origin_kind,
                               content_hash, created_by
                          FROM app.renders WHERE id = %s
                        """,
                        (
                            render_id,
                            result_id,
                            result["content_hash"],
                            chain.spec_version_id,
                            chain.render_id,
                        ),
                    )
                requested_share = render_shares.create_share(
                    owner_conn,
                    org_id=chain.org_id,
                    project_id=chain.project_id,
                    render_id=render_id,
                    actor=identity,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                    idempotency_key=f"golden-share-{ULID()}",
                )
                # Migration 323: a request carries no bearer. The golden path
                # this generator follows starts at a LIVE link, so it plays both
                # halves of the ceremony rather than reaching for a
                # `delivery_url` the request no longer has.
                created_share = render_shares.confirm_share(
                    owner_conn,
                    org_id=chain.org_id,
                    project_id=chain.project_id,
                    share_id=requested_share.share_id,
                    actor="second.holder@example.com",
                    idempotency_key=f"golden-confirm-{requested_share.share_id}",
                )
                owner_conn.commit()

            console_app = Starlette(
                routes=[
                    *query_specs_api.query_spec_routes,
                    *analyze_workbench_api.ROUTES,
                    *feedback_review_api.feedback_review_routes,
                ]
            )
            authorization = AsyncMock(return_value=(identity, chain.org_id))
            auth_check = AsyncMock(return_value=(True, identity))
            with (
                patch.object(query_specs_api, "_authorize", authorization),
                patch.object(analyze_workbench_api, "_authorize", authorization),
                patch.object(admin_api, "_check_auth", auth_check),
                patch.object(admin_api, "_require_datastream_role", return_value=None),
                patch.object(db, "request_connection", connection),
                patch.object(analyze_workbench_api, "analyze_connection", connection),
                TestClient(console_app) as console,
            ):
                evidence_response = console.get(
                    f"/api/projects/{chain.project_id}/analyze/results/"
                    f"{result_id}/evidence?render_id={render_id}"
                )
                if evidence_response.status_code != 200:
                    raise AssertionError(evidence_response.text)
                console_evidence = evidence_response.json()
                console_context = console_evidence["feedback_context"]
                lens_response = console.get(
                    f"/api/projects/{chain.project_id}/analyze/results/{result_id}/lens/view"
                )
                if lens_response.status_code != 200:
                    raise AssertionError(lens_response.text)
                console_view_lens = lens_response.json()
                console_target = {"kind": "datum", "row_index": 1, "field": "sessions"}
                console_response = console.post(
                    f"/api/projects/{chain.project_id}/test/feedback",
                    json={
                        "context": console_context,
                        "target": console_target,
                        "polarity": "positive",
                        "comment": "This exact value is useful.",
                        "retry_key": "golden-console-datum",
                    },
                )
                if console_response.status_code != 201:
                    raise AssertionError(console_response.text)
                console_receipt = console_response.json()
                console_workbench_context = console_view_lens["feedback_context"]
                console_workbench_response = console.post(
                    f"/api/projects/{chain.project_id}/test/feedback",
                    json={
                        "context": console_workbench_context,
                        "target": console_target,
                        "polarity": "positive",
                        "comment": "This workbench value is useful.",
                        "retry_key": "golden-console-workbench-datum",
                    },
                )
                if console_workbench_response.status_code != 201:
                    raise AssertionError(console_workbench_response.text)
                console_workbench_receipt = console_workbench_response.json()

            share = TestClient(
                Starlette(routes=render_shares_api.render_share_routes),
                base_url="https://testserver",
                raise_server_exceptions=False,
            )
            exchange = share.post(
                "/api/render-shares/exchange",
                json={"bearer": created_share.delivery_url.split("#render=")[1]},
            )
            if exchange.status_code != 200:
                raise AssertionError(exchange.text)
            share_delivery = share.get("/api/render-shares/session/render")
            if share_delivery.status_code != 200:
                raise AssertionError(share_delivery.text)
            share_envelope = share_delivery.json()
            share_context = share_envelope["feedback_context"]
            share_target = {"kind": "path_step", "ordinal": 0}
            share_response = share.post(
                "/api/render-shares/session/feedback",
                json={
                    "context": share_context,
                    "target": share_target,
                    "polarity": "negative",
                    "comment": "This observed step needs review.",
                    "retry_key": "golden-share-path",
                },
            )
            if share_response.status_code != 200:
                raise AssertionError(share_response.text)
            share_receipt = share_response.json()

            contexts = {
                "mcp": (mcp_context, mcp_target, mcp_receipt),
                "console": (console_context, console_target, console_receipt),
                "share": (share_context, share_target, share_receipt),
            }
            claims = {
                label: verify_feedback_context(sidecar)
                for label, (sidecar, _target, _receipt) in contexts.items()
            }
            console_workbench_claims = verify_feedback_context(console_workbench_context)
            pin_names = (
                "visualization_spec_version_id",
                "renderer_build_id",
                "runtime_build_id",
                "theme_version",
                "formatter_version",
            )
            expected_pins = tuple(claims["mcp"][name] for name in pin_names)
            for label, item in claims.items():
                sidecar = contexts[label][0]
                if (
                    item["result_id"] != result_id
                    or item["result_content_hash"] != result["content_hash"]
                    or item["interaction_ref"] != sidecar["interaction_ref"]
                ):
                    raise AssertionError(f"{label} changed Result meaning")
                if item.get("ai_path_id") != path_id or item.get("path_step_ordinals") != [0]:
                    raise AssertionError(f"{label} changed finalized AI Path meaning")
                if tuple(item[name] for name in pin_names) != expected_pins:
                    raise AssertionError(f"{label} changed Render/Spec build pins")
            if claims["mcp"].get("render_id") is not None:
                raise AssertionError("inline MCP feedback invented a retained Render")
            if claims["console"].get("render_id") != render_id or claims["share"].get(
                "render_id"
            ) != render_id:
                raise AssertionError("retained surfaces did not pin the same Render")

            mcp_render_input = mcp_delivery["_meta"]["toorow.app_payload"]["render_input"]
            share_render_input = share_envelope["render"]
            for label, delivered_input in (
                ("mcp", mcp_render_input),
                ("share", share_render_input),
            ):
                delivered_result = delivered_input["result"]
                delivered_pins = delivered_input["pins"]
                if (
                    delivered_result["result_id"] != result_id
                    or delivered_result["content_hash"] != result["content_hash"]
                    or delivered_result["rows"] != RESULT_ROWS
                    or delivered_input["spec"]["visualization_spec_version_id"]
                    != chain.spec_version_id
                    or (
                        delivered_input["spec"]["visualization_spec_version_id"],
                        delivered_pins["renderer_build"],
                        delivered_pins["runtime_build"],
                        delivered_pins["theme_version"],
                        delivered_pins["formatter_version"],
                    )
                    != expected_pins
                ):
                    raise AssertionError(f"{label} delivery changed Result rows or render pins")
            if (
                console_evidence["result_id"] != result_id
                or console_evidence["content_hash"] != result["content_hash"]
                or console_evidence["rows"] != RESULT_ROWS
                or console_view_lens["result_id"] != result_id
                or console_view_lens["content_hash"] != result["content_hash"]
            ):
                raise AssertionError("Console deliveries changed Result meaning")
            if (
                console_workbench_claims["result_id"] != result_id
                or console_workbench_claims["result_content_hash"] != result["content_hash"]
                or console_workbench_claims["surface"] != "console"
            ):
                raise AssertionError("Console view lens context changed Result meaning")
            locator = next(
                (
                    cell
                    for value in console_view_lens["view"]["values"]
                    if value["row_index"] == 1
                    for cell in value["cells"]
                    if cell["field"] == "sessions"
                ),
                None,
            )
            if locator is None or locator["value"] != RESULT_ROWS[1]["sessions"]:
                raise AssertionError("Console view lens lost the selected row 1/sessions locator")
            if (
                share_envelope.get("ai_path_evidence", {}).get("path_id") != path_id
                or share_context != contexts["share"][0]
            ):
                raise AssertionError("Share delivery changed AI Path or feedback context")

            with psycopg.connect(dsn) as inspect_conn:
                with inspect_conn.cursor() as cur:
                    authenticated_rows = []
                    for label, receipt, stored_claims in (
                        ("mcp", mcp_receipt, claims["mcp"]),
                        ("console", console_receipt, claims["console"]),
                        (
                            "console_workbench",
                            console_workbench_receipt,
                            console_workbench_claims,
                        ),
                    ):
                        cur.execute(
                            """
                            SELECT observed_surface, target_kind, datum_row_index,
                                   datum_field, path_step_ordinal, polarity, render_ref,
                                   visualization_spec_version_id, renderer_build_id,
                                   runtime_build_id, theme_version, formatter_version,
                                   ai_path_id, actor, result_id, result_content_hash
                              FROM app.feedback_annotations WHERE id = %s
                            """,
                            (receipt["feedback_id"],),
                        )
                        row = cur.fetchone()
                        if row is None:
                            raise AssertionError(f"{label} receipt has no stored annotation")
                        _assert_stored_authority(row, stored_claims, label)
                        authenticated_rows.append(
                            _stored_fixture(
                                row,
                                label,
                                expected_result_id=result_id,
                                expected_result_hash=result["content_hash"],
                            )
                        )
                    cur.execute(
                        """
                        SELECT 'share', target_kind, datum_row_index, datum_field,
                               path_step_ordinal, polarity, render_id,
                               visualization_spec_version_id, renderer_build,
                               runtime_build, theme_version, formatter_version,
                               ai_path_id, NULL, result_id, result_content_hash
                          FROM app.render_share_feedback WHERE id = %s
                        """,
                        (share_receipt["feedback_id"],),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise AssertionError("Share receipt has no stored feedback row")
                    _assert_stored_authority(row, claims["share"], "share")
                    stored = [
                        *authenticated_rows,
                        _stored_fixture(
                            row,
                            "share",
                            expected_result_id=result_id,
                            expected_result_hash=result["content_hash"],
                        ),
                    ]
            if [item["target"] for item in stored] != [
                mcp_target,
                console_target,
                console_target,
                share_target,
            ]:
                raise AssertionError("stored feedback changed an acknowledged exact target")

    replacements = {
        result_id: "qr_FIXTURE",
        result["content_hash"]: "sha256_RESULT_FIXTURE",
        path_id: "aip_FIXTURE",
        render_id: "rnd_FIXTURE",
        chain.spec_version_id: "vsv_FIXTURE",
        chain.project_id: "proj_FIXTURE",
        chain.org_id: "org_FIXTURE",
        chain.query_spec_id: "qs_FIXTURE",
        chain.query_spec_version_id: "qsv_FIXTURE",
        chain.view_id: "sv_FIXTURE",
        chain.view_version_id: "svv_FIXTURE",
        identity: "actor_FIXTURE",
    }
    result_handle = mcp_delivery["_meta"]["toorow.result"].get("result_handle")
    if isinstance(result_handle, str):
        replacements[result_handle] = "rh_FIXTURE"
    deliveries = {
        "mcp": _normalize_delivery(mcp_delivery, replacements, "mcp"),
        "console": {
            "render_input": _normalize_delivery(mcp_render_input, replacements, "console"),
            "evidence": _normalize_delivery(console_evidence, replacements, "console"),
            "view_lens": _normalize_delivery(
                console_view_lens, replacements, "console_workbench"
            ),
        },
        "share": _normalize_delivery(share_envelope, replacements, "share"),
    }
    surfaces = {}
    for label, (sidecar, target, receipt) in contexts.items():
        surfaces[label] = {
            "context": _context_fixture(sidecar, label),
            "authority": _authority_fixture(
                claims[label], label, owns_render=label in {"console", "share"}
            ),
            "target": target,
            "delivery": deliveries[label],
            "receipt": _receipt_fixture(
                receipt,
                label,
                target,
                expected_interaction_ref=sidecar["interaction_ref"],
            ),
        }
    visualization_interaction = {
        key: surfaces["console"][key]
        for key in ("context", "authority", "target", "receipt")
    }
    workbench_interaction = {
        "context": _context_fixture(console_workbench_context, "console_workbench"),
        "authority": _authority_fixture(
            console_workbench_claims, "console_workbench", owns_render=False
        ),
        "target": console_target,
        "receipt": _receipt_fixture(
            console_workbench_receipt,
            "console_workbench",
            console_target,
            expected_interaction_ref=console_workbench_context["interaction_ref"],
        ),
    }
    surfaces["console"] = {
        "authority": visualization_interaction["authority"],
        "delivery": deliveries["console"],
        "interactions": {
            "visualization": visualization_interaction,
            "workbench": workbench_interaction,
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "result": {
            "result_id": "qr_FIXTURE",
            "content_hash": "sha256_RESULT_FIXTURE",
            "outcome": "success",
            "row_count": 3,
            "ai_path_id": "aip_FIXTURE",
        },
        "pins": {
            "visualization_spec_version_id": "vsv_FIXTURE",
            **dict(zip(pin_names[1:], expected_pins[1:], strict=True)),
        },
        "surfaces": surfaces,
        "stored": stored,
    }
