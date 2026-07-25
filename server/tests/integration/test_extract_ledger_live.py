"""Live-Postgres regression for the extract ledger overlap query (review-epic-8 CRITICAL).

The mock-based unit tests in tests/core/test_extract_ledger.py all used single-day
windows (date_from == date_to), which trivially satisfied the buggy containment
condition and hid two defects: (1) the WHERE clause required the pull to FULLY
CONTAIN the window instead of OVERLAP it, and (2) the params list carried two
extra window values, so the connection_ref_id branch passed 8 params for 6
placeholders and the query ALWAYS raised (fail-soft -> every day 'never_fetched').

This test exercises the REAL SQL against live Postgres with a MULTI-DAY window and
a narrow 3-day pull that only overlaps it.
"""

from __future__ import annotations

import os
import uuid

import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("PLATFORM_DB_URL")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="TEST_POSTGRES_DSN/PLATFORM_DB_URL not set -- live ledger test skipped"
)


def test_multi_day_window_with_connection_ref_returns_real_status():
    import psycopg
    from core.extract_ledger import get_extract_ledger

    project_id = f"proj_ledger_{uuid.uuid4().hex[:8]}"
    conn_ref_id = f"conn_{uuid.uuid4().hex[:12]}"
    ds_id = f"ds_{uuid.uuid4().hex[:12]}"
    pull_id = f"pull_{uuid.uuid4().hex[:12]}"
    job_id = f"job_{uuid.uuid4().hex[:12]}"

    conn = psycopg.connect(_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by) "
                "VALUES (%s,%s,%s,'test') ON CONFLICT DO NOTHING",
                (project_id, project_id, project_id),
            )
            cur.execute(
                "INSERT INTO app.connection_ref "
                "(id, nango_connection_id, provider, project_id, status) "
                "VALUES (%s,%s,'google-analytics',%s,'active')",
                (conn_ref_id, f"nango-{conn_ref_id[-8:]}", project_id),
            )
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, name, module_name, connection_ref_id, "
                " report_profile_id, enabled) "
                "VALUES (%s,%s,%s,'google-analytics',%s,'standard_daily',TRUE)",
                (ds_id, project_id, f"stream-{ds_id[-6:]}", conn_ref_id),
            )
            # A narrow 3-day pull that OVERLAPS (does not contain) the 30-day window.
            cur.execute(
                "INSERT INTO app.pull_jobs "
                "(id, pull_id, datastream_id, connection_ref_id, date_from, date_to, "
                " state, requested_by) "
                "VALUES (%s,%s,%s,%s,'2026-06-15','2026-06-17','done','test')",
                (job_id, pull_id, ds_id, conn_ref_id),
            )
        conn.commit()

        ledger = get_extract_ledger(ds_id, "2026-06-01", "2026-06-30", conn)

        by_date = {d["date"]: d["status"] for d in ledger}
        # The 3 covered days must NOT be 'never_fetched' (the bug returned that).
        for day in ("2026-06-15", "2026-06-16", "2026-06-17"):
            assert by_date.get(day) in ("ok", "partial", "empty"), (
                f"{day} should reflect the overlapping pull, got {by_date.get(day)}"
            )
        # A day outside the pull stays never_fetched.
        assert by_date.get("2026-06-01") == "never_fetched"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.pull_jobs WHERE id = %s", (job_id,))
            cur.execute("DELETE FROM app.datastreams WHERE id = %s", (ds_id,))
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (conn_ref_id,))
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        conn.commit()
        conn.close()
