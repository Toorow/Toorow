"""Conformance / acceptance: the agent edits flows end-to-end (Story 8.7, AC6).

This is the ACCEPTANCE test for Story 8.7. It runs against a REAL Postgres
(guarded by TEST_POSTGRES_DSN, like the other live tests) and drives the four
MCP flow tools THROUGH the in-process FastMCP client (the CONTRIBUTING health-tool
pattern), proving the agent can operate the common flow interface:

  1. flows_upsert creates a datastream flow end-to-end (row + mappings),
  2. flows_upsert remaps a field (target_field change visible in datamodel),
  3. flows_upsert adjusts a report override (date_window + metric_definitions),
  4. flows_get returns the MERGED report doc (base pack + override),
  5. every mutation is audited (flow_updated rows present).

Skips when TEST_POSTGRES_DSN is unset. The DSN must point at a database where the
app schema migrations (incl. 023 datastreams + 025 report_overrides) are applied.

The in-process client carries no access token, so identity resolves to
"anonymous" -> always allowed by project_access (single-tenant compat). The AD-5
scope-rejection path is covered by the DB-less unit tests (test_flows.py).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set — live flows conformance skipped",
)


# ---------------------------------------------------------------------------
# Fixtures: seed a project + connection_ref, then clean up.
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded(monkeypatch):
    """Yield (project_id, connection_ref_id); seed rows and point core.db at the DSN."""
    dsn = os.environ["TEST_POSTGRES_DSN"]
    # core.db / core.audit read PLATFORM_DB_URL at call time.
    monkeypatch.setenv("PLATFORM_DB_URL", dsn)

    import psycopg

    project_id = f"proj_flow_{uuid.uuid4().hex[:8]}"
    conn_ref_id = f"conn_{uuid.uuid4().hex[:12]}"

    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, created_by, org_id)
                VALUES (%s, %s, %s, 'test', 'org_test_fixture') ON CONFLICT DO NOTHING
                """,
                (project_id, project_id, project_id),
            )
            cur.execute(
                """
                INSERT INTO app.connection_ref
                    (id, nango_connection_id, provider, project_id, status, owner_org_id,
                        owner_identity)
                VALUES (%s, %s, 'google-analytics', %s, 'active', 'org_test_fixture',
                    'tester@example.com')
                ON CONFLICT DO NOTHING
                """,
                (conn_ref_id, f"nango-flow-{conn_ref_id[-8:]}", project_id),
            )
        conn.commit()
        yield project_id, conn_ref_id
    finally:
        # Cleanup (order respects FKs: mappings cascade from datastreams).
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.report_overrides WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (conn_ref_id,))
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        conn.commit()
        conn.close()


