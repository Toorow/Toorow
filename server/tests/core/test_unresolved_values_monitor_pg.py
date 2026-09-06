"""AI-326: the ninth monitor announces a NEW unresolved value, once.

Target: `docs/product-architecture/unresolved-values.md`, section *Alerting*, and
the four "Incomplete if" clauses it is judged against:

  * *the monitor fires on values it already reported*;
  * *the first arming mails the whole backlog as new arrivals*;
  * *a firing opens one issue per value instead of one per
    `(datastream, dimension, reason)`*;
  * *an alert names no repair address*.

WHAT ONLY A DATABASE CAN PROVE HERE, and it is the whole reason this file is
pg-gated rather than a mock:

1. **The delta memory is a store or it is nothing.** The rule is "a value that was
   not in the previous evaluation's unresolved set", and a mocked cursor agrees
   with any set at all. The three sweeps below run against the real
   `app.unresolved_value_sightings` and its real primary key.
2. **Two findings of one Datastream on one night are two alerts.** Migration 229
   dedups a DQ firing on `(project_id, type, datastream_id, window_date)` with
   `ON CONFLICT DO NOTHING`, so before migration 326's `finding_key` the second
   group of a night was lost SILENTLY. Only the real partial unique index can
   show that it no longer is.
3. **The first arming is a write, not a branch.** `baseline_values` and
   `announced = FALSE` are what say the 516 backlog values were swallowed
   deliberately rather than lost.

THE READING IS INJECTED, and that is deliberate. `read_datastream_unresolved` is
proved by its own suite against the warehouse; what is proved HERE is the memory,
the arming and the firing. Injecting the answer is what lets a test replay "the
same night twice" and "one new value" exactly, which no warehouse fixture can.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import purge_fixture_project

TEST_ORG_ID = "org_test_fixture"
WINDOW = {"start": "2026-08-25", "end": "2026-08-31", "days": 7}
DIMENSION = "video_id"

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the delta memory needs a live Postgres",
)

pytestmark = [_skip_without_dsn]


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _value(source_value: str, occurrences: int, reason: str = "unmapped") -> dict:
    return {
        "source_value": source_value,
        "connector": "ga4",
        "occurrences": occurrences,
        "reason": reason,
    }


def _reading(values: list[dict], *, window_rows: int = 1000, dimension: str = DIMENSION) -> dict:
    """What `read_datastream_unresolved` answers, in its exact published shape."""
    return {
        "state": "measured",
        "window": dict(WINDOW),
        "connector": "ga4",
        "groups": [
            {
                "dimension": dimension,
                "canonical_dimension": dimension,
                "state": "measured",
                "reason": None,
                "window_rows": window_rows,
                "truncated": False,
                "values": values,
            }
        ],
    }


@pytest.fixture
def watched(live_postgres, monkeypatch):
    """One project, one Datastream, and one value table ASSIGNED to its column.

    The assignment is what ARMS the dimension: "a dimension is armed when a value
    mapping table is assigned to its field -- the person has already declared they
    care about that vocabulary, so no new toggle is invented to ask them twice".

    `core.db.get_connection` is pointed at the SAME database as this connection:
    `write_infra_firing` deliberately opens its own short-lived one, and a test
    that let it wander off to another DSN would be measuring nothing.
    """
    conn = live_postgres
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])

    project_id = _id("proj_")
    ds_id = _id("ds_")
    connection_id = _id("cref_")
    table_id = _id("vmt_")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'ai-326-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "  (id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
            "VALUES (%s, 'ga4', %s, %s, %s, 'owner@example.com')",
            (connection_id, connection_id, project_id, TEST_ORG_ID),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, report_profile_id, source_kind,
                 connection_ref_id, enabled, created_by, org_id)
            VALUES (%s, %s, 'Unresolved values fixture', 'ga4', 'video_daily',
                    'connector_pull', %s, TRUE, 'test', %s)
            """,
            (ds_id, project_id, connection_id, TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.value_mapping_tables "
            "  (id, org_id, project_id, scope_level, name, created_by) "
            "VALUES (%s, %s, %s, 'PROJECT', 'Video names', 'test')",
            (table_id, TEST_ORG_ID, project_id),
        )
        cur.execute(
            "INSERT INTO app.value_mapping_assignments "
            "  (id, table_id, datastream_id, org_id, source_field, created_by) "
            "VALUES (%s, %s, %s, %s, %s, 'test')",
            (_id("vma_"), table_id, ds_id, TEST_ORG_ID, DIMENSION),
        )
    conn.commit()

    ds = {
        "id": ds_id,
        "project_id": project_id,
        "org_id": TEST_ORG_ID,
        "name": "Unresolved values fixture",
        "module_name": "ga4",
    }
    yield conn, ds

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.alert_firings WHERE project_id = %s", (project_id,))
    conn.commit()
    purge_fixture_project(conn, project_id)
    conn.commit()


