"""Story 59.3: the null rate itself, measured on a real DuckDB file.

WHY A REAL FILE AND NOT A MOCKED CURSOR. `test_dq_monitors.py` says of itself
"No real Postgres or DuckDB required -- all DB calls mocked", and that is the
right shape for the arithmetic of the volume monitor. It is the wrong shape here:
what this story had to prove is that the rate goes through
`collected_mapped_reader` and therefore inherits its advance refusals -- among
them the 7 declared raw relations of 52 that carry no `date` column at all. A
mocked cursor would have agreed with whatever SQL was written, including SQL that
addresses a column that is not there. The pattern is
`test_ai96_gate_and_row_count.py::test_duckdb_mode_is_untouched`: one file, two
tables, the real backend.

`raw_x_ads_daily` is used for the dateless case because it is one of the seven:
its window is `interval_start`/`interval_end`, and reading it on a column that
"looks like" a date would publish a day nobody measured.
"""

from __future__ import annotations

from datetime import date

import pytest

PROJECT = "proj_EXAMPLE"
WINDOW = date(2026, 8, 6)


@pytest.fixture
def warehouse_file(tmp_path, monkeypatch):
    """One DuckDB file, two relations: one dated, one that has no date column."""
    import duckdb

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE main.raw_ga4_standard_daily ("
        "  project_id VARCHAR, date VARCHAR, campaign_id VARCHAR,"
        "  country VARCHAR, clicks INTEGER)"
    )
    con.executemany(
        "INSERT INTO main.raw_ga4_standard_daily VALUES (?, ?, ?, ?, ?)",
        [
            # Four rows for the measured day: `campaign_id` null once (25 %),
            # `country` null three times (75 %) -- and `country` is NOT in the
            # grain of the payloads below, so it must never be counted.
            (PROJECT, "2026-08-06", "cmp_a", "FR", 10),
            (PROJECT, "2026-08-06", None, None, 11),
            (PROJECT, "2026-08-06", "cmp_c", None, 12),
            (PROJECT, "2026-08-06", "cmp_d", None, 13),
            # Another project, same day: never counted (AD-5).
            ("proj_OTHER", "2026-08-06", None, None, 99),
            # The day before: outside the window.
            (PROJECT, "2026-08-05", None, "FR", 1),
        ],
    )
    # THE KARDINAL RELATION -- the case measured on the deployment on 2026-08-12.
    # `video` is declared in the grain and every landed row carries `''`: zero
    # nulls, one hundred percent empty. Judging nulls alone answered `0 / 3` and
    # passed on the exact column somebody opened the console to ask about.
    con.execute(
        "CREATE TABLE main.raw_youtube_daily ("
        "  project_id VARCHAR, date VARCHAR, channel_id VARCHAR,"
        "  video VARCHAR, views INTEGER)"
    )
    con.executemany(
        "INSERT INTO main.raw_youtube_daily VALUES (?, ?, ?, ?, ?)",
        [
            (PROJECT, "2026-08-06", "chan_a", "", 100),
            (PROJECT, "2026-08-06", "chan_a", "   ", 200),
            (PROJECT, "2026-08-06", "chan_a", None, 300),
        ],
    )
    con.execute(
        "CREATE TABLE main.raw_x_ads_daily ("
        "  project_id VARCHAR, interval_start VARCHAR, campaign_id VARCHAR)"
    )
    con.execute(
        "INSERT INTO main.raw_x_ads_daily VALUES ('proj_EXAMPLE', '2026-08-06', NULL)"
    )
    con.close()

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return path


# ---------------------------------------------------------------------------
# What is required: the grain, and nothing else.
# ---------------------------------------------------------------------------


def test_required_is_the_grain_and_an_observed_nullable_is_not():
    """Arbitrage 1. `profile.nullable` is a discovery SAMPLE, never a decision."""
    from core.dq_null_rate import required_fields

    payload = {
        "grain": ["date", "campaign_id"],
        "fields": [
            {"field_id": "campaign_id", "profile": {"nullable": False}},
            # Seen non-null in the sample. That is an accident, not an obligation:
            # a monitor firing on it would fire on a discovery run's luck.
            {"field_id": "country", "profile": {"nullable": False}},
        ],
        # No writer anywhere in the repository. Reading it would be a rule that
        # never applies.
        "parameters": {"required_fields": ["country"]},
    }
    assert required_fields(payload) == ["date", "campaign_id"]


