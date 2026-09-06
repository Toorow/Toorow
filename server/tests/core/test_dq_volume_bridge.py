"""Story 59.5: the volume monitor writes its verdict down, on a live Postgres.

WHAT ONLY A DATABASE CAN PROVE HERE:

1. **The check reads a REAL ledger, and the ledger is why this monitor is mute.**
   `row_count` is published only for a collection window exactly one day wide
   (`extract_ledger.py:448-458`) and it is NULL on 141 pull jobs of 141 on the
   disposable base and 6 of 6 on preprod. A mocked ledger would agree with any
   shape; only Postgres proves that a three-day window comes out of
   `_day_to_ledger_entry` with `row_count = None` and the reason
   `measured_per_window`, which is the outcome this bridge writes.
2. **`not_applicable` is stored, and a pass over an empty denominator is not.**
   `ck_dq_evaluations_empty_is_not_a_pass` is a real constraint; the counts this
   bridge derives satisfy it by construction or the INSERT fails.
3. **One issue per flux, and the second anomalous day is a RECURRENCE.** The
   fingerprint is `(monitor,)`, and what makes that a bound rather than an
   intention is `uq_dq_issues_open_root_cause`.
4. **A Datastream that never collected gets no governed object at all** -- the
   registry stays bounded to what can actually be judged.

`app.pull_jobs.execution_id` was NULL on every row of the disposable base when
this was written, so every fixture seeds it explicitly.

`pg_owner`: the teardown deletes from `app.dq_issue_events`, which is append-only
BY TRIGGER. Under the application role this file SKIPS, and a skip is not a pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest
from core.dq_monitors import STATUS_EVALUATED, STATUS_NOT_APPLICABLE

from tests.conftest import purge_fixture_project

TEST_ORG_ID = "org_test_fixture"
#: The judged day. The band reads it plus the thirty days before it.
WINDOW = date(2026, 8, 6)

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the volume bridge needs a live Postgres",
)

pytestmark = [_skip_without_dsn, pytest.mark.pg_owner]

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid_body() -> str:
    body = uuid.uuid4().int
    chars = []
    for _ in range(26):
        body, index = divmod(body, len(_ALPHABET))
        chars.append(_ALPHABET[index])
    return "".join(reversed(chars))


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def bridge(live_postgres, monkeypatch):
    """One project, two Datastreams -- one judged, one that never collected."""
    conn = live_postgres
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])

    project_id = _id("proj_")
    ds_id = _id("ds_")
    silent_ds_id = _id("ds_")
    plan_id = _id("dsp_")
    mapping_id = _id("dmap_")
    connection_id = _id("cref_")
    first_run = f"dse_{_ulid_body()}"
    second_run = f"dse_{_ulid_body()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'story-59.5-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "  (id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
            "VALUES (%s, 'ga4', %s, %s, %s, 'owner@example.com')",
            (connection_id, connection_id, project_id, TEST_ORG_ID),
        )
        for stream_id, name in (
            (ds_id, "Volume fixture"),
            (silent_ds_id, "Volume fixture that never collected"),
        ):
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, module_name, report_profile_id, source_kind,
                     connection_ref_id, enabled, created_by, org_id, window_offset_days)
                VALUES (%s, %s, %s, 'ga4', 'pages_daily_landing', 'connector_pull',
                        %s, TRUE, 'test', %s, 1)
                """,
                (stream_id, project_id, name, connection_id, TEST_ORG_ID),
            )
            cur.execute(
                "INSERT INTO app.project_flux (project_id, flux_id, org_id) "
                "VALUES (%s, %s, %s)",
                (project_id, stream_id, TEST_ORG_ID),
            )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'connector_pull', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), 'test')
            """,
            (plan_id, ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                    '1', TRUE, '{"grain": ["date"]}'::jsonb, '{}'::jsonb,
                    repeat('d', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_id),
        )
        for run_id in (first_run, second_run):
            cur.execute(
                """
                INSERT INTO app.datastream_executions
                    (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                     projection_plan_ref, state, created_by)
                VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'collected', 'test')
                """,
                (run_id, ds_id, project_id, plan_id, mapping_id),
            )
    conn.commit()

    context = {
        "project_id": project_id,
        "datastream_id": ds_id,
        "silent_datastream_id": silent_ds_id,
        "connection_ref_id": connection_id,
        "first_run": first_run,
        "second_run": second_run,
    }
    yield conn, context

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.dq_issue_events DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.dq_evaluations DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.dq_monitor_versions DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_mapping_versions DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_plan_versions DISABLE TRIGGER USER")
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
            ):
                cur.execute(statement, (project_id,))
            cur.execute("DELETE FROM app.alert_firings WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM app.pull_verifications WHERE connection_ref_id = %s",
                (connection_id,),
            )
            cur.execute(
                "DELETE FROM app.pull_jobs WHERE connection_ref_id = %s", (connection_id,)
            )
            cur.execute(
                "DELETE FROM app.datastream_executions WHERE project_id = %s", (project_id,)
            )
            cur.execute("DELETE FROM app.project_flux WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM app.datastream_mapping_versions WHERE project_id = %s",
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.datastream_plan_versions WHERE project_id = %s",
                (project_id,),
            )
            cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM app.project_capabilities WHERE project_id = %s", (project_id,)
            )
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
        finally:
            cur.execute("ALTER TABLE app.datastream_plan_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.datastream_mapping_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_monitor_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_evaluations ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_issue_events ENABLE TRIGGER USER")
    conn.commit()


def _seed_pull(
    conn,
    context,
    *,
    date_from: date,
    date_to: date | None = None,
    actual_rows: int,
    execution_id: str | None = None,
    datastream_id: str | None = None,
) -> str:
    """One `app.pull_jobs` row and its verdict. A ONE-DAY window publishes a count."""
    pull_id = _id("pull_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, datastream_id, execution_id,
                 date_from, date_to, state, requested_by, enqueued_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'done', 'test', now(), now())
            """,
            (
                _id("pjob_"),
                pull_id,
                context["connection_ref_id"],
                datastream_id or context["datastream_id"],
                execution_id,
                date_from,
                date_to or date_from,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.pull_verifications
                (id, pull_id, connection_ref_id, expected_rows, actual_rows,
                 completeness_ratio, verdict)
            VALUES (%s, %s, %s, %s, %s, 1.0, 'ok')
            """,
            (_id("pver_"), pull_id, context["connection_ref_id"], actual_rows, actual_rows),
        )
    conn.commit()
    return pull_id


def _seed_history(conn, context, *, days: int, rows: int = 100) -> None:
    """*days* one-day windows before the judged day, each carrying a row count."""
    for offset in range(1, days + 1):
        _seed_pull(conn, context, date_from=WINDOW - timedelta(days=offset), actual_rows=rows)


def _ds_row(conn, project_id: str, datastream_id: str) -> dict:
    from core.dq_monitors import _fetch_enabled_datastreams

    rows = [
        ds for ds in _fetch_enabled_datastreams(conn, project_id) if ds["id"] == datastream_id
    ]
    assert rows, "the fixture's Datastream must be visible to the sweep's own read"
    return rows[0]


def _evaluate(ds_row, conn, window_date: date = WINDOW):
    """The check's VERDICT -- `fired` and `status`, never a bare boolean.

    This helper was annotated `-> bool` and every caller below asserted
    `is True` / `is False`. Story 59.5 replaced the boolean with `CheckVerdict`
    precisely because a boolean could not tell a clean night from a night when
    nothing was measured, and `CheckVerdict.__eq__` was written to keep `== True`
    working -- but `is` compares identity, so it can never match a verdict. The
    assertions that "passed" passed only on the two exits that still answered a
    bare boolean, which is how the product defect they should have caught went on
    being invisible. They now read the two fields the verdict exists to carry.
    """
    from core import dq_monitors

    return dq_monitors._check_volume(
        ds_row["id"],
        ds_row["project_id"],
        ds_row["module_name"],
        ds_row["name"],
        conn,
        window_date,
        ds_row,
    )


def _assert_verdict(verdict, *, fired: bool, status: str) -> None:
    """Both halves of the answer, because either alone can lie.

    `fired` alone cannot tell a clean night from a night nothing was measured --
    both are `False` -- and `status` alone cannot tell a pass from a firing.
    """
    from core.dq_monitors import CheckVerdict

    assert isinstance(verdict, CheckVerdict), (
        f"_check_volume answered {verdict!r}: its signature says CheckVerdict, and the "
        "sweep reads `.status` off it. A bare boolean is counted as a PASS."
    )
    assert (verdict.fired, verdict.status) == (fired, status)


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
            "SELECT type, message, window_date FROM app.alert_firings WHERE project_id = %s",
            (project_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# The silence this monitor has been keeping, now written down.
# ---------------------------------------------------------------------------


def test_a_window_wider_than_a_day_is_not_applicable_and_says_why(bridge):
    """Arbitrage 2, against the ledger that produces the absence.

    Three-day windows are what 123 of the repository's 130 windows look like, and
    they publish no per-day row count at all. Before this bridge the check
    answered `False` -- indistinguishable from "this volume is normal" -- for
    every Datastream of both bases.
    """
    from core import dq_monitors
    from core.extract_ledger import ROW_COUNT_MEASURED_PER_WINDOW

    # The reason is the LEDGER's word, not a second vocabulary.
    assert dq_monitors.VOLUME_MEASURED_PER_WINDOW == ROW_COUNT_MEASURED_PER_WINDOW

    conn, context = bridge
    for offset in range(0, 12):
        start = WINDOW - timedelta(days=offset * 3)
        _seed_pull(
            conn,
            context,
            date_from=start - timedelta(days=2),
            date_to=start,
            actual_rows=100,
            execution_id=context["first_run"] if offset == 0 else None,
        )

    _assert_verdict(
        _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn),
        fired=False,
        status=STATUS_NOT_APPLICABLE,
    )

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    _id_, outcome, datastream_id, execution_id, start, end, observed = evaluations[0]
    assert outcome == "not_applicable"
    assert datastream_id == context["datastream_id"]
    assert execution_id == context["first_run"]
    assert (start, end) == (WINDOW, WINDOW)
    assert observed["reason"] == dq_monitors.VOLUME_MEASURED_PER_WINDOW
    assert observed["data_points"] == 0
    # A sentence, not a code: the reason reaches a person through the evaluation.
    assert observed["message"] == dq_monitors.volume_message_for(
        dq_monitors.VOLUME_MEASURED_PER_WINDOW
    )
    assert _issues(conn, context["project_id"]) == []
    assert _firings(conn, context["project_id"]) == []


def test_too_little_history_is_not_applicable_and_never_a_pass(bridge):
    """The band refuses below ten prior points, and now it says so."""
    from core import dq_monitors

    conn, context = bridge
    _seed_history(conn, context, days=4)
    _seed_pull(
        conn,
        context,
        date_from=WINDOW,
        actual_rows=100,
        execution_id=context["first_run"],
    )

    _assert_verdict(
        _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn),
        fired=False,
        status=STATUS_NOT_APPLICABLE,
    )

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "not_applicable"
    assert evaluations[0][6]["reason"] == dq_monitors.VOLUME_NOT_ENOUGH_HISTORY
    assert evaluations[0][6]["prior_n"] == 4
    assert _issues(conn, context["project_id"]) == []


def test_a_datastream_that_never_collected_gets_no_governed_object(bridge):
    """The registry stays bounded to what can be judged at all."""
    conn, context = bridge

    # AND IT IS `not_applicable`, which is the whole point of this test: a
    # Datastream that never collected must not be counted a PASS by the sweep.
    _assert_verdict(
        _evaluate(_ds_row(conn, context["project_id"], context["silent_datastream_id"]), conn),
        fired=False,
        status=STATUS_NOT_APPLICABLE,
    )

    assert _evaluations(conn, context["project_id"]) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.dq_monitors WHERE project_id = %s",
            (context["project_id"],),
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# A measured day: the pass, and the anomaly.
# ---------------------------------------------------------------------------


def test_a_measured_day_within_the_band_is_a_pass(bridge):
    conn, context = bridge
    _seed_history(conn, context, days=11)
    _seed_pull(
        conn, context, date_from=WINDOW, actual_rows=100, execution_id=context["first_run"]
    )

    _assert_verdict(
        _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn),
        fired=False,
        status=STATUS_EVALUATED,
    )

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "pass"
    assert evaluations[0][6]["row_count"] == 100
    assert _issues(conn, context["project_id"]) == []
    assert _firings(conn, context["project_id"]) == []


def test_an_anomalous_day_opens_one_issue_naming_its_run_in_english(bridge):
    """The firing and the governed issue are one gesture, and both are readable."""
    from tests.english_guard import assert_english_firings

    conn, context = bridge
    _seed_history(conn, context, days=11)
    _seed_pull(
        conn, context, date_from=WINDOW, actual_rows=9999, execution_id=context["first_run"]
    )

    _assert_verdict(
        _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn),
        fired=True,
        status=STATUS_EVALUATED,
    )

    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1
    _issue_id, datastream_id, execution_id, _severity, status = issues[0]
    assert datastream_id == context["datastream_id"]
    assert execution_id == context["first_run"]
    assert status == "open"

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "fail"

    firings = _firings(conn, context["project_id"])
    assert len(firings) == 1
    assert firings[0][0] == "dq_volume"
    # The runtime half of the English guard (arbitrage 5): the sentence a person
    # reads, as the check actually produced it -- not as a scanner reconstructed it.
    assert_english_firings([(row[0], row[1]) for row in firings], where="dq_volume")


def test_a_second_anomalous_day_recurs_and_moves_the_run(bridge):
    """One flux, one open issue -- `uq_dq_issues_open_root_cause` says so."""
    conn, context = bridge
    _seed_history(conn, context, days=12)
    _seed_pull(
        conn,
        context,
        date_from=WINDOW - timedelta(days=1),
        actual_rows=9999,
        execution_id=context["first_run"],
    )
    ds_row = _ds_row(conn, context["project_id"], context["datastream_id"])
    _assert_verdict(
        _evaluate(ds_row, conn, WINDOW - timedelta(days=1)),
        fired=True,
        status=STATUS_EVALUATED,
    )

    _seed_pull(
        conn, context, date_from=WINDOW, actual_rows=8888, execution_id=context["second_run"]
    )
    _assert_verdict(_evaluate(ds_row, conn), fired=True, status=STATUS_EVALUATED)

    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1, "the second anomalous day recurs into the same issue"
    assert issues[0][2] == context["second_run"], "the run moves with the last sighting"

    runs = [row[3] for row in _evaluations(conn, context["project_id"])]
    assert runs == [context["first_run"], context["second_run"]]


def test_the_monitor_it_derives_carries_the_registry_label(bridge):
    """`app.dq_monitors.label` is the one monitor name that reaches a person."""
    from core import dq_monitor_registry

    conn, context = bridge
    _seed_history(conn, context, days=11)
    _seed_pull(
        conn, context, date_from=WINDOW, actual_rows=100, execution_id=context["first_run"]
    )

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
    assert name == dq_monitor_registry.monitor_name("volume", context["datastream_id"])
    assert label == dq_monitor_registry.instance_label("volume", "Volume fixture")
    assert target_kind == dq_monitor_registry.TARGET_DATASTREAM
    assert check_profile == "volume"