def _firings(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT type, finding_key, metric, severity, message "
            "FROM app.alert_firings WHERE project_id = %s ORDER BY finding_key",
            (project_id,),
        )
        return cur.fetchall()


def _sightings(conn, datastream_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dimension, reason, announced FROM app.unresolved_value_sightings "
            "WHERE datastream_id = %s ORDER BY reason, value_fingerprint",
            (datastream_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# The four sweeps the page describes, played in order on one Datastream.
# ---------------------------------------------------------------------------


def test_the_first_arming_records_the_backlog_and_mails_nobody(watched):
    """*the first arming mails the whole backlog as new arrivals* -- refused.

    Three unresolved videos exist the night the dimension is first swept. The
    monitor records all three and writes NO firing: "the first evaluation of a
    (Datastream, dimension) seeds and fires nothing, exactly as the schema monitor
    seeds its baseline on the first run".
    """
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    reading = _reading([_value("vid_1", 300), _value("vid_2", 120), _value("vid_3", 40)])

    outcome = monitor.sweep_datastream(conn, ds, reading=reading)

    assert outcome["status"] == "evaluated"
    assert outcome["fired"] == 0
    assert outcome["armed"] == [DIMENSION]
    assert _firings(conn, ds["project_id"]) == []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT baseline_values FROM app.unresolved_value_watches "
            "WHERE datastream_id = %s AND dimension = %s",
            (ds["id"], DIMENSION),
        )
        assert cur.fetchone()[0] == 3
    # Recorded, and recorded as NOT announced: the row says nobody was told, which
    # is a different fact from "somebody was told".
    assert _sightings(conn, ds["id"]) == [
        (DIMENSION, "unmapped", False),
        (DIMENSION, "unmapped", False),
        (DIMENSION, "unmapped", False),
    ]


def test_a_new_value_opens_one_firing_for_its_group(watched):
    """The delta, and the grain: ONE firing for 128 new videos, not 128."""
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    backlog = [_value("vid_1", 300), _value("vid_2", 120)]
    monitor.sweep_datastream(conn, ds, reading=_reading(backlog))

    arrivals = [_value(f"new_{index}", 4) for index in range(128)]
    outcome = monitor.sweep_datastream(conn, ds, reading=_reading(backlog + arrivals))

    assert outcome["fired"] == 1, "one firing per (datastream, dimension, reason)"
    rows = _firings(conn, ds["project_id"])
    assert len(rows) == 1
    alert_type, key, metric, severity, message = rows[0]
    assert alert_type == "dq_unresolved_values"
    assert key == f"{DIMENSION}::unmapped"
    assert metric == "unresolved_values"
    # Routing is on the TYPE, never on the severity -- already load-bearing for
    # eight monitors, and this is the ninth.
    assert severity == "warning"
    assert "128 new value(s)" in message


def test_the_same_group_is_never_reported_twice(watched):
    """*the monitor fires on values it already reported* -- refused."""
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    backlog = [_value("vid_1", 300)]
    monitor.sweep_datastream(conn, ds, reading=_reading(backlog))

    arrived = backlog + [_value("new_1", 200)]
    first = monitor.sweep_datastream(conn, ds, reading=_reading(arrived))
    second = monitor.sweep_datastream(conn, ds, reading=_reading(arrived))
    third = monitor.sweep_datastream(conn, ds, reading=_reading(arrived))

    assert (first["fired"], second["fired"], third["fired"]) == (1, 0, 0)
    assert len(_firings(conn, ds["project_id"])) == 1


def test_a_second_reason_on_the_same_dimension_opens_its_own_firing(watched):
    """The three reasons never merge -- three gestures repair them.

    And this is the case migration 229's identity could not express: two findings
    about one Datastream on one night. Before `finding_key`, the second INSERT hit
    `ON CONFLICT DO NOTHING` and vanished without a word.
    """
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    backlog = [_value("vid_1", 300)]
    monitor.sweep_datastream(conn, ds, reading=_reading(backlog))

    both = backlog + [
        _value("new_1", 200, reason="unmapped"),
        _value("", 150, reason="absent_at_source"),
    ]
    outcome = monitor.sweep_datastream(conn, ds, reading=_reading(both))

    assert outcome["fired"] == 2
    keys = [row[1] for row in _firings(conn, ds["project_id"])]
    assert keys == [f"{DIMENSION}::absent_at_source", f"{DIMENSION}::unmapped"]


def test_the_firing_names_a_repair_the_console_can_open(watched):
    """*an alert names no repair address* -- refused, on the delivered seam.

    The class guard walks every DQ writer's address; this is the instance, and it
    is the S1 form because this monitor always knows its Datastream.
    """
    from core import unresolved_values_monitor as monitor

    from tests.support.console_addresses import resolve_console_address

    conn, ds = watched
    monitor.sweep_datastream(conn, ds, reading=_reading([_value("vid_1", 300)]))
    payload = monitor.build_firing(
        [_value("new_1", 200)],
        datastream_id=ds["id"],
        datastream_name=ds["name"],
        dimension=DIMENSION,
        reason="unmapped",
        window=WINDOW,
        window_rows=1000,
        threshold=monitor.DEFAULT_MIN_NEW_VALUE_ROW_SHARE,
        threshold_source=monitor.THRESHOLD_DOCUMENTED_DEFAULT,
    )
    repair = payload["metadata"]["repair"]
    assert resolve_console_address(repair) is None, repair
    assert repair["tab"] == "mapping" and repair["object_id"] == ds["id"]


# ---------------------------------------------------------------------------
# The cost threshold, and what it must NOT do to the memory.
# ---------------------------------------------------------------------------


def test_a_cheap_new_value_stays_silent_and_stays_reportable(watched):
    """Below the declared share: no firing -- and no sighting either.

    The second half is the one that is easy to get wrong. Remembering a value the
    monitor deliberately did not announce would make it "already reported", and
    the night it grows to a fifth of the window it would stay silent forever. It
    is listed on S1 the whole time, which is what the page asks for.
    """
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    backlog = [_value("vid_1", 300)]
    monitor.sweep_datastream(conn, ds, reading=_reading(backlog))

    # 5 rows of 1000 = 0.5 %, under the documented default of 1 %.
    cheap = monitor.sweep_datastream(
        conn, ds, reading=_reading(backlog + [_value("new_1", 5)])
    )
    assert cheap["fired"] == 0
    assert cheap["threshold_source"] == monitor.THRESHOLD_DOCUMENTED_DEFAULT
    assert len(_sightings(conn, ds["id"])) == 1, "the cheap value was not remembered"

    # The same value, now carrying a fifth of the window. It speaks.
    grown = monitor.sweep_datastream(
        conn, ds, reading=_reading(backlog + [_value("new_1", 200)])
    )
    assert grown["fired"] == 1


def test_the_project_threshold_overrides_the_documented_default(watched):
    """"Prefer per-project preferences", and the firing records which was applied."""
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_preferences "
            "  (project_id, min_unresolved_new_value_row_share) VALUES (%s, %s) "
            "ON CONFLICT (project_id) DO UPDATE "
            "SET min_unresolved_new_value_row_share = EXCLUDED.min_unresolved_new_value_row_share",
            (ds["project_id"], 0.5),
        )
    conn.commit()

    backlog = [_value("vid_1", 300)]
    monitor.sweep_datastream(conn, ds, reading=_reading(backlog))
    outcome = monitor.sweep_datastream(
        conn, ds, reading=_reading(backlog + [_value("new_1", 200)])
    )

    assert outcome["threshold"] == 0.5
    assert outcome["threshold_source"] == monitor.THRESHOLD_PROJECT_PREFERENCE
    # 200 of 1000 is 20 %, under the project's declared 50 %.
    assert outcome["fired"] == 0