def test_an_empty_grain_declares_nothing_required():
    """390 of the 681 mapping versions. `not_applicable`, honestly."""
    from core.dq_null_rate import required_fields

    assert required_fields({"grain": [], "fields": [{"field_id": "a"}]}) == []
    assert required_fields({"fields": [{"field_id": "a"}]}) == []
    assert required_fields(None) == []


# ---------------------------------------------------------------------------
# The rate.
# ---------------------------------------------------------------------------


def test_a_required_field_above_the_threshold_is_a_finding(warehouse_file):
    from core.dq_null_rate import STATUS_MEASURED, findings_over_threshold, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        fields=["campaign_id"],
        window_date=WINDOW,
    )
    assert measurement["status"] == STATUS_MEASURED
    assert measurement["row_count"] == 4
    assert measurement["null_counts"] == {"campaign_id": 1}
    assert measurement["null_rates"]["campaign_id"] == pytest.approx(0.25)

    findings = findings_over_threshold(measurement, 0.10)
    assert [finding["field"] for finding in findings] == ["campaign_id"]
    assert findings[0]["null_count"] == 1
    assert findings[0]["row_count"] == 4
    assert findings[0]["threshold"] == 0.10


def test_a_key_that_is_blank_and_never_null_is_a_finding(warehouse_file):
    """The Kardinal case: `video` is `''` on every row, and nothing was null.

    Measured on the deployment on 2026-08-12 -- 602 rows over 29 days, `video`
    empty on all of them. Before this, `null_rates` answered `0.0` and the monitor
    passed. The two spellings stay published apart: one blank column is repaired
    in the extraction, one null column at the source.
    """
    from core.dq_null_rate import STATUS_MEASURED, findings_over_threshold, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        fields=["video", "channel_id"],
        window_date=WINDOW,
    )
    assert measurement["status"] == STATUS_MEASURED
    assert measurement["row_count"] == 3
    assert measurement["null_counts"]["video"] == 1
    assert measurement["blank_counts"]["video"] == 2
    assert measurement["missing_counts"]["video"] == 3
    assert measurement["missing_rates"]["video"] == pytest.approx(1.0)
    # A key that IS filled stays at zero on both spellings.
    assert measurement["missing_counts"]["channel_id"] == 0

    findings = findings_over_threshold(measurement, 0.0)
    assert [finding["field"] for finding in findings] == ["video"]
    assert findings[0]["null_count"] == 1
    assert findings[0]["blank_count"] == 2
    assert findings[0]["missing_count"] == 3
    assert findings[0]["missing_rate"] == pytest.approx(1.0)


def test_the_replay_of_a_blank_finding_returns_the_blank_rows(warehouse_file):
    """The count and the replay share one predicate, or the console lies.

    A firing raised on blanks whose replay selected `IS NULL` would answer
    "nothing matches this condition now" on rows that are right there.
    """
    from core import collected_mapped_reader

    description = collected_mapped_reader.describe_relation(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        zone=collected_mapped_reader.ZONE_COLLECTED,
    )
    read = collected_mapped_reader.read_rows(
        description=description,
        project_id=PROJECT,
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        null_field="video",
    )
    assert read.get("reason") is None
    assert len(read["rows"]) == 3


def test_the_same_rate_under_the_threshold_is_no_finding(warehouse_file):
    from core.dq_null_rate import findings_over_threshold, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        fields=["campaign_id"],
        window_date=WINDOW,
    )
    assert findings_over_threshold(measurement, 0.50) == []


def test_an_optional_field_is_never_counted(warehouse_file):
    """`country` is null on 3 rows of 4 and is not in the grain: it is not read.

    The epic's own sentence -- "a global threshold on an optional field
    manufactures noise that teaches people to ignore alerts".
    """
    from core.dq_null_rate import findings_over_threshold, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        fields=["campaign_id"],
        window_date=WINDOW,
    )
    assert "country" not in measurement["null_counts"]
    assert [finding["field"] for finding in findings_over_threshold(measurement, 0.0)] == [
        "campaign_id"
    ]


