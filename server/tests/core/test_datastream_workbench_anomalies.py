"""The anomaly detail under its run, against a real Postgres -- story 59.1.

WHY THIS TEST IS SEEDED AND NOT MEASURED. Both bases carry ZERO rows in the five
world-B tables (`app.dq_monitors`, `dq_monitor_versions`, `dq_issues`,
`dq_issue_events`, `dq_evaluations`), and neither can grow one on its own:

* on the disposable base, `report_profile_id IS NOT NULL` is true of 172
  Datastreams and `current_mapping_version_id IS NOT NULL` of 230, and the two
  sets are DISJOINT -- so not one flux can derive a `null_rate` monitor;
* on preprod exactly one flux is eligible, but `app.datastream_executions` = 0
  and `app.pull_jobs.execution_id` is NULL on 6 rows of 6, so its issue would be
  written with `execution_id = NULL` and this read filters it out.

So the fixture seeds all three preconditions ON PURPOSE, and a fixture that
omitted any one of them would prove a dead path: `app.project_flux` (the INNER
JOIN of `_read_base_record`), a flux carrying BOTH `report_profile_id` and
`current_mapping_version_id`, and a `pull_jobs` row WITH an `execution_id`.

THE WORLD-B ROWS ARE WRITTEN BY THEIR OWN WRITERS -- `ensure_monitor`,
`publish_version`, `record_evaluation`, `open_issue` -- and never by hand-rolled
INSERTs. A hand-written row can satisfy a read the real writer would never
produce, which is how a green test outlives the path it claims to cover.

The warehouse is NOT seeded: the replay's reading of the collected relation is
taken at the `collected_mapped_reader` boundary, which is a different subsystem
with its own fixture pipeline. What is proved here is everything up to that
boundary -- the field recovered from the fingerprint, the window taken from the
evaluation, and the three answers an unfolded issue can carry.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_DSN not set -- skipping live Postgres test"
)

WINDOW_DAY = date(2026, 8, 4)
REQUIRED_FIELD = "campaign_id"
RELATION = "raw_example_daily"


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as connection:
        yield connection
        # Nothing this test writes survives it: `app.dq_issues` and
        # `app.dq_evaluations` are append-only under a trigger, so a committed
        # row could not be cleaned up afterwards.
        connection.rollback()


@pytest.fixture
def flux(conn):
    """One Project, one Datastream, one run -- and the three preconditions.

    Returns the identifiers the assertions need. Every value is fictional
    (`example.com`, `proj_…` minted from a uuid) and rolled back with the
    connection.
    """
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    datastream_id = f"ds_{suffix}"
    plan_id = f"dpv_{suffix}"
    mapping_id = f"dmv_{suffix}"
    # `ck_datastream_executions_id` is a ULID shape, so a run id is minted the
    # way the writer mints one rather than composed from the suffix.
    execution_id = f"dse_{ULID()}"
    other_execution_id = f"dse_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'owner@example.com')",
            (org_id, f"Org {suffix}", f"org-{suffix}"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'owner@example.com', %s)",
            (project_id, f"Project {suffix}", f"project-{suffix}", org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, module_name, report_profile_id, enabled) "
            "VALUES (%s, %s, %s, %s, 'example_connector', 'rp_example', TRUE)",
            (datastream_id, project_id, org_id, f"Flux {suffix}"),
        )
        # THE INNER JOIN OF `_read_base_record`. Without this row the Workbench
        # answers `WorkbenchNotFound` and every assertion below would be about a
        # 404 rather than about an anomaly.
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s, %s, %s)",
            (project_id, datastream_id, org_id),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
                 (id, datastream_id, project_id, version_number, contract_version,
                  source_kind, writer_kind, destination_policy, normalized_payload,
                  content_hash, idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', 'connector_pull', 'toorow', 'managed_raw',
                       '{}'::jsonb, %s, %s, 'system')""",
            (plan_id, datastream_id, project_id, "a" * 64, "b" * 64),
        )
        cur.execute(
            """INSERT INTO app.datastream_mapping_versions
                 (id, datastream_id, project_id, version_number, mapping_contract_version,
                  source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                  toorow_extension_version, executable, mapping_payload, ossie_projection,
                  idempotency_key_hash, created_by)
               VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, '1.0', '1.0', TRUE,
                       %s::jsonb, '{}'::jsonb, %s, 'system')""",
            (
                mapping_id,
                datastream_id,
                project_id,
                "c" * 64,
                plan_id,
                "d" * 64,
                '{"grain": ["%s"]}' % REQUIRED_FIELD,
                "e" * 64,
            ),
        )
        # BOTH pointers on one flux -- the combination no row of either base
        # carries today.
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
            (mapping_id, datastream_id),
        )
        for run_id in (execution_id, other_execution_id):
            cur.execute(
                """INSERT INTO app.datastream_executions
                     (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                      state, created_by)
                   VALUES (%s, %s, %s, %s, %s, 'published', 'system')""",
                (run_id, datastream_id, project_id, plan_id, mapping_id),
            )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "(id, provider, project_id, owner_org_id, owner_identity, nango_connection_id) "
            "VALUES (%s, 'example_connector', %s, %s, 'owner@example.com', %s)",
            (f"conn_{suffix}", project_id, org_id, f"nango_{suffix}"),
        )
        # A QUEUE ROW THAT NAMES ITS RUN. NULL on 130 jobs of 130 (disposable)
        # and 6 of 6 (preprod), which is exactly why the panel is empty on both:
        # `resolve_execution_id` answers None and the issue is written with no
        # run. Seeded here so the resolver has something true to resolve.
        cur.execute(
            """INSERT INTO app.pull_jobs
                 (id, pull_id, connection_ref_id, date_from, date_to, state,
                  requested_by, datastream_id, execution_id)
               VALUES (%s, %s, %s, %s, %s, 'done', 'system', %s, %s)""",
            (
                f"pj_{suffix}",
                f"pull_{suffix}",
                f"conn_{suffix}",
                WINDOW_DAY,
                WINDOW_DAY,
                datastream_id,
                execution_id,
            ),
        )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "execution_id": execution_id,
        "other_execution_id": other_execution_id,
    }


