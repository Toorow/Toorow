"""What a cross would do to the rows, measured against a real DuckDB (story 66.3).

This suite runs BOTH real stores at once, and it has to: the chain that says
*where* the rows are lives in Postgres (mapping version -> output version ->
relation) and the rows themselves live in the warehouse. A test that mocked
either half would prove the other half's arithmetic against numbers it invented.

The DuckDB file is built per test with `duckdb` directly and pointed at through
`TOOROW_DUCKDB_PATH`, which is the same environment variable production reads.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")

from core import match_profile  # noqa: E402
from core import mdm_common_keys as keys  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    make_canonical_field,
    make_datastream,
    make_project,
    publish_output,
)


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    """A real DuckDB the profiler reads through the production environment variable."""
    path = tmp_path / "epic66.duckdb"
    con = duckdb.connect(str(path))
    # `main_marts` and not `main`: it is the schema `warehouse_tenancy.mart_prefix`
    # names with the org-schema flag off, so this fixture reads at the address
    # production reads at rather than at a convenient one.
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("CREATE TABLE main_marts.spend (date DATE, campaign TEXT, spend DOUBLE)")
    con.execute(
        "CREATE TABLE main_marts.conversions "
        "(event_date DATE, campaign_key TEXT, conversions INTEGER)"
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return str(path)


def _insert(path, table, rows):
    con = duckdb.connect(path)
    try:
        for row in rows:
            placeholders = ", ".join(["?"] * len(row))
            con.execute(f"INSERT INTO main_marts.{table} VALUES ({placeholders})", list(row))
    finally:
        con.close()


@pytest.fixture()
def world(live_postgres, warehouse):
    org_id, project_id = make_project(live_postgres, "Profile")
    day = make_canonical_field(live_postgres, project_id, "day", value_type="date")
    campaign = make_canonical_field(live_postgres, project_id, "campaign_id")
    left = make_datastream(
        live_postgres, org_id, project_id, "Campaign spend",
        bindings={day: ("date", "confirmed"), campaign: ("campaign", "confirmed")},
        measures={"spend": "confirmed"},
    )
    right = make_datastream(
        live_postgres, org_id, project_id, "Conversions",
        bindings={day: ("event_date", "confirmed"), campaign: ("campaign_key", "confirmed")},
        measures={"conversions": "confirmed"},
    )
    publish_output(live_postgres, org_id, project_id, left, "spend")
    publish_output(live_postgres, org_id, project_id, right, "conversions")
    key = keys.create_common_key(
        live_postgres,
        project_id=project_id,
        name="Day and Campaign",
        canonical_field_ids=[day, campaign],
        actor="tester",
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "left": left,
        "right": right,
        "key_version_id": key["current_version"]["id"],
        "path": warehouse,
        "day": day,
        "campaign": campaign,
    }


def _profile(conn, world, *, window=None):
    return match_profile.profile_match(
        conn,
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        relationship_name="campaign_performance",
        view_version_id="svv_profile_fixture",
        window=window,
    )


def test_a_clean_one_to_one_match_multiplies_nothing(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10.0), ("2026-08-02", "B", 20.0)])
    _insert(
        world["path"], "conversions",
        [("2026-08-01", "A", 3), ("2026-08-02", "B", 4)],
    )

    profile = _profile(live_postgres, world)

    assert profile["left"]["state"] == "exact"
    assert profile["left"]["total_rows"] == 2
    assert profile["left"]["distinct_keys"] == 2
    assert profile["matched"]["matched_keys"] == 2
    assert profile["matched"]["left_unmatched_keys"] == 0
    assert profile["multiplication"]["worst_case_rows_per_key"] == 1
    assert profile["execution_safety"] == "ready"
    assert profile["bounds"]["warehouse_jobs"] == 3
    assert profile["profile_receipt"]


def test_one_profile_uses_exactly_three_warehouse_jobs(live_postgres, world, monkeypatch):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10.0)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3)])
    runner, bigquery_mode = match_profile._runner()
    queries: list[str] = []

    def counted(sql, params):
        queries.append(sql)
        return runner(sql, params)

    monkeypatch.setattr(match_profile, "_runner", lambda: (counted, bigquery_mode))

    _profile(live_postgres, world)

    assert len(queries) == 3


def test_the_profile_reports_what_its_three_jobs_cost(live_postgres, world):
    """AC 9's three figures, and none of them a bare number.

    Job count was always provable. Billed bytes and elapsed time were not: the
    BigQuery runner threw the `QueryJob` away, and `total_bytes_billed` lives on
    the job, never on the `RowIterator`. On DuckDB the honest billed-bytes answer
    is `not_applicable` -- a local file read bills nothing, and 0 would be a
    measurement nobody made.
    """
    _insert(world["path"], "spend", [("2026-08-01", "A", 10.0)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3)])

    cost = _profile(live_postgres, world)["cost"]

    assert cost["engine"] == "duckdb"
    assert cost["warehouse_jobs_issued"] == 3
    assert len(cost["jobs"]) == 3
    assert all(job["ok"] for job in cost["jobs"])
    assert cost["billed_bytes"] is None
    assert cost["billed_bytes_state"] == "not_applicable"
    assert cost["elapsed_ms_state"] == "exact"
    assert isinstance(cost["elapsed_ms"], int)
    assert cost["elapsed_ms"] == sum(job["elapsed_ms"] for job in cost["jobs"])


def test_a_job_that_never_reached_the_engine_is_recorded_as_unmeasured(
    live_postgres, world, monkeypatch
):
    """Three jobs of which one was unreadable is not the same as two jobs.

    The bound AC 9 states is on jobs ISSUED, so a job that raised still occupies
    a row -- and it carries no elapsed figure, because nothing was measured.
    """
    runner, bigquery_mode = match_profile._runner()
    calls: list[int] = []

    def failing_on_the_third(sql, params):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("connection reset")
        return runner(sql, params)

    monkeypatch.setattr(match_profile, "_runner", lambda: (failing_on_the_third, bigquery_mode))

    cost = _profile(live_postgres, world)["cost"]

    assert cost["warehouse_jobs_issued"] == 3
    assert [job["ok"] for job in cost["jobs"]] == [True, True, False]
    assert cost["jobs"][2]["elapsed_ms"] is None
    # ONE JOB COULD NOT SAY, SO NO TOTAL CAN -- and that holds for the clock
    # exactly as it holds for the bytes. A sum over two of three is smaller than
    # the truth and reads exactly like the truth. `partial` was a fourth state
    # this vocabulary does not have, and it shipped a partial total under it.
    assert cost["elapsed_ms"] is None
    assert cost["elapsed_ms_state"] == "unavailable"
    assert cost["billed_bytes"] is None
    assert cost["billed_bytes_state"] == "unavailable"
    # Nothing is lost: the two jobs that ran keep their own figure.
    assert all(isinstance(job["elapsed_ms"], int) for job in cost["jobs"][:2])


def test_the_signed_profile_measures_the_same_time_window_as_the_plan(live_postgres, world):
    _insert(
        world["path"],
        "spend",
        [("2026-08-01", "A", 10.0), ("2026-07-01", "OLD", 99.0)],
    )
    _insert(
        world["path"],
        "conversions",
        [("2026-08-01", "A", 3), ("2026-07-01", "OLD", 9)],
    )
    window = [
        {
            "stage": "pre_aggregation",
            "datastream_id": world["left"],
            "canonical_field_id": world["day"],
            "operator": "gte",
            "value": "2026-08-01",
        },
        {
            "stage": "pre_aggregation",
            "datastream_id": world["right"],
            "canonical_field_id": world["day"],
            "operator": "gte",
            "value": "2026-08-01",
        },
    ]

    measured = _profile(live_postgres, world, window=window)

    assert measured["left"]["total_rows"] == 1
    assert measured["right"]["total_rows"] == 1
    assert measured["matched"]["matched_keys"] == 1
    assert match_profile.reuse_profile_receipt(
        live_postgres,
        token=measured["profile_receipt"],
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        relationship_name="campaign_performance",
        view_version_id="svv_profile_fixture",
        window=list(reversed(window)),
    )["matched"]["matched_keys"] == 1
    with pytest.raises(match_profile.ProfileReceiptInvalid, match="edge"):
        match_profile.reuse_profile_receipt(
            live_postgres,
            token=measured["profile_receipt"],
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
            relationship_name="campaign_performance",
            view_version_id="svv_profile_fixture",
            window=[],
        )


def test_the_signed_receipt_reuses_evidence_without_a_warehouse_job(
    live_postgres, world, monkeypatch
):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10.0)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3)])
    measured = _profile(live_postgres, world)

    def warehouse_must_not_run():
        raise AssertionError("a current receipt must not profile the warehouse again")

    monkeypatch.setattr(match_profile, "_runner", warehouse_must_not_run)
    reused = match_profile.reuse_profile_receipt(
        live_postgres,
        token=measured["profile_receipt"],
        project_id=world["project_id"],
        left_datastream_id=world["left"],
        right_datastream_id=world["right"],
        common_key_version_id=world["key_version_id"],
        relationship_name="campaign_performance",
        view_version_id="svv_profile_fixture",
    )

    # Everything but the cost block is the evidence, unchanged.
    assert {k: v for k, v in reused.items() if k != "cost"} == {
        k: v for k, v in measured.items() if k not in ("profile_receipt", "cost")
    }
    # AC 9's other half: the reuse issued ZERO jobs, and the figures it carries
    # were measured when the receipt was minted. Returning them unlabelled would
    # read as "this costs three jobs every time".
    assert reused["cost"]["reused_from_receipt"] is True
    assert reused["cost"]["warehouse_jobs_issued_now"] == 0
    assert reused["cost"]["warehouse_jobs_issued"] == measured["cost"]["warehouse_jobs_issued"]


def test_a_tampered_profile_receipt_is_never_evidence(live_postgres, world):
    measured = _profile(live_postgres, world)
    token = measured["profile_receipt"]
    tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

    with pytest.raises(match_profile.ProfileReceiptInvalid):
        match_profile.reuse_profile_receipt(
            live_postgres,
            token=tampered,
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
            relationship_name="campaign_performance",
            view_version_id="svv_profile_fixture",
        )


def test_a_new_published_output_invalidates_the_old_receipt(live_postgres, world):
    measured = _profile(live_postgres, world)
    publish_output(
        live_postgres,
        world["org_id"],
        world["project_id"],
        world["left"],
        "spend_v2",
    )

    with pytest.raises(match_profile.ProfileReceiptInvalid, match="stale"):
        match_profile.reuse_profile_receipt(
            live_postgres,
            token=measured["profile_receipt"],
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
            relationship_name="campaign_performance",
            view_version_id="svv_profile_fixture",
        )


def test_unmatched_keys_are_counted_on_both_sides(live_postgres, world):
    _insert(
        world["path"], "spend",
        [("2026-08-01", "A", 10.0), ("2026-08-02", "B", 20.0), ("2026-08-03", "C", 30.0)],
    )
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3), ("2026-08-09", "Z", 1)])

    profile = _profile(live_postgres, world)

    assert profile["matched"]["matched_keys"] == 1
    assert profile["matched"]["left_unmatched_keys"] == 2
    assert profile["matched"]["right_unmatched_keys"] == 1


def test_a_null_key_is_counted_and_never_matched(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10.0), ("2026-08-02", None, 5.0)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3)])

    profile = _profile(live_postgres, world)

    assert profile["left"]["null_key_rows"] == 1
    assert profile["matched"]["matched_keys"] == 1


def test_duplicates_on_both_sides_are_unsafe_and_the_factor_says_why(live_postgres, world):
    """The case that silently multiplies every measure of both sources."""
    _insert(
        world["path"], "spend",
        [("2026-08-01", "A", 10.0), ("2026-08-01", "A", 7.0), ("2026-08-01", "A", 1.0)],
    )
    _insert(
        world["path"], "conversions",
        [("2026-08-01", "A", 3), ("2026-08-01", "A", 4)],
    )

    profile = _profile(live_postgres, world)

    assert profile["left"]["duplicated_keys"] == 1
    assert profile["left"]["max_rows_per_key"] == 3
    assert profile["right"]["max_rows_per_key"] == 2
    assert profile["multiplication"]["worst_case_rows_per_key"] == 6
    assert profile["execution_safety"] == "unsafe"
    assert "6 rows" in profile["multiplication"]["explanation"]


def test_duplicates_on_one_side_only_ask_for_review(live_postgres, world):
    _insert(world["path"], "spend", [("2026-08-01", "A", 10.0), ("2026-08-01", "A", 7.0)])
    _insert(world["path"], "conversions", [("2026-08-01", "A", 3)])

    profile = _profile(live_postgres, world)

    assert profile["multiplication"]["worst_case_rows_per_key"] == 2
    assert profile["execution_safety"] == "review_required"


def test_an_empty_pair_of_relations_is_exact_zero_not_unavailable(live_postgres, world):
    """Asked and nothing matched is a real answer, and different from 'could not ask'."""
    profile = _profile(live_postgres, world)
    assert profile["left"]["state"] == "exact"
    assert profile["left"]["total_rows"] == 0
    assert profile["matched"]["matched_keys"] == 0
    assert profile["execution_safety"] == "ready"


def test_a_source_with_no_published_output_is_refused_by_name(live_postgres, world):
    third = make_datastream(
        live_postgres, world["org_id"], world["project_id"], "Never ran",
        bindings={
            world["day"]: ("date", "confirmed"),
            world["campaign"]: ("campaign", "confirmed"),
        },
    )
    with pytest.raises(match_profile.ProfileRefused) as excinfo:
        match_profile.profile_match(
            live_postgres,
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=third,
            common_key_version_id=world["key_version_id"],
            relationship_name="campaign_performance",
            view_version_id="svv_profile_fixture",
        )
    assert excinfo.value.code == "no_published_output"
    assert excinfo.value.missing_link == "datastream_output_versions"


def test_a_receipt_cannot_cross_relationship_versions(live_postgres, world):
    measured = _profile(live_postgres, world)

    with pytest.raises(match_profile.ProfileReceiptInvalid, match="edge"):
        match_profile.reuse_profile_receipt(
            live_postgres,
            token=measured["profile_receipt"],
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
            relationship_name="another_relationship",
            view_version_id="svv_other",
        )


def test_a_receipt_expires_before_mutable_rows_can_be_trusted(live_postgres, world, monkeypatch):
    monkeypatch.setattr(match_profile.time, "time", lambda: 1_000)
    measured = _profile(live_postgres, world)
    monkeypatch.setattr(
        match_profile.time,
        "time",
        lambda: 1_000 + match_profile.PROFILE_RECEIPT_MAX_AGE_SECONDS + 1,
    )

    with pytest.raises(match_profile.ProfileReceiptInvalid, match="expired"):
        match_profile.reuse_profile_receipt(
            live_postgres,
            token=measured["profile_receipt"],
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
            relationship_name="campaign_performance",
            view_version_id="svv_profile_fixture",
        )


def test_a_key_component_nobody_mapped_is_refused_before_any_query(live_postgres, world):
    other = make_canonical_field(live_postgres, world["project_id"], "market")
    key = keys.create_common_key(
        live_postgres,
        project_id=world["project_id"],
        name="Market",
        canonical_field_ids=[other],
        actor="tester",
    )
    with pytest.raises(match_profile.ProfileRefused) as excinfo:
        match_profile.profile_match(
            live_postgres,
            project_id=world["project_id"],
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=key["current_version"]["id"],
        )
    assert excinfo.value.code == "key_component_unmapped"
    assert "market" in excinfo.value.message


def test_a_key_version_of_another_project_is_not_found(live_postgres, world):
    _other_org, other_project = make_project(live_postgres, "Elsewhere")
    with pytest.raises(match_profile.ProfileRefused) as excinfo:
        match_profile.profile_match(
            live_postgres,
            project_id=other_project,
            left_datastream_id=world["left"],
            right_datastream_id=world["right"],
            common_key_version_id=world["key_version_id"],
        )
    assert excinfo.value.code == "common_key_version_not_found"


def test_an_unreadable_warehouse_is_unavailable_and_never_zero(live_postgres, world, monkeypatch):
    """The distinction the whole story exists for: 'no rows' vs 'could not ask'."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", "")
    profile = _profile(live_postgres, world)

    assert profile["left"]["state"] == "unavailable"
    assert profile["left"]["total_rows"] is None
    assert profile["matched"]["state"] == "unavailable"
    assert profile["matched"]["matched_keys"] is None
    assert profile["execution_safety"] == "review_required"
