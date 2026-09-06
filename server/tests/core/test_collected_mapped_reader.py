"""`Collected` and `Mapped`, actually read -- story 58.3, step 2.

DRIVEN AGAINST A REAL DUCKDB WAREHOUSE, on the pattern of
`tests/core/test_datastream_sample_reader.py` and
`tests/core/test_daily_row_counts_reader.py`: a temp origin playing `main.*` (the
raw zone) and `main_staging.*` (the mapped zone), no Postgres. Those two schema
names are not a guess -- `dbt/macros/generate_schema_name.sql` states the rule
and a built fixture confirms it: without `var('org')`, dbt-duckdb materialises
`main` / `main_staging` / `main_marts` and the raw relations sit in `main`
unprefixed.

**THE BIGQUERY BRANCH IS NOT EXERCISED HERE, AND THAT IS STATED RATHER THAN
IMPLIED.** It needs a GCP dataset and credentials, this repository has no
connector test accounts, and a mocked client would prove the test can write
BigQuery SQL and nothing else. What holds the two dialects in step is that they
answer the same shape from the same function; that shape is what is pinned below.

WHAT THIS FILE HOLDS, and each line of it is a way of quietly serving the wrong
rows:

  * the two relations really are read, and the rows come back;
  * `project_id` is in every statement -- another project's row on the same day
    must not appear on either side;
  * masking is a REFUSAL BY DEFAULT: a value that must never leave the server is
    seeded, and this test fails the moment it appears in the payload;
  * a day covered by two pulls is NAMED (`superseded_by_pull`) rather than
    silently paired, because staging keeps the latest pull per grain;
  * the whole window costs a CONSTANT number of statements -- asserted on one day
    and on ninety-two, not described;
  * an absent relation, a relation with no project column and a relation with no
    date column are three different refusals with three different words.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from core.collected_mapped_reader import (
    DATE_COLUMN_ABSENT,
    MASK_SENTINEL,
    NO_ROW_IN_SHARED_RELATION,
    PROJECT_SCOPE_ABSENT,
    RELATION_ABSENT,
    SUPERSEDED_BY_PULL,
    ZONE_COLLECTED,
    ZONE_MAPPED,
    describe_pair,
    describe_relation,
    read_collected_and_mapped,
    read_stage_rows,
)

_BASE = date(2026, 7, 1)

#: A raw relation as a connector declares it: the source's own column names, a
#: `project_id`, a `pull_id`, and a column whose name matches NO English PII
#: pattern while holding a value that must never reach a browser.
_RAW_DDL = """
    CREATE TABLE main.raw_example_ads_daily (
        project_id  VARCHAR,
        date        VARCHAR,
        campagne_id VARCHAR,
        usr_mail    VARCHAR,
        depense     DOUBLE,
        pull_id     VARCHAR
    )
"""

#: And the staging model over it: canonical names, deduplicated by pull.
_STG_DDL = """
    CREATE TABLE main_staging.stg_example_ads_daily (
        project_id  VARCHAR,
        date        DATE,
        campaign_id VARCHAR,
        cost        DOUBLE,
        pull_id     VARCHAR
    )
"""

#: And a staging model that CONVERTS -- story 58.7. Its nine columns are the ones
#: the 8 FX-joining staging models really emit (`stg_meta_ads_daily` and its seven
#: siblings): the amount, the amount as the source reported it, the source
#: currency, then the relation-wide rate provenance. A separate relation rather
#: than nine more columns on the one above, so every assertion written for story
#: 58.3 keeps measuring exactly what it measured.
_MONEY_STG_DDL = """
    CREATE TABLE main_staging.stg_example_money_daily (
        project_id           VARCHAR,
        date                 DATE,
        campaign_id          VARCHAR,
        cost_source_value    DECIMAL(38, 9),
        cost_source_currency VARCHAR,
        cost                 DECIMAL(38, 9),
        fx_rate              DECIMAL(38, 9),
        fx_as_of_date        DATE,
        fx_source            VARCHAR,
        fx_tier              VARCHAR,
        pull_id              VARCHAR
    )
"""

#: The raw relation under it. It carries the amount and the source currency and NO
#: rate column at all -- measured on `raw_meta_ads_daily`, which holds `spend` and
#: `cost_source_currency` and nothing else of the conversion. That absence is what
#: `fx_columns_absent_in_raw_zone` exists to name.
_MONEY_RAW_DDL = """
    CREATE TABLE main.raw_example_money_daily (
        project_id           VARCHAR,
        date                 VARCHAR,
        campaign_id          VARCHAR,
        cost                 DOUBLE,
        cost_source_currency VARCHAR,
        pull_id              VARCHAR
    )