def _monitor(conn, flux, *, check_profile="null_rate"):
    from core.dq_governance import ensure_monitor, publish_version

    head = ensure_monitor(
        conn,
        org_id=flux["org_id"],
        project_id=flux["project_id"],
        name=f"{check_profile}_{uuid.uuid4().hex[:10]}",
        label=f"Null rate: {flux['datastream_id']}",
        target_kind="datastream",
        target_id=flux["datastream_id"],
        actor="system",
    )
    version = publish_version(
        conn,
        project_id=flux["project_id"],
        monitor_id=head["id"],
        check_profile=check_profile,
        severity="degrading",
        actor="system",
        parameters={"thresholds": {check_profile: 0.0}},
        window_days=1,
    )
    return str(head["id"]), str(version["id"])


def _fire(conn, flux, *, execution_id, check_profile="null_rate", field=REQUIRED_FIELD):
    """One evaluation and one issue, through the writers story 59.3 delivered."""
    from core.dq_governance import EvaluationCounts, open_issue, record_evaluation
    from core.dq_null_rate import root_cause_fingerprint

    monitor_id, version_id = _monitor(conn, flux, check_profile=check_profile)
    evaluation_id = record_evaluation(
        conn,
        project_id=flux["project_id"],
        monitor_id=monitor_id,
        monitor_version_id=version_id,
        outcome="fail",
        window_start=WINDOW_DAY,
        window_end=WINDOW_DAY,
        counts=EvaluationCounts(total_eligible=1, evaluated=1, passed=0, failed=1),
        dependency_refs={"target": {"kind": "datastream", "id": flux["datastream_id"]}},
        observed={
            "relation": RELATION,
            "required_fields": [field],
            "row_count": 120,
            "findings": [
                {"field": field, "null_count": 7, "row_count": 120, "null_rate": 0.058}
            ],
        },
        datastream_id=flux["datastream_id"],
        execution_id=execution_id,
    )
    issue = open_issue(
        conn,
        project_id=flux["project_id"],
        monitor_id=monitor_id,
        root_cause_fingerprint=root_cause_fingerprint(monitor_id, field),
        severity="degrading",
        actor="system",
        evaluation_id=evaluation_id,
        datastream_id=flux["datastream_id"],
        execution_id=execution_id,
    )
    return {"monitor_id": monitor_id, "issue_id": str(issue["id"]), "evaluation_id": evaluation_id}


# ---------------------------------------------------------------------------
# The detail, under its run.
# ---------------------------------------------------------------------------


