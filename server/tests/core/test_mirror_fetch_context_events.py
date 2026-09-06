"""Tests for `fetch_context_events`: the mirror path, the record path, and `unavailable`.

Covers:
  - the DuckDB mirror path (Story 4.4): populated -> events; project and window
    filters; MMM columns; legacy 5-column mirror; a withdrawn annotation leaves it.
  - AI-344 (2026-09-01, context-hub.md amendment): a deployment that keeps no
    mirror reads the RECORD (`app.context_events`) in the same shape, and a read
    that can serve from neither store raises `ContextEventsUnavailable` -- it
    never answers `[]`, which from now on means "read, and the window is empty".

The record half is pg-gated on `live_postgres` (disposable Postgres, migrations
applied): what it proves is the SQL and the shape, which a mocked cursor could
only echo back.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest


def test_fetch_from_mirror(tmp_path, monkeypatch):
    """mirror.context_events populated -> _fetch_context_events returns events."""
    import duckdb

    db_path = str(tmp_path / "mirror_test.duckdb")

    # Pre-populate mirror.context_events in DuckDB
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR,
            project_id VARCHAR,
            event_date DATE,
            type VARCHAR,
            label VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_001", "proj_test", date(2026, 7, 4), "business", "Lancement"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    assert result[0]["id"] == "evt_001"
    assert result[0]["project_id"] == "proj_test"
    assert result[0]["type"] == "business"
    assert result[0]["label"] == "Lancement"
    assert "event_date" in result[0]


def _mirror_file_without_the_table(tmp_path) -> str:
    """A DuckDB file `mirror_sync` never wrote into: not a store of events."""
    import duckdb

    db_path = str(tmp_path / "empty_mirror.duckdb")
    duckdb.connect(db_path).close()
    return db_path


def test_a_mirror_file_without_the_table_is_not_a_store(tmp_path, monkeypatch):
    """The table was never synced: the read falls through to the record, and with
    no caller and no connection to reach it, it says so instead of `[]`."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", _mirror_file_without_the_table(tmp_path))

    from core.context_events import ContextEventsUnavailable
    from core.main import _fetch_context_events

    with pytest.raises(ContextEventsUnavailable) as excinfo:
        _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")
    assert excinfo.value.reason
    assert excinfo.value.repair


def test_no_mirror_and_no_caller_says_unavailable_not_empty(monkeypatch):
    """TOOROW_DUCKDB_PATH unset and nobody to open a scoped connection for.

    This is the exact call shape that answered `[]` on the deployment (AI-344).
    """
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)

    from core.context_events import ContextEventsUnavailable
    from core.main import _fetch_context_events

    with pytest.raises(ContextEventsUnavailable) as excinfo:
        _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")
    payload = excinfo.value.payload
    assert set(payload) == {"reason", "repair"}
    assert "not read" in payload["reason"]


def test_no_mirror_and_no_record_says_unavailable_with_the_repair(monkeypatch):
    """Case (a) of AI-344: no DuckDB, a caller, and Postgres unreachable.

    The DSN points at a closed local port with a one-second connect timeout, so
    the failure is a refused connection, not a wait. The answer names the gesture
    (repair the deployment's database connection, or point at a synced mirror),
    and it is an exception -- a caller cannot mistake it for a quiet window.
    """
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.setenv(
        "PLATFORM_DB_URL", "postgresql://u:p@127.0.0.1:9/nowhere?connect_timeout=1"
    )

    from core.context_events import ContextEventsUnavailable
    from core.main import _fetch_context_events

    with pytest.raises(ContextEventsUnavailable) as excinfo:
        _fetch_context_events("proj_test", "2026-07-01", "2026-07-31", identity="tester")
    assert "could not be reached" in excinfo.value.reason
    assert "PLATFORM_DB_URL" in excinfo.value.repair
    assert "TOOROW_DUCKDB_PATH" in excinfo.value.repair