# ---------------------------------------------------------------------------
# What is swept for the list, and what is armed to alert.
# ---------------------------------------------------------------------------


def test_a_dimension_with_no_assigned_table_is_never_armed(watched):
    """"Every mapped dimension is swept for the list; only an armed one fires."

    And the consequence that makes the two rules one: no watch row is written, so
    the day a table IS assigned, that dimension's backlog becomes its baseline --
    the first arming doing its job rather than a second rule.
    """
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    outcome = monitor.sweep_datastream(
        conn, ds, reading=_reading([_value("fr", 900)], dimension="country")
    )

    assert outcome["status"] == "not_applicable"
    assert outcome["reason"] == monitor.NOT_ARMED
    assert outcome["fired"] == 0
    assert monitor.read_watches(conn, ds["id"]) == {}


def test_an_unreadable_assignment_store_is_never_a_pass(watched, monkeypatch):
    """"I could not look" is never "there is nothing", and never a firing either."""
    from core import unresolved_values_monitor as monitor
    from core import value_mapping_tables

    conn, ds = watched
    monkeypatch.setattr(
        value_mapping_tables, "read_assigned_source_fields", lambda *a, **k: None
    )
    outcome = monitor.sweep_datastream(conn, ds, reading=_reading([_value("vid_1", 300)]))

    assert outcome["status"] == "unavailable"
    assert outcome["reason"] == monitor.ASSIGNMENTS_UNREADABLE
    assert outcome["fired"] == 0
    assert monitor.read_watches(conn, ds["id"]) == {}