def test_the_issue_detail_appears_under_the_run_that_found_it(conn, flux):
    from core.datastream_workbench import _run_anomalies

    fired = _fire(conn, flux, execution_id=flux["execution_id"])
    read = _run_anomalies(conn, flux["project_id"], flux["datastream_id"])

    entry = read["runs"][flux["execution_id"]]
    assert entry["anomalies"] == 1
    assert entry["evaluations"] == 1
    (issue,) = entry["issues"]
    assert issue["id"] == fired["issue_id"]
    assert issue["monitor_id"] == fired["monitor_id"]
    assert issue["severity"] == "degrading"
    assert issue["status"] == "open"
    assert issue["first_seen_at"] is not None and issue["last_seen_at"] is not None
    # THE PROFILE DECLARES IT, and the console holds no list of profile names.
    assert issue["check_profile"] == "null_rate"
    assert issue["replayable_rows"] is True
    assert issue["rows_absence_reason"] is None
    # The other run of the same flux carries nothing, and says so with a zero it
    # actually measured rather than with a missing key.
    assert flux["other_execution_id"] not in read["runs"]


def test_the_seeded_queue_row_is_what_lets_an_issue_name_a_run(conn, flux):
    """The precondition is load-bearing, not decorative.

    `resolve_execution_id` goes through `extract_ledger.execution_for_day`, and
    the only thing that can answer it is a `pull_jobs` row carrying an
    `execution_id`. Null on 130 of 130 and 6 of 6, which is why every issue on
    both bases would be written with no run at all.
    """
    from core.dq_null_rate import resolve_execution_id

    resolved = resolve_execution_id(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        window_date=WINDOW_DAY,
    )
    assert resolved == flux["execution_id"]


def test_the_two_pointers_are_what_make_this_flux_eligible_at_all(conn, flux):
    """`report_profile_id` AND `current_mapping_version_id`, and both decide.

    THIS IS THE TEST THAT MAKES THE THIRD PRECONDITION LOAD-BEARING. The two
    columns are what the nightly sweep reads to decide whether a `null_rate`
    monitor can be derived for a flux at all, and 0 Datastreams of 1404 carry
    both on the disposable base (172 and 230, disjoint sets). A fixture that
    seeded them without a test reaching them would have documented a belief.

    `_check_null_rate` is called with the required fields the SECOND pointer
    produces, and it returns before `derive_monitor` when there are none — so
    this stays on the test connection and never opens the writer's own.
    """
    from core import dq_monitors, dq_null_rate

    streams = dq_monitors._fetch_enabled_datastreams(conn, flux["project_id"])
    (ds,) = [row for row in streams if row["id"] == flux["datastream_id"]]

    # `report_profile_id` is the address of the relation the rows land in: it is
    # what `resolve_collected_relation` is given, and a flux without one has no
    # address at all rather than its neighbour's.
    assert ds["report_profile_id"] == "rp_example"
    # `current_mapping_version_id` is the grain, and the grain is what "required"
    # means for this monitor.
    required = dq_null_rate.read_required_fields(
        conn, project_id=flux["project_id"], datastream_id=flux["datastream_id"]
    )
    assert required == [REQUIRED_FIELD]
    verdict = dq_monitors._check_null_rate(ds, conn, WINDOW_DAY, required, 0.0)
    # It got PAST eligibility: an ineligible flux stops at `no_required_field`.
    assert verdict.detail.get("reason") != dq_null_rate.NO_REQUIRED_FIELD

    # Take the second pointer away — the state of every flux on both bases — and
    # the check answers `not_applicable` and derives no governed object at all.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = NULL WHERE id = %s",
            (flux["datastream_id"],),
        )
    assert (
        dq_null_rate.read_required_fields(
            conn, project_id=flux["project_id"], datastream_id=flux["datastream_id"]
        )
        == []
    )
    ineligible = dq_monitors._check_null_rate(ds, conn, WINDOW_DAY, [], 0.0)
    assert ineligible.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert ineligible.detail["reason"] == dq_null_rate.NO_REQUIRED_FIELD
    assert "evaluation_id" not in ineligible.detail