def test_fetch_filters_by_project_id(tmp_path, monkeypatch):
    """Only events for the requested project_id are returned."""
    import duckdb

    db_path = str(tmp_path / "multi_proj.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_A", "proj_A", date(2026, 7, 1), "business", "Event A"],
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_B", "proj_B", date(2026, 7, 2), "incident", "Event B"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_A", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    assert result[0]["id"] == "evt_A"


def test_fetch_filters_by_date_range(tmp_path, monkeypatch):
    """Events outside the date range are excluded."""
    import duckdb

    db_path = str(tmp_path / "date_filter.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR
        )
        """
    )
    for i, d in enumerate(["2026-06-30", "2026-07-01", "2026-07-15", "2026-08-01"]):
        conn.execute(
            "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
            [f"evt_{i}", "proj_test", date.fromisoformat(d), "business", f"Event {i}"],
        )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    dates = [r["event_date"] for r in result]
    # Only July 1 and July 15 should appear, not June 30 or Aug 1
    assert "2026-06-30" not in dates
    assert "2026-08-01" not in dates
    assert "2026-07-01" in dates
    assert "2026-07-15" in dates


# ---------------------------------------------------------------------------
# Epic 31.2 -- the read path exposes platform/value/source (overlay + MMM)
# ---------------------------------------------------------------------------


def test_fetch_exposes_mmm_columns_when_present(tmp_path, monkeypatch):
    """migration-055 columns (platform/value/source) reach the analytical read path.

    Epic 31.2: the single read path feeds BOTH the annotation overlay AND the MMM
    feature-builder, so platform (identity level 1), value (regressor magnitude) and
    source (provenance) must be selected when the mirror has them.
    """
    import duckdb

    db_path = str(tmp_path / "mmm_cols.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR,
            label VARCHAR, platform VARCHAR, value DOUBLE, source VARCHAR
        )
        """
    )
    # A connector event with a magnitude (weighted regressor)...
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["evt_p", "proj_test", date(2026, 7, 4), "promotion", "Promo -20",
         "shopify", -20.0, "shopify"],
    )
    # ...and a unit pulse (value NULL) from a manual/legacy event.
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["evt_u", "proj_test", date(2026, 7, 5), "video_upload", "New video",
         "youtube", None, "youtube-analytics"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")
    by_id = {r["id"]: r for r in result}

    assert by_id["evt_p"]["platform"] == "shopify"
    assert by_id["evt_p"]["value"] == -20.0  # NUMERIC -> float
    assert by_id["evt_p"]["source"] == "shopify"
    # NULL value stays None -> the default unit pulse (AD-9: no black box).
    assert by_id["evt_u"]["value"] is None
    assert by_id["evt_u"]["platform"] == "youtube"
    assert by_id["evt_u"]["source"] == "youtube-analytics"


def test_fetch_backward_compatible_with_legacy_5col_mirror(tmp_path, monkeypatch):
    """A pre-055 mirror (no platform/value/source) still returns the base columns.

    Defensive column selection: the read path intersects its wanted columns with the
    table's actual columns, so an un-migrated mirror does not raise on absent columns.
    """
    import duckdb

    db_path = str(tmp_path / "legacy_5col.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_legacy", "proj_test", date(2026, 7, 4), "business", "Legacy"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    row = result[0]
    assert row["id"] == "evt_legacy"
    assert row["label"] == "Legacy"
    # MMM columns simply absent (not present in the legacy mirror) -- no crash.
    assert "platform" not in row
    assert "value" not in row
    assert "source" not in row


# ---------------------------------------------------------------------------
# A WITHDRAWN ANNOTATION LEAVES BOTH PROACTIVE WALKS -- pinned, not assumed.
#
# Migration 286 filters `retired_at IS NULL` in TWO places, and neither covers
# the other: `fetch_context_events` feeds the briefing, the cards, the reports,
# `get_events` and the scheduler, while `anomaly_alerts` runs its own SQL against
# the same mirror to list candidate CAUSES. A withdrawal honoured on one of the
# two would be a withdrawal the product still cites out loud.
#
# Verified for migration 328 (2026-08-31): the new lifecycle adds no state that
# escapes those clauses -- a corrected annotation is LIVE (its earlier wordings
# live in `app.context_event_revisions`, which no proactive walk reads), and a
# withdrawn one is frozen whole. This file pins the exclusion so the next reader
# of `mirror.context_events` cannot quietly reintroduce it.
# ---------------------------------------------------------------------------


def _mirror_with_a_withdrawal(tmp_path):
    """A mirror carrying one live annotation and one withdrawn, on the same day."""
    import duckdb

    db_path = str(tmp_path / "withdrawn_mirror.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR,
            label VARCHAR, platform VARCHAR, value DOUBLE, source VARCHAR,
            metric VARCHAR, retired_at TIMESTAMP, retired_by VARCHAR,
            retired_reason VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES "
        "('evt_live', 'proj_test', ?, 'business', 'Price change', NULL, NULL, "
        " 'manual', NULL, NULL, NULL, NULL)",
        [date(2026, 7, 4)],
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES "
        "('evt_withdrawn', 'proj_test', ?, 'business', 'Wrong day', NULL, NULL, "
        " 'manual', NULL, TIMESTAMP '2026-07-05 09:00:00', 'owner@example.com', "
        " 'the date was wrong')",
        [date(2026, 7, 4)],
    )
    conn.close()
    return db_path


def test_a_withdrawn_annotation_leaves_the_briefing_walk(tmp_path, monkeypatch):
    """`fetch_context_events` serves what the project OBSERVES, not what it recorded."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", _mirror_with_a_withdrawal(tmp_path))

    from core.context_events import fetch_context_events

    live = fetch_context_events("proj_test", "2026-07-01", "2026-07-31")
    assert [event["label"] for event in live] == ["Price change"]

    # And it is not DROPPED from a list that claims to be complete: asked for, it
    # arrives carrying its withdrawal.
    everything = fetch_context_events(
        "proj_test", "2026-07-01", "2026-07-31", include_retired=True
    )
    assert sorted(event["label"] for event in everything) == ["Price change", "Wrong day"]
    withdrawn = next(e for e in everything if e["label"] == "Wrong day")
    assert withdrawn["retired_by"] == "owner@example.com"
    assert withdrawn["retired_reason"] == "the date was wrong"


def test_a_withdrawn_annotation_is_never_named_as_a_candidate_cause(tmp_path):
    """The second walk, which names a cause OUT LOUD in an anomaly alert."""
    import duckdb
    from core.anomaly_alerts import _fetch_context_events_for_anomaly

    conn = duckdb.connect(_mirror_with_a_withdrawal(tmp_path), read_only=True)
    try:
        labels, pairing = _fetch_context_events_for_anomaly(
            "proj_test", date(2026, 7, 4), conn
        )
    finally:
        conn.close()

    assert labels == ["Price change"]
    assert pairing["claim_date"] == "2026-07-04"


# ---------------------------------------------------------------------------
# AI-344 -- THE RECORD PATH. A deployment that keeps no mirror (Cloud Run,
# TOOROW_DB_MODE=bigquery, no TOOROW_DUCKDB_PATH) reads `app.context_events`
# itself, in the mirror's shape, with the mirror's retired semantics. Pinned on a
# live Postgres because the property under test is the WHERE clause and the row
# conversion, which a mocked cursor would only echo back.
# ---------------------------------------------------------------------------

_RECORD_ROWS = (
    # (id suffix, event_date, type, label, platform, value, source)
    ("promo", date(2026, 7, 4), "promotion", "Promo -20", "shopify", -20.0, "manual"),
    ("pulse", date(2026, 7, 5), "video_upload", "New video", "youtube", None, "manual"),
    ("late", date(2026, 8, 2), "business", "Out of window", None, None, "manual"),
    # June, outside every July window above: the deployment-marker read's row.
    ("deploy", date(2026, 6, 15), "deployment", "Deploy v1", None, None, "manual"),
)


@pytest.fixture()
def record(live_postgres):
    """One project's rows in `app.context_events`, COMMITTED so a second, scoped
    connection can read them; one of them withdrawn through the product's own
    gesture; one row in another project; one outside the window."""
    from core.context_events import retire_manual_event

    from tests.conftest import purge_fixture_org

    conn = live_postgres
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_ai344_{suffix}"
    project_id = f"proj_ai344_{suffix}"
    other_project_id = f"proj_ai344b_{suffix}"
    ids: dict[str, str] = {}

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', 'owner@example.com')",
            (org_id, "AI-344 record read", org_id.lower()),
        )
        for one in (project_id, other_project_id):
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
                "VALUES (%s, %s, %s, %s, 'active', 'owner@example.com')",
                (one, org_id, "AI-344 record read", one.lower()),
            )

    def _insert(pid: str, key: str, day, kind, label, platform, value, source) -> str:
        event_id = f"evt_ai344_{key}_{suffix}"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.context_events
                    (id, project_id, event_date, type, label, description,
                     created_by, platform, value, source)
                VALUES (%s, %s, %s, %s, %s, '', 'owner@example.com', %s, %s, %s)
                """,
                (event_id, pid, day, kind, label, platform, value, source),
            )
        return event_id

    for key, day, kind, label, platform, value, source in _RECORD_ROWS:
        ids[key] = _insert(project_id, key, day, kind, label, platform, value, source)
    ids["withdrawn"] = _insert(
        project_id, "withdrawn", date(2026, 7, 4), "business", "Wrong day", None, None, "manual"
    )
    ids["elsewhere"] = _insert(
        other_project_id, "elsewhere", date(2026, 7, 4), "business", "Elsewhere", None, None,
        "manual",
    )
    conn.commit()
    retire_manual_event(
        project_id=project_id,
        event_id=ids["withdrawn"],
        retired_by="owner@example.com",
        reason="the date was wrong",
        conn=conn,
    )

    yield conn, project_id, ids

    conn.rollback()
    # The events first, by hand, then the org tree by the product's own purge
    # (the fixture committed, so a rollback reaches none of it).
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.context_events WHERE project_id = ANY(%s)",
            ([project_id, other_project_id],),
        )
    purge_fixture_org(conn, org_id)
    conn.commit()