def test_no_mapped_dimension_is_not_applicable_and_not_a_pass(watched):
    from core import unresolved_values_monitor as monitor

    conn, ds = watched
    outcome = monitor.sweep_datastream(
        conn,
        ds,
        reading={
            "state": "not_applicable",
            "reason": "no_mapped_dimension",
            "window": dict(WINDOW),
            "groups": [],
        },
    )
    assert outcome["status"] == "not_applicable"
    assert outcome["reason"] == "no_mapped_dimension"


# ---------------------------------------------------------------------------
# The dispatched check answers a verdict, and the sweep reaches it.
# ---------------------------------------------------------------------------


def test_the_dispatched_check_answers_a_verdict_and_not_a_boolean(watched):
    """`governance.md` [8]: "no issue" may never stand for "nothing was measured"."""
    from datetime import date

    from core import dq_monitors

    conn, ds = watched
    backlog = [_value("vid_1", 300)]
    seeded = dq_monitors._check_unresolved_values(
        ds, conn, date(2026, 8, 31), None, _reading(backlog)
    )
    assert seeded.status == dq_monitors.STATUS_EVALUATED
    assert bool(seeded) is False
    assert seeded.detail["armed"] == [DIMENSION]

    fired = dq_monitors._check_unresolved_values(
        ds, conn, date(2026, 8, 31), None, _reading(backlog + [_value("new_1", 200)])
    )
    assert bool(fired) is True
    assert fired.detail["fired"] == 1
    assert fired.detail["window_date"] == "2026-08-31"
