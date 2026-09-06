"""Story 59.5: the arrival monitor writes its verdict down, on a live Postgres.

WHAT ONLY A DATABASE CAN PROVE HERE:

1. **The window is the day of the observation, and the schema is why.** This check
   measures ELAPSED MINUTES and has no window of its own, while
   `app.dq_evaluations.window_start` and `window_end` are `DATE NOT NULL`
   (migration 145:460-461). Arbitrage 3 answers with the day of the sweep, and a
   real INSERT is the only thing that proves the pair is writable and stored as
   the day the other checks of the same round were judged on.
2. **A run of `NULL` is a real answer.** A delivered feed has no collection, so
   `app.dq_issues.execution_id` is NULL -- and
   `ck_dq_issues_execution_needs_datastream` still holds because the Datastream
   is named. Only the constraint can prove that.
3. **The profile is `arrival_timeliness` while the firing stays `dq_timeliness`.**
   The two vocabularies coexist on purpose (`dq_monitor_registry`), and the rows
   are where they meet: one `app.alert_firings` row typed `dq_timeliness` and
   stamped `monitor_kind='arrival'`, one `app.dq_monitor_versions` row whose
   `check_profile` is `arrival_timeliness`.
4. **One issue per flux, and the second late day is a RECURRENCE** --
   `uq_dq_issues_open_root_cause`, against a real unique index.

`app.inbound_receipts` is protected by `protect_inbound_receipt` (migration 184):
a receipt is born `RECEIVED` and its operation provenance must name the
Datastream. The fixture writes exactly that, because a receipt that could not be
written the way the product writes it would prove nothing about the product.

`pg_owner`: the teardown deletes from append-only tables. Under the application
role this file SKIPS, and a skip is not a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import purge_fixture_project

TEST_ORG_ID = "org_test_fixture"
#: The judged day -- the day the sweep is speaking about.
WINDOW = date(2026, 8, 6)
NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
#: A file every hour. Lateness starts at twice the interval (one full grace).
INTERVAL_MINUTES = 60

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the arrival bridge needs a live Postgres",
)

pytestmark = [_skip_without_dsn, pytest.mark.pg_owner]


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _ulid_like() -> str:
    return uuid.uuid4().hex[:26].upper()


@pytest.fixture
def bridge(live_postgres, monkeypatch):
    """One project and one DELIVERED Datastream that promises a file every hour."""
    conn = live_postgres
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])

    project_id = _id("proj_")
    ds_id = _id("ds_")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'story-59.5-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        # A delivered feed carries no connector and no authorization: the
        # `ck_datastreams_source_kind` CHECK requires `module_name IS NULL` for
        # `managed_feed`, which is exactly the shape this monitor exists for.
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, source_kind, enabled, created_by, org_id,
                 window_offset_days)
            VALUES (%s, %s, 'Arrival fixture', 'managed_feed', TRUE, 'test', %s, 1)
            """,
            (ds_id, project_id, TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s, %s, %s)",
            (project_id, ds_id, TEST_ORG_ID),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_arrival_monitors
                (datastream_id, project_id, expected_interval_minutes, owner_person_id,
                 state, activated_at)
            VALUES (%s, %s, %s, 'owner@example.com', 'active', %s)
            """,
            (ds_id, project_id, INTERVAL_MINUTES, NOW - timedelta(days=30)),
        )
    conn.commit()

    # The operations this fixture writes, and ONLY those: `org_test_fixture` is
    # shared by the whole pg-gated suite, so a teardown keyed on the org would
    # delete rows another test seeded.
    context = {"project_id": project_id, "datastream_id": ds_id, "operations": []}
    yield conn, context

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.dq_issue_events DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.dq_evaluations DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.dq_monitor_versions DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.inbound_receipts DISABLE TRIGGER USER")
        try:
            for statement in (
                "DELETE FROM app.dq_issue_events WHERE project_id = %s",
                "DELETE FROM app.dq_issues WHERE project_id = %s",
                "DELETE FROM app.dq_evaluations WHERE project_id = %s",
                "UPDATE app.dq_monitors SET current_version_id = NULL, "
                "pending_version_id = NULL, last_known_good_version_id = NULL "
                "WHERE project_id = %s",
                "DELETE FROM app.dq_monitor_versions WHERE project_id = %s",
                "DELETE FROM app.dq_monitors WHERE project_id = %s",
                "DELETE FROM app.alert_firings WHERE project_id = %s",
            ):
                cur.execute(statement, (project_id,))
            cur.execute(
                "DELETE FROM app.inbound_receipts WHERE datastream_id = %s",
                (context["datastream_id"],),
            )
            cur.execute(
                "DELETE FROM app.datastream_arrival_monitors WHERE project_id = %s",
                (project_id,),
            )
            if context["operations"]:
                cur.execute(
                    "DELETE FROM app.operations WHERE id = ANY(%s)", (context["operations"],)
                )
            cur.execute("DELETE FROM app.project_flux WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM app.project_capabilities WHERE project_id = %s", (project_id,)
            )
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
        finally:
            cur.execute("ALTER TABLE app.inbound_receipts ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_monitor_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_evaluations ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_issue_events ENABLE TRIGGER USER")
    conn.commit()


def _seed_receipt(conn, context, *, delivered_at: datetime) -> str:
    """One inbound receipt, written the way `protect_inbound_receipt` demands."""
    receipt_id = f"inbrx_{_ulid_like()}"
    operation_id = f"op_{_ulid_like()}"
    seed = f"{receipt_id}:{delivered_at.isoformat()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, host_context, "
            " versions, request_hash, provider_references, confirmation_mode, "
            " idempotency_key_hash, state) "
            "VALUES (%s, %s, 'inbound.receipt.recorded', 'test', %s::jsonb, '{}'::jsonb, "
            "        '{}'::jsonb, %s, '{}'::jsonb, 'server', %s, 'pending')",
            (
                operation_id,
                TEST_ORG_ID,
                json.dumps([f"datastream:{context['datastream_id']}"]),
                hashlib.sha256(f"req:{seed}".encode()).hexdigest(),
                hashlib.sha256(f"idem:{seed}".encode()).hexdigest(),
            ),
        )
        cur.execute(
            "INSERT INTO app.inbound_receipts "
            "(id, datastream_id, channel, provider_event_id, attachment_count, state, "
            " operation_id, receipt_fingerprint, created_at) "
            "VALUES (%s, %s, 'email', %s, 1, 'RECEIVED', %s, %s, %s)",
            (
                receipt_id,
                context["datastream_id"],
                f"evt-{uuid.uuid4().hex}",
                operation_id,
                hashlib.sha256(receipt_id.encode()).hexdigest(),
                delivered_at,
            ),
        )
    conn.commit()
    context["operations"].append(operation_id)
    return receipt_id


def _ds_row(conn, project_id: str, datastream_id: str) -> dict:
    from core.dq_monitors import _fetch_enabled_datastreams

    rows = [
        ds for ds in _fetch_enabled_datastreams(conn, project_id) if ds["id"] == datastream_id
    ]
    assert rows, "the fixture's Datastream must be visible to the sweep's own read"
    return rows[0]


def _evaluate(ds_row, conn, *, now: datetime = NOW, window: date = WINDOW) -> bool:
    """Through `_check_timeliness`, which is the door the sweep actually uses.

    The arrival branch is reached from the pull-based check, so calling the inner
    function directly would prove a path the nightly sweep never takes.
    """
    from core import dq_monitors

    return dq_monitors._check_timeliness(
        ds_row["id"],
        ds_row["project_id"],
        ds_row["module_name"],
        ds_row["name"],
        conn,
        window,
        now,
        1,
        ds_row,
    )


def _evaluations(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, outcome, datastream_id, execution_id, window_start, window_end, observed "
            "FROM app.dq_evaluations WHERE project_id = %s ORDER BY evaluated_at",
            (project_id,),
        )
        return cur.fetchall()


def _issues(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, datastream_id, execution_id, severity, status "
            "FROM app.dq_issues WHERE project_id = %s",
            (project_id,),
        )
        return cur.fetchall()


def _firings(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            # `app.alert_firings` has NO metadata column: `write_infra_firing`
            # appends the payload to the message as `| meta={...}` (its own
            # docstring says so). The test reads what the table actually holds.
            "SELECT type, message FROM app.alert_firings WHERE project_id = %s",
            (project_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# A file that did not arrive.
# ---------------------------------------------------------------------------


def test_a_late_delivery_opens_one_issue_dated_by_the_day_of_the_observation(bridge):
    """Arbitrage 3, on the `DATE NOT NULL` pair this check has no window for."""
    from tests.english_guard import assert_english_firings

    conn, context = bridge
    # Ten days without a file, on a feed that promised one every hour.
    _seed_receipt(conn, context, delivered_at=NOW - timedelta(days=10))

    assert _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn) is True

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    _id_, outcome, datastream_id, execution_id, start, end, observed = evaluations[0]
    assert outcome == "fail"
    assert datastream_id == context["datastream_id"]
    # A delivered feed has no run, and NULL is the honest answer.
    assert execution_id is None
    # NOT the day of the last delivery: the day this round is speaking about.
    assert (start, end) == (WINDOW, WINDOW)
    assert observed["expected_interval_minutes"] == INTERVAL_MINUTES
    assert observed["minutes_since_last_delivery"] >= 10 * 24 * 60

    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1
    assert issues[0][1] == context["datastream_id"]
    assert issues[0][2] is None
    assert issues[0][4] == "open"

    firings = _firings(conn, context["project_id"])
    assert len(firings) == 1
    alert_type, message = firings[0]
    # The type is its SIBLING's -- one operator question, one alert list -- and
    # `monitor_kind` is the only thing that tells the two apart.
    assert alert_type == "dq_timeliness"
    assert '"monitor_kind": "arrival"' in message
    assert_english_firings([(alert_type, message)], where="arrival_timeliness")


def test_a_recent_delivery_is_a_pass_and_opens_nothing(bridge):
    conn, context = bridge
    _seed_receipt(conn, context, delivered_at=NOW - timedelta(minutes=30))

    assert _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn) is False

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "pass"
    assert _issues(conn, context["project_id"]) == []
    assert _firings(conn, context["project_id"]) == []


def test_a_feed_that_never_delivered_is_measured_from_its_activation(bridge):
    """No receipt at all, activated thirty days ago: that is late, and it is a fail."""
    conn, context = bridge

    assert _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn) is True

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "fail"
    assert evaluations[0][6]["ever_delivered"] is False
    assert len(_issues(conn, context["project_id"])) == 1


def test_a_second_late_day_recurs_and_keeps_its_own_evidence(bridge):
    """One flux, one open issue, and each day keeps its append-only evaluation."""
    conn, context = bridge
    _seed_receipt(conn, context, delivered_at=NOW - timedelta(days=10))
    ds_row = _ds_row(conn, context["project_id"], context["datastream_id"])

    assert _evaluate(ds_row, conn, now=NOW, window=WINDOW - timedelta(days=1)) is True
    assert _evaluate(ds_row, conn, now=NOW + timedelta(days=1), window=WINDOW) is True

    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1, "the second late day recurs into the same issue"

    windows = [(row[4], row[5]) for row in _evaluations(conn, context["project_id"])]
    assert windows == [
        (WINDOW - timedelta(days=1), WINDOW - timedelta(days=1)),
        (WINDOW, WINDOW),
    ]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_kind FROM app.dq_issue_events WHERE project_id = %s "
            "ORDER BY occurred_at",
            (context["project_id"],),
        )
        events = [row[0] for row in cur.fetchall()]
    assert events.count("observed") == 2


def test_a_feed_with_no_usable_interval_writes_nothing_at_all(bridge):
    """Not eligible: nobody promised anything, so no governed object is derived.

    The interval cannot be zero in the table (`expected_interval_minutes > 0`),
    so the case is reachable only through the check's own argument -- which is
    where it has to be refused, before a monitor exists.
    """
    from core import dq_monitors

    conn, context = bridge
    ds_row = _ds_row(conn, context["project_id"], context["datastream_id"])

    fired = dq_monitors._check_arrival_timeliness(
        ds_row["id"],
        ds_row["project_id"],
        ds_row["name"],
        {"expected_interval_minutes": 0, "owner_person_id": "owner@example.com"},
        conn,
        NOW,
        ds_row,
        WINDOW,
    )

    assert fired is False
    assert _evaluations(conn, context["project_id"]) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.dq_monitors WHERE project_id = %s",
            (context["project_id"],),
        )
        assert cur.fetchone()[0] == 0


def test_the_monitor_it_derives_is_its_own_profile_under_a_shared_type(bridge):
    """`arrival_timeliness` is a check profile; `dq_timeliness` is an alert type."""
    from core import dq_monitor_registry

    conn, context = bridge
    _seed_receipt(conn, context, delivered_at=NOW - timedelta(minutes=30))

    _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.name, m.label, m.target_kind, v.check_profile "
            "FROM app.dq_monitors m "
            "JOIN app.dq_monitor_versions v ON v.id = m.current_version_id "
            "WHERE m.project_id = %s",
            (context["project_id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    name, label, target_kind, check_profile = rows[0]
    assert name == dq_monitor_registry.monitor_name(
        "arrival_timeliness", context["datastream_id"]
    )
    assert label == dq_monitor_registry.instance_label("arrival_timeliness", "Arrival fixture")
    assert target_kind == dq_monitor_registry.TARGET_DATASTREAM
    # The VERSION carries `timeliness`: `publish_version` validates the profile
    # against what a DQ policy may name, and `arrival_timeliness` is deliberately
    # not one of those -- it is reached THROUGH `timeliness`, never named alone.
    # What keeps the two apart is the monitor's own name and label.
    from core import controls_quality

    assert check_profile == dq_monitor_registry.published_profile("arrival_timeliness")
    assert check_profile == "timeliness"
    assert "arrival_timeliness" not in controls_quality.DQ_CHECKS
    assert name.startswith("arrival_timeliness_")