def _shape(rows: list[dict]) -> list[tuple]:
    return [
        (r["id"], r["event_date"], r["type"], r["label"], r["platform"], r["value"], r["source"])
        for r in rows
    ]


def test_no_mirror_reads_the_record_in_the_mirrors_shape(record, monkeypatch):
    """Case (b) of AI-344: no DuckDB, rows in Postgres -> the rows, same shape."""
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    conn, project_id, ids = record

    from core.context_events import fetch_context_events

    rows = fetch_context_events(project_id, "2026-07-01", "2026-07-31", conn=conn)

    assert [r["id"] for r in rows] == [ids["promo"], ids["pulse"]]  # ordered by day
    assert set(rows[0]) == {
        "id", "project_id", "event_date", "type", "label", "platform", "value", "source",
        "metric",
    }
    promo, pulse = rows
    assert promo["event_date"] == "2026-07-04" and isinstance(promo["event_date"], str)
    assert promo["value"] == -20.0 and isinstance(promo["value"], float)  # NUMERIC -> float
    assert pulse["value"] is None  # NULL stays None (unit pulse, AD-9)
    assert promo["platform"] == "shopify" and promo["source"] == "manual"
    assert all(r["project_id"] == project_id for r in rows)  # never the neighbour's
    # Nothing was written: the connection is still clean to roll back.
    assert conn.info.transaction_status.name in {"IDLE", "INTRANS"}