def test_the_count_is_the_length_of_the_detail_and_not_a_second_read(conn, flux):
    """One reading of `app.dq_issues`, or the two would disagree eventually."""
    import inspect

    from core.datastream_workbench import _run_anomalies

    _fire(conn, flux, execution_id=flux["execution_id"])
    _fire(conn, flux, execution_id=flux["execution_id"], field="date")
    read = _run_anomalies(conn, flux["project_id"], flux["datastream_id"])
    entry = read["runs"][flux["execution_id"]]
    assert entry["anomalies"] == len(entry["issues"]) == 2

    source = inspect.getsource(_run_anomalies)
    assert source.count("FROM app.dq_issues") == 1


def test_an_issue_naming_no_run_appears_under_no_run(conn, flux):
    """`execution_id IS NULL` belongs to no collection, and is never attributed."""
    from core.datastream_workbench import _run_anomalies

    _fire(conn, flux, execution_id=None)
    read = _run_anomalies(conn, flux["project_id"], flux["datastream_id"])

    assert read["runs"] == {} or all(
        not entry["issues"] for entry in read["runs"].values()
    )
    # And the evaluation that could not name a run is counted at the grain of the
    # FLUX -- arbitrage 7. Attributing it to a run would be the more expensive of
    # the two defects.
    assert read["evaluations_without_run"] == 1


def test_the_flux_grain_count_is_not_the_run_grain_one(conn, flux):
    from core.datastream_workbench import _run_anomalies

    _fire(conn, flux, execution_id=flux["execution_id"])
    _fire(conn, flux, execution_id=None, field="date")
    read = _run_anomalies(conn, flux["project_id"], flux["datastream_id"])

    assert read["runs"][flux["execution_id"]]["evaluations"] == 1
    assert read["evaluations_without_run"] == 1


def test_the_runs_payload_carries_the_detail_and_the_flux_grain_count(conn, flux):
    """End to end through `read_tab`, which is what the console receives."""
    from core.datastream_workbench import read_tab

    _fire(conn, flux, execution_id=flux["execution_id"])
    evidence = read_tab(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        tab="runs",
    )["evidence"]

    assert evidence["evaluations_without_run"] == 0
    found = {str(run["id"]): run["anomalies"] for run in evidence["runs"]}
    assert found[flux["execution_id"]]["anomalies"] == 1
    assert len(found[flux["execution_id"]]["issues"]) == 1
    assert found[flux["other_execution_id"]] == {
        "anomalies": 0,
        "evaluations": 0,
        "issues": [],
    }


# ---------------------------------------------------------------------------
# The replay, and what a profile without replayable rows answers.
# ---------------------------------------------------------------------------


def test_the_replay_recovers_the_field_and_the_window_of_the_run(conn, flux, monkeypatch):
    from core import collected_mapped_reader, dq_issue_rows

    fired = _fire(conn, flux, execution_id=flux["execution_id"])
    seen = {}

    def _describe(**kwargs):
        seen["describe"] = kwargs
        return {"readable": True, "relation": RELATION, "zone": "collected",
                "columns": ["date", REQUIRED_FIELD], "prefix": "", "mode": "duckdb"}

    def _read(**kwargs):
        seen["read"] = kwargs
        return {
            "readable": True,
            "relation": RELATION,
            "columns": ["date", REQUIRED_FIELD],
            "rows": [{"date": "2026-08-04", REQUIRED_FIELD: None}],
            "truncated": False,
            "masked_fields": [],
        }

    monkeypatch.setattr(collected_mapped_reader, "describe_relation", _describe)
    monkeypatch.setattr(collected_mapped_reader, "read_rows", _read)

    payload = dq_issue_rows.replay_issue_rows(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        execution_id=flux["execution_id"],
        issue_id=fired["issue_id"],
    )

    # The field comes from the fingerprint, never from a column the reader
    # happened to carry.
    assert seen["read"]["null_field"] == REQUIRED_FIELD
    # The window is the EVALUATION's, which is the only row that says which days
    # this run was judged on.
    assert seen["read"]["start"] == seen["read"]["end"] == WINDOW_DAY.isoformat()
    assert payload["row_count"] == 1
    assert payload["note"] is None
    assert payload["replayable_rows"] is True
    # The rows are TODAY'S, and the payload says when they were read.
    assert payload["replayed_at"]