"""

#: The value the masking policy exists for. If this string appears anywhere in a
#: response, the mask fell.
_SECRET = "someone@example.com"


def _seed(path: str) -> None:
    import duckdb

    con = duckdb.connect(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_staging")
        con.execute(_RAW_DDL)
        con.execute(_STG_DDL)
        raw = [
            ("proj_EXAMPLE", "2026-07-01", "c1", _SECRET, 10.0, "pull_a"),
            ("proj_EXAMPLE", "2026-07-01", "c2", _SECRET, 20.0, "pull_a"),
            # A SECOND pull covering the same day: staging keeps the latest per
            # grain, so one of these has no counterpart there.
            ("proj_EXAMPLE", "2026-07-01", "c1", _SECRET, 11.0, "pull_b"),
            ("proj_EXAMPLE", "2026-07-02", "c1", _SECRET, 30.0, "pull_b"),
            # Another project, same day. Either side leaking it is a breach.
            ("proj_OTHER", "2026-07-01", "c9", _SECRET, 99.0, "pull_z"),
            # And a day outside the window.
            ("proj_EXAMPLE", "2026-08-15", "c1", _SECRET, 40.0, "pull_c"),
        ]
        con.executemany(
            "INSERT INTO main.raw_example_ads_daily VALUES (?,?,?,?,?,?)", raw
        )
        con.executemany(
            "INSERT INTO main_staging.stg_example_ads_daily VALUES (?,?,?,?,?)",
            [
                ("proj_EXAMPLE", _BASE, "c1", 11.0, "pull_b"),
                ("proj_EXAMPLE", _BASE, "c2", 20.0, "pull_a"),
                ("proj_OTHER", _BASE, "c9", 99.0, "pull_z"),
            ],
        )
        # Story 58.7: the converting pair. Three rows, three monetary outcomes --
        # converted, no rate, no currency -- because a fixture with only the happy
        # one proves that the happy one renders.
        con.execute(_MONEY_STG_DDL)
        con.execute(_MONEY_RAW_DDL)
        con.executemany(
            "INSERT INTO main_staging.stg_example_money_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("proj_EXAMPLE", _BASE, "c1", 100.0, "USD", 100.0,
                 0.92, date(2026, 7, 1), "seed", "fixed", "pull_a"),
                ("proj_EXAMPLE", _BASE, "c2", 50.0, "GBP", 50.0,
                 None, None, None, None, "pull_a"),
                ("proj_EXAMPLE", _BASE, "c3", 25.0, None, 25.0,
                 None, None, None, None, "pull_a"),
            ],
        )
        con.executemany(
            "INSERT INTO main.raw_example_money_daily VALUES (?,?,?,?,?,?)",
            [("proj_EXAMPLE", "2026-07-01", "c1", 100.0, "USD", "pull_a")],
        )
        # A relation with no `date` column at all -- 7 of the 52 declared raw
        # relations are in that state.
        con.execute(
            "CREATE TABLE main.raw_example_snapshot "
            "(project_id VARCHAR, snapshot_date VARCHAR, value DOUBLE)"
        )
        # And one with no project column, which must be refused rather than read.
        con.execute("CREATE TABLE main.raw_example_unscoped (date VARCHAR, value DOUBLE)")
    finally:
        con.close()


@pytest.fixture
def warehouse_file(tmp_path, monkeypatch):
    origin = str(tmp_path / "origin_local.duckdb")
    _seed(origin)
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", origin)
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    return origin


#: The classification the mapping declares. `none` is the ONLY value that shows a
#: column; everything else, and every column absent from this map, is masked.
_CLASSIFICATIONS = {
    "date": "none",
    "campagne_id": "none",
    "campaign_id": "none",
    "depense": "none",
    "cost": "none",
    "usr_mail": "pii",
}


def _pair(**over):
    pair = {
        "collected_relation": "raw_example_ads_daily",
        "mapped_relation": "stg_example_ads_daily",
        "reason": None,
        "message": None,
    }
    pair.update(over)
    return pair


def _read(**over):
    kwargs = dict(
        project_id="proj_EXAMPLE",
        pair=_pair(),
        start="2026-07-01",
        end="2026-07-01",
        classifications=_CLASSIFICATIONS,
    )
    kwargs.update(over)
    return read_collected_and_mapped(**kwargs)


# ---------------------------------------------------------------------------
# The two readings.
# ---------------------------------------------------------------------------


def test_both_relations_are_read_and_each_says_which_one_it_was(warehouse_file) -> None:
    out = _read()
    assert out["collected"]["relation"] == "raw_example_ads_daily"
    assert out["collected"]["zone"] == "main"
    assert out["mapped"]["relation"] == "stg_example_ads_daily"
    assert out["mapped"]["zone"] == "main_staging"
    assert out["collected"]["row_count"] == 3
    assert out["mapped"]["row_count"] == 2


def test_the_collected_side_keeps_the_sources_own_column_names(warehouse_file) -> None:
    """That is what `Collected` MEANS, and it is why the two sides are comparable."""
    out = _read()
    assert out["collected"]["columns"] == [
        "project_id", "date", "campagne_id", "usr_mail", "depense", "pull_id"
    ]
    assert out["mapped"]["columns"] == [
        "project_id", "date", "campaign_id", "cost", "pull_id"
    ]


def test_the_date_bounds_exclude_what_is_outside_the_window(warehouse_file) -> None:
    narrow = _read()
    wide = _read(start="2026-07-01", end="2026-07-02")
    assert narrow["collected"]["row_count"] == 3
    assert wide["collected"]["row_count"] == 4
    # The row of 2026-08-15 is in neither.
    assert all(row["date"] != "2026-08-15" for row in wide["collected"]["rows"])


# ---------------------------------------------------------------------------
# Isolation. `project_id` is in every statement of every relation.
# ---------------------------------------------------------------------------


def test_another_projects_rows_reach_neither_side(warehouse_file) -> None:
    """Interrogated on the rows, because `project_id` itself is masked.

    That is the two policies meeting: the WHERE clause carries the project on
    every statement, and the column that carries it is not classified `none`, so
    it comes back as the sentinel. `c9` is the other project's only row and it is
    what would be visible if the filter were missing.
    """
    out = _read()
    assert all(row["project_id"] == MASK_SENTINEL for row in out["collected"]["rows"])
    assert all(row.get("campagne_id") != "c9" for row in out["collected"]["rows"])
    assert all(row.get("campaign_id") != "c9" for row in out["mapped"]["rows"])
    assert out["collected"]["row_count"] == 3
    assert out["mapped"]["row_count"] == 2


def test_the_other_project_can_read_its_own_row_and_only_it(warehouse_file) -> None:
    out = _read(project_id="proj_OTHER")
    assert out["collected"]["row_count"] == 1
    assert out["collected"]["rows"][0]["campagne_id"] == "c9"


def test_a_relation_with_no_project_column_is_refused_rather_than_read_wide(
    warehouse_file,
) -> None:
    out = read_stage_rows(
        project_id="proj_EXAMPLE",
        relation="raw_example_unscoped",
        zone=ZONE_COLLECTED,
        start="2026-07-01",
        end="2026-07-01",
    )
    assert out["reason"] == PROJECT_SCOPE_ABSENT
    assert out["rows"] is None
    assert out["message"]


# ---------------------------------------------------------------------------
# Masking is a refusal by default (arbitrage 6).
# ---------------------------------------------------------------------------


def test_a_value_that_must_never_leave_the_server_never_does(warehouse_file) -> None:
    """`usr_mail` matches NO English PII pattern, which is the whole point.

    `cache_warehouse._pii_columns` tests thirteen words against CANONICAL names;
    a raw relation carries the source's, and `usr_mail`, `tel` and `cust_id` walk
    straight past that filter. Here the classification decides, and a column with
    none is masked.
    """
    out = _read()
    assert _SECRET not in repr(out)
    assert all(row["usr_mail"] == MASK_SENTINEL for row in out["collected"]["rows"])
    assert "usr_mail" in out["collected"]["masked_fields"]


def test_a_column_with_no_classification_at_all_is_masked(warehouse_file) -> None:
    """Refusal by default: an unclassified column is not an allowed one."""
    out = _read(classifications={"date": "none"})
    row = out["collected"]["rows"][0]
    assert row["date"] != MASK_SENTINEL
    assert row["campagne_id"] == MASK_SENTINEL
    assert row["depense"] == MASK_SENTINEL
    assert row["pull_id"] == MASK_SENTINEL


def test_with_no_classifications_at_all_nothing_is_shown(warehouse_file) -> None:
    out = _read(classifications={})
    assert out["collected"]["row_count"] == 3
    assert all(
        value == MASK_SENTINEL
        for row in out["collected"]["rows"]
        for value in row.values()
    )


# ---------------------------------------------------------------------------
# The absences of the side-by-side (arbitrage 5): named, never opened.
# ---------------------------------------------------------------------------


def test_a_day_covered_by_two_pulls_says_so_instead_of_pairing_blind(
    warehouse_file,
) -> None:
    """3 collected rows, 2 mapped: the difference has a name and a sentence.

    Which collected row lost is a question about the relation's GRAIN, which this
    reader does not know -- so it says that several pulls cover the day and stops
    there. A per-row verdict would be a claim nothing backs.
    """
    out = _read()
    assert out["collected"]["note"] == SUPERSEDED_BY_PULL
    assert "latest pull" in out["collected"]["note_message"]
    assert out["collected"]["row_count"] > out["mapped"]["row_count"]


def test_an_empty_day_says_what_was_measured_and_refuses_to_explain_it(
    warehouse_file,
) -> None:
    """The note says NOTHING IS THERE and then says what it cannot tell apart.

    An earlier version answered `candidate_not_published` on every empty day, so a
    flux that had simply never been pulled was told about a candidate run nobody
    had observed -- no statement here looks for one. Arbitrage 5 asks for the
    absence to be NAMED; naming is not supposing, and when two causes cannot be
    separated the honest answer says so.
    """
    out = _read(start="2026-07-20", end="2026-07-20")
    assert out["collected"]["rows"] == []
    assert out["collected"]["note"] == NO_ROW_IN_SHARED_RELATION
    message = out["collected"]["note_message"]
    assert message.startswith("No row for this day is in the shared relation.")
    assert "cannot say" in message
    # And it is NOT a failure: the relation was read and it answered.
    assert out["collected"]["reason"] is None


def test_a_single_pull_day_carries_no_supersession_note(warehouse_file) -> None:
    out = _read(start="2026-07-02", end="2026-07-02")
    assert out["collected"]["row_count"] == 1
    assert out["collected"]["note"] is None


# ---------------------------------------------------------------------------
# The refusals, one word each.
# ---------------------------------------------------------------------------


def test_a_relation_that_does_not_exist_is_named_absent_and_not_a_failure(
    warehouse_file,
) -> None:
    out = _read(pair=_pair(collected_relation="raw_example_never_created"))
    assert out["collected"]["reason"] == RELATION_ABSENT
    assert out["collected"]["rows"] is None
    # The other side is untouched: two zones, two facts.
    assert out["mapped"]["row_count"] == 2


def test_a_relation_with_no_date_column_is_refused_by_its_own_word(
    warehouse_file,
) -> None:
    """7 of the 52 declared relations have no `date`. Guessing `snapshot_date`
    would publish a day nobody measured."""
    out = read_stage_rows(
        project_id="proj_EXAMPLE",
        relation="raw_example_snapshot",
        zone=ZONE_COLLECTED,
        start="2026-07-01",
        end="2026-07-01",
    )
    assert out["reason"] == DATE_COLUMN_ABSENT
    assert out["rows"] is None


# ---------------------------------------------------------------------------
# The DESCRIPTION -- what makes the refusal arrive before the click.
# ---------------------------------------------------------------------------


def test_a_readable_relation_describes_itself_in_one_statement(warehouse_file) -> None:
    out, executed = _statements(
        lambda: describe_relation(
            project_id="proj_EXAMPLE",
            relation="raw_example_ads_daily",
            zone=ZONE_COLLECTED,
        )
    )
    assert out["readable"] is True
    assert out["reason"] is None
    assert out["columns"][:2] == ["project_id", "date"]
    assert len(executed) == 1, executed
    assert "information_schema" in executed[0]


@pytest.mark.parametrize(
    "relation,reason",
    [
        ("raw_example_snapshot", DATE_COLUMN_ABSENT),
        ("raw_example_unscoped", PROJECT_SCOPE_ABSENT),
        ("raw_example_never_created", RELATION_ABSENT),
    ],
)
def test_a_relation_that_cannot_be_read_by_day_says_so_BEFORE_any_row_is_asked_for(
    warehouse_file, relation, reason
) -> None:
    """The defect this pass exists to remove.

    14 report profiles over 7 connectors name a relation with no `date` column
    (`raw_x_ads_daily` carries `interval_start`, `raw_strava_club_daily` a
    `snapshot_date`). Deciding availability on the DECLARATION alone offers them an
    enabled control that refuses once pressed -- the reason arriving after the
    click is exactly what arbitrage 2 forbids.
    """
    out = describe_relation(
        project_id="proj_EXAMPLE", relation=relation, zone=ZONE_COLLECTED
    )
    assert out["readable"] is not True
    assert out["reason"] == reason
    assert out["message"]


def test_describing_the_pair_costs_one_statement_per_named_relation(
    warehouse_file,
) -> None:
    both, two = _statements(lambda: describe_pair(project_id="proj_EXAMPLE", pair=_pair()))
    assert len(two) == 2, two
    assert both["collected"]["readable"] and both["mapped"]["readable"]

    _one_side, none = _statements(
        lambda: describe_pair(
            project_id="proj_EXAMPLE",
            pair=_pair(collected_relation=None, mapped_relation=None,
                       reason="report_profile_not_set", message="No profile."),
        )
    )
    assert none == [], none


def test_the_description_is_reused_and_the_catalog_is_never_asked_twice(
    warehouse_file,
) -> None:
    """The control a person pressed and the rows they then see are ONE measurement."""
    described = describe_pair(project_id="proj_EXAMPLE", pair=_pair())
    _out, executed = _statements(
        lambda: read_collected_and_mapped(
            project_id="proj_EXAMPLE",
            pair=_pair(),
            start="2026-07-01",
            end="2026-07-01",
            classifications=_CLASSIFICATIONS,
            description=described,
        )
    )
    assert len(executed) == 2, executed
    assert not any("information_schema" in sql for sql in executed), executed


def test_the_two_zones_are_named_by_the_single_naming_point(warehouse_file) -> None:
    """`raw_prefix` / `staging_prefix` are what compose the `FROM`, not this reader.

    Both are read from `warehouse_tenancy` rather than assembled here: a schema
    name written at a call site is the drift that module exists to prevent, and a
    resolver nothing calls is a promise nothing keeps.
    """
    from core import warehouse_tenancy

    assert warehouse_tenancy.raw_prefix("proj_EXAMPLE") == "main."
    assert warehouse_tenancy.staging_prefix("proj_EXAMPLE") == "main_staging."

    _out, executed = _statements(lambda: _read())
    rows = [sql for sql in executed if "information_schema" not in sql]
    assert any("FROM main.\"raw_example_ads_daily\"" in sql for sql in rows), rows
    assert any(
        "FROM main_staging.\"stg_example_ads_daily\"" in sql for sql in rows
    ), rows


def test_a_side_the_resolver_could_not_name_carries_the_resolvers_reason(
    warehouse_file,
) -> None:
    out = _read(
        pair=_pair(
            mapped_relation=None,
            reason="staging_model_absent",
            message="No staging model reads this raw relation.",
        )
    )
    assert out["mapped"]["reason"] == "staging_model_absent"
    assert out["mapped"]["message"] == "No staging model reads this raw relation."
    assert out["mapped"]["rows"] is None
    # And nothing was asked of the warehouse for that side.
    assert out["mapped"]["zone"] is None


def test_an_unreadable_warehouse_takes_down_neither_the_pair_nor_the_process(
    warehouse_file, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "no_such.duckdb"))
    out = _read()
    assert out["collected"]["reason"] == RELATION_ABSENT
    assert out["mapped"]["reason"] == RELATION_ABSENT


# ---------------------------------------------------------------------------
# What it costs. The measurement, not the description.
# ---------------------------------------------------------------------------


def _statements(callable_):
    from core import warehouse

    executed: list[str] = []
    real = warehouse._query_duckdb

    def _spy(sql, params):
        executed.append(" ".join(str(sql).split()))
        return real(sql, params)

    warehouse._query_duckdb = _spy
    try:
        out = callable_()
    finally:
        warehouse._query_duckdb = real
    return out, executed


def test_one_day_and_ninety_two_cost_the_same_number_of_statements(
    warehouse_file,
) -> None:
    """The defect this reader was written to avoid.

    `read_datastream_sample` beside it runs `for day in days:` -- 92 warehouse
    queries at its ceiling. Two per relation answers the whole window: the column
    list, then the rows.
    """
    one, narrow = _statements(lambda: _read())
    wide_end = (_BASE + timedelta(days=91)).isoformat()
    many, wide = _statements(lambda: _read(start="2026-07-01", end=wide_end))

    assert one["collected"]["row_count"] == 3  # it really read something
    # The wide window reaches the 2026-08-15 row too, so the two readings really
    # did cover different amounts of ground for the same price.
    assert many["collected"]["row_count"] == 5
    assert len(narrow) == 4, narrow
    assert len(wide) == 4, wide
    assert sum("information_schema" in sql for sql in wide) == 2, wide


def test_a_side_with_no_relation_asks_the_warehouse_nothing(warehouse_file) -> None:
    _out, executed = _statements(
        lambda: _read(pair=_pair(mapped_relation=None, reason="staging_model_absent"))
    )
    assert len(executed) == 2, executed


def test_the_reading_is_bounded_and_says_when_it_truncated(warehouse_file) -> None:
    out = read_stage_rows(
        project_id="proj_EXAMPLE",
        relation="raw_example_ads_daily",
        zone=ZONE_COLLECTED,
        start="2026-07-01",
        end="2026-07-01",
        classifications=_CLASSIFICATIONS,
        limit=2,
    )
    assert out["row_count"] == 2
    assert out["truncated"] is True


def test_two_readings_of_an_unchanged_relation_are_identical(warehouse_file) -> None:
    """A side-by-side is only comparable if the order does not drift between reads."""
    assert _read()["collected"]["rows"] == _read()["collected"]["rows"]


# ---------------------------------------------------------------------------
# The money provenance travels WITH the row -- story 58.7.
#
# Driven through `read_day_reading`, against the same real DuckDB warehouse, so
# what is proved is the association over a RELATION and not over a column list
# somebody typed. Three sentences have to stay apart here, and each of them is a
# different thing a person would do next: this column is an amount and here is
# what converted it; this zone cannot say (raw); nothing declares an amount at all.
# ---------------------------------------------------------------------------


#: The mapping, in the shape `classifications_of` reads. `cost` is classified
#: `none` so it is SHOWN -- which is the only case where the line under it can be
#: read, and therefore the only case worth asserting the provenance of.
_MONEY_COLUMNS = [
    {"source_field": "date", "target_field": "date", "sensitivity": "none"},
    {"source_field": "campaign_id", "target_field": "campaign_id", "sensitivity": "none"},
    {"source_field": "spend", "target_field": "cost", "sensitivity": "none"},
]


def _money_pair(**over):
    pair = {
        "collected_relation": "raw_example_money_daily",
        "mapped_relation": "stg_example_money_daily",
        "reason": None,
        "message": None,
    }
    pair.update(over)
    return pair


def _day_reading(*, monetary_names=("cost",), pair=None, columns=None):
    from core.datastream_daily_breakdown_api import read_day_reading

    return read_day_reading(
        project_id="proj_EXAMPLE",
        pair=pair or _money_pair(),
        day="2026-07-01",
        columns=_MONEY_COLUMNS if columns is None else columns,
        monetary_names=list(monetary_names) if monetary_names is not None else None,
    )


def test_a_designated_amount_names_the_currency_the_rate_and_the_date(
    warehouse_file,
) -> None:
    """The association, over the columns the 8 converting staging models emit.

    The ROLES are the mart's words and the values are the relation's own spelling:
    nobody has to guess that `cost_source_currency` is what `native_currency`
    means one model downstream.
    """
    side = _day_reading()["mapped"]
    designation = side["money_provenance"]
    assert designation["reason"] is None
    assert designation["columns"] == [
        {
            "column": "cost",
            "native_currency": "cost_source_currency",
            "native_value": "cost_source_value",
            "fx_rate": "fx_rate",
            "fx_as_of_date": "fx_as_of_date",
        }
    ]


def test_the_three_values_come_from_the_row_and_carry_no_default(
    warehouse_file,
) -> None:
    """A converted row, a row with no rate and a row with no currency.

    `1.0` for a missing rate and a default date are exactly what the story exists
    to refuse: both would make an unconverted amount look converted.
    """
    from core.money_provenance_columns import GAP_NO_CURRENCY, GAP_NO_RATE

    rows = _day_reading()["mapped"]["money_provenance"]["rows"]
    assert len(rows) == 3

    converted = rows[0]["cost"]
    assert converted["native_currency"] == "USD"
    assert converted["fx_rate"] == "0.920000000"
    assert converted["fx_as_of_date"] == "2026-07-01"
    assert converted["money_gap_code"] is None

    no_rate = rows[1]["cost"]
    assert no_rate["native_currency"] == "GBP"
    assert no_rate["fx_rate"] is None
    assert no_rate["money_gap_code"] == GAP_NO_RATE
    assert "not converted" in no_rate["money_gap_message"]

    no_currency = rows[2]["cost"]
    assert no_currency["native_currency"] is None
    assert no_currency["money_gap_code"] == GAP_NO_CURRENCY
    # Two gaps, two sentences: "no currency" and "no rate" are two repairs.
    assert no_currency["money_gap_message"] != no_rate["money_gap_message"]


def test_no_reporting_currency_is_named_and_the_door_that_confirms_one_is(
    warehouse_file,
) -> None:
    """Arbitrage 8. 0 project of 18 has confirmed one; naming `EUR` would present
    a column DEFAULT as a decision, which migration 144 opens by naming."""
    designation = _day_reading()["mapped"]["money_provenance"]
    assert designation["reporting_currency"] is None
    assert "the currency the source reported" in designation["reporting_currency_reason"]
    assert designation["reporting_currency_gate"]


def test_the_provenance_of_a_shown_amount_is_shown_and_of_a_masked_one_masked(
    warehouse_file,
) -> None:
    """The FX columns are named by no mapping, so on their own they are masked.

    Masking is a refusal by default and it stays one: the provenance inherits the
    classification of the amount it explains -- never its own -- so a masked amount
    keeps a masked rate, and a shown amount stops printing `[MASKED]` under itself.
    """
    shown = _day_reading()["mapped"]
    assert shown["rows"][0]["cost"] == "100.000000000"
    assert shown["money_provenance"]["rows"][0]["cost"]["fx_rate"] == "0.920000000"

    masked = _day_reading(
        columns=[{"source_field": "spend", "target_field": "cost", "sensitivity": "pii"}]
    )["mapped"]
    assert masked["rows"][0]["cost"] == MASK_SENTINEL
    assert masked["money_provenance"]["rows"][0]["cost"]["fx_rate"] == MASK_SENTINEL


def test_the_raw_zone_refuses_to_invent_a_conversion_it_carries_no_column_for(
    warehouse_file,
) -> None:
    """`raw_meta_ads_daily` holds `spend` and `cost_source_currency` and no rate.

    A raw zone that answered with an association would be describing a conversion
    that happens one stage later.
    """
    from core.money_provenance_columns import FX_COLUMNS_ABSENT_IN_RAW_ZONE

    designation = _day_reading()["collected"]["money_provenance"]
    assert designation["columns"] == []
    assert designation["reason"] == FX_COLUMNS_ABSENT_IN_RAW_ZONE
    assert "one stage later" in designation["message"]
    assert designation["rows"] == []


def test_with_no_declared_amount_the_key_is_present_empty_and_says_what_would_fill_it(
    warehouse_file,
) -> None:
    """The state of the estate: 0 `money` concept over 13 published versions.

    The key never disappears -- a missing key and an empty one are the two
    different sentences the screen has to say -- and the reason names the ONE
    authority that could fill it rather than falling back to a column name.
    """
    from core.money_provenance_columns import NO_MONETARY_CONCEPT_DECLARED

    for zone in ("collected", "mapped"):
        designation = _day_reading(monetary_names=[])[zone]["money_provenance"]
        assert designation["columns"] == []
        assert designation["reason"] == NO_MONETARY_CONCEPT_DECLARED
        assert "semantic_concept_versions" in designation["message"]
        assert "not classifiers" in designation["message"]


def test_a_relation_carrying_no_declared_amount_says_that_and_not_the_other_thing(
    warehouse_file,
) -> None:
    """"Nothing declares an amount" and "this relation carries none" are two facts."""
    from core.money_provenance_columns import NO_MONETARY_COLUMN_IN_RELATION

    designation = _day_reading(
        monetary_names=["revenue"]
    )["mapped"]["money_provenance"]
    assert designation["reason"] == NO_MONETARY_COLUMN_IN_RELATION


def test_a_relation_that_could_not_be_read_does_not_answer_an_empty_designation(
    warehouse_file,
) -> None:
    """« Vide » and « cassé » again, one layer down: a relation nobody could open
    has no columns, and saying "it declares no amount" about it would be a
    measurement nobody made."""
    from core.money_provenance_columns import RELATION_NOT_READ

    designation = _day_reading(
        pair=_money_pair(mapped_relation="stg_example_never_created")
    )["mapped"]["money_provenance"]
    assert designation["reason"] == RELATION_NOT_READ
    assert designation["columns"] == []


def test_the_designation_costs_no_extra_statement(warehouse_file) -> None:
    """It reads the SAME description the availability was computed from.

    Two statements per relation is the count story 58.3 asserted and 58.7 does not
    get to raise it: the columns are already in hand.
    """
    _out, executed = _statements(lambda: _day_reading())
    assert len(executed) == 4, executed


def test_the_mapped_zone_is_the_staging_schema_and_not_the_mart(warehouse_file) -> None:
    """`main_staging`, from the single naming point -- never composed at the call site."""
    _out, executed = _statements(lambda: _read())
    assert any("main_staging" in sql for sql in executed), executed
    assert not any("main_marts" in sql for sql in executed), executed
    assert ZONE_MAPPED == "mapped"


# ---------------------------------------------------------------------------
# The two readings, PAIRED on the row -- amendment 12, lot B2.
#
# What is held here is the one thing a pairing can quietly get wrong and still
# look complete: pairing something the mapping does not pair. So the key is the
# mapping's own `source -> target`, the join runs on the values the database
# returned rather than on the mask, and every row that could not be paired says
# which side it came from instead of disappearing.
# ---------------------------------------------------------------------------

#: The active mapping of the fixture, in the shape the route already reads it:
#: two key fields (the grain), one bound value field, and one field bound to
#: nothing -- which is a real state of a mapping and the line a person opens this
#: tab to repair.
_FIELDS = [
    {"source_field": "date", "target_field": "date", "is_key_column": True},
    {"source_field": "campagne_id", "target_field": "campaign_id", "is_key_column": True},
    {"source_field": "depense", "target_field": "cost", "is_key_column": False},
    {"source_field": "usr_mail", "target_field": None, "is_key_column": False},
]


def _paired(**over):
    kwargs = dict(fields=_FIELDS)
    kwargs.update(over)
    return _read(**kwargs)["pairing"]


def test_the_pairing_key_is_the_mappings_own_source_to_target(warehouse_file) -> None:
    """`campagne_id -> campaign_id` and `depense -> cost` -- named by the mapping.

    Not by a name that matches on both sides: `campagne_id` and `campaign_id` are
    not the same string, and they pair. `usr_mail` and `cost` are both columns of
    their relations, and they do not.
    """
    pairing = _paired()
    assert pairing["available"] is True
    assert [(c["source_field"], c["target_field"]) for c in pairing["columns"]] == [
        ("date", "date"),
        ("campagne_id", "campaign_id"),
        ("depense", "cost"),
    ]
    assert [k["source_field"] for k in pairing["key_columns"]] == ["date", "campagne_id"]


def test_a_row_that_cannot_be_paired_says_which_side_it_came_from(warehouse_file) -> None:
    """Two pulls cover this day, so the grain does not tell two collected rows apart.

    Staging keeps the latest pull per grain; the raw zone keeps both. `c1` is
    therefore twice on the collected side and once on the mapped one, and NONE of
    those three rows may be paired -- choosing one would be the invention this
    module refuses. `c2` is unique on both and pairs.
    """
    from core.collected_mapped_pairing import ROW_KEY_NOT_UNIQUE

    pairing = _paired()
    assert pairing["paired_row_count"] == 1
    assert pairing["unpaired_row_count"] == 3
    sides = sorted(row["side"] for row in pairing["rows"] if row["side"])
    assert sides == ["collected", "collected", "mapped"]
    assert {row["reason"] for row in pairing["rows"] if row["side"]} == {
        ROW_KEY_NOT_UNIQUE
    }
    # And every one of them carries the SERVER's sentence, never a code alone.
    assert all(row["message"] for row in pairing["rows"] if row["side"])


def test_the_paired_row_carries_the_raw_value_and_the_value_it_becomes(
    warehouse_file,
) -> None:
    """One row, both values, and what separates them -- the promise of the epic."""
    pairing = _paired()
    row = next(entry for entry in pairing["rows"] if entry["side"] is None)
    cells = {cell["source_field"]: cell for cell in row["cells"]}
    assert cells["campagne_id"]["raw"] == "c2"
    assert cells["campagne_id"]["mapped"] == "c2"
    assert cells["depense"]["raw"] == 20.0
    assert cells["depense"]["mapped"] == 20.0
    # `20.0` and `20.0` are one amount: a staging cast is not a transformation and
    # highlighting it would make every column of the product look changed.
    assert cells["depense"]["changed"] is False
    # The two dialects of one day compare equal: the raw zone stores a STRING and
    # staging a DATE, and calling that a change would flag every row of every flux.
    assert cells["date"]["changed"] is False


def test_a_transformed_value_is_reported_as_changed_and_an_identity_is_not() -> None:
    """The highlight is a MEASUREMENT, taken on the two values, not on the names.

    Driven on two hand-built sides rather than the warehouse: this fixture's
    staging model renames its columns and changes not one value, so a relation is
    not where an actual transformation can be observed today.
    """
    from core.collected_mapped_pairing import pair_readings

    pairing = pair_readings(
        collected={
            "columns": ["day", "spend_cents"],
            "rows": [{"day": "2026-07-01", "spend_cents": 1200}],
            "_raw_rows": [{"day": "2026-07-01", "spend_cents": 1200}],
        },
        mapped={
            "columns": ["date", "media_cost_micros"],
            "rows": [{"date": "2026-07-01", "media_cost_micros": 12000000}],
            "_raw_rows": [{"date": "2026-07-01", "media_cost_micros": 12000000}],
        },
        fields=[
            {"source_field": "day", "target_field": "date", "is_key_column": True},
            {"source_field": "spend_cents", "target_field": "media_cost_micros",
             "is_key_column": False},
        ],
    )
    cells = {cell["source_field"]: cell for cell in pairing["rows"][0]["cells"]}
    assert cells["spend_cents"]["changed"] is True
    assert cells["day"]["changed"] is False


def test_a_row_with_one_side_never_claims_the_other_is_unchanged(
    warehouse_file,
) -> None:
    """`changed: null` -- "nothing to compare with" is not "nothing changed"."""
    pairing = _paired()
    lonely = next(entry for entry in pairing["rows"] if entry["side"] == "mapped")
    assert all(cell["changed"] is None for cell in lonely["cells"])
    assert all(cell["raw"] is None for cell in lonely["cells"])


def test_the_join_runs_on_the_values_the_database_returned_not_on_the_mask(
    warehouse_file,
) -> None:
    """THE ONE THING THAT MAKES THIS PAIRING TRUE RATHER THAN PLAUSIBLE.

    The key columns are masked here -- no classification says `none` for them -- so
    every row DISPLAYS `[MASKED]` on both sides. A join computed on what the screen
    sees would then match every row against every other and report zero unique
    keys; this one is identical to the unmasked run, because the key is taken
    before the mask and the mask is what is published.
    """
    masked = _paired(classifications={"depense": "none", "cost": "none"})
    shown = _paired()
    assert masked["paired_row_count"] == shown["paired_row_count"] == 1
    assert masked["unpaired_row_count"] == shown["unpaired_row_count"] == 3
    row = next(entry for entry in masked["rows"] if entry["side"] is None)
    cells = {cell["source_field"]: cell for cell in row["cells"]}
    assert cells["campagne_id"]["raw"] == MASK_SENTINEL
    assert cells["campagne_id"]["mapped"] == MASK_SENTINEL


def test_no_unmasked_value_reaches_the_payload_through_the_pairing(
    warehouse_file,
) -> None:
    """The masking policy has one place, and the pairing did not become a second."""
    import json

    out = _read(fields=_FIELDS)
    assert _SECRET not in json.dumps(out, default=str)
    # And the internal hand-over never travels either.
    from core.collected_mapped_pairing import RAW_ROWS

    assert RAW_ROWS not in out["collected"]
    assert RAW_ROWS not in out["mapped"]


def test_a_column_the_mapping_does_not_pair_stays_unpaired_and_says_why(
    warehouse_file,
) -> None:
    """`usr_mail` is bound to nothing; `pull_id` is named by no field at all."""
    from core.collected_mapped_pairing import COLUMN_NAMED_BY_NO_FIELD, COLUMN_UNBOUND

    pairing = _paired()
    unpaired = {
        (entry["name"], entry["side"]): entry for entry in pairing["unpaired_columns"]
    }
    assert unpaired[("usr_mail", "collected")]["reason"] == COLUMN_UNBOUND
    assert unpaired[("pull_id", "collected")]["reason"] == COLUMN_NAMED_BY_NO_FIELD
    assert unpaired[("pull_id", "mapped")]["reason"] == COLUMN_NAMED_BY_NO_FIELD
    assert all(entry["message"] for entry in pairing["unpaired_columns"])


def test_a_mapping_that_names_no_key_column_refuses_the_ROWS_and_keeps_the_columns(
    warehouse_file,
) -> None:
    """The columns pair and the rows cannot -- said with the columns that DID pair.

    This is the state amendment 12 asks the screen to name rather than fall silent
    on, and it is not a failure of the warehouse: it is a mapping with no declared
    grain.
    """
    from core.collected_mapped_pairing import NO_KEY_COLUMN

    pairing = _paired(fields=[dict(field, is_key_column=False) for field in _FIELDS])
    assert pairing["available"] is False
    assert pairing["reason"] == NO_KEY_COLUMN
    assert "Mapping" in pairing["message"]
    assert len(pairing["columns"]) == 3
    assert pairing["rows"] == []


def test_a_mapping_with_no_bound_field_is_a_different_refusal(warehouse_file) -> None:
    """Measured on the estate: the reviewed Datastream carries 22 fields, 0 targets."""
    from core.collected_mapped_pairing import NO_BOUND_FIELD

    pairing = _paired(
        fields=[{"source_field": "date", "target_field": None, "is_key_column": True}]
    )
    assert pairing["reason"] == NO_BOUND_FIELD
    assert pairing["declared_field_count"] == 1


def test_no_mapping_at_all_is_not_the_same_sentence_as_no_bound_field(
    warehouse_file,
) -> None:
    from core.collected_mapped_pairing import NO_MAPPING_FIELD

    assert _paired(fields=None)["reason"] == NO_MAPPING_FIELD
    assert _read()["pairing"]["reason"] == NO_MAPPING_FIELD


def test_a_field_naming_a_column_neither_relation_carries_is_refused_by_name(
    warehouse_file,
) -> None:
    from core.collected_mapped_pairing import NO_COLUMN_IN_BOTH_RELATIONS

    pairing = _paired(
        fields=[
            {"source_field": "impressions", "target_field": "impressions",
             "is_key_column": True},
        ]
    )
    assert pairing["reason"] == NO_COLUMN_IN_BOTH_RELATIONS
    assert pairing["bound_field_count"] == 1


def test_a_side_that_could_not_be_read_refuses_the_pairing_rather_than_half_of_it(
    warehouse_file,
) -> None:
    """A relation that is not there has no rows to pair, and `[]` would say it had."""
    from core.collected_mapped_pairing import SIDE_UNREADABLE

    pairing = _read(
        pair=_pair(mapped_relation="stg_example_never_created"), fields=_FIELDS
    )["pairing"]
    assert pairing["available"] is False
    assert pairing["reason"] == SIDE_UNREADABLE


def test_the_pairing_costs_no_extra_statement(warehouse_file) -> None:
    """It joins the rows already in hand. Four statements, two per relation."""
    _out, executed = _statements(lambda: _read(fields=_FIELDS))
    assert len(executed) == 4, executed
    assert sum("information_schema" in sql for sql in executed) == 2, executed


def test_the_pairing_says_its_bound_and_says_when_it_was_reached(
    warehouse_file,
) -> None:
    """A counterpart cut off at the bound reads exactly like one that never was."""
    from core.collected_mapped_reader import MAX_ROWS

    assert _paired()["bounded_at"] == MAX_ROWS
    assert _paired()["truncated"] is False
    narrow = _paired(limit=1)
    assert narrow["bounded_at"] == 1
    assert narrow["truncated"] is True


# ---------------------------------------------------------------------------
# The capability's effect reaches the PAIRED row -- lot B3, amendment 11.
#
# The money line already sat under the amount of a single reading (story 58.7)
# and stopped at the edge of the paired table, which is the one place the epic
# asks a person to compare two values. What is held here is that the line under a
# paired cell and the line under the same cell of the single reading are THE SAME
# VALUES -- reached by the index the join already knows, never re-matched -- and
# that a cell of a side that has no row carries nothing rather than a rate about
# a row that is not there.
# ---------------------------------------------------------------------------

#: The money mapping WITH its grain. `_MONEY_COLUMNS` above declares no key
#: column, because the tests it serves are about the designation; a pairing needs
#: the fields a row is keyed by, and refuses by name without them.
_MONEY_FIELDS = [
    {"source_field": "date", "target_field": "date", "is_key_column": True,
     "sensitivity": "none"},
    {"source_field": "campaign_id", "target_field": "campaign_id",
     "is_key_column": True, "sensitivity": "none"},
    {"source_field": "cost", "target_field": "cost", "is_key_column": False,
     "sensitivity": "none"},
]


def _money_reading_with_capability(state="ready"):
    """`read_day_reading` over the money relations, then the capability projection.

    Driven exactly as the route drives it, with `conn=None` because none of the
    three keys asked for here reads the database: the states are handed in, which
    is the whole point of reading the switch once.
    """
    from core import datastream_reading_capabilities

    reading = _day_reading(columns=_MONEY_FIELDS)
    return datastream_reading_capabilities.project_onto_reading(
        None,
        project_id="proj_EXAMPLE",
        reading=reading,
        columns=_MONEY_FIELDS,
        states={"currency_fx": state, "country": "disabled",
                "reporting_timezone": "draft"},
    )


def test_the_paired_row_says_which_row_of_each_side_it_came_from(
    warehouse_file,
) -> None:
    """The index the join already knows, published so nothing has to re-match.

    Every block that travels parallel to a side's rows has to reach the same row
    of it. Matching on a value would re-do the join in a second, weaker way, and
    two joins over one reading is how one amount ends up under two rates.
    """
    pairing = _read(fields=_MONEY_FIELDS, pair=_money_pair())["pairing"]
    assert pairing["available"] is True
    paired = next(row for row in pairing["rows"] if row["side"] is None)
    assert paired["collected_index"] == 0
    assert paired["mapped_index"] == 0
    # The raw relation holds one row and staging three, so two mapped rows have no
    # counterpart. Each says which row of ITS side it is and `None` for the other.
    lonely = [row for row in pairing["rows"] if row["side"] == "mapped"]
    assert sorted(row["mapped_index"] for row in lonely) == [1, 2]
    assert {row["collected_index"] for row in lonely} == {None}


def test_an_active_capability_puts_the_rate_under_the_paired_cell(
    warehouse_file,
) -> None:
    """The same three values as the single reading, at the same index."""
    reading = _money_reading_with_capability()
    paired = next(row for row in reading["pairing"]["rows"] if row["side"] is None)
    cell = next(entry for entry in paired["cells"] if entry["target_field"] == "cost")

    assert cell["mapped_money"]["native_currency"] == "USD"
    assert cell["mapped_money"]["fx_rate"] == "0.920000000"
    assert cell["mapped_money"]["fx_as_of_date"] == "2026-07-01"
    # And it IS the same object the single reading shows, not a second reading of
    # the same row.
    assert cell["mapped_money"] == reading["mapped"]["money_provenance"]["rows"][0]["cost"]
    # The RAW zone designates no amount -- it carries no rate column at all -- so
    # the other half of the cell carries nothing. A line composed there would be
    # conversion evidence the collected relation does not hold.
    assert cell["raw_money"] is None


def test_an_unpaired_row_carries_the_gap_of_the_side_it_actually_has(
    warehouse_file,
) -> None:
    """`c2` is in staging alone and its amount was never converted.

    The gap travels onto the paired table too, because « no rate covered this row »
    and « this amount converted » are the two sentences a person opens this view to
    tell apart.
    """
    from core.money_provenance_columns import GAP_NO_RATE

    reading = _money_reading_with_capability()
    row = next(
        entry
        for entry in reading["pairing"]["rows"]
        if entry["side"] == "mapped" and entry["mapped_index"] == 1
    )
    cell = next(entry for entry in row["cells"] if entry["target_field"] == "cost")
    assert cell["mapped_money"]["money_gap_code"] == GAP_NO_RATE
    assert cell["mapped_money"]["money_gap_message"]
    assert cell["raw_money"] is None


def test_an_inactive_capability_annotates_no_paired_cell_at_all(
    warehouse_file,
) -> None:
    """Off, the capability appears nowhere -- « ni onglet, ni panneau, ni colonne ».

    Not an empty line and not a `null` where a rate would go: the keys are not
    written, so nothing on the paired table holds a space open for a projection
    this Project never asked for.
    """
    from core.datastream_reading_capabilities import MONEY_CAPABILITY_NOT_ACTIVE

    reading = _money_reading_with_capability(state="draft")
    assert reading["capabilities"] == []
    for row in reading["pairing"]["rows"]:
        for cell in row["cells"]:
            assert "mapped_money" not in cell
            assert "raw_money" not in cell
    for zone in ("collected", "mapped"):
        assert (
            reading[zone]["money_provenance"]["reason"] == MONEY_CAPABILITY_NOT_ACTIVE
        )