def test_the_record_path_honours_a_withdrawal_exactly_like_the_mirror(record, monkeypatch):
    """A withdrawn annotation leaves the default read, and arrives carrying its
    withdrawal when the caller asks for a complete list (migration 286)."""
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    conn, project_id, ids = record

    from core.context_events import fetch_context_events

    live = fetch_context_events(project_id, "2026-07-01", "2026-07-31", conn=conn)
    assert ids["withdrawn"] not in {r["id"] for r in live}
    assert all("retired_at" not in r for r in live)

    everything = fetch_context_events(
        project_id, "2026-07-01", "2026-07-31", conn=conn, include_retired=True
    )
    withdrawn = next(r for r in everything if r["id"] == ids["withdrawn"])
    assert withdrawn["retired_by"] == "owner@example.com"
    assert withdrawn["retired_reason"] == "the date was wrong"
    assert isinstance(withdrawn["retired_at"], str)


def test_the_record_path_serves_through_the_callers_scoped_connection(record, monkeypatch):
    """No `conn` handed in: the read opens `request_connection(identity)` itself --
    the same seam every MCP read of this product uses since 67-1."""
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    _conn, project_id, ids = record

    from core.context_events import fetch_context_events

    rows = fetch_context_events(project_id, "2026-07-01", "2026-07-31", identity="tester")
    assert [r["id"] for r in rows] == [ids["promo"], ids["pulse"]]


