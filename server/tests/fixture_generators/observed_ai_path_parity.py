"""Real PostgreSQL producer for the one-path, three-surface parity fixture."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from ulid import ULID


def canonical_bytes(value: object) -> bytes:
    """Canonical bytes used by the parity assertion and fixture consumer."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write_runtime(runtime_dir: Path) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    bundle = runtime_dir / "mcp-app.html"
    bundle.write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    bundle.with_name("runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                "runtime_build": "runtime@story-65.4",
                "theme_version": "theme@story-65.4",
                "formatter_version": "formatters@story-65.4",
                "renderers": {
                    "table": {
                        "renderer_build": "table@story-65.4",
                        "schema_versions": {"min": 1, "max": 1},
                        "profiles": ["mcp-inline"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return bundle


def _normalized_projection(value: dict, *, result_id: str) -> dict:
    normalized = json.loads(canonical_bytes(value))
    normalized["path_id"] = "aip_FIXTURE"
    for step in normalized["steps"]:
        step["observed_at"] = "2026-01-01T00:00:00Z"
        if step.get("evidence_record_id") == result_id:
            step["evidence_record_id"] = "qr_FIXTURE"
    return normalized


async def build_fixture(dsn: str, runtime_dir: Path) -> dict:
    """Execute once, then traverse the real MCP, Workbench and Share readers."""
    from core import analyze_render_mcp as adapter
    from core import query_specs_api, render_shares
    from core.analyze_workbench import compose_result_lens

    from tests.integration.test_render_shares_postgres import Chain, _uid

    with psycopg.connect(dsn) as seed_conn:
        chain = Chain(seed_conn).build()
        identity = f"story-65-4-{ULID()}@example.com"
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

    runtime = _write_runtime(runtime_dir)
    with (
        patch.object(adapter, "_identity", return_value=identity),
        patch.object(query_specs_api, "analyze_connection", connection),
        patch.dict(
            os.environ,
            {
                "TOOROW_AUTH_MODE": "oauth",
                "TOOROW_FEEDBACK_CONTEXT_SECRET": "story-65-4-feedback-secret-32-bytes",
                "TOOROW_VISUALIZATION_RUNTIME_DIST": str(runtime),
            },
        ),
    ):
        target = FastMCP("story-65-4-three-surfaces")
        adapter.register(target)
        async with Client(FastMCPTransport(target)) as client:
            produced = await client.call_tool(
                "execute_analyze_query_spec",
                {
                    "project_id": chain.project_id,
                    "query_spec_version_id": chain.query_spec_version_id,
                },
            )
            result_id = produced.structured_content["result"]["result_id"]
            analyzed = await client.call_tool(
                "analyze_result",
                {"project_id": chain.project_id, "result_id": result_id},
            )
            rendered = await client.call_tool(
                "render_analyze_result",
                {
                    "project_id": chain.project_id,
                    "result_id": result_id,
                    "visualization_spec_version_id": chain.spec_version_id,
                },
            )

    analyzed_path = analyzed.meta["toorow.result"]["ai_path_walk"]
    mcp_path = rendered.meta["toorow.result"]["ai_path_walk"]
    if canonical_bytes(analyzed_path) != canonical_bytes(mcp_path):
        raise AssertionError("Analyze and render MCP readers projected different AI paths")

    with psycopg.connect(dsn) as conn:
        workbench_path = compose_result_lens(
            conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            result_id=result_id,
            lens="ai-path",
        )["ai_path"]
        with conn.cursor() as cur:
            render_id = _uid("rnd")
            cur.execute(
                "INSERT INTO app.renders "
                "(id, org_id, project_id, result_id, result_content_hash, "
                "visualization_spec_version_id, renderer_adapter, renderer_build_id, "
                "runtime_build_id, theme_version, formatter_version, responsive_profile, "
                "display_state, evidence_manifest, creation_surface, origin_kind, "
                "content_hash, created_by) "
                "SELECT %s, org_id, project_id, %s, %s, visualization_spec_version_id, "
                "renderer_adapter, renderer_build_id, runtime_build_id, theme_version, "
                "formatter_version, responsive_profile, display_state, evidence_manifest, "
                "creation_surface, origin_kind, content_hash, created_by "
                "FROM app.renders WHERE id = %s",
                (
                    render_id,
                    result_id,
                    produced.structured_content["result"]["content_hash"],
                    chain.render_id,
                ),
            )
        requested = render_shares.create_share(
            conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            render_id=render_id,
            actor=identity,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            idempotency_key=f"story-65-4-{ULID()}",
        )
        # Migration 323: a requested Share carries no bearer at all. The link this
        # parity check follows exists only once a SECOND role holder authorizes
        # the exit, so the generator plays both halves rather than reaching for a
        # `delivery_url` the request no longer has.
        created = render_shares.confirm_share(
            conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            share_id=requested.share_id,
            actor="second.holder@example.com",
            idempotency_key=f"story-65-4-confirm-{requested.share_id}",
        )
        conn.commit()
        exchanged = render_shares.exchange_bearer(
            conn,
            bearer=created.delivery_url.split("#render=")[1],
            ip_hash=None,
            client_class="browser",
        )
        session = render_shares.resolve_session(
            conn, session_value=exchanged.session_value
        )
        share_path = render_shares.load_frozen_render(conn, session)["ai_path_evidence"]

    projected = (mcp_path, workbench_path, share_path)
    if len({canonical_bytes(value) for value in projected}) != 1:
        raise AssertionError("MCP, Workbench and Share projected different AI paths")
    if mcp_path["path_id"] != produced.structured_content["ai_path"]:
        raise AssertionError("Tool summary and projected path identities differ")

    return {
        "schema_version": "observed-ai-path-parity.v1",
        "result_id": "qr_FIXTURE",
        "mcp": _normalized_projection(mcp_path, result_id=result_id),
        "workbench": _normalized_projection(workbench_path, result_id=result_id),
        "share": _normalized_projection(share_path, result_id=result_id),
    }