def _structured(res):
    return res.structured_content or res.data


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_agent_edits_flows_end_to_end(seeded):
    project_id, conn_ref_id = seeded

    from core.main import mcp
    from fastmcp import Client

    async def scenario():
        async with Client(mcp) as c:
            # --- 1. Create a datastream flow (row + mappings) ---
            create_doc = {
                "schema_version": "1",
                "kind": "datastream",
                "project_id": project_id,
                "name": "GA Conformance",
                "module_name": "google-analytics",
                "connection_ref_id": conn_ref_id,
                "report_profile_id": "standard_daily",
                "enabled": True,
                "schedule_mode": "nightly",
                "refetch_days": 3,
                "date_window_days": 30,
                "mappings": [
                    {"source_field": "sessions", "target_field": "sessions"},
                    {"source_field": "activeUsers", "target_field": "active_users"},
                ],
            }
            res = await c.call_tool(
                "flows_upsert", {"project_id": project_id, "definition": create_doc}
            )
            data = _structured(res)["data"]
            assert data["changed"] is True
            assert data["kind"] == "datastream"
            ds_id = data["id"]

            # --- 1b. Idempotency: same doc again -> changed:false ---
            create_doc_with_id = {**create_doc, "id": ds_id}
            res = await c.call_tool(
                "flows_upsert",
                {"project_id": project_id, "definition": create_doc_with_id},
            )
            assert _structured(res)["data"]["changed"] is False

            # --- 2. Remap a field: activeUsers -> sessions (target change) ---
            remap_doc = {
                **create_doc_with_id,
                "mappings": [
                    {"source_field": "sessions", "target_field": "sessions"},
                    {"source_field": "activeUsers", "target_field": "sessions"},
                ],
            }
            res = await c.call_tool(
                "flows_upsert", {"project_id": project_id, "definition": remap_doc}
            )
            assert _structured(res)["data"]["changed"] is True

            # flows_get reflects the remap.
            res = await c.call_tool(
                "flows_get",
                {"project_id": project_id, "kind": "datastream", "id": ds_id},
            )
            got = _structured(res)["data"]["flow"]
            remapped = {m["source_field"]: m["target_field"] for m in got["mappings"]}
            assert remapped["activeUsers"] == "sessions"

            # --- 3. Adjust a report override (date_window + metric_definitions) ---
            report_doc = {
                "schema_version": "1",
                "kind": "report",
                "id": "gsc/position_movements",
                "base_report_id": "gsc/position_movements",
                "project_id": project_id,
                "date_window": {"default_days": 30},
                "metric_definitions": {
                    "clicks": {
                        "definition": "Clics organiques depuis la recherche.",
                        "unit": "count",
                        "direction": "up_good",
                    }
                },
                "llm_commentary_guidelines": "Reste factuel; cite les chiffres.",
            }
            res = await c.call_tool(
                "flows_upsert", {"project_id": project_id, "definition": report_doc}
            )
            assert _structured(res)["data"]["changed"] is True

            # --- 4. flows_get returns the MERGED report doc ---
            res = await c.call_tool(
                "flows_get",
                {
                    "project_id": project_id,
                    "kind": "report",
                    "id": "gsc/position_movements",
                },
            )
            merged = _structured(res)["data"]["flow"]
            # Override wins on date_window.default_days.
            assert merged["date_window"]["default_days"] == 30
            # R6 fields present from the override.
            assert merged["metric_definitions"]["clicks"]["direction"] == "up_good"
            assert "llm_commentary_guidelines" in merged
            # Base pack metrics still present (merge, not replace).
            assert "average_position" in merged.get("metrics", [])

            # --- 5. flows_list shows both flows ---
            res = await c.call_tool("flows_list", {"project_id": project_id})
            listed = _structured(res)["data"]["flows"]
            kinds = {it["kind"] for it in listed}
            assert {"datastream", "report"} <= kinds

            return ds_id

    _run(scenario())

    # --- Audit assertions: flow_updated rows written for the mutations ---
    import psycopg

    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM app.audit_log
                WHERE action = 'flow_updated'
                  AND metadata->>'project_id' = %s
                """,
                (project_id,),
            )
            flow_updated_count = cur.fetchone()[0]
    finally:
        conn.close()

    # 3 effective mutations audited (create datastream, remap, report override);
    # the idempotent re-apply wrote none.
    assert flow_updated_count >= 3


def test_smuggled_secret_rejected_via_mcp(seeded):
    """GUARDRAIL end-to-end: a doc with a token field is rejected by flows_validate."""
    project_id, _ = seeded

    from core.main import mcp
    from fastmcp import Client

    async def scenario():
        async with Client(mcp) as c:
            doc = {
                "schema_version": "1",
                "kind": "datastream",
                "project_id": project_id,
                "name": "Sneaky",
                "module_name": "google-analytics",
                "access_token": "ya29.SHOULD_NOT_BE_HERE",
            }
            res = await c.call_tool("flows_validate", {"definition": doc})
            return _structured(res)["data"]

    data = _run(scenario())
    assert data["ok"] is False
    assert any(
        "non autorise" in e["message"] or "secret" in e["message"].lower() for e in data["errors"]
    )