def test_a_relation_with_no_date_column_is_unavailable_and_never_a_pass(warehouse_file):
    """7 of the 52 declared raw relations. The refusal comes before any count."""
    from core import collected_mapped_reader
    from core.dq_null_rate import STATUS_UNAVAILABLE, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_x_ads_daily",
        fields=["campaign_id"],
        window_date=WINDOW,
    )
    assert measurement["status"] == STATUS_UNAVAILABLE
    assert measurement["reason"] == collected_mapped_reader.DATE_COLUMN_ABSENT
    assert measurement["relation"] == "raw_x_ads_daily"
    # No rate was taken, so no finding can exist -- and no firing either.
    assert measurement["null_rates"] == {}


def test_a_relation_that_does_not_exist_is_unavailable(warehouse_file):
    from core import collected_mapped_reader
    from core.dq_null_rate import STATUS_UNAVAILABLE, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_nothing_lands_here",
        fields=["campaign_id"],
        window_date=WINDOW,
    )
    assert measurement["status"] == STATUS_UNAVAILABLE
    assert measurement["reason"] == collected_mapped_reader.RELATION_ABSENT


def test_a_window_with_no_row_is_not_applicable_and_never_a_pass(warehouse_file):
    from core.dq_null_rate import (
        NO_ELIGIBLE_ROW,
        STATUS_NOT_APPLICABLE,
        findings_over_threshold,
        measure_null_rates,
    )

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        fields=["campaign_id"],
        window_date=date(2026, 1, 1),
    )
    assert measurement["status"] == STATUS_NOT_APPLICABLE
    assert measurement["reason"] == NO_ELIGIBLE_ROW
    assert findings_over_threshold(measurement, 0.0) == []


def test_a_required_field_absent_from_the_relation_is_reported_not_counted_as_zero(
    warehouse_file,
):
    """"Not there" and "never null" are two sentences and only one is a pass."""
    from core.dq_null_rate import STATUS_MEASURED, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        fields=["campaign_id", "device"],
        window_date=WINDOW,
    )
    assert measurement["status"] == STATUS_MEASURED
    assert measurement["absent_fields"] == ["device"]
    assert "device" not in measurement["null_counts"]


def test_no_required_field_measures_nothing(warehouse_file):
    from core.dq_null_rate import NO_REQUIRED_FIELD, STATUS_NOT_APPLICABLE, measure_null_rates

    measurement = measure_null_rates(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        fields=[],
        window_date=WINDOW,
    )
    assert (measurement["status"], measurement["reason"]) == (
        STATUS_NOT_APPLICABLE,
        NO_REQUIRED_FIELD,
    )


def test_the_count_is_one_statement_and_carries_no_row(warehouse_file):
    """The firing carries the measurement; the faulty rows are replayed on demand."""
    from core import collected_mapped_reader

    description = collected_mapped_reader.describe_relation(
        project_id=PROJECT,
        relation="raw_ga4_standard_daily",
        zone=collected_mapped_reader.ZONE_COLLECTED,
    )
    counted = collected_mapped_reader.count_null_rows(
        description=description,
        project_id=PROJECT,
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        fields=["campaign_id"],
    )
    assert counted["row_count"] == 4
    assert counted["null_counts"] == {"campaign_id": 1}
    assert "rows" not in counted


# ---------------------------------------------------------------------------
# The address, read from the manifests and never composed.
# ---------------------------------------------------------------------------


def test_a_datastream_with_no_report_profile_has_no_address():
    from core import stage_relation_resolver
    from core.dq_null_rate import resolve_collected_relation

    resolved = resolve_collected_relation(connector="ga4", report_profile_id=None)
    assert resolved["relation"] is None
    assert resolved["reason"] == stage_relation_resolver.REPORT_PROFILE_NOT_SET
    assert resolved["message"]


def test_the_monitor_name_is_a_slug_and_the_fingerprint_is_per_field():
    from core.dq_null_rate import monitor_name, root_cause_fingerprint

    name = monitor_name("ds_01KZ8NH3R8HYY3JS6HSG3W94HE")
    assert name == "null_rate_ds_01kz8nh3r8hyy3js6hsg3w94he"

    first = root_cause_fingerprint("dqm_1", "campaign_id")
    assert first == root_cause_fingerprint("dqm_1", "campaign_id")
    assert first != root_cause_fingerprint("dqm_1", "date")
    assert first != root_cause_fingerprint("dqm_2", "campaign_id")
    # `app.dq_issues.root_cause_fingerprint` CHECKs the sha256 shape.
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")