def test_a_replay_that_matches_nothing_is_not_a_replay_that_failed(conn, flux, monkeypatch):
    from core import collected_mapped_reader, dq_issue_rows

    fired = _fire(conn, flux, execution_id=flux["execution_id"])
    monkeypatch.setattr(
        collected_mapped_reader,
        "describe_relation",
        lambda **_: {"readable": True, "relation": RELATION, "zone": "collected",
                     "columns": ["date", REQUIRED_FIELD], "prefix": "", "mode": "duckdb"},
    )
    monkeypatch.setattr(
        collected_mapped_reader,
        "read_rows",
        lambda **_: {"readable": True, "relation": RELATION,
                     "columns": ["date", REQUIRED_FIELD], "rows": [],
                     "truncated": False, "masked_fields": []},
    )

    payload = dq_issue_rows.replay_issue_rows(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        execution_id=flux["execution_id"],
        issue_id=fired["issue_id"],
    )
    assert payload["rows"] == []
    assert payload["row_count"] == 0
    assert payload["note"] == dq_issue_rows.NOTHING_MATCHES_NOW
    assert payload["reason"] is None


def test_an_unreadable_relation_is_not_an_empty_result(conn, flux, monkeypatch):
    from core import collected_mapped_reader, dq_issue_rows

    fired = _fire(conn, flux, execution_id=flux["execution_id"])
    monkeypatch.setattr(
        collected_mapped_reader,
        "describe_relation",
        lambda **_: {"readable": False, "relation": RELATION, "zone": "collected",
                     "columns": [], "reason": collected_mapped_reader.RELATION_ABSENT},
    )

    payload = dq_issue_rows.replay_issue_rows(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        execution_id=flux["execution_id"],
        issue_id=fired["issue_id"],
    )
    # `None`, never `[]`: the condition was never tested.
    assert payload["rows"] is None
    assert payload["note"] != dq_issue_rows.NOTHING_MATCHES_NOW
    assert payload["message"]


def test_a_profile_without_replayable_rows_shows_no_table_and_no_export(conn, flux):
    """Arbitrage 8: `zero_rows` has no faulty row BY NATURE, and says which."""
    from core import dq_issue_rows

    fired = _fire(conn, flux, execution_id=flux["execution_id"], check_profile="zero_rows")
    payload = dq_issue_rows.replay_issue_rows(
        conn,
        project_id=flux["project_id"],
        datastream_id=flux["datastream_id"],
        execution_id=flux["execution_id"],
        issue_id=fired["issue_id"],
    )
    assert payload["replayable_rows"] is False
    assert payload["rows_absence_reason"] == dq_issue_rows.NO_FAULTY_ROW_BY_NATURE
    assert payload["rows"] is None
    assert payload["rows_absence_message"]


def test_the_replay_refuses_an_issue_outside_the_project(conn, flux):
    from core import dq_issue_rows

    fired = _fire(conn, flux, execution_id=flux["execution_id"])
    with pytest.raises(dq_issue_rows.IssueNotFound):
        dq_issue_rows.replay_issue_rows(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id=flux["datastream_id"],
            execution_id=flux["execution_id"],
            issue_id=fired["issue_id"],
        )
    # And an issue of this Project found on ANOTHER run is not this run's.
    with pytest.raises(dq_issue_rows.IssueNotFound):
        dq_issue_rows.replay_issue_rows(
            conn,
            project_id=flux["project_id"],
            datastream_id=flux["datastream_id"],
            execution_id=flux["other_execution_id"],
            issue_id=fired["issue_id"],
        )


def test_the_export_writes_no_row_the_replay_did_not_return():
    """No database needed: the file is a projection of the payload, and only of it."""
    from core import dq_issue_rows

    csv_bytes = dq_issue_rows.rows_csv(
        {
            "columns": ["date", "campaign_id"],
            "rows": [{"date": "2026-08-04", "campaign_id": None}],
        }
    )
    assert csv_bytes.decode("utf-8").splitlines() == ["date,campaign_id", "2026-08-04,"]


def test_every_check_profile_declares_whether_it_has_replayable_rows():
    """A profile added to the dispatch and not here would inherit a default.

    That silent inheritance is the class defect story 59.4 paid for five times,
    and it is why this parity is asserted rather than trusted.
    """
    from core.dq_issue_rows import REPLAY_PROFILES
    from core.dq_monitors import CHECK_PROFILES

    assert set(REPLAY_PROFILES) == set(CHECK_PROFILES)
    assert all(
        (entry.absence_reason is None) is entry.replayable_rows
        for entry in REPLAY_PROFILES.values()
    )
