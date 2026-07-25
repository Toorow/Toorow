"""End-to-end flow test for the GitHub context connector -- Story 4.5 (AC6).

Tests the full flow:
  1. pull() writes context_events rows to Postgres.
  2. mirror_sync.sync_tables(['context_events']) picks them up.
  3. get_daily_report (via summarizer) shows the events in the summary.

Guarded by Postgres availability (PLATFORM_DB_URL or default dev credentials).
Skips automatically if Postgres is not reachable.

This test uses a real Postgres connection but mocks the GitHub API (respx).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
import respx

# Ensure server/ is on sys.path
_SERVER_DIR = Path(__file__).parent.parent.parent.parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


# ---------------------------------------------------------------------------
# Skip guard: only run if Postgres is reachable
# ---------------------------------------------------------------------------


def _postgres_available() -> bool:
    """Return True if the platform Postgres is reachable."""
    try:
        import psycopg  # noqa: PLC0415

        url = os.environ.get(
            "PLATFORM_DB_URL",
            "postgresql://connector:connector_dev_only@localhost:5432/connector",
        )
        conn = psycopg.connect(url)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres not reachable -- skipping E2E flow test (HG-GitHub)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FAKE_TOKEN = "ghp_e2e_test_token"  # noqa: S105 -- test fixture only
_E2E_PROJECT_ID = "e2e-test-github-4-5"
_E2E_PULL_ID = "pull_E2E4500000001"


def _cleanup_test_rows(project_id: str) -> None:
    """Delete stale test rows and ensure the test project row exists.

    fk_context_events_project (migration 018) requires a matching app.projects
    row; seed it here so the test is self-sufficient instead of depending on
    ambient DB state (the historical backfilled row is not guaranteed to exist).
    """
    import psycopg  # noqa: PLC0415

    url = os.environ.get(
        "PLATFORM_DB_URL",
        "postgresql://connector:connector_dev_only@localhost:5432/connector",
    )
    conn = psycopg.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.context_events WHERE project_id = %s",
            (project_id,),
        )
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by)
            VALUES (%s, 'E2E GitHub Test', %s, 'e2e-test')
            ON CONFLICT (id) DO NOTHING
            """,
            (project_id, project_id),
        )
    conn.commit()
    conn.close()


def _count_context_events(project_id: str, pull_id: str) -> int:
    """Count rows in app.context_events for the given project_id and pull_id."""
    import psycopg  # noqa: PLC0415

    url = os.environ.get(
        "PLATFORM_DB_URL",
        "postgresql://connector:connector_dev_only@localhost:5432/connector",
    )
    conn = psycopg.connect(url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.context_events WHERE project_id = %s AND created_by = %s",
            (project_id, f"github_pull:{pull_id}"),
        )
        row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _fetch_context_events_via_mirror(project_id: str, duckdb_path: str) -> list[dict]:
    """Read context_events from the DuckDB mirror."""
    import duckdb  # noqa: PLC0415

    try:
        conn = duckdb.connect(duckdb_path, read_only=True)
        try:
            rows = conn.execute(
                "SELECT * FROM mirror.context_events WHERE project_id = ?",
                [project_id],
            ).fetchall()
            if not rows:
                return []
            cols = [d[0] for d in conn.execute(
                "DESCRIBE mirror.context_events"
            ).fetchall()]
            return [dict(zip(cols, row)) for row in rows]
        except Exception:
            return []
        finally:
            conn.close()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


@respx.mock
def test_e2e_github_pull_to_daily_report(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Full flow: pull() -> Postgres -> mirror sync -> get_daily_report summary.

    Story 4.5 AC6: Events written by pull() appear in get_daily_report summaries.
    """
    from core import mirror_sync  # noqa: PLC0415
    from core.summarizer import build_daily_report_summary  # noqa: PLC0415

    import server.modules.github.connector as connector_mod  # noqa: PLC0415, E402

    # Step 0: cleanup any stale test rows
    _cleanup_test_rows(_E2E_PROJECT_ID)

    # Step 1: mock the GitHub API
    respx.get("https://api.github.com/repos/e2e-owner/e2e-repo/deployments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "ref": "main",
                    "environment": "production",
                    "created_at": "2026-07-04T12:00:00Z",
                },
            ],
        )
    )

    # Patch GitHub token fetch (no real Nango needed)
    monkeypatch.setattr(
        connector_mod,
        "_get_github_token",
        lambda connection_id: _FAKE_TOKEN,
    )

    # Step 2: call pull() -- writes to Postgres
    result = connector_mod.pull(
        connection_id="e2e-conn",
        date_from="2026-07-01",
        date_to="2026-07-10",
        project_id=_E2E_PROJECT_ID,
        pull_id=_E2E_PULL_ID,
        profile_id="deployments",
        connection_config={"owner": "e2e-owner", "repo": "e2e-repo"},
    )

    # Verify pull() result
    assert result["rows_written"] == 1, f"Expected 1 row written, got {result}"
    assert result["pull_id"] == _E2E_PULL_ID

    # Step 3: verify rows in Postgres
    pg_count = _count_context_events(_E2E_PROJECT_ID, _E2E_PULL_ID)
    assert pg_count == 1, (
        f"E2E: expected 1 context_events row in Postgres, got {pg_count}"
    )

    # Step 4: trigger mirror sync (Postgres -> DuckDB)
    duckdb_path = str(tmp_path / "e2e_mirror.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", duckdb_path)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")

    sync_result = mirror_sync.sync_tables(
        tables=["context_events"],
        target_backend="duckdb",
    )
    assert "error" not in sync_result, f"Mirror sync failed: {sync_result}"
    assert sync_result["synced"].get("context_events", 0) >= 1, (
        f"Mirror sync: expected >= 1 context_events row synced, got {sync_result}"
    )

    # Step 5: read events from mirror
    mirror_events = _fetch_context_events_via_mirror(_E2E_PROJECT_ID, duckdb_path)
    assert len(mirror_events) >= 1, (
        f"E2E: expected >= 1 event in DuckDB mirror, got {mirror_events}"
    )

    # Find the deployment event we wrote
    deployment_events = [e for e in mirror_events if e.get("type") == "deployment"]
    assert len(deployment_events) >= 1, (
        f"E2E: expected 'deployment' type in mirror events, got {mirror_events}"
    )

    # Step 6: build_daily_report_summary includes the event
    summary = build_daily_report_summary(
        rows=[],  # no KPI rows needed
        date_range={"start": "2026-07-01", "end": "2026-07-10"},
        connectors=["github"],
        project_id=_E2E_PROJECT_ID,
        context_events=mirror_events,
    )

    assert "deployment" in summary.lower(), (
        f"E2E: 'deployment' not found in daily report summary.\nSummary:\n{summary}"
    )
    assert "main" in summary or "production" in summary, (
        f"E2E: deployment label not found in summary.\nSummary:\n{summary}"
    )

    # Step 7: cleanup
    _cleanup_test_rows(_E2E_PROJECT_ID)