def test_a_mirror_file_without_the_table_falls_through_to_the_record(
    record, tmp_path, monkeypatch
):
    """A DuckDB file `mirror_sync` never wrote into is not a store: the record answers."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", _mirror_file_without_the_table(tmp_path))
    conn, project_id, ids = record

    from core.context_events import fetch_context_events

    rows = fetch_context_events(project_id, "2026-07-01", "2026-07-31", conn=conn)
    assert [r["id"] for r in rows] == [ids["promo"], ids["pulse"]]


def test_both_stores_answer_the_same_rows_the_same_way(record, tmp_path, monkeypatch):
    """Case (c) beside (b): the SAME rows in a synced mirror and in the record
    produce identical dicts, so a caller cannot tell which store served it."""
    import duckdb

    conn, project_id, ids = record

    from core.context_events import fetch_context_events

    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    from_record = fetch_context_events(project_id, "2026-07-01", "2026-07-31", conn=conn)

    db_path = str(tmp_path / "parity.duckdb")
    mirror = duckdb.connect(db_path)
    mirror.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    mirror.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR,
            label VARCHAR, platform VARCHAR, value DOUBLE, source VARCHAR,
            metric VARCHAR, retired_at TIMESTAMP, retired_by VARCHAR,
            retired_reason VARCHAR
        )
        """
    )
    for key, day, kind, label, platform, value, source in _RECORD_ROWS:
        mirror.execute(
            "INSERT INTO mirror.context_events VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
            [ids[key], project_id, day, kind, label, platform, value, source],
        )
    mirror.close()
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    from_mirror = fetch_context_events(project_id, "2026-07-01", "2026-07-31", conn=conn)

    assert from_mirror == from_record
    assert len(from_record) == 2


# ---------------------------------------------------------------------------
# AI-344 -- THE OTHER TWO READERS OF THE CLASS, on the record. The candidate-cause
# walk of the anomaly evaluator and the deployment-marker read of the reports both
# read the mirror alone; on a live Postgres they now answer from the record, with
# the mirror's retired semantics and discriminants.
# ---------------------------------------------------------------------------


def test_the_candidate_cause_walk_reads_the_record_when_there_is_no_mirror(record):
    from core import anomaly_alerts
    from core.briefing import CONTEXT_BASIS_PLATFORM

    conn, project_id, ids = record

    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        project_id, date(2026, 7, 4), None, connector="shopify", record_connection=lambda: conn
    )
    # The withdrawn "Wrong day" of the same day is not a candidate cause.
    assert labels == ["Promo -20"]
    assert CONTEXT_BASIS_PLATFORM in pairing["basis"]
    assert "unavailable" not in pairing

    # An event that DECLARES another platform is out.
    labels, _ = anomaly_alerts._fetch_context_events_for_anomaly(
        project_id, date(2026, 7, 4), None, connector="youtube", record_connection=lambda: conn
    )
    assert labels == []


def test_the_deployment_markers_are_read_from_the_record(record, monkeypatch):
    from core import reports

    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    _conn, project_id, ids = record

    events = reports._fetch_deployment_events(
        project_id, "2026-06-01", "2026-06-30", identity="tester"
    )
    assert events == [
        {
            "id": ids["deploy"],
            "project_id": project_id,
            "event_date": "2026-06-15",
            "type": "deployment",
            "label": "Deploy v1",
        }
    ]
